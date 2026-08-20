from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_ai import (
    Agent,
    BinaryContent,
    DeferredToolRequests,
    ModelMessagesTypeAdapter,
    ModelSettings,
    capture_run_messages,
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.usage import UsageLimits

from .agent_factory import AgentFactory
from .approval_service import ApprovalResume, ApprovalService, CronTestApprovalBatch
from .approvals import ApprovalStore
from .compaction import ContextWindowCompaction
from .config import ProjectConfig
from .context_service import (
    ContextService,
    ModelContextRegistry,
    effective_context_window,
    without_images,
)
from .errors import AuthenticationError, ConfigurationError, KernelError
from .events import JsonlEventStore
from .git_service import GitService
from .llama_server import ThroughputCounters
from .models import (
    ApprovalRequest,
    Event,
    ProviderConfig,
    RunArtifact,
    RunError,
    RunRequest,
    RunResult,
    RunStatus,
    SecurityMode,
)
from .modules import ModuleRegistry
from .orchestration import (
    RuntimeDeps,
)
from .providers import (
    ProviderFactory,
    compaction_trigger_ratio,
    server_context_cap,
)
from .run_executor import RunExecutor
from .secrets import SecretStore
from .session_recovery import recover_stale_sessions
from .snapshots import SnapshotStore
from .trace_context import bind_event_run_id
from .vision import LocalVisionService
from .vision_preparation import VisionPreparation
from .workflows import WorkflowDefinition, workflow_grants
from .workspace_map import WorkspaceMapService


class Kernel:
    """Stable facade around Pydantic AI and the harness orchestration layer."""

    def __init__(self, root: Path | str = ".") -> None:
        self.config = ProjectConfig(root)
        self.module_registry = ModuleRegistry(self.config.tools_root)
        self.events = JsonlEventStore(self.config.content_root / "sessions")
        self.events.rebuild_projection()
        recover_stale_sessions(self.events)
        self.snapshots = SnapshotStore(self.config.content_root / "sessions")
        self.approvals = ApprovalStore(self.config.content_root / "sessions")
        self.approval_service = ApprovalService(self.approvals, self.events)
        self.secrets = SecretStore(self.config.content_root / "secrets.json")
        self.workspace_maps = WorkspaceMapService()
        self.vision = LocalVisionService(self.config.content_root)
        self.vision_preparation = VisionPreparation(self.events, self.vision)
        self.context_registry = ModelContextRegistry(self.config.content_root)
        self.context = ContextService(
            config=self.config,
            events=self.events,
            snapshots=self.snapshots,
            secrets=self.secrets,
            module_registry=self.module_registry,
            workspace_maps=self.workspace_maps,
            registry=self.context_registry,
        )
        self.agent_factory = AgentFactory(
            config=self.config,
            module_registry=self.module_registry,
            context=self.context,
            context_registry=self.context_registry,
            workspace_maps=self.workspace_maps,
            runtime_instruction=_runtime_context_instruction,
        )
        self.executor = RunExecutor(
            self.events,
            self.secrets,
            self.snapshots,
            self.config.content_root / "state.db",
        )
        self.active_runs: dict[Any, RuntimeDeps] = {}
        self.git = GitService()

    async def run(  # noqa: C901 - dette: boucle d'exécution centrale (point 2)
        self, request: RunRequest
    ) -> RunResult:
        display_prompt = request.prompt
        workspace = self.config.resolve_workspace(request.agent_id, request.workspace)
        command = self.config.resolve_command(request.prompt, workspace)
        if command and command["command"] in {"/secret", "/secret_list"}:
            return self._run_native_secret_command(
                request=request,
                command=command["command"],
            )
        agents = self.config.agents()
        if request.agent_id not in agents:
            raise ConfigurationError(f"unknown agent: {request.agent_id}")
        if command and command["kind"] == "native":
            request = request.model_copy(
                update={
                    "prompt": self.config.expand_native_command(request.prompt, command["command"])
                }
            )
        elif command and command["kind"] == "skill":
            updates: dict[str, Any] = {}
            if command["skill"] not in request.skills:
                updates["skills"] = [*request.skills, command["skill"]]
            if command.get("prompt"):
                # Charger la skill ne suffit pas : quand l'agent la précharge
                # déjà, la commande n'ajoutait rien et le protocole restait une
                # simple suggestion noyée dans le contexte. La consigne prend
                # ici la place du préfixe, la demande réelle la suit.
                updates["prompt"] = _expand_skill_command(
                    request.prompt, command["command"], command["prompt"]
                )
            if updates:
                request = request.model_copy(update=updates)
        provider_factory = ProviderFactory(
            self.config.providers(),
            runtime_dir=self.config.content_root / "runtime" / "providers",
        )
        agent_config = agents[request.agent_id]
        active_provider_id = request.provider_id or agent_config.provider
        provider_config = provider_factory.get_config(active_provider_id)
        active_model = request.model or agent_config.model or provider_config.model
        supports_vision = bool(getattr(provider_config, "vision", False))
        context_window_tokens = self._model_context_window(
            active_provider_id, active_model, provider_config
        )
        if command and command["command"] in {"/context", "/model-context"}:
            return self._run_native_context_command(
                request=request,
                display_prompt=display_prompt,
                command=command["command"],
                provider_id=active_provider_id,
                model_name=active_model,
                context_window_tokens=context_window_tokens,
                workspace=workspace,
                server_cap=server_context_cap(provider_config),
                trigger_ratio=compaction_trigger_ratio(provider_config),
            )
        pending = [
            approval
            for approval in self.approvals.list_pending()
            if approval.session_id == request.session_id
        ]
        if pending:
            latest = max(pending, key=lambda approval: approval.created_at)
            return RunResult(
                session_id=request.session_id,
                run_id=latest.run_id,
                agent_id=request.agent_id,
                status=RunStatus.APPROVAL_PENDING,
                output="Approval required before the run can continue.",
            )
        skills = self.config.skills(workspace)
        budgets = request.budgets or agents[request.agent_id].budgets
        if budgets is None:
            from .models import BudgetConfig

            budgets = BudgetConfig()
        run_id = uuid4()
        deps = self.executor.dependencies(
            session_id=request.session_id,
            run_id=run_id,
            budgets=budgets,
            workspace=workspace,
            security_mode=request.security_mode,
            approved_scopes=self._approved_scopes(request.session_id),
            tool_catalog=self._tool_catalog(),
            provider_id=active_provider_id,
            model_name=active_model,
            context_window_tokens=context_window_tokens,
            context_calibration=self._context_calibration(request.session_id),
            # L'agent demandé est la racine du run : c'est lui qui détermine la
            # bibliothèque, y compris pour les sous-agents qu'il invoquera.
            orchestrator_id=request.agent_id,
            workflow_grants=_workflow_grants(request.workflow),
        )
        self.active_runs[request.session_id] = deps
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.started",
                payload={
                    "prompt": display_prompt,
                    "budgets": budgets.model_dump(),
                    "skills": request.skills,
                    "resolved_command": command["command"] if command else None,
                    "workspace": (
                        str(request.workspace.expanduser().resolve())
                        if request.workspace is not None
                        else None
                    ),
                    "effective_workspace": str(workspace),
                    "workspace_kind": (
                        "project" if request.workspace is not None else "agent_default"
                    ),
                    "security_mode": request.security_mode,
                    "provider_id": active_provider_id,
                    "model": active_model,
                    "reasoning": request.reasoning,
                    "trigger": request.trigger,
                    "hidden": request.hidden,
                    "cron_job_id": request.cron_job_id,
                    "workflow": request.workflow,
                    "tool_allowlist": request.tool_allowlist,
                    "images": [
                        {"name": item.name, "media_type": item.media_type}
                        for item in request.images
                    ],
                },
            )
        )
        self.executor.transition(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            state="running",
            previous="created",
        )
        git_baseline = self._capture_git_baseline(request, run_id, workspace)
        # Ce que le modèle a produit avant de lever. Rempli par
        # `capture_run_messages` autour de l'appel, lu par les gestionnaires
        # d'erreur plus bas : sans cela, une réponse arrêtée en pleine phase de
        # raisonnement repartirait avec l'exception.
        exchanged: list[Any] = []
        try:
            archived_images = self.vision_preparation.archive_input_images(request, run_id)
            if request.images and not supports_vision:
                request = await self.vision_preparation.prepare_with_local_vision(
                    request, run_id, archived_images
                )
            message_history = self._latest_message_history(
                request.session_id,
                run_id,
                request.agent_id,
                context_window_tokens=context_window_tokens,
                compaction_threshold_ratio=compaction_trigger_ratio(provider_config),
                overhead_tokens=self._context_overhead_tokens(
                    agent_config, skills, request.prompt, workspace
                ),
                supports_vision=supports_vision,
            )
            if command and command["command"] == "/compact":
                # This is a native kernel operation, never a normal agent run.
                # The former path exposed tools and delegates after producing
                # the summary, so stale history could silently resume old work.
                tool_names = {
                    str(item["name"])
                    for item in self._tool_catalog()
                    if item.get("name")
                }
                compactor = ContextWindowCompaction(
                    agent_id=request.agent_id,
                    context_window_tokens=context_window_tokens,
                    all_tool_names=tool_names,
                    overhead_tokens=self._context_overhead_tokens(
                        agent_config, skills, request.prompt, workspace
                    ),
                    trigger_ratio=compaction_trigger_ratio(provider_config),
                    force=True,
                )
                async with asyncio.timeout(budgets.session_timeout_seconds):
                    compacted_history = await compactor.compact_now(
                        message_history,
                        deps,
                        provider_factory.build(active_provider_id, active_model),
                    )
                output = self._manual_compaction_output(request.session_id, run_id)
                compacted_history.extend(
                    [
                        ModelRequest(parts=[UserPromptPart(content=display_prompt)]),
                        ModelResponse(parts=[TextPart(content=output)]),
                    ]
                )
                messages = ModelMessagesTypeAdapter.dump_python(
                    compacted_history, mode="json"
                )
                snapshot_payload = self.snapshots.save(request.session_id, messages)
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id=request.agent_id,
                        type="messages.snapshot",
                        payload=snapshot_payload,
                    )
                )
                response = RunResult(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                    status=RunStatus.SUCCESS,
                    output=output,
                    artifacts=self._run_artifacts(request.session_id, run_id),
                )
                self._capture_git_snapshot(request, run_id, workspace, git_baseline)
                self.executor.terminal(response)
                self.active_runs.pop(request.session_id, None)
                return response
            root_agent = self._build_agent(
                request.agent_id,
                agents,
                provider_factory,
                budgets,
                depth=1,
                skills=skills,
                runtime_skills=request.skills,
                workflow=request.workflow,
                tool_allowlist=request.tool_allowlist,
                knowledge_instruction=self._knowledge_instruction(request),
                workspace=workspace,
                security_mode=request.security_mode,
                provider_override=request.provider_id,
                model_override=request.model,
                force_compaction=False,
            )
            # Relevé pris après la construction de l'agent, donc après le
            # démarrage éventuel du serveur local : le débit du run se lira
            # comme la différence avec le relevé de fin.
            debit_avant = provider_factory.throughput_counters(active_provider_id)
            with capture_run_messages() as captured:
                try:
                    async with asyncio.timeout(budgets.session_timeout_seconds):
                        result = await self._run_root_with_retries(
                            root_agent,
                            request,
                            deps,
                            budgets,
                            run_id,
                            message_history,
                        )
                finally:
                    # Copie immédiate : la liste appartient au contexte et n'est
                    # plus alimentée une fois celui-ci quitté.
                    exchanged = list(captured)
            if isinstance(result.output, DeferredToolRequests):
                response = self._persist_pending(request, run_id, deps, result)
                self.executor.suspend(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                )
                self.active_runs.pop(request.session_id, None)
                return response
            usage = asdict(result.usage)
            _merge_throughput(
                usage,
                debit_avant,
                provider_factory.throughput_counters(active_provider_id),
            )
            messages = json.loads(result.all_messages_json())
            output = str(result.output)
            snapshot_payload = self.snapshots.save(request.session_id, messages)
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                    type="messages.snapshot",
                    payload=snapshot_payload,
                )
            )
            response = RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.SUCCESS,
                output=output,
                usage=usage,
                artifacts=self._run_artifacts(request.session_id, run_id),
            )
        except TimeoutError as exc:
            response = self._failed(
                request, run_id, RunStatus.TIMEOUT, exc, retryable=True, messages=exchanged
            )
        except asyncio.CancelledError as exc:
            response = self._failed(
                request, run_id, RunStatus.CANCELLED, exc, retryable=False, messages=exchanged
            )
        except UsageLimitExceeded as exc:
            response = self._failed(
                request, run_id, RunStatus.PARTIAL, exc, retryable=False, messages=exchanged
            )
        except ModelHTTPError as exc:
            context_overflow = _context_overflow_details(str(exc)) is not None
            response = self._failed(
                request,
                run_id,
                RunStatus.PARTIAL if context_overflow else RunStatus.FAILED,
                exc,
                retryable=context_overflow or _transient_http_status(exc.status_code),
                messages=exchanged,
            )
        except (AuthenticationError, ConfigurationError, KernelError) as exc:
            response = self._failed(
                request, run_id, RunStatus.FAILED, exc, retryable=False, messages=exchanged
            )
        except ModelAPIError as exc:
            # Les retries internes sont épuisés : l'indisponibilité du provider
            # reste retryable pour qu'une routine puisse reprendre plus tard.
            response = self._failed(
                request, run_id, RunStatus.FAILED, exc, retryable=True, messages=exchanged
            )
        except Exception as exc:
            # Toute exception inconnue est un bug, pas un incident transitoire :
            # la marquer retryable faisait boucler indéfiniment une routine sur
            # une erreur de programmation via l'auto-reprise du scheduler.
            response = self._failed(
                request, run_id, RunStatus.FAILED, exc, retryable=False, messages=exchanged
            )
        if not response.artifacts:
            response.artifacts = self._run_artifacts(request.session_id, run_id)
        self._capture_git_snapshot(request, run_id, workspace, git_baseline)
        self.executor.terminal(response)
        self.active_runs.pop(request.session_id, None)
        return response

    def _manual_compaction_output(self, session_id, run_id) -> str:
        event = next(
            (
                item
                for item in reversed(self.events.read(session_id))
                if item.run_id == run_id
                and item.type in {"context.compacted", "context.compaction_noop"}
            ),
            None,
        )
        if event is None:
            return "La compaction manuelle n’a pas pu être confirmée."
        before = int(event.payload.get("estimated_tokens_before", 0))
        after = int(event.payload.get("estimated_tokens_after", before))
        if after < before:
            result = f"contexte actif réduit de {before:,} à {after:,} tokens estimés"
        else:
            result = f"aucun contenu supplémentaire à réduire ({after:,} tokens estimés)"
        return (
            f"Compaction manuelle terminée : {result}. "
            "L’historique complet reste conservé dans l’audit."
        )

    @staticmethod
    def _replace_latest_model_text(messages: list[dict[str, Any]], output: str) -> None:
        for message in reversed(messages):
            if message.get("kind") != "response":
                continue
            for part in reversed(message.get("parts", [])):
                if part.get("part_kind") == "text":
                    part["content"] = output
                    return

    def _capture_git_baseline(
        self, request: RunRequest, run_id: UUID, workspace: Path
    ) -> dict[str, str] | None:
        """État du dépôt avant le run, ou None si on n'a pas pu le lire.

        La distinction porte tout le mécanisme : un dictionnaire vide signifie
        « le dépôt était propre », donc tout ce qu'on trouvera ensuite vient de
        l'agent. `None` signifie « on ne sait pas », et on ne peut alors rien
        attribuer à l'agent.
        """
        if request.workspace is None:
            return None
        try:
            snapshot = self.git.snapshot(workspace, include_patches=False)
            if not snapshot.available:
                return None
            baseline = {item.path: item.fingerprint for item in snapshot.files}
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="git.baseline",
                    payload={"files": baseline},
                )
            )
            return baseline
        except Exception:
            return None

    def _capture_git_snapshot(
        self,
        request: RunRequest,
        run_id: UUID,
        workspace: Path,
        baseline: dict[str, str] | None = None,
    ) -> None:
        if request.workspace is None:
            return
        try:
            if baseline is None:
                baseline_event = next(
                    (
                        event
                        for event in reversed(self.events.read(request.session_id))
                        if event.run_id == run_id and event.type == "git.baseline"
                    ),
                    None,
                )
                if baseline_event is None:
                    # Sans point de comparaison, tout fichier déjà modifié avant
                    # le run passerait pour une modification de l'agent : la
                    # carte présenterait l'état entier du dépôt comme son
                    # travail. Ne rien montrer est le seul repli honnête.
                    return
                baseline = dict(baseline_event.payload.get("files", {}))
            snapshot = self.git.snapshot(workspace)
            changed = [
                item for item in snapshot.files if baseline.get(item.path) != item.fingerprint
            ]
            if snapshot.available and changed:
                payload = snapshot.model_copy(
                    update={
                        "files": changed,
                        "additions": sum(item.additions for item in changed),
                        "deletions": sum(item.deletions for item in changed),
                    }
                )
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id="kernel",
                        type="git.snapshot",
                        payload=payload.model_dump(),
                    )
                )
        except Exception:  # noqa: S110 - repli silencieux volontaire, voir commentaire
            # Git is an optional presentation capability and must never turn
            # an otherwise successful agent response into a failed run.
            pass

    def set_security_mode(self, session_id, mode: SecurityMode) -> bool:
        deps = self.active_runs.get(session_id)
        if deps is None:
            return False
        deps.security_mode = mode
        self.events.append(
            Event(
                session_id=session_id,
                run_id=deps.root_run_id,
                agent_id="kernel",
                type="security.changed",
                payload={"security_mode": mode.value},
            )
        )
        return True

    def list_approvals(self) -> list[ApprovalRequest]:
        return self.approval_service.list_pending()

    def inspect_pending_cron_test(
        self,
        session_id: UUID,
        cron_job_id: str,
    ) -> CronTestApprovalBatch | None:
        return self.approval_service.inspect_pending_cron_test(session_id, cron_job_id)

    def supersede_pending_cron_test(
        self,
        batch: CronTestApprovalBatch,
        *,
        workflow_revision: int,
    ) -> RunResult:
        if batch.session_id in self.active_runs:
            raise ConfigurationError("cron_test prevalidation is still running")
        result = self.approval_service.supersede_pending_cron_test(
            batch,
            workflow_revision=workflow_revision,
        )
        self.executor.terminal(result)
        return result

    async def resolve_approval(
        self,
        approval_id,
        approved: bool,
        *,
        _pre_resolved: bool = False,
    ) -> RunResult:
        prepared = self.approval_service.resolve(
            approval_id,
            approved,
            pre_resolved=_pre_resolved,
        )
        if isinstance(prepared, RunResult):
            return prepared
        try:
            return await self._resume_approval(prepared)
        except Exception as exc:
            # The approval decision is already durable at this point. Return a
            # structured terminal result if restoring the suspended model run
            # fails, so clients can distinguish "decision saved" from
            # "continuation failed" without retrying an already-consumed grant.
            return self._failed(
                prepared.request,
                prepared.approval.run_id,
                RunStatus.FAILED,
                exc,
                retryable=False,
            )

    async def _resume_approval(self, prepared: ApprovalResume) -> RunResult:
        request = prepared.request
        approval = prepared.approval
        state = prepared.state
        agents = self.config.agents()
        provider_factory = ProviderFactory(
            self.config.providers(),
            runtime_dir=self.config.content_root / "runtime" / "providers",
        )
        active_provider_id = request.provider_id or agents[request.agent_id].provider
        provider_config = provider_factory.get_config(active_provider_id)
        active_model = request.model or agents[request.agent_id].model or provider_config.model
        budgets = request.budgets or agents[request.agent_id].budgets
        if budgets is None:
            from .models import BudgetConfig

            budgets = BudgetConfig()
        run_id = approval.run_id
        deps = self.executor.dependencies(
            session_id=request.session_id,
            run_id=run_id,
            budgets=budgets,
            workspace=self.config.resolve_workspace(request.agent_id, request.workspace),
            security_mode=request.security_mode,
            approved_scopes=self._approved_scopes(request.session_id),
            tool_catalog=self._tool_catalog(),
            provider_id=active_provider_id,
            model_name=active_model,
            context_window_tokens=self._model_context_window(
                active_provider_id, active_model, provider_config
            ),
            # La reprise doit partir du même état que le run initial : sans ces
            # trois champs, une routine reprise redemandait des approbations
            # déjà couvertes par son workflow, retombait sur la bibliothèque de
            # `main` pour ses sous-agents et recalibrait sa compaction à 1.0.
            context_calibration=self._context_calibration(request.session_id),
            orchestrator_id=request.agent_id,
            workflow_grants=_workflow_grants(request.workflow),
        )
        deps.approved_scopes.update(prepared.approved_scopes)
        agent = self._build_agent(
            request.agent_id,
            agents,
            provider_factory,
            budgets,
            depth=1,
            skills=self.config.skills(deps.workspace),
            runtime_skills=request.skills,
            workflow=request.workflow,
            tool_allowlist=request.tool_allowlist,
            workspace=deps.workspace,
            security_mode=request.security_mode,
            provider_override=request.provider_id,
            model_override=request.model,
        )
        messages = ModelMessagesTypeAdapter.validate_json(state["messages"])
        deferred = DeferredToolResults(approvals=prepared.tool_results)
        self.active_runs[request.session_id] = deps
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="run.resumed",
                payload={"state": "resuming", "approval_count": len(prepared.batch)},
            )
        )
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="run.transitioned",
                payload={"state": "running", "from": "resuming"},
            )
        )
        try:
            with bind_event_run_id(run_id):
                result = await agent.run(
                    None,
                    message_history=messages,
                    deferred_tool_results=deferred,
                    deps=deps,
                    model_settings=ModelSettings(thinking=request.reasoning)
                    if request.reasoning
                    else None,
                    usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
                )
        except asyncio.CancelledError as exc:
            response = self._failed(request, run_id, RunStatus.CANCELLED, exc, retryable=False)
        except Exception as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=False)
        else:
            if isinstance(result.output, DeferredToolRequests):
                self.active_runs.pop(request.session_id, None)
                response = self._persist_pending(request, run_id, deps, result)
                self.executor.suspend(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                )
                return response
            response = RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.SUCCESS,
                output=str(result.output),
                usage=asdict(result.usage),
            )
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                    type="messages.snapshot",
                    payload=self.snapshots.save(
                        request.session_id,
                        json.loads(result.all_messages_json()),
                    ),
                )
            )
        self._capture_git_snapshot(request, run_id, deps.workspace)
        self.executor.terminal(response)
        self.active_runs.pop(request.session_id, None)
        return response

    async def resolve_approval_batch(
        self,
        approval_ids: list,
        approved: bool,
    ) -> RunResult:
        prepared = self.approval_service.resolve_many(approval_ids, approved)
        if isinstance(prepared, RunResult):
            return prepared
        try:
            return await self._resume_approval(prepared)
        except Exception as exc:
            return self._failed(
                prepared.request,
                prepared.approval.run_id,
                RunStatus.FAILED,
                exc,
                retryable=False,
            )

    def _run_native_secret_command(
        self,
        *,
        request: RunRequest,
        command: str,
    ) -> RunResult:
        """Handle secret commands without creating a model run or audit event."""
        run_id = uuid4()
        try:
            if command == "/secret":
                parts = request.prompt.strip().split(maxsplit=2)
                if len(parts) != 3:
                    raise ValueError("syntaxe attendue : `/secret NOM_DE_VARIABLE valeur`")
                name, value = parts[1], parts[2]
                self.secrets.set(name, value)
                output = (
                    f"Secret `{name}` enregistré localement. "
                    "Sa valeur n’a pas été transmise au modèle ni aux traces."
                )
            else:
                names = self.secrets.names()
                output = "### Secrets disponibles\n\n" + (
                    "\n".join(f"- `{name}`" for name in names)
                    if names
                    else "Aucun secret enregistré."
                )
            return RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.SUCCESS,
                output=output,
            )
        except (ValueError, ConfigurationError) as exc:
            return RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.FAILED,
                output=f"Impossible d’exécuter `{command}` : {exc}",
                errors=[RunError(type="validation", message=str(exc), retryable=False)],
            )

    def _persist_pending(self, request, run_id, deps, result) -> RunResult:
        return self.approval_service.persist_pending(request, run_id, deps, result)

    def _approved_scopes(self, session_id):
        return self.approval_service.approved_scopes(session_id)

    def _tool_catalog(self) -> list[dict[str, Any]]:
        indexed = [
            {
                **tool.model_dump(mode="json"),
                "module": manifest.id,
            }
            for manifest in self.module_registry.check_index().modules
            if manifest.enabled
            for tool in manifest.tools
        ]
        indexed.append(
            {
                "name": "agent_delegate",
                "description": "Delegate an isolated task to one configured child agent.",
                "category": "orchestration",
                "risk_tags": [],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "agent_name": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    "required": ["agent_name", "task"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "string"},
                "timeout_seconds": None,
                "cancellable": True,
                "persistent": False,
                "module": "kernel",
            }
        )
        indexed.append(
            {
                "name": "subagent_spawn",
                "description": (
                    "Spawn a neutral isolated subagent with a temporary role and bounded task."
                ),
                "category": "orchestration",
                "risk_tags": [],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "task": {"type": "string"},
                        "expected_output": {"type": "string"},
                        "scope": {"type": "array", "items": {"type": "string"}},
                        "plan_id": {"type": "string"},
                        "step_id": {"type": "string"},
                        "justification": {"type": "string"},
                    },
                    "required": ["role", "task", "expected_output"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object"},
                "timeout_seconds": None,
                "cancellable": True,
                "persistent": False,
                "module": "kernel",
            }
        )
        return indexed

    def _run_artifacts(self, session_id, run_id) -> list[RunArtifact]:
        return [
            RunArtifact.model_validate(event.payload)
            for event in self.events.read(session_id)
            if event.type == "artifact.created" and event.run_id == run_id
        ]

    def _run_native_context_command(
        self,
        *,
        request: RunRequest,
        display_prompt: str,
        command: str,
        provider_id: str,
        model_name: str | None,
        context_window_tokens: int | None,
        workspace: Path,
        server_cap: int | None = None,
        trigger_ratio: float = 0.7,
    ) -> RunResult:
        return self.context.run_native_command(
            request=request,
            display_prompt=display_prompt,
            command=command,
            provider_id=provider_id,
            model_name=model_name,
            context_window_tokens=context_window_tokens,
            workspace=workspace,
            server_cap=server_cap,
            trigger_ratio=trigger_ratio,
        )

    @staticmethod
    def _parse_context_size(value: str) -> int:
        return ModelContextRegistry.parse_size(value)

    def _store_model_context_window(
        self, provider_id: str, model_name: str | None, size: int
    ) -> None:
        self.context_registry.set(provider_id, model_name, size)

    def context_status(
        self,
        *,
        session_id: UUID | None,
        provider_id: str,
        model_name: str | None,
    ) -> dict[str, Any]:
        """Return a measured context status without creating a model turn."""
        try:
            provider_config = ProviderFactory(self.config.providers()).get_config(provider_id)
        except ConfigurationError:
            provider_config = None
        return self.context.status(
            session_id=session_id,
            provider_id=provider_id,
            model_name=model_name,
            context_window_tokens=self._model_context_window(
                provider_id, model_name, provider_config
            ),
            compaction_threshold_ratio=(
                compaction_trigger_ratio(provider_config) if provider_config is not None else 0.7
            ),
        )

    def _knowledge_instruction(self, request: RunRequest) -> str:
        """Matériel de bibliothèque à joindre au contexte, selon le mode.

        Résolu ici plutôt que dans la fabrique d'agents : c'est la requête qui
        porte le choix de l'utilisateur, et la bibliothèque dépend de
        l'orchestrateur, que seul le kernel connaît à ce stade.
        """
        if request.knowledge_mode == "off":
            return ""
        from .knowledge import KnowledgeLibrary

        racine = self.config.content_root
        library = KnowledgeLibrary(racine / "workspaces" / request.agent_id / "knowledge")
        try:
            return _knowledge_instruction(library, request.knowledge_mode, request.knowledge_pages)
        except OSError:
            # Une bibliothèque illisible ne doit pas empêcher la conversation.
            return ""

    def _context_calibration(self, session_id) -> float:
        """Écart mesuré entre les tokens facturés et notre estimation.

        Vaut 1.0 tant qu'aucun tour n'a été observé : on ne corrige pas sur une
        supposition. La valeur s'affine ensuite à chaque réponse du fournisseur.
        """
        try:
            projected = self.events.projection.context(session_id)
        except Exception:
            return 1.0
        if not projected:
            return 1.0
        return max(
            0.1,
            min(4.0, float(projected.get("calibration_factor") or 1.0)),
        )

    def _model_context_window(
        self,
        provider_id: str,
        model_name: str | None,
        provider_config: ProviderConfig | None = None,
    ) -> int | None:
        # La fenêtre déclarée (/model-context) ne peut pas dépasser ce que le
        # serveur llama.cpp administré alloue au démarrage (`--ctx-size`) :
        # c'est lui qui rejette la requête, pas le kernel.
        declared = self.context_registry.get(provider_id, model_name)
        cap = server_context_cap(provider_config) if provider_config is not None else None
        return effective_context_window(declared, cap)

    @staticmethod
    def _estimate_tokens(value: str) -> int:
        return ContextService.estimate_tokens(value)

    def _context_overhead_tokens(
        self, agent_config, skills, prompt: str, workspace: Path | None = None
    ) -> int:
        return self.context.overhead_tokens(agent_config, skills, prompt, workspace)

    def _secret_catalog_instruction(self) -> str:
        return self.context.secret_catalog_instruction()

    def _latest_message_history(
        self,
        session_id,
        run_id=None,
        agent_id="kernel",
        *,
        context_window_tokens: int | None = None,
        compaction_threshold_ratio: float = 0.7,
        overhead_tokens: int = 0,
        supports_vision: bool = True,
    ):
        return self.context.latest_history(
            session_id,
            run_id,
            agent_id,
            context_window_tokens=context_window_tokens,
            compaction_threshold_ratio=compaction_threshold_ratio,
            supports_vision=supports_vision,
        )

    async def _run_root_with_retries(
        self, root_agent, request, deps, budgets, run_id, message_history=None
    ):
        for attempt in range(budgets.retries + 1):
            try:
                user_prompt: Any = request.prompt
                if request.images:
                    user_prompt = [
                        request.prompt,
                        *[
                            BinaryContent(
                                data=base64.b64decode(image.data_base64, validate=True),
                                media_type=image.media_type,
                                identifier=image.name,
                            )
                            for image in request.images
                        ],
                    ]
                with bind_event_run_id(run_id):
                    return await root_agent.run(
                        user_prompt,
                        message_history=message_history,
                        deps=deps,
                        model_settings=(
                            ModelSettings(thinking=request.reasoning) if request.reasoning else None
                        ),
                        usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
                    )
            except ModelHTTPError as exc:
                if not _transient_http_status(exc.status_code):
                    raise
                if attempt >= budgets.retries:
                    raise
                await deps.reserve_run(request.agent_id)
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id=request.agent_id,
                        type="agent.retrying",
                        attempt=attempt + 1,
                        payload={
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            except ModelAPIError as exc:
                if attempt >= budgets.retries:
                    raise
                await deps.reserve_run(request.agent_id)
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id=request.agent_id,
                        type="agent.retrying",
                        attempt=attempt + 1,
                        payload={"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        raise AssertionError("unreachable")

    def _build_agent(
        self,
        agent_id: str,
        configs,
        provider_factory: ProviderFactory,
        budgets,
        depth: int,
        skills,
        runtime_skills: list[str] | None = None,
        workflow: dict[str, Any] | None = None,
        tool_allowlist: list[str] | None = None,
        knowledge_instruction: str = "",
        workspace: Path | None = None,
        security_mode: SecurityMode | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        force_compaction: bool = False,
    ) -> Agent[RuntimeDeps, Any]:
        return self.agent_factory.build(
            agent_id,
            configs,
            provider_factory,
            budgets,
            depth,
            skills,
            runtime_skills=runtime_skills,
            workflow=workflow,
            tool_allowlist=tool_allowlist,
            knowledge_instruction=knowledge_instruction,
            workspace=workspace,
            security_mode=security_mode,
            provider_override=provider_override,
            model_override=model_override,
            force_compaction=force_compaction,
        )

    def _failed(self, request, run_id, status, exc, retryable, messages=None) -> RunResult:
        """Un run terminé rend toujours un texte, y compris quand il échoue.

        `output` restait vide et le message d'erreur ne vivait que dans le state
        de la surface : au rechargement de la session, il ne restait qu'une bulle
        vide. Le diagnostic existait sans jamais atteindre le disque.
        """
        message = str(self.secrets.redact(str(exc)))
        return RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=status,
            output=self._failure_output(
                request.session_id,
                run_id,
                type(exc).__name__,
                message,
            ),
            errors=[
                RunError(
                    type=type(exc).__name__,
                    message=message,
                    retryable=retryable,
                )
            ],
        )

    def _failure_output(  # noqa: C901 - agrège plusieurs types d'événements terminaux
        self,
        session_id: UUID,
        run_id: UUID,
        error_type: str,
        message: str,
    ) -> str:
        """Produit un bilan factuel sans publier le raisonnement interne.

        Un `ThinkingPart` interrompu est un brouillon : il peut contenir des
        hypothèses périmées, des intentions jamais exécutées et des détails de
        contrôle internes. Le journal append-only contient une meilleure source
        de vérité : les délégations, outils, écritures et compactages réellement
        enregistrés avant l'échec.
        """
        events = [
            event
            for event in self.events.read(session_id)
            if event.run_id == run_id or event.parent_run_id == run_id
        ]
        delegations = [event for event in events if event.type == "agent.started"]
        delegate_failures = [event for event in events if event.type == "agent.failed"]
        tools_completed = [event for event in events if event.type == "tool.completed"]
        tools_failed = [event for event in events if event.type == "tool.failed"]
        compactions = [event for event in events if event.type == "context.compacted"]
        changed_paths: set[str] = set()
        for event in tools_completed:
            if event.payload.get("tool") not in {"write", "patch"}:
                continue
            result = event.payload.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict) or data.get("changed") is False:
                continue
            path = data.get("path")
            if isinstance(path, str) and path:
                changed_paths.add(path)

        overflow = _context_overflow_details(message) if error_type == "ModelHTTPError" else None
        if overflow is not None:
            prompt_tokens, context_tokens = overflow
            excess = max(0, prompt_tokens - context_tokens)
            lines = [
                "**Run interrompu — fenêtre de contexte dépassée.**",
                (
                    f"Le modèle accepte {context_tokens:,} tokens ; la prochaine requête "
                    f"en contenait {prompt_tokens:,}, soit {excess:,} de trop."
                ),
            ]
        else:
            lines = [f"**Run interrompu — {error_type}.**"]
        if message and overflow is None:
            lines.append(self.secrets.redact(message))
        facts: list[str] = []
        if delegations:
            facts.append(
                f"{len(delegations)} délégation(s) démarrée(s), {len(delegate_failures)} en échec."
            )
        if tools_completed or tools_failed:
            facts.append(
                f"{len(tools_completed)} action(s) d’outil terminée(s), "
                f"{len(tools_failed)} en échec."
            )
        if changed_paths:
            paths = ", ".join(sorted(Path(path).name for path in changed_paths))
            facts.append(f"Fichiers écrits ou modifiés : {paths}.")
        if compactions:
            facts.append(f"{len(compactions)} compaction(s) enregistrée(s).")
        completed_tool_names = {event.payload.get("tool") for event in tools_completed}
        visual_steps = [
            label
            for tool, label in (
                ("browser_open", "page ouverte"),
                ("browser_screenshot", "capture réalisée"),
                ("image_inspect", "capture inspectée par la vision"),
            )
            if tool in completed_tool_names
        ]
        if visual_steps:
            facts.append("Test navigateur : " + ", ".join(visual_steps) + ".")
        if facts:
            lines.extend(
                ["", "État factuel avant l’interruption :", *(f"- {fact}" for fact in facts)]
            )
        lines.extend(["", "Le travail effectué est conservé dans la trace."])
        if overflow is not None:
            lines.append(
                "Tu peux reprendre avec « continue » ; le prochain run repartira de cet état "
                "avec une nouvelle passe de réduction du contexte."
            )
        else:
            lines.append("Le travail peut être partiel.")
        return "\n".join(lines)


MAX_INJECTED_PAGE_BYTES = 60_000


def _knowledge_instruction(library: Any, mode: str, pages: list[str]) -> str:
    """Ce que la bibliothèque apporte au contexte, selon le mode retenu.

    Rien par défaut : la connaissance n'entre que sur demande explicite. En
    `auto`, seul l'index est fourni — l'agent disposait déjà de la recherche,
    mais ne s'en servait jamais faute de savoir que la bibliothèque contenait
    quelque chose. En `manual`, les pages retenues sont fournies en entier.
    """
    if mode == "off":
        return ""
    inventaire = library.inventory()
    if not inventaire:
        return ""
    if mode == "auto":
        # Les titres seuls. Le slug n'apporte rien — `knowledge_search` prend
        # une requête libre, jamais un identifiant de page — et les tags
        # doublaient le coût par ligne pour une aide au tri marginale. Cet index
        # est payé à chaque message : tout ce qui n'aide pas à décider s'il faut
        # chercher est du poids mort.
        return "\n".join(
            [
                "# Bibliothèque de connaissance",
                "Sujets disponibles. Si l'un d'eux porte la réponse, lis-le avec"
                " `knowledge_search` avant toute recherche web.",
                *(f"- {page['title']}" for page in inventaire),
            ]
        )

    retenues = [page for page in (library.page(slug) for slug in pages) if page]
    if not retenues:
        return ""
    blocs = [
        contenu
        if len(contenu) <= MAX_INJECTED_PAGE_BYTES
        # Tronquer plutôt qu'échouer : une page démesurée ne doit pas emporter
        # la sélection entière, et l'agent peut la relire par la recherche.
        else contenu[:MAX_INJECTED_PAGE_BYTES] + "\n\n*(page tronquée)*"
        for contenu in retenues
    ]
    return "\n\n---\n\n".join(["# Bibliothèque de connaissance — pages sélectionnées", *blocs])


def _expand_skill_command(prompt: str, command: str, instruction: str) -> str:
    """Remplace le préfixe de commande par la consigne qu'il désigne.

    Même forme que `expand_native_command`, à une différence près : la demande
    de l'utilisateur suit toujours la consigne, car une commande de skill
    accompagne un besoin (« /plan crée l'application ») là où une native se
    suffit à elle-même.
    """
    reste = prompt.lstrip()[len(command) :].strip()
    return instruction + (f"\n\nDemande de l’utilisateur : {reste}" if reste else "")


def _without_images(messages: list[Any]) -> list[Any]:
    return without_images(messages)


def _context_overflow_details(message: str) -> tuple[int, int] | None:
    """Extract provider prompt/window sizes from common local-server errors."""
    prompt = re.search(r"['\"]?n_prompt_tokens['\"]?\s*:\s*(\d+)", message)
    window = re.search(r"['\"]?n_ctx['\"]?\s*:\s*(\d+)", message)
    if prompt and window:
        return int(prompt.group(1)), int(window.group(1))
    prose = re.search(
        r"request\s*\((\d+)\s+tokens\).*?context size\s*\((\d+)\s+tokens\)",
        message,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return (int(prose.group(1)), int(prose.group(2))) if prose else None


def _transient_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _merge_throughput(
    usage: dict[str, Any],
    avant: ThroughputCounters | None,
    apres: ThroughputCounters | None,
) -> None:
    """Ajoute au relevé d'usage les débits réels du serveur local.

    Le nombre de tokens divisé par la durée du run n'est pas une vitesse : il
    met au même dénominateur la lecture du prompt, l'écriture de la réponse,
    les appels d'outils et l'attente d'une autorisation. Un serveur llama.cpp,
    lui, chronomètre séparément la lecture et l'écriture; on lui demande ses
    chiffres plutôt que d'en fabriquer un.

    Sans serveur local, sans `--metrics`, ou si le relevé de départ manque,
    l'usage repart tel quel : mieux vaut pas de vitesse qu'une vitesse fausse.
    """
    if apres is None:
        return
    mesures = apres.since(avant)
    if mesures:
        usage.setdefault("details", {}).update(mesures)


def _workflow_grants(workflow: dict[str, Any] | None) -> frozenset[tuple[str, str]]:
    """Concessions d'un workflow accepté; vide quand la routine n'en a pas.

    Un contrat illisible ne doit pas ouvrir de droits : à la moindre anomalie on
    retombe sur le régime normal, où le Guardian demande.
    """
    if not workflow:
        return frozenset()
    try:
        return workflow_grants(WorkflowDefinition.model_validate(workflow))
    except ValidationError:
        return frozenset()


def _runtime_context_instruction(
    workspace: Path,
    security_mode: SecurityMode,
    now: datetime | None = None,
    provider_id: str | None = None,
    model_name: str | None = None,
    context_window_tokens: int | None = None,
    compaction_threshold_ratio: float = 0.7,
    agent_id: str | None = None,
    agent_description: str | None = None,
    application_root: Path | None = None,
    content_root: Path | None = None,
) -> str:
    """Contexte d'exécution, identité de l'agent comprise.

    Rien ne disait à l'agent qui il était. Son fichier de définition peut le
    nommer, mais rien ne l'y oblige, et en l'absence de nom le modèle en invente
    un — avec l'aplomb d'une information vérifiée. Un agent qui se trompe sur
    son propre nom discrédite tout ce qu'il affirme ensuite.
    """
    current = now or datetime.now().astimezone()
    identite = ""
    if agent_id:
        identite = f"You are the agent `{agent_id}`."
        if agent_description:
            identite += f" {agent_description.strip()}"
        identite += (
            " Never introduce yourself under another name, and never invent one: "
            "if the user asks who you are, answer with this identifier.\n"
        )
    layout = ""
    if application_root is not None and content_root is not None:
        layout = (
            "Runtime layout (these roots have different roles):\n"
            f"- Application/harness: {application_root.resolve()} — source code, Python venv, "
            "web surface and developer dependencies. Inspect this root when diagnosing or "
            "modifying AMK itself.\n"
            f"- AMK user data: {content_root.resolve()} — agents, sessions, models, runtime "
            "artifacts and configuration. It is not the application source and never contains "
            "the kernel Python venv.\n"
            f"- Active project/workspace: {workspace.resolve()} — the user's project for this "
            "run. Keep project investigation and changes here unless the task explicitly concerns "
            "the AMK application or its user data.\n"
        )
    return (
        "# Runtime context\n\n"
        f"{identite}"
        # Le bloc est réécrit à chaque requête, donc toujours juste. Sans cette
        # phrase, la consigne « vérifier plutôt qu'affirmer » du système
        # s'appliquait aussi à l'heure : le modèle traitait une valeur inscrite
        # dans le prompt comme une affirmation à contrôler et appelait l'horloge.
        # 450 runs sur 710 le faisaient, pour 454 appels — 29 % de toute son
        # activité d'outils passée à redemander ce qu'il avait sous les yeux.
        "The date and time below are regenerated for this request and are "
        "authoritative. Use them directly; do not call a clock tool to confirm "
        "them.\n"
        f"Current local date and time: {current.isoformat(timespec='seconds')}\n"
        f"Timezone: {current.tzname() or current.strftime('%z')}\n"
        f"{layout}"
        f"Workspace/CWD: {workspace.resolve()}\n"
        f"Security mode: {security_mode.value}\n"
        f"Active provider/model: {provider_id or 'unknown'}/{model_name or 'unknown'}\n"
        f"Model context window: "
        f"{context_window_tokens if context_window_tokens is not None else 'unknown'} tokens\n"
        f"Automatic compaction threshold: {compaction_threshold_ratio:.0%} "
        "of the model context window"
    )
