from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from agentic_kernel.kernel import Kernel, _runtime_context_instruction
from agentic_kernel.models import RunRequest, RunStatus, SecurityMode
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.providers import ProviderFactory


def test_runtime_context_contains_timestamp_timezone_and_workspace(tmp_path: Path) -> None:
    now = datetime(2026, 7, 23, 14, 5, 6, tzinfo=UTC)
    instruction = _runtime_context_instruction(tmp_path, SecurityMode.LIMITED, now)
    assert "2026-07-23T14:05:06+00:00" in instruction
    assert "Timezone: UTC" in instruction
    assert f"Workspace/CWD: {tmp_path.resolve()}" in instruction
    assert "Security mode: limited" in instruction


async def test_kernel_run_writes_complete_session(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()
    monkeypatch.setattr(
        ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[])
    )
    kernel = Kernel(project)
    result = await kernel.run(RunRequest(prompt="Say hello"))
    assert result.status == RunStatus.SUCCESS
    events = kernel.events.read(result.session_id)
    assert events[0].type == "session.started"
    assert events[-1].type == "session.completed"
    assert any(event.type == "messages.snapshot" for event in events)


async def test_session_reuses_complete_message_history(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()
    observed_prompts: list[list[str]] = []

    def respond(messages, _info):
        prompts = [
            str(part.content)
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", None) == "user-prompt"
        ]
        observed_prompts.append(prompts)
        return ModelResponse(parts=[TextPart(f"answer {len(prompts)}")])

    monkeypatch.setattr(
        ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond)
    )
    kernel = Kernel(project)
    first = await kernel.run(RunRequest(prompt="first turn"))
    second = await kernel.run(
        RunRequest(prompt="second turn", session_id=first.session_id)
    )

    assert second.session_id == first.session_id
    assert observed_prompts[-1] == ["first turn", "second turn"]
    events = kernel.events.read(first.session_id)
    assert sum(event.type == "session.started" for event in events) == 2
    assert sum(event.type == "messages.snapshot" for event in events) == 2


async def test_runtime_skill_is_injected(project: Path, monkeypatch) -> None:
    skill_dir = project / "content-agents" / "skills" / "answer-style"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: answer-style
description: Controls answer style.
---
Answer with concise prose.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    model = TestModel(call_tools=[], custom_output_text="done")
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: model)
    result = await Kernel(project).run(
        RunRequest(prompt="Answer", skills=["answer-style"])
    )
    assert result.status == RunStatus.SUCCESS
    assert model.last_model_request_parameters is not None


async def test_supervisor_delegates_with_isolated_child_run(project: Path, monkeypatch) -> None:
    agents = project / "content-agents" / "agents"
    (agents / "main.md").write_text(
        """---
id: main
description: Supervisor
provider: test
modules: []
delegates: [child]
---
Delegate the request.
""",
        encoding="utf-8",
    )
    (agents / "child.md").write_text(
        """---
id: child
description: Child worker
provider: test
modules: []
delegates: []
---
Complete the delegated task.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()

    def model_response(messages, info):
        tool_names = {tool.name for tool in info.function_tools}
        already_delegated = any(
            getattr(part, "tool_name", None) == "delegate_task"
            for message in messages
            for part in message.parts
        )
        if "delegate_task" in tool_names and not already_delegated:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "delegate_task", {"agent_name": "child", "task": "Do the work"}
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("completed")])

    monkeypatch.setattr(
        ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(model_response)
    )
    kernel = Kernel(project)
    result = await kernel.run(RunRequest(prompt="Delegate this task"))
    assert result.status == RunStatus.SUCCESS
    child_events = [
        event for event in kernel.events.read(result.session_id) if event.agent_id == "child"
    ]
    assert [event.type for event in child_events] == ["agent.started", "agent.completed"]
    assert child_events[0].parent_run_id is not None
