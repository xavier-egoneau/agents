import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from agentic_kernel.errors import ConfigurationError
from agentic_kernel.kernel import (
    Kernel,
    _runtime_context_instruction,
    _without_images,
)
from agentic_kernel.models import (
    ApprovalRequest,
    Event,
    ImageAttachment,
    RunRequest,
    RunStatus,
    SecurityMode,
)
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.providers import ProviderFactory
from agentic_kernel.vision import VisionUnavailable


def test_runtime_context_contains_timestamp_timezone_and_workspace(tmp_path: Path) -> None:
    now = datetime(2026, 7, 23, 14, 5, 6, tzinfo=UTC)
    instruction = _runtime_context_instruction(tmp_path, SecurityMode.LIMITED, now)
    assert "2026-07-23T14:05:06+00:00" in instruction
    assert "Timezone: UTC" in instruction
    assert f"Workspace/CWD: {tmp_path.resolve()}" in instruction
    assert "Security mode: limited" in instruction


async def test_kernel_run_writes_complete_session(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[]))
    kernel = Kernel(project)
    result = await kernel.run(RunRequest(prompt="Say hello"))
    assert result.status == RunStatus.SUCCESS
    events = kernel.events.read(result.session_id)
    assert events[0].type == "session.started"
    assert events[-1].type == "session.completed"
    assert any(event.type == "messages.snapshot" for event in events)


async def test_null_workspace_uses_the_agents_personal_directory(
    project: Path, monkeypatch
) -> None:
    ModuleRegistry(project / "tools").build_index()
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[]))
    kernel = Kernel(project)

    result = await kernel.run(RunRequest(prompt="Use my personal workspace"))

    started = kernel.events.read(result.session_id)[0]
    expected = project / "content-agents" / "workspaces" / "main"
    assert expected.is_dir()
    assert started.payload["workspace"] is None
    assert started.payload["effective_workspace"] == str(expected.resolve())
    assert started.payload["workspace_kind"] == "agent_default"


def test_kernel_supersedes_only_a_pending_cron_test_batch(project: Path) -> None:
    kernel = Kernel(project)
    cron_job_id = "cron-workflow"
    request = RunRequest(
        prompt="Prévalider la routine",
        session_id=uuid4(),
        workspace=project,
        trigger="cron_test",
        cron_job_id=cron_job_id,
    )
    run_id = uuid4()
    approvals = [
        ApprovalRequest(
            session_id=request.session_id,
            run_id=run_id,
            agent_id="main",
            tool_call_id=f"call-{index}",
            tool_name="web_search",
            action_family="network",
            justification="Prévalider le workflow.",
            reason="Network approval required",
        )
        for index in range(2)
    ]
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    for approval in approvals:
        kernel.approvals.save_state(approval, state)

    waiting = kernel.approval_service.resolve(approvals[0].approval_id, True)
    assert waiting.status is RunStatus.APPROVAL_PENDING
    batch = kernel.inspect_pending_cron_test(request.session_id, cron_job_id)
    assert batch is not None
    assert len(batch.approvals) == 2

    result = kernel.supersede_pending_cron_test(batch, workflow_revision=1)

    assert result.status is RunStatus.CANCELLED
    assert kernel.list_approvals() == []
    events = kernel.events.read(request.session_id)
    assert [event.type for event in events].count("approval.superseded") == 2
    assert events[-2].type == "run.transitioned"
    assert events[-2].payload["state"] == "cancelled"
    assert events[-1].type == "session.completed"
    assert events[-1].payload["status"] == "cancelled"

    scheduled_request = RunRequest(
        prompt="Occurrence réelle",
        session_id=uuid4(),
        workspace=project,
        trigger="cron",
        cron_job_id=cron_job_id,
        cron_occurrence_id="occurrence-1",
    )
    scheduled = ApprovalRequest(
        session_id=scheduled_request.session_id,
        run_id=uuid4(),
        agent_id="main",
        tool_call_id="call-scheduled",
        tool_name="web_search",
        action_family="network",
        justification="Exécuter l'occurrence.",
        reason="Network approval required",
    )
    kernel.approvals.save_state(
        scheduled,
        {"request": scheduled_request.model_dump(mode="json"), "messages": "[]"},
    )

    with pytest.raises(ConfigurationError, match="cron_test prevalidation"):
        kernel.inspect_pending_cron_test(scheduled_request.session_id, cron_job_id)
    assert kernel.approvals.load_state(scheduled.approval_id) is not None


async def test_context_commands_are_deterministic_kernel_operations(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    kernel = Kernel(project)
    configured = await kernel.run(RunRequest(prompt="/model-context 128k"))
    assert configured.status == RunStatus.SUCCESS
    assert "128,000 tokens" in configured.output
    inspected = await kernel.run(RunRequest(prompt="/context", session_id=configured.session_id))
    assert inspected.status == RunStatus.SUCCESS
    assert "Fenêtre connue : **128,000 tokens**" in inspected.output
    events = kernel.events.read(configured.session_id)
    assert any(event.type == "context.window_updated" for event in events)
    assert any(event.type == "context.inspected" for event in events)


async def test_invalid_model_context_is_a_persisted_validation_failure(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    kernel = Kernel(project)

    result = await kernel.run(RunRequest(prompt="/model-context beaucoup"))

    assert result.status == RunStatus.FAILED
    assert result.errors[0].type == "validation"
    events = kernel.events.read(result.session_id)
    assert [event.type for event in events] == [
        "session.started",
        "context.window_update_failed",
        "session.completed",
    ]
    assert events[-1].payload["status"] == "failed"


async def test_secret_commands_never_reach_model_or_session_log(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()

    def model_must_not_be_built(*args, **kwargs):
        raise AssertionError("secret command reached the provider")

    monkeypatch.setattr(ProviderFactory, "build", model_must_not_be_built)
    kernel = Kernel(project)
    value = "very-private-value"
    stored = await kernel.run(RunRequest(prompt=f"/secret DATABASE_PASSWORD {value}"))

    assert stored.status == RunStatus.SUCCESS
    assert value not in (stored.output or "")
    assert kernel.events.read(stored.session_id) == []
    secrets_path = project / "content-agents" / "secrets.json"
    assert json.loads(secrets_path.read_text()) == {"DATABASE_PASSWORD": value}
    if os.name != "nt":
        assert secrets_path.stat().st_mode & 0o777 == 0o600

    listed = await kernel.run(RunRequest(prompt="/secret_list", session_id=stored.session_id))
    assert listed.status == RunStatus.SUCCESS
    assert "DATABASE_PASSWORD" in (listed.output or "")
    assert value not in (listed.output or "")
    assert kernel.events.read(stored.session_id) == []
    catalog = kernel._secret_catalog_instruction()
    assert "DATABASE_PASSWORD" in catalog
    assert value not in catalog

    hyphenated = await kernel.run(RunRequest(prompt="/secret nom-de-la-variable autre-valeur"))
    assert hyphenated.status == RunStatus.SUCCESS


async def test_non_vision_provider_uses_local_vision_transparently(
    project: Path, monkeypatch
) -> None:
    ModuleRegistry(project / "tools").build_index()
    observed: list[tuple[str, str]] = []
    kernel = Kernel(project)

    async def inspect(data: bytes, media_type: str, question: str, detail: str):
        observed.append((media_type, question))
        return "A settings screen with one visible validation error."

    monkeypatch.setattr(kernel.vision, "analyze_bytes", inspect)
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[]))
    result = await kernel.run(
        RunRequest(
            prompt="Inspect this image",
            images=[
                ImageAttachment(
                    name="screen.png",
                    media_type="image/png",
                    data_base64="eA==",
                )
            ],
        )
    )

    assert result.status == RunStatus.SUCCESS
    assert observed and observed[0][0] == "image/png"
    events = kernel.events.read(result.session_id)
    assert any(
        event.type == "tool.completed" and event.payload.get("tool_name") == "image_inspect"
        for event in events
    )
    assert any(event.type == "artifact.created" for event in events)
    snapshot = next(event for event in events if event.type == "messages.snapshot")
    serialized = json.dumps(kernel.snapshots.load(result.session_id, snapshot.payload))
    assert "A settings screen" in serialized
    assert "image_url" not in serialized


async def test_vision_provider_archives_input_image_for_session_reload(
    project: Path, monkeypatch
) -> None:
    ModuleRegistry(project / "tools").build_index()
    providers_path = project / "content-agents" / "providers.json"
    providers = json.loads(providers_path.read_text(encoding="utf-8"))
    providers["providers"][0]["vision"] = True
    providers_path.write_text(json.dumps(providers), encoding="utf-8")
    kernel = Kernel(project)

    async def should_not_run(*args, **kwargs):
        raise AssertionError("local vision must not run for a vision provider")

    monkeypatch.setattr(kernel.vision, "analyze_bytes", should_not_run)
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[]))
    result = await kernel.run(
        RunRequest(
            prompt="Inspect this image",
            images=[
                ImageAttachment(
                    name="screen.png",
                    media_type="image/png",
                    data_base64="eA==",
                )
            ],
        )
    )

    assert result.status == RunStatus.SUCCESS
    messages = kernel.events.projection.messages(result.session_id)
    user = next(message for message in messages if message["role"] == "user")
    assert user["artifacts"][0]["kind"] == "input_image"
    assert user["artifacts"][0]["name"] == "screen.png"


async def test_missing_local_vision_degrades_without_failing_the_run(
    project: Path, monkeypatch
) -> None:
    ModuleRegistry(project / "tools").build_index()
    kernel = Kernel(project)

    async def unavailable(*args, **kwargs):
        raise VisionUnavailable("llama-server absent")

    monkeypatch.setattr(kernel.vision, "analyze_bytes", unavailable)
    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: TestModel(call_tools=[]))
    result = await kernel.run(
        RunRequest(
            prompt="Use this design",
            images=[
                ImageAttachment(
                    name="screen.png",
                    media_type="image/png",
                    data_base64="eA==",
                )
            ],
        )
    )

    assert result.status == RunStatus.SUCCESS
    events = kernel.events.read(result.session_id)
    failure = next(event for event in events if event.type == "tool.failed")
    assert failure.payload["tool_name"] == "image_inspect"
    snapshot = next(event for event in events if event.type == "messages.snapshot")
    serialized = json.dumps(
        kernel.snapshots.load(result.session_id, snapshot.payload), ensure_ascii=False
    )
    assert "Analyse visuelle indisponible" in serialized
    assert "Ne déduis aucun détail visuel" in serialized


def test_image_history_is_sanitized_for_text_only_models() -> None:
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[
                        "What is wrong?",
                        BinaryContent(data=b"image", media_type="image/png"),
                    ]
                )
            ]
        )
    ]

    sanitized = _without_images(history)
    content = sanitized[0].parts[0].content

    assert content[0] == "What is wrong?"
    assert isinstance(content[1], str)
    assert "modèle actif" in content[1]


async def test_compact_command_forces_manual_compaction(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()
    monkeypatch.setattr(
        ProviderFactory,
        "build",
        lambda *args, **kwargs: TestModel(custom_output_text="incorrect model reply"),
    )
    kernel = Kernel(project)
    first = await kernel.run(RunRequest(prompt="Keep this decision: alpha."))
    compacted = await kernel.run(RunRequest(prompt="/compact", session_id=first.session_id))
    assert compacted.status == RunStatus.SUCCESS
    assert compacted.output.startswith("Compaction manuelle terminée :")
    assert "incorrect model reply" not in compacted.output
    events = kernel.events.read(first.session_id)
    compact_event = next(event for event in events if event.type == "context.compacted")
    assert compact_event.payload["manual"] is True
    assert any(event.type == "context.pre_compaction_snapshot" for event in events)
    snapshot_event = next(event for event in reversed(events) if event.type == "messages.snapshot")
    snapshot = kernel.snapshots.load(first.session_id, snapshot_event.payload)
    assert any(
        part.get("part_kind") == "text"
        and str(part.get("content", "")).startswith("Compaction manuelle terminée :")
        for message in snapshot
        for part in message.get("parts", [])
    )


async def test_compact_command_reduces_a_long_history(project: Path, monkeypatch) -> None:
    ModuleRegistry(project / "tools").build_index()

    def respond(_messages, _info):
        return ModelResponse(parts=[TextPart("Résumé conservé.")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    session_id = None
    for index in range(12):
        result = await kernel.run(
            RunRequest(
                prompt=f"Décision {index}: " + (chr(65 + index) * 6_000),
                session_id=session_id or uuid4(),
            )
        )
        session_id = result.session_id

    compacted = await kernel.run(RunRequest(prompt="/compact", session_id=session_id))

    event = next(
        item
        for item in reversed(kernel.events.read(session_id))
        if item.type == "context.compacted"
    )
    assert event.payload["estimated_tokens_after"] < event.payload["estimated_tokens_before"]
    assert "contexte actif réduit" in compacted.output


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

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    first = await kernel.run(RunRequest(prompt="first turn"))
    second = await kernel.run(RunRequest(prompt="second turn", session_id=first.session_id))

    assert second.session_id == first.session_id
    assert observed_prompts[-1] == ["first turn", "second turn"]
    events = kernel.events.read(first.session_id)
    assert sum(event.type == "session.started" for event in events) == 2
    assert sum(event.type == "messages.snapshot" for event in events) == 2


async def test_routine_notification_is_visible_to_an_immediate_user_reply(
    project: Path, monkeypatch
) -> None:
    ModuleRegistry(project / "tools").build_index()
    observed_text: list[list[str]] = []

    def respond(messages, _info):
        observed_text.append(
            [
                str(part.content)
                for message in messages
                for part in message.parts
                if getattr(part, "part_kind", None) in {"user-prompt", "text"}
            ]
        )
        return ModelResponse(parts=[TextPart("ok")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    session_id = uuid4()
    notification = "Veille du matin\n\nVoici la veille actualis" + chr(233) + "e."
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="routine.notification",
            payload={
                "cron_job_id": "cron_morning",
                "status": "success",
                "content": notification,
            },
        )
    )

    await kernel.run(RunRequest(prompt="Relivre-la pour mon test", session_id=session_id))

    assert observed_text[-1] == [
        notification,
        "Relivre-la pour mon test",
    ]


async def test_runtime_skill_is_injected(project: Path, monkeypatch) -> None:
    skill_dir = project / "content-agents" / "skills" / "answer-style"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: answer-style
description: Controls answer style.
allowed-tools: [read]
---
Answer with concise prose.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    observed_instructions: list[str] = []

    def respond(_messages, info):
        observed_instructions.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    result = await Kernel(project).run(RunRequest(prompt="Answer", skills=["answer-style"]))
    assert result.status == RunStatus.SUCCESS
    rendered = "\n".join(observed_instructions)
    assert "Answer with concise prose." in rendered
    assert f"Skill root: {skill_dir}" in rendered
    assert "Requested tools: read." in rendered


async def test_workflow_is_injected_after_skills_and_enforces_exact_tool_allowlist(
    project: Path, monkeypatch
) -> None:
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
    module = project / "tools" / "modules" / "choices"
    module.mkdir()
    (module / "module.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "choices",
                "name": "Choices",
                "description": "Allowlist test tools",
                "version": "1.0.0",
                "entrypoint": "module.py:module",
                "capabilities": ["tools"],
                "enabled": True,
                "tools": [
                        {
                            "name": "allow_a",
                            "description": "First tool",
                            "category": "test",
                            "risk_tags": ["read"],
                            "timeout_seconds": 5,
                            "input_schema": {"type": "object", "properties": {}},
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    "ok": {"type": "boolean"},
                                    "data": {},
                                    "error": {},
                                    "metadata": {"type": "object"},
                                },
                            },
                        },
                        {
                            "name": "allow_b",
                            "description": "Second tool",
                            "category": "test",
                            "risk_tags": ["read"],
                            "timeout_seconds": 5,
                            "input_schema": {"type": "object", "properties": {}},
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    "ok": {"type": "boolean"},
                                    "data": {},
                                    "error": {},
                                    "metadata": {"type": "object"},
                                },
                            },
                        },
                ],
            }
        ),
        encoding="utf-8",
    )
    (module / "module.py").write_text(
        """from pydantic_ai import FunctionToolset

def allow_a() -> str: return "a"
def allow_b() -> str: return "b"

class Module:
    def toolsets(self): return [FunctionToolset(tools=[allow_a, allow_b])]
    def instructions(self): return []
    def capabilities(self): return []
module = Module()
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    observations: list[tuple[set[str], str]] = []

    def respond(_messages, info):
        observations.append(
            ({tool.name for tool in info.function_tools}, info.instructions or "")
        )
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))
    kernel = Kernel(project)
    unrestricted = await kernel.run(RunRequest(prompt="Free"))
    assert unrestricted.status == RunStatus.SUCCESS
    assert {"allow_a", "allow_b"} <= observations[-1][0]

    workflow = {
        "schema": "amk.workflow/v1",
        "id": "routine-test-v1",
        "title": "Routine test",
        "status": "ready",
        "execution": {
            "mode": "agent_guided",
            "deviation": "stop_and_report",
            "timezone": "Europe/Paris",
        },
        "permissions": {
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "allow_a"}],
        },
        "missing_dependencies": [],
        "steps": [
            {"id": "one", "kind": "tool", "tool": "allow_a", "args": {}},
            {
                "id": "summary",
                "kind": "synthesize",
                "needs": ["one"],
                "instructions": "Summarize the result.",
            },
        ],
        "output": {"sections": ["Result"]},
    }
    guided = await kernel.run(
        RunRequest(
            prompt="Guided",
            skills=["answer-style"],
            workflow=workflow,
            tool_allowlist=["allow_a"],
        )
    )
    assert guided.status == RunStatus.SUCCESS
    tools, instructions = observations[-1]
    assert tools == {"allow_a"}
    assert instructions.index("Answer with concise prose.") < instructions.index(
        "Workflow de routine accepté"
    )

    synthesis_only = await kernel.run(
        RunRequest(
            prompt="No tools",
            workflow={
                **workflow,
                "permissions": {
                    "authority": "kernel_guardian",
                    "unlisted": "stop_and_report",
                    "declarations": [],
                },
                "steps": [
                    {
                        "id": "summary",
                        "kind": "synthesize",
                        "instructions": "Summarize without tools.",
                    }
                ],
            },
            tool_allowlist=[],
        )
    )
    assert synthesis_only.status == RunStatus.SUCCESS
    assert observations[-1][0] == set()


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
    observed_toolsets: list[set[str]] = []

    def model_response(messages, info):
        tool_names = {tool.name for tool in info.function_tools}
        observed_toolsets.append(tool_names)
        already_delegated = any(
            getattr(part, "tool_name", None) == "agent_delegate"
            for message in messages
            for part in message.parts
        )
        if "agent_delegate" in tool_names and not already_delegated:
            return ModelResponse(
                parts=[
                    ToolCallPart("agent_delegate", {"agent_name": "child", "task": "Do the work"})
                ]
            )
        return ModelResponse(parts=[TextPart("completed")])

    monkeypatch.setattr(
        ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(model_response)
    )
    kernel = Kernel(project)
    result = await kernel.run(
        RunRequest(prompt="Delegate this task", tool_allowlist=["agent_delegate"])
    )
    assert result.status == RunStatus.SUCCESS
    child_events = [
        event for event in kernel.events.read(result.session_id) if event.agent_id == "child"
    ]
    assert [event.type for event in child_events] == [
        "agent.queued",
        "agent.started",
        "agent.completed",
    ]
    assert child_events[0].parent_run_id is not None
    assert observed_toolsets[0] == {"agent_delegate"}
    assert set() in observed_toolsets[1:]
