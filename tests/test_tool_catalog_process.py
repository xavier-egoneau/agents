from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.guardian import review_tool_call
from agentic_kernel.kernel import Kernel
from agentic_kernel.models import Event, GuardianVerdict, SecurityMode, ToolRisk
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.platform.sandbox import sandbox_capabilities


def test_enriched_index_contains_runtime_schemas() -> None:
    root = Path(__file__).parents[1]
    index = ModuleRegistry(root / "tools").check_index()
    command = next(
        tool
        for module in index.modules
        if module.id == "process"
        for tool in module.tools
        if tool.name == "command_run"
    )
    assert index.schema_version == 2
    assert command.category == "execution"
    assert command.input_schema["type"] == "object"
    assert "program" in command.input_schema["properties"]
    assert command.cancellable is True


def test_command_guardian_modes(tmp_path: Path) -> None:
    arguments = {
        "program": "npm",
        "args": ["run", "dev"],
        "cwd": ".",
        "justification": "Launch the requested development server.",
    }
    safe = review_tool_call(
        tool_name="process_start",
        tool_call_id="1",
        agent_id="main",
        arguments=arguments,
        risks=[ToolRisk.EXECUTE],
        mode=SecurityMode.SAFE,
        workspace=tmp_path,
    )
    limited = review_tool_call(
        tool_name="process_start",
        tool_call_id="2",
        agent_id="main",
        arguments=arguments,
        risks=[ToolRisk.EXECUTE],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )
    install = review_tool_call(
        tool_name="command_run",
        tool_call_id="3",
        agent_id="main",
        arguments={**arguments, "args": ["install"]},
        risks=[ToolRisk.EXECUTE],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )
    assert safe.verdict is GuardianVerdict.ASK
    expected = (
        GuardianVerdict.ALLOW
        if sandbox_capabilities().execution_isolated
        else GuardianVerdict.ASK
    )
    assert limited.verdict is expected
    assert install.verdict is GuardianVerdict.ASK


async def test_command_and_persistent_process_lifecycle(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "tools/modules/process/module.py"
    spec = importlib.util.spec_from_file_location("test_process_module", source)
    assert spec and spec.loader
    process_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(process_module)

    session_id = uuid4()
    content = tmp_path / "content-agents"
    events = JsonlEventStore(content / "sessions")
    deps = SimpleNamespace(
        workspace=tmp_path,
        state_db=content / "state.db",
        events=events,
        session_id=session_id,
        security_mode=SecurityMode.POWER,
    )
    ctx = SimpleNamespace(deps=deps)
    short = await process_module.command_run(
        ctx, sys.executable, ["-c", "print('ready')"], timeout_seconds=5
    )
    assert short["ok"] is True
    assert short["data"]["stdout"].splitlines() == ["ready"]
    timed_out = await process_module.command_run(
        ctx, sys.executable, ["-c", "import time; time.sleep(2)"], timeout_seconds=0.1
    )
    assert timed_out["ok"] is False
    assert timed_out["error"]["type"] == "timeout"

    started = await process_module.process_start(
        ctx,
        sys.executable,
        ["-u", "-c", "import time; print('server:8123'); time.sleep(30)"],
    )
    process_id = started["data"]["process_id"]
    status = await process_module.process_status(ctx, process_id)
    assert status["data"]["running"] is True
    output = await process_module.process_output(ctx, process_id)
    assert "server:8123" in output["data"]["output"]
    stopped = await process_module.process_stop(ctx, process_id, grace_seconds=1)
    assert stopped["ok"] is True
    assert stopped["data"]["running"] is False

    if not shutil.which("npm") or not shutil.which("node"):
        pytest.skip("Node.js toolchain is unavailable")
    if sandbox_capabilities().backend == "codex-windows-sandbox":
        pytest.skip("Node cannot canonicalize pytest's restricted nested temp workspace")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "amk-vite-lifecycle",
                "private": True,
                "scripts": {"dev": "node server.js"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "require('http').createServer((q,r)=>r.end('vite-ready'))"
        ".listen(8137,'127.0.0.1',()=>console.log('http://127.0.0.1:8137'))",
        encoding="utf-8",
    )
    installed = await process_module.command_run(
        ctx, "npm", ["install", "--offline", "--ignore-scripts"], timeout_seconds=30
    )
    assert installed["data"]["exit_code"] == 0
    server = await process_module.process_start(ctx, "npm", ["run", "dev"], network=True)
    server_id = server["data"]["process_id"]
    for _ in range(30):
        current = await process_module.process_status(ctx, server_id)
        if 8137 in current["data"]["ports"]:
            break
        await asyncio.sleep(0.1)
    server_output = await process_module.process_output(ctx, server_id)
    assert 8137 in current["data"]["ports"], server_output["data"]["output"]
    async with httpx.AsyncClient() as client:
        response = await client.get("http://127.0.0.1:8137")
    assert response.text == "vite-ready"
    await process_module.process_stop(ctx, server_id, grace_seconds=1)


def test_long_history_is_loaded_intact_for_safe_request_time_compaction(
    project: Path,
) -> None:
    kernel = Kernel(project)
    session_id, run_id = uuid4(), uuid4()
    history = []
    for index in range(40):
        history.extend(
            [
                ModelRequest(parts=[UserPromptPart(content=f"request-{index}-" + "x" * 5000)]),
                ModelResponse(parts=[TextPart(content=f"response-{index}-" + "y" * 5000)]),
            ]
        )
    payload = ModelMessagesTypeAdapter.dump_python(history, mode="json")
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="messages.snapshot",
            payload={"messages": payload},
        )
    )
    loaded = kernel._latest_message_history(
        session_id,
        uuid4(),
        "main",
        context_window_tokens=100_000,
    )
    assert loaded is not None
    assert len(loaded) == len(history)
    assert "response-39" in str(loaded[-1])
    snapshots = [
        event for event in kernel.events.read(session_id) if event.type == "messages.snapshot"
    ]
    assert len(kernel.snapshots.load(session_id, snapshots[0].payload)) == len(history)
    assert not any(event.type == "context.compacted" for event in kernel.events.read(session_id))


def test_failed_turn_prompt_is_recovered_for_continue(project: Path) -> None:
    kernel = Kernel(project)
    session_id, failed_run, current_run = uuid4(), uuid4(), uuid4()
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=failed_run,
            agent_id="main",
            type="session.started",
            payload={"prompt": "Build the requested project."},
        )
    )
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=failed_run,
            agent_id="main",
            type="session.completed",
            payload={"status": "failed"},
        )
    )
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=current_run,
            agent_id="main",
            type="session.started",
            payload={"prompt": "Continue."},
        )
    )
    history = kernel._latest_message_history(session_id, current_run, "main")
    assert history is not None
    assert "Build the requested project." in str(history)
    assert "Continue." not in str(history)


async def test_model_context_reference_is_persisted(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "tools/modules/model_info/module.py"
    spec = importlib.util.spec_from_file_location("test_model_info_module", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deps = SimpleNamespace(
        provider_id="test-provider",
        model_name="test-model",
        context_window_tokens=None,
        events=JsonlEventStore(tmp_path / "content-agents/sessions"),
    )
    ctx = SimpleNamespace(deps=deps)
    unknown = await module.model_context_status(ctx)
    assert unknown["data"]["known"] is False
    stored = await module.model_context_store(ctx, 128_000, source="user-confirmed")
    assert stored["data"]["context_window_tokens"] == 128_000
    assert deps.context_window_tokens == 128_000
    known = await module.model_context_status(ctx)
    assert known["data"]["known"] is True
    assert known["data"]["context_window_tokens"] == 128_000
