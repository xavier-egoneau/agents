from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.subagents import SubAgent, SubAgents
from pydantic_ai_harness.subagents import SubAgentToolset as HarnessSubAgentToolset

from .errors import BudgetExceeded
from .events import JsonlEventStore
from .guardian import action_family
from .models import ApprovalRequest, BudgetConfig, Event, GuardianDecision, SecurityMode


@dataclass
class RuntimeDeps:
    session_id: UUID
    root_run_id: UUID
    budgets: BudgetConfig
    events: JsonlEventStore
    semaphore: asyncio.Semaphore
    workspace: Path = field(default_factory=Path.cwd)
    security_mode: SecurityMode = SecurityMode.LIMITED
    pending_approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    approved_scopes: set[tuple[str, str | None]] = field(default_factory=set)
    agent_runs: int = 1
    attempts: dict[str, int] = field(default_factory=dict)
    tool_catalog: list[dict[str, Any]] = field(default_factory=list)
    state_db: Path | None = None
    provider_id: str | None = None
    model_name: str | None = None
    context_window_tokens: int | None = None
    secret_resolver: Callable[[str], str | None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_scope_approved(self, decision: GuardianDecision) -> bool:
        return (action_family(decision.risks), decision.path) in self.approved_scopes

    async def reserve_run(self, agent_id: str = "unknown") -> int:
        async with self.lock:
            if self.agent_runs >= self.budgets.max_agent_runs:
                raise BudgetExceeded(
                    f"session agent-run limit reached ({self.budgets.max_agent_runs})"
                )
            self.agent_runs += 1
            self.attempts[agent_id] = self.attempts.get(agent_id, 0) + 1
            return self.attempts[agent_id] - 1


class TracedSubAgentToolset(HarnessSubAgentToolset[RuntimeDeps]):
    def __init__(self, *, parent_agent_id: str, **kwargs: Any) -> None:
        self.parent_agent_id = parent_agent_id
        super().__init__(**kwargs)

    async def delegate_task(
        self, ctx: RunContext[RuntimeDeps], agent_name: str, task: str
    ) -> str:
        deps: RuntimeDeps = ctx.deps
        attempt = await deps.reserve_run(agent_name)
        child_run_id = uuid4()
        parent_run_id = _uuid_or(ctx.run_id, deps.root_run_id)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id=agent_name,
                parent_run_id=parent_run_id,
                type="agent.started",
                attempt=attempt,
                payload={"task": task, "delegated_by": self.parent_agent_id},
            )
        )
        async with deps.semaphore:
            try:
                output = await super().delegate_task(ctx, agent_name, task)
            except Exception as exc:
                deps.events.append(
                    Event(
                        session_id=deps.session_id,
                        run_id=child_run_id,
                        agent_id=agent_name,
                        parent_run_id=parent_run_id,
                        type="agent.failed",
                        attempt=attempt,
                        payload={"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
                raise
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id=agent_name,
                parent_run_id=parent_run_id,
                type="agent.completed",
                attempt=attempt,
                payload={"output": output},
            )
        )
        return output


class TracedSubAgents(SubAgents[RuntimeDeps]):
    def __init__(self, *, parent_agent_id: str, **kwargs: Any) -> None:
        self.parent_agent_id = parent_agent_id
        super().__init__(**kwargs)

    def get_toolset(self):
        if not self._by_name:
            return None
        return TracedSubAgentToolset(
            parent_agent_id=self.parent_agent_id,
            agents=self._by_name,
            forward_usage=self.forward_usage,
            inherit_tools=self.inherit_tools,
            shared_capabilities=self.shared_capabilities,
            event_stream_handler=self.event_stream_handler,
            tool_name=self.tool_name,
            tool_retries=self.tool_retries,
            contain_errors=self.contain_errors,
            call_counts=self._call_counts,
        )


def make_subagents(
    parent_agent_id: str,
    agents: list[Agent[RuntimeDeps, Any]],
    budgets: BudgetConfig,
) -> TracedSubAgents:
    return TracedSubAgents(
        parent_agent_id=parent_agent_id,
        agents=[
            SubAgent(
                agent,
                usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
                timeout_seconds=budgets.child_timeout_seconds,
                max_calls=budgets.max_agent_runs,
                contain_errors=True,
            )
            for agent in agents
        ],
        agent_folders=None,
        forward_usage=True,
        tool_retries=budgets.retries,
        contain_errors=True,
        tool_name="agent_delegate",
    )


def make_neutral_subagent_toolset(
    parent_agent_id: str,
    agent: Agent[RuntimeDeps, Any],
    budgets: BudgetConfig,
) -> FunctionToolset[RuntimeDeps]:
    """Expose an ephemeral neutral subagent whose role is supplied per call."""

    async def subagent_spawn(
        ctx: RunContext[RuntimeDeps],
        role: str,
        task: str,
        expected_output: str,
        scope: list[str] | None = None,
        justification: str = "",
    ) -> dict[str, Any]:
        if not role.strip() or not task.strip() or not expected_output.strip():
            raise ValueError("role, task and expected_output are required")
        deps = ctx.deps
        attempt = await deps.reserve_run("subagent")
        child_run_id = uuid4()
        parent_run_id = _uuid_or(ctx.run_id, deps.root_run_id)
        prompt = (
            f"# Temporary role\n{role.strip()}\n\n"
            f"# Bounded task\n{task.strip()}\n\n"
            f"# Allowed scope\n{', '.join(scope or ['workspace'])}\n\n"
            f"# Expected output\n{expected_output.strip()}\n\n"
            "Stay within this task and scope. Report files changed, evidence, "
            "validation and blockers. Do not redefine your permissions."
        )
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id="subagent",
                parent_run_id=parent_run_id,
                type="agent.started",
                attempt=attempt,
                payload={
                    "role": role,
                    "task": task,
                    "scope": scope or [],
                    "expected_output": expected_output,
                    "delegated_by": parent_agent_id,
                    "ephemeral": True,
                },
            )
        )
        async with deps.semaphore:
            try:
                result = await agent.run(
                    prompt,
                    deps=deps,
                    usage_limits=UsageLimits(
                        request_limit=budgets.max_requests_per_agent
                    ),
                )
            except Exception as exc:
                deps.events.append(
                    Event(
                        session_id=deps.session_id,
                        run_id=child_run_id,
                        agent_id="subagent",
                        parent_run_id=parent_run_id,
                        type="agent.failed",
                        attempt=attempt,
                        payload={
                            "role": role,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
                return {
                    "ok": False,
                    "data": None,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "metadata": {"run_id": str(child_run_id)},
                }
        output = str(result.output)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id="subagent",
                parent_run_id=parent_run_id,
                type="agent.completed",
                attempt=attempt,
                payload={"role": role, "output": output, "ephemeral": True},
            )
        )
        return {
            "ok": True,
            "data": {"role": role, "output": output},
            "error": None,
            "metadata": {"run_id": str(child_run_id)},
        }

    return FunctionToolset(tools=[subagent_spawn])


def _uuid_or(value: str | None, fallback: UUID) -> UUID:
    if not value:
        return fallback
    try:
        return UUID(str(value))
    except ValueError:
        return fallback
