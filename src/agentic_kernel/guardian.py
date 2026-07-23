from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .models import (
    ApprovalRequest,
    Event,
    GuardianDecision,
    GuardianVerdict,
    SecurityMode,
    ToolRisk,
)

PROTECTED_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".git", ".codex"}
SENSITIVE_NAMES = {".env", ".env.local", ".envrc", "providers.json"}
SECRET_KEYS = {"api_key", "token", "password", "secret", "authorization"}


def canonical_path(raw: Any, workspace: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    candidate = Path(raw).expanduser()
    return (workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def review_tool_call(
    *,
    tool_name: str,
    tool_call_id: str,
    agent_id: str,
    arguments: dict[str, Any],
    risks: list[ToolRisk],
    mode: SecurityMode,
    workspace: Path,
) -> GuardianDecision:
    justification = arguments.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        return GuardianDecision(
            verdict=GuardianVerdict.DENY,
            reason="A non-empty justification is required.",
            tool_name=tool_name,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            risks=risks,
            justification="",
            security_mode=mode,
        )
    raw_path = arguments.get("path")
    path = canonical_path(raw_path, workspace) if raw_path is not None else None
    if raw_path is not None and path is None:
        verdict, reason = GuardianVerdict.DENY, "The path is invalid or ambiguous."
    elif raw_path is not None and _contains_symlink(raw_path, workspace):
        verdict, reason = GuardianVerdict.DENY, "Paths traversing symbolic links are denied."
    elif path is not None and (set(path.parts) & PROTECTED_PARTS or path.name in SENSITIVE_NAMES):
        verdict, reason = GuardianVerdict.DENY, "Protected or secret paths are denied."
    elif ToolRisk.SECRET in risks or ToolRisk.SYSTEM in risks:
        verdict, reason = GuardianVerdict.DENY, "Secret and system actions are denied."
    elif _targets_private_network(arguments):
        verdict, reason = GuardianVerdict.ASK, "Private or local network targets require approval."
    elif path is not None and not _inside(path, workspace.resolve()):
        verdict, reason = GuardianVerdict.ASK, "Actions outside the workspace require approval."
    elif ToolRisk.DESTRUCTIVE in risks:
        verdict, reason = GuardianVerdict.ASK, "Destructive actions always require approval."
    elif ToolRisk.WRITE in risks and mode is SecurityMode.SAFE:
        verdict, reason = GuardianVerdict.ASK, "Writes require approval in safe mode."
    elif (
        ToolRisk.WRITE in risks
        and mode is SecurityMode.LIMITED
        and path is not None
        and path.exists()
    ):
        verdict = GuardianVerdict.ASK
        reason = "Overwriting an existing path requires approval in limited mode."
    elif ToolRisk.NETWORK in risks or ToolRisk.EXTERNAL in risks:
        if mode is SecurityMode.POWER:
            verdict, reason = GuardianVerdict.ALLOW, "External action allowed in power mode."
        else:
            verdict, reason = GuardianVerdict.ASK, "External actions require approval."
    else:
        verdict, reason = GuardianVerdict.ALLOW, "Action allowed by the active security mode."
    return GuardianDecision(
        verdict=verdict,
        reason=reason,
        tool_name=tool_name,
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        path=str(path) if path else None,
        risks=risks,
        justification=justification.strip(),
        security_mode=mode,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _targets_private_network(arguments: dict[str, Any]) -> bool:
    for key in ("url", "seed"):
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        hostname = urlparse(value).hostname
        if not hostname:
            continue
        if hostname.lower() == "localhost" or hostname.lower().endswith(".local"):
            return True
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            return True
    return False


def _contains_symlink(raw: Any, workspace: Path) -> bool:
    if not isinstance(raw, str):
        return False
    source = Path(raw).expanduser()
    if source.is_absolute():
        current = Path(source.anchor)
        parts = source.parts[1:]
    else:
        current = workspace
        parts = source.parts
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def action_family(risks: list[ToolRisk]) -> str:
    for risk in (ToolRisk.DESTRUCTIVE, ToolRisk.WRITE, ToolRisk.READ, ToolRisk.NETWORK):
        if risk in risks:
            return risk.value
    return "other"


def approval_scope(decision: GuardianDecision) -> str:
    payload = {
        "family": action_family(decision.risks),
        "path": decision.path,
        "session": None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def redact(value: Any, key: str = "") -> Any:
    if any(secret in key.lower() for secret in SECRET_KEYS):
        return "***"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


@dataclass
class GuardianToolset(WrapperToolset[Any]):
    agent_id: str
    risks: dict[str, list[ToolRisk]]

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = await super().get_tools(ctx)
        result: dict[str, ToolsetTool[Any]] = {}
        for name, item in tools.items():
            schema = dict(item.tool_def.parameters_json_schema)
            properties = dict(schema.get("properties", {}))
            properties["justification"] = {
                "type": "string",
                "description": "Why this tool call is necessary for the user's request.",
                "minLength": 1,
            }
            required = list(dict.fromkeys([*schema.get("required", []), "justification"]))
            schema.update(properties=properties, required=required)
            result[name] = replace(
                item,
                tool_def=replace(item.tool_def, parameters_json_schema=schema),
            )
        return result

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        deps = ctx.deps
        call_id = str(ctx.tool_call_id or "unknown")
        run_id = _run_uuid(ctx.run_id, deps.root_run_id)
        risks = self.risks.get(name, [])
        deps.events.append(Event(
            session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
            type="tool.proposed",
            payload={"tool_call_id": call_id, "tool": name, "arguments": redact(tool_args)},
        ))
        decision = review_tool_call(
            tool_name=name, tool_call_id=call_id, agent_id=self.agent_id,
            arguments=tool_args, risks=risks, mode=deps.security_mode, workspace=deps.workspace,
        )
        deps.events.append(Event(
            session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
            type="guardian.reviewed", payload=decision.model_dump(mode="json"),
        ))
        if decision.verdict is GuardianVerdict.DENY:
            return {"ok": False, "denied": True, "reason": decision.reason}
        if decision.verdict is GuardianVerdict.ASK and not ctx.tool_call_approved:
            if not deps.is_scope_approved(decision):
                request = ApprovalRequest(
                    session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
                    tool_call_id=call_id, tool_name=name,
                    action_family=action_family(risks), path=decision.path,
                    justification=decision.justification, risks=risks, reason=decision.reason,
                )
                deps.pending_approvals[call_id] = request
                deps.events.append(Event(
                    session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
                    type="approval.requested", payload=request.model_dump(mode="json"),
                ))
                raise ApprovalRequired(metadata={"approval_id": str(request.approval_id)})
        clean_args = dict(tool_args)
        clean_args.pop("justification", None)
        started = time.monotonic()
        deps.events.append(Event(
            session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
            type="tool.started",
            payload={"tool_call_id": call_id, "tool": name, "path": decision.path},
        ))
        try:
            value = await self.wrapped.call_tool(name, clean_args, ctx, tool)
        except Exception as exc:
            deps.events.append(Event(
                session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
                type="tool.failed", payload={"tool_call_id": call_id, "tool": name,
                "duration_ms": (time.monotonic() - started) * 1000, "error": str(exc)},
            ))
            raise
        deps.events.append(Event(
            session_id=deps.session_id, run_id=run_id, agent_id=self.agent_id,
            type="tool.completed", payload={"tool_call_id": call_id, "tool": name,
            "duration_ms": (time.monotonic() - started) * 1000, "result": redact(value)},
        ))
        return value


def _run_uuid(value: str | None, fallback: UUID) -> UUID:
    try:
        return UUID(str(value)) if value else fallback
    except ValueError:
        return fallback
