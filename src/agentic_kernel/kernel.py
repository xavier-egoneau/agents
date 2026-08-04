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

from pydantic_ai import (
    Agent,
    BinaryContent,
    DeferredToolRequests,
    ModelMessagesTypeAdapter,
    ModelSettings,
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
        elif command and command["skill"] not in request.skills:
            request = request.model_copy(update={"skills": [*request.skills, command["skill"]]})
        provider_factory = ProviderFactory(self.config.providers())
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
            async with asyncio.timeout(budgets.session_timeout_seconds):
                result = await self._run_root_with_retries(
                    root_agent,
                    request,
                    deps,
                    budgets,
                    run_id,
                    message_history,
                )
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
            response = self._failed(request, run_id, RunStatus.TIMEOUT, exc, retryable=True)
        except asyncio.CancelledError as exc:
            response = self._failed(request, run_id, RunStatus.CANCELLED, exc, retryable=False)
        except UsageLimitExceeded as exc:
            response = self._failed(request, run_id, RunStatus.PARTIAL, exc, retryable=False)
        except ModelHTTPError as exc:
            response = self._failed(
                request,
                run_id,
                RunStatus.FAILED,
                exc,
                retryable=_transient_http_status(exc.status_code),
            )
        except (AuthenticationError, ConfigurationError, KernelError) as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=False)
        except Exception as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=True)
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
    ) -> dict[str, str]:
        if request.workspace is None:
            return {}
        try:
            snapshot = self.git.snapshot(workspace, include_patches=False)
            baseline = {item.path: item.fingerprint for item in snapshot.files}
            if snapshot.available:
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
            return {}

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
                baseline = dict(baseline_event.payload.get("files", {})) if baseline_event else {}
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
        provider_factory = ProviderFactory(self.config.providers())
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
            workspace=workspace,
            security_mode=security_mode,
            provider_override=provider_override,
            model_override=model_override,
            force_compaction=force_compaction,
        )

    def _failed(self, request, run_id, status, exc, retryable) -> RunResult:
        message = self.secrets.redact(str(exc))
        return RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=status,
            errors=[
                RunError(
                    type=type(exc).__name__,
                    message=str(message),
                    retryable=retryable,
                )
            ],
        )


def _without_images(messages: list[Any]) -> list[Any]:
    return without_images(messages)


def _transient_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _runtime_context_instruction(
    workspace: Path,
    security_mode: SecurityMode,
    now: datetime | None = None,
    provider_id: str | None = None,
    model_name: str | None = None,
    context_window_tokens: int | None = None,
) -> str:
    current = now or datetime.now().astimezone()
    return (
        "# Runtime context\n\n"
        f"Current local date and time: {current.isoformat(timespec='seconds')}\n"
        f"Timezone: {current.tzname() or current.strftime('%z')}\n"
        f"Workspace/CWD: {workspace.resolve()}\n"
        f"Security mode: {security_mode.value}\n"
        f"Active provider/model: {provider_id or 'unknown'}/{model_name or 'unknown'}\n"
        f"Model context window: "
        f"{context_window_tokens if context_window_tokens is not None else 'unknown'} tokens\n"
        "Automatic compaction threshold: 70% of the model context window"
    )
