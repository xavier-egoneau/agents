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
from agentic_kernel.platform.sandbox import _container_command, sandbox_capabilities


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
        GuardianVerdict.ALLOW if sandbox_capabilities().execution_isolated else GuardianVerdict.ASK
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
    failed = await process_module.command_run(
        ctx, sys.executable, ["-c", "raise SystemExit(7)"], timeout_seconds=5
    )
    assert failed["ok"] is False
    assert failed["data"]["exit_code"] == 7
    assert failed["error"]["type"] == "nonzero_exit"

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
        ".listen(8137,'0.0.0.0',()=>console.log('http://127.0.0.1:8137'))",
        encoding="utf-8",
    )
    installed = await process_module.command_run(
        ctx, "npm", ["install", "--offline", "--ignore-scripts"], timeout_seconds=30
    )
    assert installed["data"]["exit_code"] == 0
    server = await process_module.process_start(ctx, "npm", ["run", "dev"], network=True, port=8137)
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


def _process_module():
    source = Path(__file__).parents[1] / "tools/modules/process/module.py"
    spec = importlib.util.spec_from_file_location("test_process_arguments", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arguments_accept_the_string_form_models_actually_send() -> None:
    """Un run entier a été perdu sur cette erreur de forme.

    Le modèle envoie régulièrement une ligne de commande là où le schéma attend
    une liste. La validation refusait l'appel, et deux essais suffisaient à
    condamner le run — alors que l'intention ne souffrait aucune ambiguïté.
    """
    normalise = _process_module()._arguments

    assert normalise("-m http.server 8080") == ["-m", "http.server", "8080"]
    assert normalise(["-m", "http.server", "8080"]) == ["-m", "http.server", "8080"]
    assert normalise(None) == []


def test_quoted_arguments_stay_whole() -> None:
    """Un chemin contenant une espace reste un seul argument."""
    normalise = _process_module()._arguments

    assert normalise('run "mon dossier/fichier.txt"') == ["run", "mon dossier/fichier.txt"]


def test_normalising_arguments_never_builds_a_shell_command() -> None:
    """Le découpage ne doit pas interpréter les métacaractères d'un shell :
    ils restent des arguments littéraux, et la commande est exécutée sans shell."""
    normalise = _process_module()._arguments

    assert normalise("echo a && rm -rf /") == ["echo", "a", "&&", "rm", "-rf", "/"]


def test_http_server_detection() -> None:
    """Les serveurs HTTP doivent être reconnus pour activer le réseau et les ports."""
    is_server = _process_module()._is_http_server_command
    assert is_server(["python", "-m", "http.server", "8000"])
    assert is_server(["vite", "dev"])
    assert is_server(["next", "dev"])
    assert is_server(["npm", "run", "dev"])
    assert not is_server(["echo", "hello"])


def test_docker_translates_host_resolved_windows_executables() -> None:
    assert _container_command([r"C:\Program Files\nodejs\node.EXE", "--check", "js/app.js"]) == [
        "node",
        "--check",
        "js/app.js",
    ]
    assert _container_command(
        [
            r"C:\Windows\System32\cmd.exe",
            "/d",
            "/c",
            "call",
            r"C:\Program Files\nodejs\npx.CMD",
            "http-server",
            "8000",
        ]
    ) == ["npx", "http-server", "8000"]
    assert _container_command(
        ["python", "-m", "http.server", "8000", "--bind", "127.0.0.1"],
        expose_network=True,
    ) == [
        "python",
        "-m",
        "http.server",
        "8000",
        "--bind",
        "0.0.0.0",  # noqa: S104 - adresse interne au conteneur publié
    ]


def test_http_server_port_extraction() -> None:
    """Le port du serveur doit être extrait des indicateurs explicites ou des
    valeurs par défaut par outil."""
    extract = _process_module()._http_server_port
    assert extract(["python", "-m", "http.server", "8080"]) == 8080
    assert extract(["python", "-m", "http.server"]) == 8000
    assert extract(["npx", "serve", "-p", "3000"]) == 3000
    assert extract(["npx", "serve", "--port", "5000"]) == 5000
    assert extract(["vite", "dev"]) == 5173
    assert extract(["next", "dev"]) == 3000
    assert extract(["astro", "dev"]) == 5173
    assert extract(["nuxt", "dev"]) == 3000
    assert extract(["ng", "serve"]) == 4200
    assert extract(["npm", "run", "dev"]) is None
    assert extract(["echo", "hello"]) is None


async def test_command_run_refuses_persistent_server(tmp_path: Path) -> None:
    module = _process_module()
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=tmp_path))

    result = await module.command_run(ctx, "python", ["-m", "http.server", "8000"])

    assert result["ok"] is False
    assert result["error"]["type"] == "persistent_command"


async def test_process_start_refuses_an_occupied_port(tmp_path: Path) -> None:
    module = _process_module()
    content = tmp_path / "content-agents"
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            workspace=tmp_path,
            state_db=content / "state.db",
            events=JsonlEventStore(content / "sessions"),
            session_id=uuid4(),
            security_mode=SecurityMode.POWER,
        )
    )
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await module.process_start(
            ctx, "python", ["-m", "http.server", str(port)], network=True, port=port
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result["ok"] is False
    assert result["error"]["type"] == "port_in_use"
    assert result["metadata"]["suggested_port"] != port
