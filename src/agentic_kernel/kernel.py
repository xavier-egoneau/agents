from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.usage import UsageLimits

from .agent_factory import AgentFactory
from .approval_service import ApprovalResume, ApprovalService, CronTestApprovalBatch
from .approvals import ApprovalStore
from .config import ProjectConfig
from .context_service import ContextService, ModelContextRegistry, without_images
from .errors import AuthenticationError, ConfigurationError, KernelError
from .events import JsonlEventStore
from .git_service import GitService
from .models import (
    ApprovalRequest,
    Event,
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
from .providers import ProviderFactory
from .run_executor import RunExecutor
from .secrets import SecretStore
from .snapshots import SnapshotStore
from .trace_context import bind_event_run_id
from .vision import LocalVisionService, VisionUnavailable
from .workflows import WorkflowDefinition, workflow_grants
from .workspace_map import WorkspaceMapService


class Kernel:
    """Stable facade around Pydantic AI and the harness orchestration layer."""

    def __init__(self, root: Path | str = ".") -> None:
        self.config = ProjectConfig(root)
        self.module_registry = ModuleRegistry(self.config.tools_root)
        self.events = JsonlEventStore(self.config.content_root / "sessions")
        self.events.rebuild_projection()
        self.snapshots = SnapshotStore(self.config.content_root / "sessions")
        self.approvals = ApprovalStore(self.config.content_root / "sessions")
        self.approval_service = ApprovalService(self.approvals, self.events)
        self.secrets = SecretStore(self.config.content_root / "secrets.json")
        self.workspace_maps = WorkspaceMapService()
        self.vision = LocalVisionService(self.config.content_root)
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

    async def run(self, request: RunRequest) -> RunResult:
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
        context_window_tokens = self._model_context_window(active_provider_id, active_model)
        if command and command["command"] in {"/context", "/model-context"}:
            return self._run_native_context_command(
                request=request,
                display_prompt=display_prompt,
                command=command["command"],
                provider_id=active_provider_id,
                model_name=active_model,
                context_window_tokens=context_window_tokens,
                workspace=workspace,
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
            archived_images = self._archive_input_images(request, run_id)
            if request.images and not supports_vision:
                request = await self._prepare_images_with_local_vision(
                    request, run_id, archived_images
                )
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
                force_compaction=bool(command and command["command"] == "/compact"),
            )
            message_history = self._latest_message_history(
                request.session_id,
                run_id,
                request.agent_id,
                context_window_tokens=context_window_tokens,
                overhead_tokens=self._context_overhead_tokens(
                    agent_config, skills, request.prompt, workspace
                ),
                supports_vision=supports_vision,
            )
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
            messages = json.loads(result.all_messages_json())
            output = str(result.output)
            if command and command["command"] == "/compact":
                output = self._manual_compaction_output(request.session_id, run_id)
                self._replace_latest_model_text(messages, output)
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
            response = self._failed(
                request,
                run_id,
                RunStatus.FAILED,
                exc,
                retryable=_transient_http_status(exc.status_code),
                messages=exchanged,
            )
        except (AuthenticationError, ConfigurationError, KernelError) as exc:
            response = self._failed(
                request, run_id, RunStatus.FAILED, exc, retryable=False, messages=exchanged
            )
        except Exception as exc:
            response = self._failed(
                request, run_id, RunStatus.FAILED, exc, retryable=True, messages=exchanged
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
                if item.run_id == run_id and item.type == "context.compacted"
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
        except Exception:
            # Git is an optional presentation capability and must never turn
            # an otherwise successful agent response into a failed run.
            pass

    def _archive_input_images(self, request: RunRequest, run_id) -> list[tuple[Any, bytes, Path]]:
        archived: list[tuple[Any, bytes, Path]] = []
        artifact_root = self.events.directory / "artifacts" / str(request.session_id)
        for index, image in enumerate(request.images, 1):
            try:
                raw = base64.b64decode(image.data_base64, validate=True)
            except ValueError as exc:
                raise VisionUnavailable("image encodée invalide") from exc
            artifact_id = uuid4().hex
            directory = artifact_root / artifact_id
            directory.mkdir(parents=True, exist_ok=True)
            safe_name = Path(image.name).name or f"image-{index}.png"
            target = directory / safe_name
            target.write_bytes(raw)
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="artifact.created",
                    payload={
                        "artifact_id": artifact_id,
                        "name": safe_name,
                        "media_type": image.media_type,
                        "kind": "input_image",
                        "bytes": len(raw),
                        # Voir `routers/artifacts.py` : le relatif est l'adresse,
                        # l'absolu n'est qu'une trace de production.
                        "relative_path": f"{artifact_id}/{safe_name}",
                        "path": str(target),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                )
            )
            archived.append((image, raw, target))
        return archived

    async def _prepare_images_with_local_vision(
        self,
        request: RunRequest,
        run_id,
        archived: list[tuple[Any, bytes, Path]],
    ) -> RunRequest:
        observations: list[str] = []
        for index, (image, raw, target) in enumerate(archived, 1):
            artifact_id = target.parent.name
            safe_name = target.name
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="tool.started",
                    payload={
                        "tool_name": "image_inspect",
                        "automatic": True,
                        "artifact_id": artifact_id,
                        "path": str(target),
                    },
                )
            )
            try:
                observation = await self.vision.analyze_bytes(
                    raw,
                    image.media_type,
                    (f"Analyse cette image pour répondre à la demande suivante : {request.prompt}"),
                    "balanced",
                )
            except VisionUnavailable as exc:
                self.events.append(
                    Event(
                        session_id=request.session_id,
                        run_id=run_id,
                        agent_id="kernel",
                        type="tool.failed",
                        payload={
                            "tool_name": "image_inspect",
                            "automatic": True,
                            "artifact_id": artifact_id,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
                observations.append(
                    f"## Image {index}: {safe_name}\n"
                    "Analyse visuelle indisponible. L’image est bien jointe et archivée, "
                    "mais son contenu n’a pas été observé. Ne déduis aucun détail visuel et "
                    f"signale cette limite à l’utilisateur. Cause locale : {exc}"
                )
                continue
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="tool.completed",
                    payload={
                        "tool_name": "image_inspect",
                        "automatic": True,
                        "artifact_id": artifact_id,
                        "observation_chars": len(observation),
                        "preview": observation[:500],
                    },
                )
            )
            observations.append(
                f"## Image {index}: {safe_name}\n"
                f"Artifact reference: `{artifact_id}`\n\n{observation}"
            )
        augmented = "\n\n".join(
            [
                request.prompt,
                (
                    "# Local vision observations\n\n"
                    "The active model is text-only. The notes below state whether each "
                    "attachment was analyzed locally. Use only recorded observations, "
                    "never infer unavailable visual content, and state any limitation."
                ),
                *observations,
            ]
        )
        return request.model_copy(update={"prompt": augmented, "images": []})

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
            context_window_tokens=self._model_context_window(active_provider_id, active_model),
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
    ) -> RunResult:
        return self.context.run_native_command(
            request=request,
            display_prompt=display_prompt,
            command=command,
            provider_id=provider_id,
            model_name=model_name,
            context_window_tokens=context_window_tokens,
            workspace=workspace,
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
        return self.context.status(
            session_id=session_id,
            provider_id=provider_id,
            model_name=model_name,
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
            return _knowledge_instruction(
                library, request.knowledge_mode, request.knowledge_pages
            )
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
        return max(1.0, float(projected.get("calibration_factor") or 1.0))

    def _model_context_window(self, provider_id: str, model_name: str | None) -> int | None:
        return self.context_registry.get(provider_id, model_name)

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
        overhead_tokens: int = 0,
        supports_vision: bool = True,
    ):
        return self.context.latest_history(
            session_id,
            run_id,
            agent_id,
            context_window_tokens=context_window_tokens,
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
            output=self._salvaged_output(type(exc).__name__, message, messages),
            errors=[
                RunError(
                    type=type(exc).__name__,
                    message=message,
                    retryable=retryable,
                )
            ],
        )

    def _salvaged_output(
        self, error_type: str, message: str, messages: list[Any] | None
    ) -> str:
        """Compose une réponse dégradée à partir de ce que le modèle a produit.

        Quand la limite de tokens tombe pendant la phase de raisonnement, la
        réponse ne contient que des `ThinkingPart` : pydantic-ai considère
        qu'aucune sortie exploitable n'existe et lève, emportant avec lui un
        travail qui peut représenter plusieurs minutes. Ce raisonnement reste
        présent dans les messages capturés, et vaut mieux que rien.
        """
        recovered = _last_model_text(messages) if messages else ("", "")
        texte, reflexion = recovered
        blocs: list[str] = []
        if texte.strip():
            blocs.append(texte.strip())
        elif reflexion.strip():
            blocs.append(
                "*Réponse interrompue. Voici le raisonnement produit avant "
                "l'interruption.*\n\n" + reflexion.strip()
            )
        blocs.append(
            f"**Le run ne s'est pas terminé — {error_type}.**\n\n{self.secrets.redact(message)}"
            if message
            else f"**Le run ne s'est pas terminé — {error_type}.**"
        )
        return "\n\n---\n\n".join(blocs)


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
        contenu if len(contenu) <= MAX_INJECTED_PAGE_BYTES
        # Tronquer plutôt qu'échouer : une page démesurée ne doit pas emporter
        # la sélection entière, et l'agent peut la relire par la recherche.
        else contenu[:MAX_INJECTED_PAGE_BYTES] + "\n\n*(page tronquée)*"
        for contenu in retenues
    ]
    return "\n\n---\n\n".join(
        ["# Bibliothèque de connaissance — pages sélectionnées", *blocs]
    )


def _expand_skill_command(prompt: str, command: str, instruction: str) -> str:
    """Remplace le préfixe de commande par la consigne qu'il désigne.

    Même forme que `expand_native_command`, à une différence près : la demande
    de l'utilisateur suit toujours la consigne, car une commande de skill
    accompagne un besoin (« /plan crée l'application ») là où une native se
    suffit à elle-même.
    """
    reste = prompt.lstrip()[len(command) :].strip()
    return instruction + (f"\n\nDemande de l’utilisateur : {reste}" if reste else "")


def _last_model_text(messages: list[Any]) -> tuple[str, str]:
    """Texte et raisonnement de la dernière réponse du modèle.

    Renvoie les deux séparément parce qu'ils ne valent pas la même chose : un
    texte est la réponse, un raisonnement n'en est que la trace. On lit à
    rebours pour trouver la dernière réponse, celle sur laquelle le run a buté.

    Les objets viennent du SDK et leur forme n'est pas garantie d'une version à
    l'autre : on lit par attributs, sans jamais supposer qu'ils existent.
    """
    for message in reversed(messages or []):
        parts = getattr(message, "parts", None)
        if not parts:
            continue
        textes: list[str] = []
        reflexions: list[str] = []
        for part in parts:
            contenu = getattr(part, "content", None)
            if not isinstance(contenu, str) or not contenu.strip():
                continue
            genre = getattr(part, "part_kind", "")
            if genre == "thinking":
                reflexions.append(contenu)
            elif genre == "text":
                textes.append(contenu)
        if textes or reflexions:
            return "\n".join(textes), "\n".join(reflexions)
    return "", ""


def _without_images(messages: list[Any]) -> list[Any]:
    return without_images(messages)


def _transient_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


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
    agent_id: str | None = None,
    agent_description: str | None = None,
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
    return (
        "# Runtime context\n\n"
        f"{identite}"
        f"Current local date and time: {current.isoformat(timespec='seconds')}\n"
        f"Timezone: {current.tzname() or current.strftime('%z')}\n"
        f"Workspace/CWD: {workspace.resolve()}\n"
        f"Security mode: {security_mode.value}\n"
        f"Active provider/model: {provider_id or 'unknown'}/{model_name or 'unknown'}\n"
        f"Model context window: "
        f"{context_window_tokens if context_window_tokens is not None else 'unknown'} tokens\n"
        "Automatic compaction threshold: 70% of the model context window"
    )
