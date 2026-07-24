from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from dataclasses import asdict, replace
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
from pydantic_ai.messages import ImageUrl, ModelRequest, UserPromptPart
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.toolsets import FilteredToolset
from pydantic_ai.usage import UsageLimits

from .approvals import ApprovalStore
from .compaction import ContextWindowCompaction
from .config import ProjectConfig
from .errors import AuthenticationError, ConfigurationError, KernelError
from .events import JsonlEventStore
from .guardian import GuardianToolset
from .models import (
    ApprovalRequest,
    ApprovalResolution,
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
    make_neutral_subagent_toolset,
    make_subagents,
)
from .providers import ProviderFactory
from .run_executor import RunExecutor
from .secrets import SecretStore
from .skills import skill_catalog_instruction, skill_toolset
from .snapshots import SnapshotStore
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
        self.secrets = SecretStore(self.config.content_root / "secrets.json")
        self.workspace_maps = WorkspaceMapService()
        self.vision = LocalVisionService(self.config.content_root)
        self.executor = RunExecutor(
            self.events,
            self.secrets,
            self.snapshots,
            self.config.content_root / "state.db",
        )
        self.active_runs: dict[Any, RuntimeDeps] = {}

    async def run(self, request: RunRequest) -> RunResult:
        display_prompt = request.prompt
        workspace = (request.workspace or self.config.root).resolve()
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
                    "workspace": str(workspace),
                    "security_mode": request.security_mode,
                    "provider_id": active_provider_id,
                    "model": active_model,
                    "reasoning": request.reasoning,
                    "trigger": request.trigger,
                    "cron_job_id": request.cron_job_id,
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
        try:
            if request.images and not supports_vision:
                request = await self._prepare_images_with_local_vision(request, run_id)
            root_agent = self._build_agent(
                request.agent_id,
                agents,
                provider_factory,
                budgets,
                depth=1,
                skills=skills,
                runtime_skills=request.skills,
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
                output=str(result.output),
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
        self.executor.terminal(response)
        self.active_runs.pop(request.session_id, None)
        return response

    async def _prepare_images_with_local_vision(self, request: RunRequest, run_id) -> RunRequest:
        observations: list[str] = []
        artifact_root = self.events.directory / "artifacts" / str(request.session_id)
        for index, image in enumerate(request.images, 1):
            artifact_id = uuid4().hex
            directory = artifact_root / artifact_id
            directory.mkdir(parents=True, exist_ok=True)
            safe_name = Path(image.name).name or f"image-{index}.png"
            target = directory / safe_name
            try:
                raw = base64.b64decode(image.data_base64, validate=True)
            except ValueError as exc:
                raise VisionUnavailable("image encodée invalide") from exc
            target.write_bytes(raw)
            payload = {
                "artifact_id": artifact_id,
                "name": safe_name,
                "media_type": image.media_type,
                "kind": "image",
                "bytes": len(raw),
                "path": str(target),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id="kernel",
                    type="artifact.created",
                    payload=payload,
                )
            )
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
                raise
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
                    "The active model is text-only. Gemma 4 analyzed the attached "
                    "images locally; use these observations as image evidence and "
                    "state any remaining uncertainty."
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
        return self.approvals.list_pending()

    async def resolve_approval(
        self,
        approval_id,
        approved: bool,
        *,
        _pre_resolved: bool = False,
    ) -> RunResult:
        from uuid import UUID

        approval_uuid = approval_id if isinstance(approval_id, UUID) else UUID(str(approval_id))
        state = self.approvals.load_state(approval_uuid)
        if state is None:
            raise ConfigurationError(f"unknown pending approval: {approval_uuid}")
        approval = ApprovalRequest.model_validate(state["approval"])
        if "decision" in state and not _pre_resolved:
            raise ConfigurationError(f"approval already resolved: {approval_uuid}")
        request = RunRequest.model_validate(state["request"])
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
            workspace=(request.workspace or self.config.root).resolve(),
            security_mode=request.security_mode,
            approved_scopes=self._approved_scopes(request.session_id),
            tool_catalog=self._tool_catalog(),
            provider_id=active_provider_id,
            model_name=active_model,
            context_window_tokens=self._model_context_window(active_provider_id, active_model),
        )
        if not _pre_resolved:
            resolution = ApprovalResolution(approval_id=approval_uuid, approved=approved)
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=approval.agent_id,
                    type="approval.resolved",
                    payload={
                        **resolution.model_dump(mode="json"),
                        "action_family": approval.action_family,
                        "path": approval.path,
                    },
                )
            )
            self.approvals.save_state(
                approval,
                {key: value for key, value in state.items() if key != "approval"}
                | {"decision": approved},
            )
        batch = self.approvals.states_for_run(request.session_id, approval.run_id)
        unresolved = [item for item in batch if "decision" not in item]
        if unresolved:
            response = RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.APPROVAL_PENDING,
                output=(
                    f"Approval recorded. {len(unresolved)} approval(s) still pending "
                    "before the run can continue."
                ),
            )
            return response

        # A session must only have one suspended model run. Older approvals can
        # exist in data created before that invariant was enforced; once the
        # latest run is resolved they are obsolete and must not block it again.
        for stale in self.approvals.list_pending():
            if stale.session_id != request.session_id or stale.run_id == approval.run_id:
                continue
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=stale.run_id,
                    agent_id=stale.agent_id,
                    type="approval.superseded",
                    payload={
                        "approval_id": str(stale.approval_id),
                        "superseded_by_run_id": str(approval.run_id),
                    },
                )
            )
            self.approvals.remove(stale.approval_id)

        approval_results = {}
        for item in batch:
            batch_approval = ApprovalRequest.model_validate(item["approval"])
            batch_approved = bool(item["decision"])
            if batch_approved:
                deps.approved_scopes.add((batch_approval.action_family, batch_approval.path))
                approval_results[batch_approval.tool_call_id] = ToolApproved()
            else:
                approval_results[batch_approval.tool_call_id] = ToolDenied("Denied by the user.")
            self.approvals.remove(batch_approval.approval_id)
        agent = self._build_agent(
            request.agent_id,
            agents,
            provider_factory,
            budgets,
            depth=1,
            skills=self.config.skills(),
            runtime_skills=request.skills,
            workspace=deps.workspace,
            security_mode=request.security_mode,
            provider_override=request.provider_id,
            model_override=request.model,
        )
        messages = ModelMessagesTypeAdapter.validate_json(state["messages"])
        deferred = DeferredToolResults(approvals=approval_results)
        self.active_runs[request.session_id] = deps
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="run.resumed",
                payload={"state": "resuming", "approval_count": len(batch)},
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
        self.executor.terminal(response)
        self.active_runs.pop(request.session_id, None)
        return response

    async def resolve_approval_batch(self, approval_ids: list, approved: bool) -> RunResult:
        if not approval_ids:
            raise ConfigurationError("approval batch cannot be empty")
        ids = [item if isinstance(item, UUID) else UUID(str(item)) for item in approval_ids]
        pending_states = [self.approvals.load_state(item) for item in ids]
        if any(state is None for state in pending_states):
            raise ConfigurationError("approval batch contains an unknown approval")
        pending_approvals = [
            ApprovalRequest.model_validate(state["approval"])
            for state in pending_states
            if state is not None
        ]
        if len({(item.session_id, item.run_id) for item in pending_approvals}) != 1:
            raise ConfigurationError("all approvals in a batch must belong to the same run")
        try:
            states = self.approvals.resolve_many(ids, approved)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError(str(exc)) from exc
        approvals = [ApprovalRequest.model_validate(item["approval"]) for item in states]
        for approval in approvals:
            resolution = ApprovalResolution(
                approval_id=approval.approval_id,
                approved=approved,
            )
            self.events.append(
                Event(
                    session_id=approval.session_id,
                    run_id=approval.run_id,
                    agent_id=approval.agent_id,
                    type="approval.resolved",
                    payload={
                        **resolution.model_dump(mode="json"),
                        "action_family": approval.action_family,
                        "path": approval.path,
                    },
                )
            )
        return await self.resolve_approval(
            approvals[-1].approval_id,
            approved,
            _pre_resolved=True,
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
        for call in result.output.approvals:
            approval = deps.pending_approvals.get(call.tool_call_id)
            if approval is None:
                continue
            self.approvals.save_state(
                approval,
                {
                    "request": request.model_dump(mode="json"),
                    "messages": result.all_messages_json().decode(),
                },
            )
        return RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=RunStatus.APPROVAL_PENDING,
            output="Approval required before the run can continue.",
        )

    def _approved_scopes(self, session_id):
        scopes: set[tuple[str, str | None]] = set()
        for event in self.events.read(session_id):
            if event.type == "approval.resolved" and event.payload.get("approved"):
                scopes.add((event.payload.get("action_family", "other"), event.payload.get("path")))
        return scopes

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
        run_id = uuid4()
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.started",
                payload={
                    "prompt": display_prompt,
                    "skills": request.skills,
                    "resolved_command": command,
                    "workspace": str(workspace),
                    "security_mode": request.security_mode,
                    "provider_id": provider_id,
                    "model": model_name,
                    "reasoning": request.reasoning,
                    "trigger": request.trigger,
                    "cron_job_id": request.cron_job_id,
                    "images": [],
                },
            )
        )
        if command == "/model-context":
            raw_value = display_prompt.lstrip()[len(command) :].strip()
            try:
                size = self._parse_context_size(raw_value)
                self._store_model_context_window(provider_id, model_name, size)
                status = RunStatus.SUCCESS
                output = (
                    f"Fenêtre enregistrée pour `{provider_id}/{model_name}` : "
                    f"**{size:,} tokens**. La compaction automatique se déclenchera "
                    f"à **{math.floor(size * 0.7):,} tokens**."
                )
                event_type = "context.window_updated"
                event_payload = {
                    "provider_id": provider_id,
                    "model": model_name,
                    "context_window_tokens": size,
                    "source": "rppl:/model-context",
                }
            except (ValueError, ConfigurationError) as exc:
                status = RunStatus.FAILED
                output = (
                    f"Impossible de définir la fenêtre : {exc}. "
                    "Utilise par exemple `/model-context 128000`."
                )
                event_type = "context.window_update_failed"
                event_payload = {"error": str(exc)}
        else:
            events = self.events.read(request.session_id)
            snapshot = next(
                (
                    event
                    for event in reversed(events)
                    if event.type == "messages.snapshot"
                    and self.snapshots.load(request.session_id, event.payload) is not None
                ),
                None,
            )
            snapshot_messages = (
                self.snapshots.load(request.session_id, snapshot.payload) if snapshot else []
            )
            estimated = self._estimate_tokens(
                json.dumps(
                    snapshot_messages,
                    ensure_ascii=False,
                )
            )
            ratio = estimated / context_window_tokens if context_window_tokens else None
            compactions = [event for event in events if event.type == "context.compacted"]
            output = "\n".join(
                [
                    "### État du contexte",
                    "",
                    f"- Provider : `{provider_id}`",
                    f"- Modèle : `{model_name or 'non résolu'}`",
                    (
                        f"- Fenêtre connue : **{context_window_tokens:,} tokens**"
                        if context_window_tokens
                        else "- Fenêtre connue : **non**"
                    ),
                    f"- Historique estimé : **{estimated:,} tokens**",
                    (
                        f"- Occupation estimée : **{ratio:.1%}**"
                        if ratio is not None
                        else "- Occupation estimée : indisponible"
                    ),
                    (
                        f"- Seuil automatique (70 %) : "
                        f"**{math.floor(context_window_tokens * 0.7):,} tokens**"
                        if context_window_tokens
                        else "- Seuil automatique : inconnu — utilise `/model-context <tokens>`"
                    ),
                    f"- Compactions enregistrées : **{len(compactions)}**",
                    "- Source de vérité complète : journal JSONL append-only",
                ]
            )
            status = RunStatus.SUCCESS
            event_type = "context.inspected"
            event_payload = {
                "provider_id": provider_id,
                "model": model_name,
                "context_window_tokens": context_window_tokens,
                "estimated_history_tokens": estimated,
                "estimated_ratio": ratio,
                "compaction_count": len(compactions),
            }
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id="kernel",
                type=event_type,
                payload=event_payload,
            )
        )
        result = RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=status,
            output=output,
            errors=(
                [RunError(type="validation", message=event_payload["error"])]
                if status is RunStatus.FAILED
                else []
            ),
        )
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.completed",
                payload=result.model_dump(mode="json"),
            )
        )
        return result

    @staticmethod
    def _parse_context_size(value: str) -> int:
        normalized = value.strip().lower().replace("_", "").replace(" ", "")
        multiplier = 1
        if normalized.endswith("k"):
            normalized, multiplier = normalized[:-1], 1_000
        elif normalized.endswith("m"):
            normalized, multiplier = normalized[:-1], 1_000_000
        try:
            result = int(float(normalized) * multiplier)
        except ValueError as exc:
            raise ValueError("taille absente ou invalide") from exc
        if not 1_024 <= result <= 10_000_000:
            raise ValueError("la taille doit être comprise entre 1 024 et 10 000 000")
        return result

    def _store_model_context_window(
        self, provider_id: str, model_name: str | None, size: int
    ) -> None:
        if not model_name:
            raise ConfigurationError("le modèle actif n’est pas résolu")
        path = self.config.content_root / "models-infos.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"schema_version": 1, "models": []}
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"registre invalide : {exc}") from exc
        models = [
            item
            for item in data.get("models", [])
            if not (item.get("provider_id") == provider_id and item.get("model") == model_name)
        ]
        models.append(
            {
                "provider_id": provider_id,
                "model": model_name,
                "context_window_tokens": size,
                "source": "user-confirmed-rppl",
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )
        data = {
            "schema_version": 1,
            "models": sorted(models, key=lambda item: (item["provider_id"], item["model"])),
        }
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def context_status(
        self,
        *,
        session_id: UUID | None,
        provider_id: str,
        model_name: str | None,
    ) -> dict[str, Any]:
        """Return a measured context status without creating a model turn."""
        window = self._model_context_window(provider_id, model_name)
        estimated = 0
        observed = None
        compaction_count = 0
        calibration_factor = 1.0
        calibration_samples = 0
        session = None
        if session_id is not None:
            projected = self.events.projection.context(session_id)
            if projected is not None:
                estimated = int(projected["estimated_history_tokens"])
                observed = projected["observed_input_tokens"]
                compaction_count = int(projected["compaction_count"])
                calibration_factor = float(projected["calibration_factor"])
                calibration_samples = int(projected["calibration_samples"])
            session = self.events.projection.session(session_id)
        try:
            agent = self.config.agents()["main"]
            skills = self.config.skills(
                Path(session["workspace"]) if session and session.get("workspace") else None
            )
            overhead = self._context_overhead_tokens(
                agent,
                skills,
                "",
                Path(session["workspace"]) if session and session.get("workspace") else None,
            )
        except (KeyError, OSError, ValueError):
            overhead = 0
        calibrated_history = round(estimated * calibration_factor)
        complete_estimate = calibrated_history + overhead
        gauge_value = int(observed) if isinstance(observed, int) else complete_estimate
        ratio = gauge_value / window if window else None
        return {
            "provider_id": provider_id,
            "model": model_name,
            "context_window_tokens": window,
            "estimated_history_tokens": estimated,
            "estimated_request_tokens": complete_estimate,
            "observed_input_tokens": observed,
            "estimated_ratio": ratio,
            "compaction_threshold_ratio": 0.7,
            "compaction_count": compaction_count,
            "measurement": "observed" if observed is not None else "estimated",
            "calibration_factor": calibration_factor,
            "calibration_samples": calibration_samples,
        }

    def _model_context_window(self, provider_id: str, model_name: str | None) -> int | None:
        if not model_name:
            return None
        path = self.config.content_root / "models-infos.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for item in data.get("models", []):
            if (
                isinstance(item, dict)
                and item.get("provider_id") == provider_id
                and item.get("model") == model_name
                and isinstance(item.get("context_window_tokens"), int)
            ):
                return item["context_window_tokens"]
        return None

    @staticmethod
    def _estimate_tokens(value: str) -> int:
        # Provider-independent conservative estimate. Exact tokenizers differ;
        # UTF-8 bytes / 3.5 avoids systematically undercounting non-ASCII text.
        return max(1, math.ceil(len(value.encode("utf-8")) / 3.5))

    def _context_overhead_tokens(
        self, agent_config, skills, prompt: str, workspace: Path | None = None
    ) -> int:
        components = [
            self.config.system_instructions(),
            agent_config.instructions,
            prompt,
            self.workspace_maps.build(workspace or self.config.root).render(),
            self._secret_catalog_instruction(),
        ]
        components.extend(
            skills[name].instructions for name in agent_config.skills if name in skills
        )
        try:
            components.append(self.module_registry.index_path.read_text(encoding="utf-8"))
        except OSError:
            pass
        return self._estimate_tokens("\n".join(components))

    def _secret_catalog_instruction(self) -> str:
        names = self.secrets.names()
        if not names:
            return ""
        return "\n".join(
            [
                "# Available secret references",
                (
                    "Only these variable names are visible. Their values are held by "
                    "the kernel and must never be requested from secrets.json or "
                    "repeated in arguments, output, or traces."
                ),
                *(f"- `{name}`" for name in names),
            ]
        )

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
        """Load, recover failed turns, and compact history without losing audit."""
        events = self.events.read(session_id)
        history: list[Any] = []
        snapshot_index = -1
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            messages = (
                self.snapshots.load(session_id, event.payload)
                if event.type == "messages.snapshot"
                else None
            )
            if messages is not None:
                history = list(ModelMessagesTypeAdapter.validate_python(messages))
                if not supports_vision:
                    history = _without_images(history)
                snapshot_index = index
                break

        # A provider or usage-limit failure may happen before Pydantic returns a
        # model snapshot. Preserve those user turns from the append-only audit
        # so “continue” in the same session still has the original request.
        for event in events[snapshot_index + 1 :]:
            if (
                event.type == "session.started"
                and event.run_id != run_id
                and isinstance(event.payload.get("prompt"), str)
            ):
                history.append(
                    ModelRequest(parts=[UserPromptPart(content=event.payload["prompt"])])
                )
        if context_window_tokens is None and run_id is not None:
            self.events.append(
                Event(
                    session_id=session_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    type="context.window_unknown",
                    payload={"compaction_threshold": 0.7},
                )
            )
        if not history:
            return None
        # Compaction is intentionally deferred to ContextWindowCompaction,
        # immediately before the model request. That provider-safe pipeline
        # preserves tool-call/result pairs and is shared by old and live turns.
        return history

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
        workspace: Path | None = None,
        security_mode: SecurityMode | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        force_compaction: bool = False,
    ) -> Agent[RuntimeDeps, Any]:
        if depth > budgets.max_depth:
            raise ConfigurationError(
                f"agent graph exceeds configured depth {budgets.max_depth} at {agent_id}"
            )
        config = configs[agent_id]
        resolved_provider_id = provider_override or config.provider
        resolved_model_name = (
            model_override
            or config.model
            or provider_factory.get_config(resolved_provider_id).model
        )
        context_window_tokens = self._model_context_window(
            resolved_provider_id, resolved_model_name
        )
        loaded_modules = self.module_registry.load_enabled()
        requested_skills = list(dict.fromkeys([*config.skills, *(runtime_skills or [])]))
        missing_skills = set(requested_skills) - skills.keys()
        if missing_skills:
            raise ConfigurationError(
                f"agent {agent_id} references unknown skills: {sorted(missing_skills)}"
            )
        child_agents = [
            self._build_agent(
                child,
                configs,
                provider_factory,
                budgets,
                depth + 1,
                skills,
                workspace=workspace,
                security_mode=security_mode,
            )
            for child in config.delegates
        ]
        instructions = [
            self.config.system_instructions(),
            _runtime_context_instruction(
                workspace or self.config.root,
                security_mode or SecurityMode.LIMITED,
                provider_id=resolved_provider_id,
                model_name=resolved_model_name,
                context_window_tokens=context_window_tokens,
            ),
            self.workspace_maps.build(workspace or self.config.root).render(),
            self._secret_catalog_instruction(),
            config.instructions,
        ]
        for skill_id in requested_skills:
            skill = skills[skill_id]
            instructions.append(
                f"# Skill: {skill.name}\n\n{skill.description}\n\n{skill.instructions}"
            )
        toolsets: list[Any] = []
        neutral_toolsets: list[Any] = []
        capabilities: list[Any] = []
        disabled_tools = self.config.disabled_tools(agent_id)
        selected_tools = set(config.declared_tools)
        known_tools = {tool.name for manifest, _ in loaded_modules for tool in manifest.tools}
        unknown_disabled = disabled_tools - known_tools
        if unknown_disabled:
            raise ConfigurationError(
                f"agent {agent_id} disables unknown tools: {sorted(unknown_disabled)}"
            )
        for manifest, module in loaded_modules:
            instructions.extend(module.instructions())
            risks = {tool.name: tool.risk_tags for tool in manifest.tools}
            timeouts = {tool.name: tool.timeout_seconds for tool in manifest.tools}
            for module_toolset in module.toolsets():
                filtered = FilteredToolset(
                    module_toolset,
                    lambda _ctx, tool_def, disabled=disabled_tools, selected=selected_tools: (
                        tool_def.name not in disabled
                        and (not selected or tool_def.name in selected)
                    ),
                )
                toolsets.append(
                    GuardianToolset(filtered, agent_id=agent_id, risks=risks, timeouts=timeouts)
                )
            for module_toolset in module.toolsets():
                neutral_filtered = FilteredToolset(
                    module_toolset,
                    lambda _ctx, tool_def, disabled=disabled_tools, selected=selected_tools: (
                        tool_def.name not in disabled
                        and (not selected or tool_def.name in selected)
                    ),
                )
                neutral_toolsets.append(
                    GuardianToolset(
                        neutral_filtered,
                        agent_id="subagent",
                        risks=risks,
                        timeouts=timeouts,
                    )
                )
            capabilities.extend(module.capabilities())
        catalog = skill_catalog_instruction(skills)
        loader = skill_toolset(skills)
        if catalog:
            instructions.append(catalog)
        if loader:
            toolsets.append(loader)
        if child_agents:
            capabilities.append(make_subagents(config.id, child_agents, budgets))
        capabilities.append(
            ContextWindowCompaction(
                agent_id=config.id,
                context_window_tokens=context_window_tokens,
                all_tool_names=known_tools,
                overhead_tokens=self._context_overhead_tokens(config, skills, "", workspace),
                force=force_compaction,
            )
        )
        neutral_model = provider_factory.build(
            resolved_provider_id,
            resolved_model_name,
        )
        neutral_agent = Agent(
            neutral_model,
            name=f"{config.id}_subagent",
            description="Neutral ephemeral subagent with a runtime-assigned role.",
            deps_type=RuntimeDeps,
            instructions=[
                self.config.system_instructions(),
                _runtime_context_instruction(
                    workspace or self.config.root,
                    security_mode or SecurityMode.LIMITED,
                    provider_id=resolved_provider_id,
                    model_name=resolved_model_name,
                    context_window_tokens=context_window_tokens,
                ),
                self.workspace_maps.build(workspace or self.config.root).render(),
                self._secret_catalog_instruction(),
                (
                    "You are a neutral ephemeral subagent. Your role, bounded task, "
                    "scope and expected output are supplied in the user prompt. "
                    "Do not expand them, delegate again, or modify the parent plan."
                ),
            ],
            toolsets=neutral_toolsets,
            capabilities=[
                ContextWindowCompaction(
                    agent_id="subagent",
                    context_window_tokens=context_window_tokens,
                    all_tool_names=known_tools,
                    overhead_tokens=self._context_overhead_tokens(config, skills, "", workspace),
                )
            ],
            output_type=str,
            max_concurrency=budgets.max_concurrency,
        )
        toolsets.append(make_neutral_subagent_toolset(config.id, neutral_agent, budgets))
        return Agent(
            provider_factory.build(
                provider_override or config.provider,
                model_override or config.model,
            ),
            name=config.id,
            description=config.description,
            deps_type=RuntimeDeps,
            instructions=instructions,
            toolsets=toolsets,
            capabilities=capabilities,
            output_type=[str, DeferredToolRequests],
            max_concurrency=budgets.max_concurrency,
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
    """Keep text history usable when switching from a vision to a text model."""
    sanitized: list[Any] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            sanitized.append(message)
            continue
        parts: list[Any] = []
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, list):
                parts.append(part)
                continue
            content: list[Any] = []
            for item in part.content:
                if isinstance(item, ImageUrl) or (
                    isinstance(item, BinaryContent) and item.is_image
                ):
                    content.append(
                        "[Image jointe omise : le modèle actif ne prend pas en charge la vision.]"
                    )
                else:
                    content.append(item)
            parts.append(replace(part, content=content))
        sanitized.append(replace(message, parts=parts))
    return sanitized


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
