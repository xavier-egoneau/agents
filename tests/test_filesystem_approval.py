import json
import shutil
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agentic_kernel.errors import ConfigurationError
from agentic_kernel.kernel import Kernel
from agentic_kernel.models import RunRequest, RunStatus
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.providers import ProviderFactory


async def test_overwrite_suspends_resumes_once_and_traces(
    project: Path, monkeypatch
) -> None:
    source = Path(__file__).parents[1] / "tools" / "modules" / "filesystem"
    shutil.copytree(source, project / "tools" / "modules" / "filesystem")
    ModuleRegistry(project / "tools").build_index()
    target = project / "note.txt"
    target.write_text("old", encoding="utf-8")

    def respond(messages, info):
        assert "write" in {tool.name for tool in info.function_tools}
        called = any(
            getattr(part, "tool_name", None) == "write"
            for message in messages
            for part in message.parts
        )
        if not called:
            return ModelResponse(parts=[ToolCallPart(
                "write",
                {"path": "note.txt", "content": "new", "justification": "Update requested."},
                tool_call_id="write-1",
            )])
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    first = await kernel.run(RunRequest(prompt="Update the note", workspace=project))
    assert first.status is RunStatus.APPROVAL_PENDING
    assert target.read_text() == "old"
    approval = kernel.list_approvals()[0]

    final = await kernel.resolve_approval(approval.approval_id, True)
    assert final.status is RunStatus.SUCCESS
    assert target.read_text() == "new"
    event_types = [event.type for event in kernel.events.read(first.session_id)]
    assert event_types.count("tool.started") == 1
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
    assert event_types.index("approval.resolved") < event_types.index("tool.started")


async def test_parallel_approvals_resume_as_one_batch(project: Path, monkeypatch) -> None:
    source = Path(__file__).parents[1] / "tools" / "modules" / "filesystem"
    shutil.copytree(source, project / "tools" / "modules" / "filesystem")
    ModuleRegistry(project / "tools").build_index()
    first_target = project / "first.txt"
    second_target = project / "second.txt"
    first_target.write_text("old")
    second_target.write_text("old")

    def respond(messages, info):
        called = any(
            getattr(part, "tool_name", None) == "write"
            for message in messages
            for part in message.parts
        )
        if not called:
            return ModelResponse(parts=[
                ToolCallPart(
                    "write",
                    {"path": "first.txt", "content": "new", "justification": "Update one."},
                    tool_call_id="write-1",
                ),
                ToolCallPart(
                    "write",
                    {"path": "second.txt", "content": "new", "justification": "Update two."},
                    tool_call_id="write-2",
                ),
            ])
        return ModelResponse(parts=[TextPart("both done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    initial = await kernel.run(RunRequest(prompt="Update both", workspace=project))
    assert initial.status is RunStatus.APPROVAL_PENDING
    pending = kernel.list_approvals()
    assert len(pending) == 2

    completed = await kernel.resolve_approval_batch(
        [item.approval_id for item in pending], True
    )
    assert completed.status is RunStatus.SUCCESS
    assert completed.output == "both done"
    assert first_target.read_text() == "new"
    assert second_target.read_text() == "new"


def test_disabled_tools_sidecar(project: Path) -> None:
    sidecar = project / "content-agents" / "agents" / "main.tools-disabled.json"
    sidecar.write_text(json.dumps({"tools": ["missing"]}), encoding="utf-8")
    ModuleRegistry(project / "tools").build_index()
    # Unknown exclusions are rejected instead of silently weakening configuration checks.
    with pytest.raises(ConfigurationError, match="unknown tools"):
        Kernel(project)._build_agent(
            "main", Kernel(project).config.agents(),
            ProviderFactory(Kernel(project).config.providers()),
            Kernel(project).config.agents()["main"].budgets
            or __import__("agentic_kernel.models", fromlist=["BudgetConfig"]).BudgetConfig(),
            1, {},
        )
