import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.errors import BudgetExceeded
from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import (
    BudgetConfig,
    GuardianDecision,
    GuardianVerdict,
    SecurityMode,
    ToolRisk,
)
from agentic_kernel.orchestration import RuntimeDeps


async def test_global_agent_run_budget_counts_retries(tmp_path: Path) -> None:
    deps = RuntimeDeps(
        session_id=uuid4(),
        root_run_id=uuid4(),
        budgets=BudgetConfig(max_agent_runs=2),
        events=JsonlEventStore(tmp_path),
        semaphore=asyncio.Semaphore(1),
    )
    await deps.reserve_run()
    with pytest.raises(BudgetExceeded):
        await deps.reserve_run()


def test_tool_heavy_runs_have_a_practical_default_budget() -> None:
    assert BudgetConfig().max_requests_per_agent == 100


def test_durable_scope_is_bound_to_the_exact_tool(tmp_path: Path) -> None:
    deps = RuntimeDeps(
        session_id=uuid4(),
        root_run_id=uuid4(),
        budgets=BudgetConfig(),
        events=JsonlEventStore(tmp_path),
        semaphore=asyncio.Semaphore(1),
        approved_scopes={("web_search", "network", None)},
    )

    def decision(tool_name: str) -> GuardianDecision:
        return GuardianDecision(
            verdict=GuardianVerdict.ASK,
            reason="approval required",
            tool_name=tool_name,
            agent_id="main",
            tool_call_id="call",
            risks=[ToolRisk.NETWORK],
            justification="test",
            security_mode=SecurityMode.LIMITED,
        )

    assert deps.is_scope_approved(decision("web_search"))
    assert not deps.is_scope_approved(decision("http_post"))
