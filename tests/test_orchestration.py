import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.errors import BudgetExceeded
from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import BudgetConfig
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
