from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic_ai import (
    Agent,
    BinaryContent,
    DeferredToolRequests,
    ModelMessagesTypeAdapter,
    ModelSettings,
)
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.toolsets import FilteredToolset
from pydantic_ai.usage import UsageLimits

from .approvals import ApprovalStore
from .config import ProjectConfig
from .errors import AuthenticationError, ConfigurationError, KernelError
from .events import JsonlEventStore
from .guardian import GuardianToolset
from .models import (
    ApprovalRequest,
    ApprovalResolution,
    Event,
    RunError,
    RunRequest,
    RunResult,
    RunStatus,
    SecurityMode,
)
from .modules import ModuleRegistry
from .orchestration import RuntimeDeps, make_subagents
from .providers import ProviderFactory
from .skills import skill_catalog_instruction, skill_toolset


class Kernel:
    """Stable facade around Pydantic AI and the harness orchestration layer."""

    def __init__(self, root: Path | str = ".") -> None:
        self.config = ProjectConfig(root)
        self.module_registry = ModuleRegistry(self.config.tools_root)
        self.events = JsonlEventStore(self.config.content_root / "sessions")
        self.approvals = ApprovalStore(self.config.content_root / "sessions")
        self.active_runs: dict[Any, RuntimeDeps] = {}

    async def run(self, request: RunRequest) -> RunResult:
        agents = self.config.agents()
        if request.agent_id not in agents:
            raise ConfigurationError(f"unknown agent: {request.agent_id}")
        provider_factory = ProviderFactory(self.config.providers())
        skills = self.config.skills()
        budgets = request.budgets or agents[request.agent_id].budgets
        if budgets is None:
            from .models import BudgetConfig

            budgets = BudgetConfig()
        run_id = uuid4()
        workspace = (request.workspace or self.config.root).resolve()
        deps = RuntimeDeps(
            session_id=request.session_id,
            root_run_id=run_id,
            budgets=budgets,
            events=self.events,
            semaphore=asyncio.Semaphore(budgets.max_concurrency),
            workspace=workspace,
            security_mode=request.security_mode,
            approved_scopes=self._approved_scopes(request.session_id),
        )
        self.active_runs[request.session_id] = deps
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.started",
                payload={"prompt": request.prompt, "budgets": budgets.model_dump(),
                         "workspace": str(workspace), "security_mode": request.security_mode,
                         "provider_id": request.provider_id, "model": request.model,
                         "reasoning": request.reasoning,
                         "images": [{"name": item.name, "media_type": item.media_type}
                                    for item in request.images]},
            )
        )
        try:
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
            )
            message_history = self._latest_message_history(request.session_id)
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
                self.events.append(Event(
                    session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
                    type="session.completed", payload=response.model_dump(mode="json"),
                ))
                self.active_runs.pop(request.session_id, None)
                return response
            usage = asdict(result.usage)
            messages = json.loads(result.all_messages_json())
            self.events.append(
                Event(
                    session_id=request.session_id,
                    run_id=run_id,
                    agent_id=request.agent_id,
                    type="messages.snapshot",
                    payload={"messages": messages},
                )
            )
            response = RunResult(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                status=RunStatus.SUCCESS,
                output=str(result.output),
                usage=usage,
            )
        except TimeoutError as exc:
            response = self._failed(request, run_id, RunStatus.TIMEOUT, exc, retryable=True)
        except asyncio.CancelledError as exc:
            response = self._failed(request, run_id, RunStatus.CANCELLED, exc, retryable=False)
        except (AuthenticationError, ConfigurationError, KernelError) as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=False)
        except Exception as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=True)
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.completed",
                payload=response.model_dump(mode="json"),
            )
        )
        self.active_runs.pop(request.session_id, None)
        return response

    def set_security_mode(self, session_id, mode: SecurityMode) -> bool:
        deps = self.active_runs.get(session_id)
        if deps is None:
            return False
        deps.security_mode = mode
        self.events.append(Event(
            session_id=session_id,
            run_id=deps.root_run_id,
            agent_id="kernel",
            type="security.changed",
            payload={"security_mode": mode.value},
        ))
        return True

    def list_approvals(self) -> list[ApprovalRequest]:
        return self.approvals.list_pending()

    async def resolve_approval(self, approval_id, approved: bool) -> RunResult:
        from uuid import UUID

        approval_uuid = approval_id if isinstance(approval_id, UUID) else UUID(str(approval_id))
        state = self.approvals.load_state(approval_uuid)
        if state is None:
            raise ConfigurationError(f"unknown pending approval: {approval_uuid}")
        approval = ApprovalRequest.model_validate(state["approval"])
        if "decision" in state:
            raise ConfigurationError(f"approval already resolved: {approval_uuid}")
        request = RunRequest.model_validate(state["request"])
        agents = self.config.agents()
        budgets = request.budgets or agents[request.agent_id].budgets
        if budgets is None:
            from .models import BudgetConfig
            budgets = BudgetConfig()
        run_id = approval.run_id
        deps = RuntimeDeps(
            session_id=request.session_id, root_run_id=run_id, budgets=budgets,
            events=self.events, semaphore=asyncio.Semaphore(budgets.max_concurrency),
            workspace=(request.workspace or self.config.root).resolve(),
            security_mode=request.security_mode,
            approved_scopes=self._approved_scopes(request.session_id),
        )
        resolution = ApprovalResolution(approval_id=approval_uuid, approved=approved)
        self.events.append(Event(
            session_id=request.session_id, run_id=run_id, agent_id=approval.agent_id,
            type="approval.resolved",
            payload={**resolution.model_dump(mode="json"), "action_family": approval.action_family,
                     "path": approval.path},
        ))
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
            self.events.append(Event(
                session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
                type="session.completed", payload=response.model_dump(mode="json"),
            ))
            return response

        approval_results = {}
        for item in batch:
            batch_approval = ApprovalRequest.model_validate(item["approval"])
            batch_approved = bool(item["decision"])
            if batch_approved:
                deps.approved_scopes.add(
                    (batch_approval.action_family, batch_approval.path)
                )
                approval_results[batch_approval.tool_call_id] = ToolApproved()
            else:
                approval_results[batch_approval.tool_call_id] = ToolDenied(
                    "Denied by the user."
                )
            self.approvals.remove(batch_approval.approval_id)
        provider_factory = ProviderFactory(self.config.providers())
        agent = self._build_agent(
            request.agent_id, agents, provider_factory, budgets, depth=1,
            skills=self.config.skills(), runtime_skills=request.skills,
            workspace=deps.workspace, security_mode=request.security_mode,
            provider_override=request.provider_id, model_override=request.model,
        )
        messages = ModelMessagesTypeAdapter.validate_json(state["messages"])
        deferred = DeferredToolResults(approvals=approval_results)
        self.active_runs[request.session_id] = deps
        try:
            result = await agent.run(
                None, message_history=messages, deferred_tool_results=deferred, deps=deps,
                model_settings=ModelSettings(thinking=request.reasoning)
                if request.reasoning else None,
                usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
            )
        except asyncio.CancelledError as exc:
            response = self._failed(request, run_id, RunStatus.CANCELLED, exc, retryable=False)
        except Exception as exc:
            response = self._failed(request, run_id, RunStatus.FAILED, exc, retryable=False)
        else:
            if isinstance(result.output, DeferredToolRequests):
                self.active_runs.pop(request.session_id, None)
                return self._persist_pending(request, run_id, deps, result)
            response = RunResult(
                session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
                status=RunStatus.SUCCESS, output=str(result.output), usage=asdict(result.usage),
            )
            self.events.append(Event(
                session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
                type="messages.snapshot",
                payload={"messages": json.loads(result.all_messages_json())},
            ))
        self.events.append(Event(
            session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
            type="session.completed", payload=response.model_dump(mode="json"),
        ))
        self.active_runs.pop(request.session_id, None)
        return response

    async def resolve_approval_batch(
        self, approval_ids: list, approved: bool
    ) -> RunResult:
        if not approval_ids:
            raise ConfigurationError("approval batch cannot be empty")
        result: RunResult | None = None
        for approval_id in approval_ids:
            result = await self.resolve_approval(approval_id, approved)
        assert result is not None
        return result

    def _persist_pending(self, request, run_id, deps, result) -> RunResult:
        for call in result.output.approvals:
            approval = deps.pending_approvals.get(call.tool_call_id)
            if approval is None:
                continue
            self.approvals.save_state(approval, {
                "request": request.model_dump(mode="json"),
                "messages": result.all_messages_json().decode(),
            })
        return RunResult(
            session_id=request.session_id, run_id=run_id, agent_id=request.agent_id,
            status=RunStatus.APPROVAL_PENDING,
            output="Approval required before the run can continue.",
        )

    def _approved_scopes(self, session_id):
        scopes: set[tuple[str, str | None]] = set()
        for event in self.events.read(session_id):
            if event.type == "approval.resolved" and event.payload.get("approved"):
                scopes.add((event.payload.get("action_family", "other"), event.payload.get("path")))
        return scopes

    def _latest_message_history(self, session_id):
        """Load the last complete model history for another turn in this session."""
        for event in reversed(self.events.read(session_id)):
            if event.type != "messages.snapshot":
                continue
            messages = event.payload.get("messages")
            if isinstance(messages, list):
                return ModelMessagesTypeAdapter.validate_python(messages)
        return None

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
                        ModelSettings(thinking=request.reasoning)
                        if request.reasoning
                        else None
                    ),
                    usage_limits=UsageLimits(
                        request_limit=budgets.max_requests_per_agent
                    ),
                )
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
    ) -> Agent[RuntimeDeps, Any]:
        if depth > budgets.max_depth:
            raise ConfigurationError(
                f"agent graph exceeds configured depth {budgets.max_depth} at {agent_id}"
            )
        config = configs[agent_id]
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
            ),
            config.instructions,
        ]
        for skill_id in requested_skills:
            skill = skills[skill_id]
            instructions.append(
                f"# Skill: {skill.name}\n\n{skill.description}\n\n{skill.instructions}"
            )
        toolsets: list[Any] = []
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
            for module_toolset in module.toolsets():
                filtered = FilteredToolset(
                    module_toolset,
                    lambda _ctx, tool_def, disabled=disabled_tools, selected=selected_tools: (
                        tool_def.name not in disabled
                        and (not selected or tool_def.name in selected)
                    ),
                )
                toolsets.append(GuardianToolset(filtered, agent_id=agent_id, risks=risks))
            capabilities.extend(module.capabilities())
        catalog = skill_catalog_instruction(skills)
        loader = skill_toolset(skills)
        if catalog:
            instructions.append(catalog)
        if loader:
            toolsets.append(loader)
        if child_agents:
            capabilities.append(make_subagents(config.id, child_agents, budgets))
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

    @staticmethod
    def _failed(request, run_id, status, exc, retryable) -> RunResult:
        return RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=status,
            errors=[
                RunError(
                    type=type(exc).__name__,
                    message=str(exc),
                    retryable=retryable,
                )
            ],
        )


def _runtime_context_instruction(
    workspace: Path,
    security_mode: SecurityMode,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now().astimezone()
    return (
        "# Runtime context\n\n"
        f"Current local date and time: {current.isoformat(timespec='seconds')}\n"
        f"Timezone: {current.tzname() or current.strftime('%z')}\n"
        f"Workspace/CWD: {workspace.resolve()}\n"
        f"Security mode: {security_mode.value}"
    )
