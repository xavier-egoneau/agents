"""Secure Telegram channel configuration and long-polling supervisor."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from markdown_it import MarkdownIt
from pydantic import BaseModel, field_validator

from .errors import ConfigurationError
from .models import ApprovalRequest, RunRequest, RunResult, RunStatus, SecurityMode
from .platform.secure_files import secure_file
from .scheduler import agent_session_id
from .secrets import SecretStore

# Telegram écrit désormais dans le canal de l'agent plutôt que dans un fil par
# conversation : c'est le même interlocuteur, sur une autre surface, et le
# séparer coupait l'agent de ce qu'il venait de dire ailleurs.
#
# Conséquence assumée : deux conversations Telegram distinctes avec le même
# agent partagent ce fil. Un seul utilisateur est autorisé par configuration,
# donc le cas se réduit à parler au bot depuis un groupe autant que depuis la
# conversation privée — auquel cas partager le contexte est ce qu'on veut.
_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
_USER_ID = re.compile(r"^[1-9]\d*$")
_RISK_LABELS = {
    "read": "lecture",
    "write": "écriture",
    "destructive": "suppression ou modification destructive",
    "network": "accès réseau",
    "external": "action sur un service externe",
    "secret": "accès à un secret",
    "system": "modification système",
    "execute": "exécution de commande",
    "screen": "capture d’écran",
}
_REASON_LABELS = {
    "Approval required": "cette action ne peut pas être autorisée automatiquement",
    "Network approval required": "un accès au réseau est nécessaire",
    "Overwrite requires approval.": "un fichier existant va être écrasé",
    "Private or local network targets require approval.": (
        "la cible est située sur un réseau privé ou local"
    ),
    "Actions outside the workspace require approval.": (
        "l’action vise une cible située hors du workspace"
    ),
    "Destructive actions always require approval.": "l’action peut être destructive",
    "Screen capture always requires approval.": "une capture d’écran est demandée",
    "Mutating HTTP requests require approval.": (
        "la requête HTTP peut modifier un service distant"
    ),
    "Writes require approval in safe mode.": "le mode safe exige une confirmation d’écriture",
    "Overwriting an existing path requires approval in limited mode.": (
        "un fichier existant va être écrasé"
    ),
    "External actions require approval.": "l’action agit sur un service externe",
    "Potentially destructive command requires approval.": (
        "la commande peut modifier ou supprimer des données"
    ),
    "Command execution requires approval in safe mode.": (
        "le mode safe exige une confirmation avant d’exécuter une commande"
    ),
    "Unknown executable requires approval in limited mode.": (
        "le programme demandé n’est pas reconnu comme étant autorisé"
    ),
}
_ARGUMENT_LABELS = {
    "args": "Arguments",
    "command": "Commande",
    "cwd": "Dossier de travail",
    "max_chars": "Longueur maximale",
    "method": "Méthode",
    "path": "Chemin",
    "program": "Programme",
    "query": "Recherche",
    "url": "Adresse",
}
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})
_TELEGRAM_HTML_TAGS = {
    "a",
    "b",
    "blockquote",
    "code",
    "i",
    "pre",
    "s",
    "tg-spoiler",
    "u",
}

LaunchRun = Callable[[RunRequest], Awaitable[RunResult]]
ListApprovals = Callable[[], list[ApprovalRequest]]
ResolveApprovals = Callable[[list[UUID], bool], Awaitable[RunResult]]
ClearSession = Callable[[UUID], bool]


class TelegramAgentInput(BaseModel):
    enabled: bool = False
    hide_session: bool = False
    user_id: str = ""
    bot_token: str = ""

    @field_validator("user_id", "bot_token", mode="before")
    @classmethod
    def normalize_secret_input(cls, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value).strip()
        raise ValueError("la valeur doit être du texte")


@dataclass(frozen=True)
class TelegramRuntimeConfig:
    agent_id: str
    user_id: int
    bot_token: str
    hide_session: bool
    offset: int | None
    chats: tuple[tuple[str, int], ...] = ()
    approval_notices: tuple[tuple[str, str], ...] = ()

    @property
    def fingerprint(self) -> str:
        value = f"{self.user_id}\0{self.bot_token}\0{self.hide_session}"
        return hashlib.sha256(value.encode()).hexdigest()[:16]


class TelegramConfigStore:
    def __init__(self, content_root: Path) -> None:
        self.path = content_root / "telegram.json"
        self.secrets = SecretStore(content_root / "secrets.json")
        self._lock = threading.Lock()

    @staticmethod
    def secret_names(agent_id: str) -> tuple[str, str]:
        suffix = re.sub(r"[^A-Za-z0-9]", "_", agent_id).upper()
        return f"TELEGRAM_{suffix}_USER_ID", f"TELEGRAM_{suffix}_BOT_TOKEN"

    def view(self, agent_id: str) -> dict[str, object]:
        document = self._read()
        stored = document["agents"].get(agent_id, {})
        user_secret, token_secret = self.secret_names(agent_id)
        names = set(self.secrets.names())
        return {
            "enabled": bool(stored.get("enabled", False)),
            "hide_session": bool(stored.get("hide_session", False)),
            "user_id": "",
            "bot_token": "",
            "user_id_configured": user_secret in names,
            "bot_token_configured": token_secret in names,
        }

    def validate(self, agent_id: str, payload: TelegramAgentInput) -> None:
        user_secret, token_secret = self.secret_names(agent_id)
        user_id = payload.user_id or self.secrets.resolve(user_secret) or ""
        bot_token = payload.bot_token or self.secrets.resolve(token_secret) or ""
        if payload.user_id and not _USER_ID.fullmatch(payload.user_id):
            raise ConfigurationError(
                "l’identifiant utilisateur Telegram doit être un entier positif"
            )
        if payload.bot_token and not _TOKEN.fullmatch(payload.bot_token):
            raise ConfigurationError("le token du bot Telegram a un format invalide")
        if payload.enabled:
            if not _USER_ID.fullmatch(user_id):
                raise ConfigurationError("l’identifiant utilisateur Telegram est requis")
            if not _TOKEN.fullmatch(bot_token):
                raise ConfigurationError("le token du bot Telegram est requis")

    def update(self, agent_id: str, payload: TelegramAgentInput) -> dict[str, object]:
        self.validate(agent_id, payload)
        user_secret, token_secret = self.secret_names(agent_id)
        with self._lock:
            document = self._read()
            existing = document["agents"].get(agent_id, {})
            changed_identity = bool(payload.user_id or payload.bot_token)
            document["agents"][agent_id] = {
                "enabled": payload.enabled,
                "hide_session": payload.hide_session,
                "offset": None if changed_identity else existing.get("offset"),
                "chats": {} if changed_identity else existing.get("chats", {}),
                "approval_notices": (
                    {} if changed_identity else existing.get("approval_notices", {})
                ),
            }
            if payload.user_id:
                self.secrets.set(user_secret, payload.user_id)
            if payload.bot_token:
                self.secrets.set(token_secret, payload.bot_token)
            self._write(document)
        return self.view(agent_id)

    def delete(self, agent_id: str) -> None:
        user_secret, token_secret = self.secret_names(agent_id)
        with self._lock:
            document = self._read()
            document["agents"].pop(agent_id, None)
            self._write(document)
            self.secrets.delete(user_secret)
            self.secrets.delete(token_secret)

    def runtime_configs(self, valid_agent_ids: set[str]) -> list[TelegramRuntimeConfig]:
        document = self._read()
        result: list[TelegramRuntimeConfig] = []
        for agent_id, stored in document["agents"].items():
            if agent_id not in valid_agent_ids or not stored.get("enabled"):
                continue
            user_secret, token_secret = self.secret_names(agent_id)
            raw_user = self.secrets.resolve(user_secret) or ""
            token = self.secrets.resolve(token_secret) or ""
            if not _USER_ID.fullmatch(raw_user) or not _TOKEN.fullmatch(token):
                continue
            stored_chats = stored.get("chats", {})
            if not isinstance(stored_chats, dict):
                stored_chats = {}
            stored_notices = stored.get("approval_notices", {})
            if not isinstance(stored_notices, dict):
                stored_notices = {}
            result.append(
                TelegramRuntimeConfig(
                    agent_id=agent_id,
                    user_id=int(raw_user),
                    bot_token=token,
                    hide_session=bool(stored.get("hide_session", False)),
                    offset=(int(stored["offset"]) if stored.get("offset") is not None else None),
                    chats=tuple(
                        (str(session_id), int(chat_id))
                        for session_id, chat_id in stored_chats.items()
                        if isinstance(session_id, str) and isinstance(chat_id, int)
                    ),
                    approval_notices=tuple(
                        (str(session_id), str(fingerprint))
                        for session_id, fingerprint in stored_notices.items()
                        if isinstance(session_id, str) and isinstance(fingerprint, str)
                    ),
                )
            )
        return result

    def remember_chat(self, agent_id: str, session_id: UUID, chat_id: int) -> None:
        with self._lock:
            document = self._read()
            stored = document["agents"].get(agent_id)
            if not isinstance(stored, dict):
                return
            chats = stored.setdefault("chats", {})
            if not isinstance(chats, dict):
                chats = {}
                stored["chats"] = chats
            chats[str(session_id)] = chat_id
            self._write(document)

    def remember_approval_notice(
        self, agent_id: str, session_id: UUID, fingerprint: str | None
    ) -> None:
        with self._lock:
            document = self._read()
            stored = document["agents"].get(agent_id)
            if not isinstance(stored, dict):
                return
            notices = stored.setdefault("approval_notices", {})
            if not isinstance(notices, dict):
                notices = {}
                stored["approval_notices"] = notices
            if fingerprint is None:
                notices.pop(str(session_id), None)
            else:
                notices[str(session_id)] = fingerprint
            self._write(document)

    def set_offset(self, agent_id: str, offset: int) -> None:
        with self._lock:
            document = self._read()
            stored = document["agents"].get(agent_id)
            if isinstance(stored, dict):
                stored["offset"] = offset
                self._write(document)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "agents": {}}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"configuration Telegram invalide : {self.path}") from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("agents"), dict)
        ):
            raise ConfigurationError(f"configuration Telegram invalide : {self.path}")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            secure_file(temporary)
            os.replace(temporary, self.path)
            secure_file(self.path)
        finally:
            temporary.unlink(missing_ok=True)


class TelegramSupervisor:
    def __init__(
        self,
        store: TelegramConfigStore,
        agent_ids: Callable[[], set[str]],
        agent_security_mode: Callable[[str], SecurityMode],
        launch: LaunchRun,
        list_approvals: ListApprovals,
        resolve_approvals: ResolveApprovals,
        clear_session: ClearSession,
    ) -> None:
        self.store = store
        self.agent_ids = agent_ids
        self.agent_security_mode = agent_security_mode
        self.launch = launch
        self.list_approvals = list_approvals
        self.resolve_approvals = resolve_approvals
        self.clear_session = clear_session
        self._workers: dict[str, tuple[str, asyncio.Task[None]]] = {}

    async def run_forever(self) -> None:
        try:
            while True:
                await self._reconcile()
                await asyncio.sleep(2)
        finally:
            await self._stop_all()

    async def _reconcile(self) -> None:
        desired = {item.agent_id: item for item in self.store.runtime_configs(self.agent_ids())}
        for agent_id, (fingerprint, task) in list(self._workers.items()):
            config = desired.get(agent_id)
            if task.done() or config is None or config.fingerprint != fingerprint:
                task.cancel()
                await _cancel(task)
                self._workers.pop(agent_id, None)
        for agent_id, config in desired.items():
            if agent_id not in self._workers:
                task = asyncio.create_task(
                    self._poll(config),
                    name=f"amk-telegram-{agent_id}",
                )
                self._workers[agent_id] = (config.fingerprint, task)

    async def _stop_all(self) -> None:
        tasks = [item[1] for item in self._workers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()

    async def _poll(self, config: TelegramRuntimeConfig) -> None:
        offset = config.offset
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10)) as client:
            try:
                await self._set_commands(client, config.bot_token)
            except Exception:
                # Command discovery is optional; polling must remain available.
                pass
            await self._restore_pending_approvals(client, config)
            if offset is None:
                updates = await self._get_updates(client, config.bot_token, -1, timeout=0)
                if updates:
                    offset = int(updates[-1]["update_id"]) + 1
                    self.store.set_offset(config.agent_id, offset)
            while True:
                try:
                    updates = await self._get_updates(
                        client, config.bot_token, offset, timeout=25
                    )
                    for update in updates:
                        next_offset = int(update["update_id"]) + 1
                        await self._handle_update(client, config, update)
                        offset = next_offset
                        self.store.set_offset(config.agent_id, offset)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Token-bearing URLs and payloads must never reach logs.
                    await asyncio.sleep(5)

    async def _get_updates(
        self,
        client: httpx.AsyncClient,
        token: str,
        offset: int | None,
        *,
        timeout: int,
    ) -> list[dict[str, Any]]:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/getUpdates",
            json={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok") or not isinstance(payload.get("result"), list):
            raise RuntimeError("Telegram getUpdates failed")
        return [item for item in payload["result"] if isinstance(item, dict)]

    async def _handle_update(
        self,
        client: httpx.AsyncClient,
        config: TelegramRuntimeConfig,
        update: dict[str, Any],
    ) -> bool:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            return await self._handle_callback(client, config, callback)
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        sender = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        if (
            not isinstance(sender, dict)
            or sender.get("id") != config.user_id
            or sender.get("is_bot") is True
            or not isinstance(chat, dict)
            or not isinstance(chat.get("id"), int)
            or not isinstance(text, str)
            or not text.strip()
        ):
            return False
        chat_id = int(chat["id"])
        session_id = agent_session_id(config.agent_id)
        self.store.remember_chat(config.agent_id, session_id, chat_id)
        command = text.strip().split(maxsplit=1)[0].lower()
        if command == "/clear" or command.startswith("/clear@"):
            if command != text.strip().lower():
                await self._send_message(
                    client, config.bot_token, chat_id, "Utilisation : /clear"
                )
                return True
            try:
                self.clear_session(session_id)
            except ConfigurationError:
                await self._send_message(
                    client,
                    config.bot_token,
                    chat_id,
                    "La conversation ne peut pas être réinitialisée pendant un run actif.",
                )
                return True
            await self._send_message(
                client,
                config.bot_token,
                chat_id,
                "Conversation réinitialisée. Le prochain message repartira sans contexte.",
            )
            return True
        pending = self._latest_approvals(session_id, agent_id=config.agent_id)
        if pending:
            await self._send_approval(client, config, chat_id, pending)
            return True
        result = await self._run_with_chat_action(
            client,
            config.bot_token,
            chat_id,
            self.launch(
                RunRequest(
                    prompt=text.strip(),
                    agent_id=config.agent_id,
                    session_id=session_id,
                    workspace=None,
                    # Le niveau vient de l'agent, pas de la surface. Une valeur
                    # écrite en dur ici s'imposait sans que rien ne la montre ni
                    # ne permette de la changer.
                    security_mode=self.agent_security_mode(config.agent_id),
                    trigger="telegram",
                    hidden=config.hide_session,
                )
            )
        )
        await self._deliver_result(client, config, chat_id, result)
        return True

    async def _deliver_result(
        self,
        client: httpx.AsyncClient,
        config: TelegramRuntimeConfig,
        chat_id: int,
        result: RunResult,
    ) -> None:
        if result.status == RunStatus.APPROVAL_PENDING:
            approvals = self._approvals_for(result.session_id, result.run_id)
            if approvals:
                await self._send_approval(client, config, chat_id, approvals)
                return
            answer = "Une autorisation est requise, mais son détail est indisponible."
        else:
            answer = (result.output or "").strip() or "L’agent n’a pas produit de réponse."
        for chunk in _telegram_html_chunks(answer):
            await self._send_message(
                client,
                config.bot_token,
                chat_id,
                chunk,
                parse_mode="HTML",
            )

    async def _handle_callback(
        self,
        client: httpx.AsyncClient,
        config: TelegramRuntimeConfig,
        callback: dict[str, Any],
    ) -> bool:
        sender = callback.get("from")
        message = callback.get("message")
        query_id = callback.get("id")
        data = callback.get("data")
        if (
            not isinstance(sender, dict)
            or sender.get("id") != config.user_id
            or sender.get("is_bot") is True
            or not isinstance(query_id, str)
        ):
            if isinstance(query_id, str):
                await self._answer_callback(
                    client, config.bot_token, query_id, "Utilisateur non autorisé.", alert=True
                )
            return False
        if not isinstance(message, dict) or not isinstance(data, str):
            await self._answer_callback(
                client, config.bot_token, query_id, "Demande invalide.", alert=True
            )
            return False
        chat = message.get("chat")
        message_id = message.get("message_id")
        if (
            not isinstance(chat, dict)
            or not isinstance(chat.get("id"), int)
            or not isinstance(message_id, int)
        ):
            await self._answer_callback(
                client, config.bot_token, query_id, "Demande invalide.", alert=True
            )
            return False
        parsed = _parse_approval_callback(data)
        if parsed is None:
            await self._answer_callback(
                client, config.bot_token, query_id, "Action inconnue.", alert=True
            )
            return False
        run_id, fingerprint, approved = parsed
        chat_id = int(chat["id"])
        session_id = agent_session_id(config.agent_id)
        self.store.remember_chat(config.agent_id, session_id, chat_id)
        approvals = self._approvals_for(session_id, run_id)
        if not approvals or _approval_fingerprint(approvals) != fingerprint:
            await self._answer_callback(
                client,
                config.bot_token,
                query_id,
                "Cette demande n’est plus active.",
                alert=True,
            )
            await self._try_edit_message(
                client,
                config.bot_token,
                chat_id,
                message_id,
                "Cette demande d’autorisation n’est plus active.",
            )
            return True
        await self._answer_callback(client, config.bot_token, query_id, "Traitement en cours…")
        decision = "accordée" if approved else "refusée"
        await self._try_edit_message(
            client,
            config.bot_token,
            chat_id,
            message_id,
            f"Autorisation {decision}. Reprise de l’agent en cours…",
        )
        try:
            result = await self._run_with_chat_action(
                client,
                config.bot_token,
                chat_id,
                self.resolve_approvals(
                    [item.approval_id for item in approvals], approved
                ),
            )
        except Exception:
            current = self._approvals_for(session_id, run_id)
            await self._try_edit_message(
                client,
                config.bot_token,
                chat_id,
                message_id,
                "La décision n’a pas pu être appliquée.",
            )
            if current:
                await self._send_approval(client, config, chat_id, current)
            return True
        self.store.remember_approval_notice(config.agent_id, session_id, None)
        await self._try_edit_message(
            client,
            config.bot_token,
            chat_id,
            message_id,
            f"Autorisation {decision}.",
        )
        await self._deliver_result(client, config, chat_id, result)
        return True

    def _approvals_for(self, session_id: UUID, run_id: UUID) -> list[ApprovalRequest]:
        return sorted(
            (
                item
                for item in self.list_approvals()
                if item.session_id == session_id and item.run_id == run_id
            ),
            key=lambda item: (item.created_at, str(item.approval_id)),
        )

    def _latest_approvals(
        self, session_id: UUID, *, agent_id: str
    ) -> list[ApprovalRequest]:
        pending = [
            item
            for item in self.list_approvals()
            if item.session_id == session_id and item.agent_id == agent_id
        ]
        if not pending:
            return []
        newest_run = max(pending, key=lambda item: item.created_at).run_id
        return sorted(
            (item for item in pending if item.run_id == newest_run),
            key=lambda item: (item.created_at, str(item.approval_id)),
        )

    async def _send_approval(
        self,
        client: httpx.AsyncClient,
        config: TelegramRuntimeConfig,
        chat_id: int,
        approvals: list[ApprovalRequest],
    ) -> None:
        run_id = approvals[0].run_id
        fingerprint = _approval_fingerprint(approvals)
        lines = ["🔐 Autoriser cette action ?"]
        for index, approval in enumerate(approvals, 1):
            label = f"Action {index}" if len(approvals) > 1 else "Action"
            lines.extend(["", f"📌 {label}", approval.justification])
            target = _approval_display_target(approval)
            if target:
                lines.extend(["", "🎯 Cible", target])
            reason = _approval_reason(approval.reason)
            lines.extend(
                [
                    "",
                    "⚠️ Pourquoi votre accord est nécessaire",
                    f"Parce que {reason}.",
                ]
            )
            if approval.risks:
                lines.append(
                    "Cela implique : "
                    + ", ".join(
                        _RISK_LABELS.get(str(item.value), str(item.value))
                        for item in approval.risks
                    )
                    + "."
                )
            lines.extend(
                [
                    "",
                    "🕒 Durée de l’autorisation",
                    _approval_scope(approval),
                    "",
                    "🔧 Détails techniques",
                    f"Outil : {approval.tool_name}",
                ]
            )
            arguments = _visible_approval_arguments(approval)
            if arguments:
                lines.extend(["Paramètres :", arguments])
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "Refuser",
                    "callback_data": _approval_callback(run_id, fingerprint, False),
                },
                {
                    "text": "Autoriser",
                    "callback_data": _approval_callback(run_id, fingerprint, True),
                },
            ]]
        }
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3897].rstrip() + "..."
        await self._send_message(
            client,
            config.bot_token,
            chat_id,
            text,
            reply_markup=keyboard,
            disable_link_preview=True,
        )
        self.store.remember_approval_notice(
            config.agent_id, approvals[0].session_id, fingerprint
        )

    async def _restore_pending_approvals(
        self, client: httpx.AsyncClient, config: TelegramRuntimeConfig
    ) -> None:
        chats = dict(config.chats)
        private_session = agent_session_id(config.agent_id)
        chats.setdefault(str(private_session), config.user_id)
        notices = dict(config.approval_notices)
        pending = self.list_approvals()
        for raw_session_id, chat_id in chats.items():
            try:
                session_id = UUID(raw_session_id)
            except ValueError:
                continue
            approvals = [
                item
                for item in pending
                if item.session_id == session_id and item.agent_id == config.agent_id
            ]
            if not approvals:
                continue
            newest_run = max(approvals, key=lambda item: item.created_at).run_id
            approvals = sorted(
                (item for item in approvals if item.run_id == newest_run),
                key=lambda item: (item.created_at, str(item.approval_id)),
            )
            fingerprint = _approval_fingerprint(approvals)
            if notices.get(raw_session_id) == fingerprint:
                continue
            try:
                await self._send_approval(client, config, chat_id, approvals)
            except Exception:
                # A blocked or migrated chat must not stop polling other updates.
                continue

    async def _send_message(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_link_preview: bool = False,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if disable_link_preview:
            payload["link_preview_options"] = {"is_disabled": True}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
        )
        response.raise_for_status()

    async def _send_chat_action(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        action: str,
    ) -> None:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
        )
        response.raise_for_status()

    async def _keep_chat_action(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        *,
        interval: float = 4.0,
    ) -> None:
        while True:
            try:
                await self._send_chat_action(client, token, chat_id, "typing")
            except Exception:
                # A cosmetic Telegram failure must never interrupt the agent run.
                pass
            await asyncio.sleep(interval)

    async def _run_with_chat_action(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        operation: Awaitable[RunResult],
        *,
        interval: float = 4.0,
    ) -> RunResult:
        indicator = asyncio.create_task(
            self._keep_chat_action(
                client,
                token,
                chat_id,
                interval=interval,
            )
        )
        # Let the first action start without delaying the actual run on Telegram I/O.
        await asyncio.sleep(0)
        try:
            return await operation
        finally:
            indicator.cancel()
            await _cancel(indicator)

    async def _answer_callback(
        self,
        client: httpx.AsyncClient,
        token: str,
        query_id: str,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": text, "show_alert": alert},
        )
        response.raise_for_status()

    async def _edit_message(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": {"inline_keyboard": []},
            },
        )
        response.raise_for_status()

    async def _try_edit_message(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        try:
            await self._edit_message(client, token, chat_id, message_id, text)
        except Exception:
            # Editing is cosmetic; a Telegram limitation must not block the decision.
            pass

    async def _set_commands(self, client: httpx.AsyncClient, token: str) -> None:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={
                "commands": [
                    {"command": "clear", "description": "Réinitialiser la conversation"}
                ]
            },
        )
        response.raise_for_status()


def _approval_fingerprint(approvals: list[ApprovalRequest]) -> str:
    value = "\0".join(sorted(str(item.approval_id) for item in approvals))
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _visible_approval_arguments(approval: ApprovalRequest) -> str:
    arguments = {
        key: value for key, value in approval.arguments.items() if key != "justification"
    }
    if not arguments:
        return ""
    lines: list[str] = []
    for key, value in sorted(arguments.items()):
        label = _ARGUMENT_LABELS.get(key, key.replace("_", " ").capitalize())
        if isinstance(value, str):
            rendered = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = f"{value:,}".replace(",", " ")
        else:
            rendered = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        lines.append(f"• {label} : {rendered}")
    return "\n".join(lines)


def _approval_display_target(approval: ApprovalRequest) -> str:
    for key in ("url", "path", "file_path", "target", "cwd"):
        value = approval.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return approval.path or ""


def _approval_reason(reason: str) -> str:
    return _REASON_LABELS.get(
        reason,
        "cette action sort du périmètre autorisé automatiquement",
    ).rstrip(". ")


def _approval_scope(approval: ApprovalRequest) -> str:
    target = f"la cible « {approval.path} »" if approval.path else "toute cible équivalente"
    return (
        f"Valable aussi pour les prochaines actions similaires sur {target}, "
        "dans cette conversation, jusqu’à la commande /clear."
    )


def _approval_callback(run_id: UUID, fingerprint: str, approved: bool) -> str:
    return f"amk:a:{run_id.hex}:{fingerprint}:{int(approved)}"


def _parse_approval_callback(value: str) -> tuple[UUID, str, bool] | None:
    match = re.fullmatch(r"amk:a:([0-9a-f]{32}):([0-9a-f]{12}):([01])", value)
    if match is None:
        return None
    return UUID(hex=match.group(1)), match.group(2), match.group(3) == "1"


class _TelegramHTMLRenderer(HTMLParser):
    """Reduce markdown-it HTML to the small HTML subset accepted by Telegram."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[tuple[str, int]] = []
        self.links: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "strong"}:
            self.parts.append("<b>")
        elif tag == "em":
            self.parts.append("<i>")
        elif tag in {"del", "s"}:
            self.parts.append("<s>")
        elif tag == "blockquote":
            self.parts.append("<blockquote>")
        elif tag == "pre":
            self.parts.append("<pre>")
        elif tag == "code":
            language = ""
            class_name = attributes.get("class") or ""
            match = re.fullmatch(r"language-([A-Za-z0-9_+.-]+)", class_name)
            if match:
                language = f' class="language-{match.group(1)}"'
            self.parts.append(f"<code{language}>")
        elif tag == "a":
            href = attributes.get("href") or ""
            allowed = _telegram_link_allowed(href)
            self.links.append(allowed)
            if allowed:
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag == "ul":
            self.lists.append(("ul", 0))
        elif tag == "ol":
            try:
                start = max(1, int(attributes.get("start") or "1"))
            except ValueError:
                start = 1
            self.lists.append(("ol", start - 1))
        elif tag == "li":
            depth = max(0, len(self.lists) - 1)
            prefix = "• "
            if self.lists and self.lists[-1][0] == "ol":
                kind, counter = self.lists[-1]
                counter += 1
                self.lists[-1] = (kind, counter)
                prefix = f"{counter}. "
            self.parts.append("  " * depth + prefix)
        elif tag == "br":
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "img":
            attributes = dict(attrs)
            alt = attributes.get("alt") or "Image"
            src = attributes.get("src") or ""
            label = html.escape(alt)
            if _telegram_link_allowed(src):
                self.parts.append(
                    f'<a href="{html.escape(src, quote=True)}">{label}</a>'
                )
            else:
                self.parts.append(label)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("</b>\n\n")
        elif tag == "strong":
            self.parts.append("</b>")
        elif tag == "em":
            self.parts.append("</i>")
        elif tag in {"del", "s"}:
            self.parts.append("</s>")
        elif tag == "blockquote":
            self.parts.append("</blockquote>\n\n")
        elif tag == "pre":
            self.parts.append("</pre>\n\n")
        elif tag == "code":
            self.parts.append("</code>")
        elif tag == "a":
            if self.links.pop() if self.links else False:
                self.parts.append("</a>")
        elif tag == "p":
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            if self.lists:
                self.lists.pop()
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def render(self) -> str:
        return "".join(self.parts).strip()


class _TelegramHTMLChunker(HTMLParser):
    """Split formatted HTML without cutting an entity or leaving tags unbalanced."""

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts: list[str] = []
        self.chunks: list[str] = []
        self.active: list[tuple[str, str]] = []
        self.length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _TELEGRAM_HTML_TAGS:
            return
        rendered_attrs = "".join(
            f' {name}="{html.escape(value or "", quote=True)}"' for name, value in attrs
        )
        opening = f"<{tag}{rendered_attrs}>"
        self.parts.append(opening)
        self.active.append((tag, opening))

    def handle_endtag(self, tag: str) -> None:
        if tag not in _TELEGRAM_HTML_TAGS:
            return
        self.parts.append(f"</{tag}>")
        if self.active and self.active[-1][0] == tag:
            self.active.pop()

    def handle_data(self, data: str) -> None:
        remaining = data
        while remaining:
            budget = self.limit - self.length
            if budget <= 0:
                self._split()
                budget = self.limit
            count = _utf16_prefix_length(remaining, budget)
            if count == 0:
                self._split()
                continue
            segment = remaining[:count]
            self.parts.append(html.escape(segment))
            self.length += _utf16_length(segment)
            remaining = remaining[count:]
            if remaining:
                self._split()

    def _split(self) -> None:
        if self.length:
            closing = "".join(f"</{tag}>" for tag, _opening in reversed(self.active))
            self.chunks.append("".join(self.parts) + closing)
        self.parts = [opening for _tag, opening in self.active]
        self.length = 0

    def result(self) -> list[str]:
        if self.length:
            self.chunks.append("".join(self.parts))
        return self.chunks


def _telegram_link_allowed(url: str) -> bool:
    return urlsplit(url).scheme.lower() in {"http", "https", "mailto", "tg"}


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _utf16_prefix_length(value: str, limit: int) -> int:
    used = 0
    for index, character in enumerate(value):
        used += 2 if ord(character) > 0xFFFF else 1
        if used > limit:
            return index
    return len(value)


def _markdown_to_telegram_html(markdown: str) -> str:
    renderer = _TelegramHTMLRenderer()
    renderer.feed(_MARKDOWN.render(markdown))
    renderer.close()
    return renderer.render()


def _telegram_html_chunks(markdown: str, limit: int = 3900) -> list[str]:
    rendered = _markdown_to_telegram_html(markdown)
    if not rendered:
        return []
    chunker = _TelegramHTMLChunker(limit)
    chunker.feed(rendered)
    chunker.close()
    return chunker.result()


async def _cancel(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
