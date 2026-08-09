import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentic_kernel.models import ApprovalRequest, RunResult, RunStatus, SecurityMode, ToolRisk
from agentic_kernel.scheduler import agent_session_id
from agentic_kernel.telegram import (
    TelegramAgentInput,
    TelegramConfigStore,
    TelegramRuntimeConfig,
    TelegramSupervisor,
    _approval_callback,
    _approval_fingerprint,
    _markdown_to_telegram_html,
    _telegram_html_chunks,
)

TOKEN = "123456:abcdefghijklmnopqrstuvwxyz_ABCD"


def test_telegram_secrets_are_write_only_and_blank_values_preserve_them(
    tmp_path: Path,
) -> None:
    store = TelegramConfigStore(tmp_path)
    view = store.update(
        "helper",
        TelegramAgentInput(
            enabled=True,
            hide_session=True,
            user_id="1234567",
            bot_token=TOKEN,
        ),
    )
    assert view["user_id"] == view["bot_token"] == ""
    assert view["user_id_configured"] is True
    assert view["bot_token_configured"] is True
    assert "1234567" not in (tmp_path / "telegram.json").read_text(encoding="utf-8")
    assert TOKEN not in (tmp_path / "telegram.json").read_text(encoding="utf-8")

    store.update(
        "helper",
        TelegramAgentInput(enabled=True, hide_session=False),
    )
    runtime = store.runtime_configs({"helper"})[0]
    assert runtime.user_id == 1234567
    assert runtime.bot_token == TOKEN
    assert runtime.hide_session is False


def test_telegram_persists_chat_and_approval_notice_for_restart(tmp_path: Path) -> None:
    store = TelegramConfigStore(tmp_path)
    store.update(
        "helper",
        TelegramAgentInput(
            enabled=True,
            user_id="42",
            bot_token=TOKEN,
        ),
    )
    session_id = uuid4()
    store.remember_chat("helper", session_id, -100123)
    store.remember_approval_notice("helper", session_id, "abc123")

    runtime = store.runtime_configs({"helper"})[0]

    assert runtime.chats == ((str(session_id), -100123),)
    assert runtime.approval_notices == ((str(session_id), "abc123"),)


@pytest.mark.asyncio
async def test_telegram_rejects_every_sender_except_exact_allowed_user(tmp_path: Path) -> None:
    requests = []

    async def launch(request):
        requests.append(request)
        return RunResult(
            session_id=request.session_id,
            run_id=uuid4(),
            agent_id=request.agent_id,
            status=RunStatus.SUCCESS,
            output="Bonjour depuis AMK",
        )

    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        launch,
        lambda: [],
        AsyncMock(),
        lambda _session_id: True,
    )
    supervisor._send_message = AsyncMock()  # type: ignore[method-assign]
    config = TelegramRuntimeConfig(
        agent_id="helper",
        user_id=42,
        bot_token=TOKEN,
        hide_session=True,
        offset=1,
    )
    group_chat = {"id": -100123, "type": "supergroup"}

    rejected = await supervisor._handle_update(
        object(),  # type: ignore[arg-type]
        config,
        {
            "update_id": 1,
            "message": {
                "from": {"id": 99, "is_bot": False},
                "chat": group_chat,
                "text": "message étranger",
            },
        },
    )
    assert rejected is False
    assert requests == []
    supervisor._send_message.assert_not_awaited()

    accepted = await supervisor._handle_update(
        object(),  # type: ignore[arg-type]
        config,
        {
            "update_id": 2,
            "message": {
                "from": {"id": 42, "is_bot": False},
                "chat": group_chat,
                "text": "message autorisé",
            },
        },
    )
    assert accepted is True
    assert len(requests) == 1
    assert requests[0].agent_id == "helper"
    assert requests[0].trigger == "telegram"
    assert requests[0].hidden is True
    supervisor._send_message.assert_awaited_once()
    assert supervisor._send_message.await_args.kwargs["parse_mode"] == "HTML"


def test_telegram_converts_commonmark_to_supported_html() -> None:
    rendered = _markdown_to_telegram_html(
        """## Les faits disponibles

Du **gras**, de l'*italique* et `<code & sûr>`.

- Premier élément
- [Une source](https://example.com/?a=1&b=2)

```python
print("<ok>")
```
"""
    )

    assert rendered.startswith("<b>Les faits disponibles</b>")
    assert "<b>gras</b>" in rendered
    assert "<i>italique</i>" in rendered
    assert "<code>&lt;code &amp; sûr&gt;</code>" in rendered
    assert "• Premier élément" in rendered
    assert '<a href="https://example.com/?a=1&amp;b=2">Une source</a>' in rendered
    assert (
        '<pre><code class="language-python">print(&quot;&lt;ok&gt;&quot;)\n</code></pre>'
        in rendered
    )
    assert all(tag not in rendered for tag in ("<h2>", "<p>", "<ul>", "<li>"))


def test_telegram_html_chunks_keep_formatting_balanced() -> None:
    chunks = _telegram_html_chunks(f"**{'🙂' * 125}**", limit=100)

    assert len(chunks) == 3
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)
    assert all(chunk.count("🙂") <= 50 for chunk in chunks)
    assert sum(chunk.count("🙂") for chunk in chunks) == 125


@pytest.mark.asyncio
async def test_telegram_refreshes_typing_action_until_run_finishes(tmp_path: Path) -> None:
    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        AsyncMock(),
        lambda: [],
        AsyncMock(),
        lambda _session_id: True,
    )
    refreshed = asyncio.Event()
    calls = 0

    async def send_action(*_args) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            refreshed.set()

    supervisor._send_chat_action = AsyncMock(side_effect=send_action)  # type: ignore[method-assign]
    expected = RunResult(
        session_id=uuid4(),
        run_id=uuid4(),
        agent_id="helper",
        status=RunStatus.SUCCESS,
        output="Terminé",
    )

    async def operation() -> RunResult:
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        return expected

    client = object()
    result = await supervisor._run_with_chat_action(
        client,  # type: ignore[arg-type]
        TOKEN,
        123,
        operation(),
        interval=0,
    )
    calls_after_run = calls
    await asyncio.sleep(0)

    assert result is expected
    assert calls_after_run >= 2
    assert calls == calls_after_run
    supervisor._send_chat_action.assert_any_await(client, TOKEN, 123, "typing")


@pytest.mark.asyncio
async def test_telegram_renders_pending_approval_as_inline_keyboard(tmp_path: Path) -> None:
    run_id = uuid4()
    session_id = uuid4()
    approval = ApprovalRequest(
        session_id=session_id,
        run_id=run_id,
        agent_id="helper",
        tool_call_id="call-1",
        tool_name="shell_command",
        tool_description="Exécute une commande locale.",
        action_family="execute",
        path="C:/workspace",
        justification="Lancer la commande demandée",
        arguments={
            "program": "git",
            "args": ["status", "--short"],
            "cwd": "C:/workspace",
            "justification": "Lancer la commande demandée",
        },
        risks=[ToolRisk.EXECUTE],
        reason="Actions outside the workspace require approval.",
    )
    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        AsyncMock(),
        lambda: [approval],
        AsyncMock(),
        lambda _session_id: True,
    )
    supervisor._send_message = AsyncMock()  # type: ignore[method-assign]
    config = TelegramRuntimeConfig("helper", 42, TOKEN, False, 1)
    result = RunResult(
        session_id=session_id,
        run_id=run_id,
        agent_id="helper",
        status=RunStatus.APPROVAL_PENDING,
        output="Approval required before the run can continue.",
    )

    await supervisor._deliver_result(object(), config, 123, result)  # type: ignore[arg-type]

    call = supervisor._send_message.await_args
    message = call.args[3]
    assert "🔐 Autoriser cette action ?" in message
    assert "📌 Action\nLancer la commande demandée" in message
    assert "🎯 Cible\nC:/workspace" in message
    assert "Outil : shell_command" in message
    assert "Fonction : Exécute une commande locale." not in message
    assert "• Programme : git" in message
    assert '• Arguments : ["status", "--short"]' in message
    assert '"justification"' not in message
    assert "Parce que l’action vise une cible située hors du workspace." in message
    assert "Cela implique : exécution de commande." in message
    assert "🕒 Durée de l’autorisation" in message
    assert "jusqu’à la commande /clear" in message
    assert call.kwargs["disable_link_preview"] is True
    keyboard = call.kwargs["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in keyboard] == ["Refuser", "Autoriser"]
    assert len(keyboard[1]["callback_data"].encode()) <= 64


@pytest.mark.asyncio
async def test_telegram_approval_callback_is_authorized_and_resumes_batch(
    tmp_path: Path,
) -> None:
    chat_id = 123
    # Telegram écrit dans le canal de l'agent : c'est le même interlocuteur,
    # et un fil par conversation coupait l'agent de ce qu'il venait de dire.
    telegram_session_id = agent_session_id("helper")
    run_id = uuid4()
    approval = ApprovalRequest(
        session_id=telegram_session_id,
        run_id=run_id,
        agent_id="helper",
        tool_call_id="call-1",
        tool_name="shell_command",
        action_family="execute",
        justification="Lancer la commande demandée",
        reason="Approval required",
    )
    resolved = RunResult(
        session_id=telegram_session_id,
        run_id=run_id,
        agent_id="helper",
        status=RunStatus.SUCCESS,
        output="Commande terminée",
    )
    resolver = AsyncMock(return_value=resolved)
    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        AsyncMock(),
        lambda: [approval],
        resolver,
        lambda _session_id: True,
    )
    supervisor._answer_callback = AsyncMock()  # type: ignore[method-assign]
    supervisor._edit_message = AsyncMock()  # type: ignore[method-assign]
    supervisor._send_message = AsyncMock()  # type: ignore[method-assign]
    config = TelegramRuntimeConfig("helper", 42, TOKEN, False, 1)
    data = _approval_callback(run_id, _approval_fingerprint([approval]), True)
    client = object()

    accepted = await supervisor._handle_update(
        client,  # type: ignore[arg-type]
        config,
        {
            "callback_query": {
                "id": "query-1",
                "from": {"id": 42, "is_bot": False},
                "data": data,
                "message": {"message_id": 9, "chat": {"id": chat_id}},
            }
        },
    )

    assert accepted is True
    resolver.assert_awaited_once_with([approval.approval_id], True)
    supervisor._answer_callback.assert_awaited_once()
    assert supervisor._edit_message.await_count == 2
    supervisor._send_message.assert_awaited_once_with(
        client, TOKEN, chat_id, "Commande terminée", parse_mode="HTML"
    )


@pytest.mark.asyncio
async def test_telegram_clear_is_native_and_does_not_launch_model(tmp_path: Path) -> None:
    launched = AsyncMock()
    cleared = []
    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        launched,
        lambda: [],
        AsyncMock(),
        lambda session_id: cleared.append(session_id) is None,
    )
    supervisor._send_message = AsyncMock()  # type: ignore[method-assign]
    config = TelegramRuntimeConfig("helper", 42, TOKEN, False, 1)

    accepted = await supervisor._handle_update(
        object(),  # type: ignore[arg-type]
        config,
        {
            "message": {
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": 123, "type": "private"},
                "text": "/clear",
            }
        },
    )

    assert accepted is True
    assert len(cleared) == 1
    launched.assert_not_awaited()
    supervisor._send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_rejects_approval_callback_from_another_user(
    tmp_path: Path,
) -> None:
    resolver = AsyncMock()
    supervisor = TelegramSupervisor(
        TelegramConfigStore(tmp_path),
        lambda: {"helper"},
        lambda _agent_id: SecurityMode.LIMITED,
        AsyncMock(),
        lambda: [],
        resolver,
        lambda _session_id: True,
    )
    supervisor._answer_callback = AsyncMock()  # type: ignore[method-assign]
    config = TelegramRuntimeConfig("helper", 42, TOKEN, False, 1)

    accepted = await supervisor._handle_update(
        object(),  # type: ignore[arg-type]
        config,
        {
            "callback_query": {
                "id": "foreign-query",
                "from": {"id": 99, "is_bot": False},
                "data": "amk:a:invalid",
            }
        },
    )

    assert accepted is False
    resolver.assert_not_awaited()
    supervisor._answer_callback.assert_awaited_once()
