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
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from agentic_kernel.errors import ConfigurationError
from agentic_kernel.kernel import (
    Kernel,
    _last_model_text,
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


def test_the_runtime_clock_states_that_it_needs_no_confirmation(tmp_path: Path) -> None:
    """Sans cette phrase, l'agent revérifiait l'heure qu'il avait déjà.

    `system.md` demande de vérifier plutôt que d'affirmer. Le modèle appliquait
    la règle à l'horodatage du contexte : une valeur inscrite dans le prompt est
    une affirmation, un appel d'outil est une vérification. Sur les journaux de
    production, 450 runs sur 710 appelaient l'horloge, pour 454 appels — le
    premier outil de l'agent, loin devant lire et écrire.

    Le bloc étant réécrit à chaque requête, la valeur est toujours juste : il
    manquait seulement de le dire.
    """
    instruction = _runtime_context_instruction(tmp_path, SecurityMode.LIMITED)

    assert "authoritative" in instruction
    assert "do not call a clock tool" in instruction


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


async def test_an_attached_skill_is_announced_rather_than_recopied(
    project: Path, monkeypatch
) -> None:
    """Le corps d'une skill rattachée n'entre plus dans le prompt par défaut.

    Onze skills rattachées à l'orchestrateur pesaient près de 10 000 tokens
    recopiés à chaque requête, soit plusieurs secondes de traitement du prompt
    avant que le modèle écrive quoi que ce soit — y compris pour répondre
    « ça va ? ». L'index les annonce, `load_skill` apporte le corps à l'usage.

    Celles qui se déclarent `load: always` restent inscrites : elles
    conditionnent le comportement avant que le modèle sache qu'il en a besoin.
    """
    skills_root = project / "content-agents" / "skills"
    (skills_root / "on-occasion").mkdir(parents=True)
    (skills_root / "on-occasion" / "SKILL.md").write_text(
        """---
name: on-occasion
description: Explains how to answer a rare question.
---
Body of the occasional skill.
""",
        encoding="utf-8",
    )
    (skills_root / "at-all-times").mkdir(parents=True)
    (skills_root / "at-all-times" / "SKILL.md").write_text(
        """---
name: at-all-times
description: Conditions every answer.
load: always
---
Body of the permanent skill.
""",
        encoding="utf-8",
    )
    agent_path = project / "content-agents" / "agents" / "main.md"
    agent_path.write_text(
        agent_path.read_text(encoding="utf-8").replace(
            "provider: test\n",
            "provider: test\nskills:\n  - on-occasion\n  - at-all-times\n",
        ),
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    observed_instructions: list[str] = []

    def respond(_messages, info):
        observed_instructions.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))

    result = await Kernel(project).run(RunRequest(prompt="Bonjour"))

    assert result.status == RunStatus.SUCCESS
    rendered = "\n".join(observed_instructions)
    assert "Body of the permanent skill." in rendered
    assert "Body of the occasional skill." not in rendered
    # Annoncée, et signalée comme faisant partie du répertoire de l'agent :
    # sans cela le modèle n'a aucune raison d'aller la chercher.
    assert "- on-occasion [attached to you]: Explains how to answer a rare question." in rendered


async def test_enabled_user_memory_and_implicit_skill_are_injected(
    project: Path, monkeypatch
) -> None:
    agent_path = project / "content-agents" / "agents" / "main.md"
    agent_path.write_text(
        agent_path.read_text(encoding="utf-8").replace(
            "provider: test\n",
            "provider: test\nuser_memory: true\n",
        ),
        encoding="utf-8",
    )
    skill_dir = project / "content-agents" / "skills" / "user-memory"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: user-memory
description: Maintain durable user memory.
---
Update confirmed durable facts carefully.
""",
        encoding="utf-8",
    )
    workspace = project / "content-agents" / "workspaces" / "main"
    workspace.mkdir(parents=True)
    (workspace / "USER.md").write_text(
        "# User profile\n\n- Name: Camille\n",
        encoding="utf-8",
    )
    (workspace / "DECISIONS.md").write_text(
        "# Decisions\n\n- Prefer concise answers.\n",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    observed_instructions: list[str] = []

    def respond(_messages, info):
        observed_instructions.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(ProviderFactory, "build", lambda *args, **kwargs: FunctionModel(respond))

    result = await Kernel(project).run(RunRequest(prompt="Bonjour"))

    assert result.status == RunStatus.SUCCESS
    rendered = "\n".join(observed_instructions)
    assert "Camille" in rendered
    assert "Prefer concise answers" in rendered
    assert "Update confirmed durable facts carefully." in rendered


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
subagent: true
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


def test_thinking_is_recovered_when_no_text_was_produced() -> None:
    """Le cas qui a coûté un run entier.

    Quand la limite de tokens tombe pendant la phase de raisonnement, la réponse
    ne contient que des `ThinkingPart`; pydantic-ai lève alors sans sortie
    exploitable. Ce raisonnement représente le travail réel du modèle et doit
    survivre à l'échec.
    """
    messages = [
        ModelRequest(parts=[UserPromptPart(content="Crée l'application")]),
        ModelResponse(parts=[ThinkingPart(content="Je décompose le problème…")]),
    ]

    texte, reflexion = _last_model_text(messages)

    assert texte == ""
    assert reflexion == "Je décompose le problème…"


def test_text_wins_over_thinking() -> None:
    """Un texte est une réponse; un raisonnement n'en est que la trace."""
    messages = [
        ModelResponse(parts=[ThinkingPart(content="hésitation"), TextPart("la réponse")]),
    ]

    texte, reflexion = _last_model_text(messages)

    assert texte == "la réponse"
    assert reflexion == "hésitation"


def test_the_last_response_is_the_one_that_failed() -> None:
    """On lit à rebours : c'est la dernière réponse qui a buté."""
    messages = [
        ModelResponse(parts=[TextPart("un tour précédent")]),
        ModelRequest(parts=[UserPromptPart(content="continue")]),
        ModelResponse(parts=[ThinkingPart(content="le tour interrompu")]),
    ]

    assert _last_model_text(messages) == ("", "le tour interrompu")


def test_nothing_to_recover_is_not_an_error() -> None:
    assert _last_model_text([]) == ("", "")
    assert _last_model_text([ModelResponse(parts=[])]) == ("", "")


async def test_the_run_model_overrides_every_agent_in_the_chain(
    project: Path, monkeypatch
) -> None:
    """Le modèle choisi pour le run vaut pour l'enfant comme pour le parent.

    Le champ `model` d'un agent n'est qu'un défaut. Basculer de modèle en cours
    de délégation impose un rechargement complet — prohibitif sur un modèle
    local — et contredit le choix que l'utilisateur vient de faire.
    """
    agents = project / "content-agents" / "agents"
    (agents / "main.md").write_text(
        """---
id: main
description: Supervisor
provider: test
model: modele-du-parent
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
model: modele-de-l-enfant
modules: []
subagent: true
---
Complete the delegated task.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    demandes: list[str | None] = []

    def build(self, provider_id=None, model=None, **kwargs):
        demandes.append(model)
        return TestModel(call_tools=[])

    monkeypatch.setattr(ProviderFactory, "build", build)
    kernel = Kernel(project)

    await kernel.run(RunRequest(prompt="Delegate this", model="choisi-pour-le-run"))

    assert demandes, "aucun modèle n'a été construit"
    assert set(demandes) == {"choisi-pour-le-run"}, (
        f"un agent a rebasculé sur son modèle déclaré : {demandes}"
    )


async def test_without_an_override_each_agent_keeps_its_declared_model(
    project: Path, monkeypatch
) -> None:
    """Sans choix explicite, le défaut de chaque agent s'applique."""
    agents = project / "content-agents" / "agents"
    (agents / "main.md").write_text(
        """---
id: main
description: Supervisor
provider: test
model: modele-du-parent
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
model: modele-de-l-enfant
modules: []
subagent: true
---
Complete the delegated task.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    demandes: list[str | None] = []

    def build(self, provider_id=None, model=None, **kwargs):
        demandes.append(model)
        return TestModel(call_tools=[])

    monkeypatch.setattr(ProviderFactory, "build", build)
    kernel = Kernel(project)

    await kernel.run(RunRequest(prompt="Delegate this"))

    assert "modele-du-parent" in demandes
    assert "modele-de-l-enfant" in demandes


def test_a_skill_command_carries_its_instruction(project: Path) -> None:
    """Charger la skill ne suffisait pas.

    Quand l'agent précharge déjà `plan-build`, `/plan` n'ajoutait rien : le
    protocole restait une suggestion parmi le contexte permanent, et le modèle
    enchaînait la planification et l'exécution sans laisser de point d'arrêt.
    """
    skill = project / "content-agents" / "skills" / "arret"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        """---
name: arret
description: Planifier sans exécuter.
amk:
  commands:
    - /plan
  command_descriptions:
    /plan: Construire un plan.
  command_prompts:
    /plan: Produis un plan et arrête-toi là.
---
Corps de la skill.
""",
        encoding="utf-8",
    )
    from agentic_kernel.config import ProjectConfig

    commande = ProjectConfig(project).resolve_command("/plan crée l'application")

    assert commande is not None
    assert commande["prompt"] == "Produis un plan et arrête-toi là."
    assert commande["skill"] == "arret"


def test_the_user_request_survives_the_command_prompt() -> None:
    """La consigne prend la place du préfixe; la demande la suit."""
    from agentic_kernel.kernel import _expand_skill_command

    expanded = _expand_skill_command(
        "/plan crée l'application", "/plan", "Produis un plan et arrête-toi là."
    )

    assert expanded.startswith("Produis un plan et arrête-toi là.")
    assert "crée l'application" in expanded
    assert "/plan" not in expanded


def test_a_bare_command_needs_no_request() -> None:
    from agentic_kernel.kernel import _expand_skill_command

    assert _expand_skill_command("/build", "/build", "Exécute le plan.") == "Exécute le plan."


def test_the_runtime_context_tells_the_agent_who_it_is() -> None:
    """Sans identité, le modèle en invente une et l'affirme sans réserve.

    Un agent qui se trompe sur son propre nom discrédite tout ce qu'il énonce
    ensuite : l'utilisateur n'a plus de repère pour distinguer ce qui est mesuré
    de ce qui est comblé.
    """
    from agentic_kernel.kernel import _runtime_context_instruction
    from agentic_kernel.models import SecurityMode

    rendu = _runtime_context_instruction(
        Path("/tmp/espace"),
        SecurityMode.LIMITED,
        agent_id="main",
        agent_description="Agent superviseur principal du kernel.",
    )

    assert "You are the agent `main`" in rendu
    assert "Agent superviseur principal du kernel." in rendu
    assert "never invent one" in rendu


def test_an_agent_without_identity_says_nothing_about_it() -> None:
    """Le contexte reste utilisable là où l'identité n'a pas de sens."""
    from agentic_kernel.kernel import _runtime_context_instruction
    from agentic_kernel.models import SecurityMode

    rendu = _runtime_context_instruction(Path("/tmp/espace"), SecurityMode.LIMITED)

    assert "You are the agent" not in rendu
    assert "Runtime context" in rendu
