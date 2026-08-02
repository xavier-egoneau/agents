from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import platform
import shutil
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
    ToolResult,
    ToolRisk,
)
from .network_policy import network_scope

PROTECTED_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".git", ".codex"}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "providers.json",
    "secrets.json",
}
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
    trusted_read_roots: tuple[Path, ...] = (),
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
    raw_paths = [
        arguments[key] for key in ("path", "cwd", "destination") if arguments.get(key) is not None
    ]
    paths = [canonical_path(raw, workspace) for raw in raw_paths]
    path = paths[0] if paths else None
    network_path = next(
        (
            network_scope(value)
            for key in ("url", "seed")
            if isinstance((value := arguments.get(key)), str) and network_scope(value) is not None
        ),
        None,
    )
    if any(candidate is None for candidate in paths):
        verdict, reason = GuardianVerdict.DENY, "The path is invalid or ambiguous."
    elif any(_contains_symlink(raw, workspace) for raw in raw_paths):
        verdict, reason = GuardianVerdict.DENY, "Paths traversing symbolic links are denied."
    elif any(
        candidate is not None
        and (set(candidate.parts) & PROTECTED_PARTS or candidate.name in SENSITIVE_NAMES)
        for candidate in paths
    ):
        verdict, reason = GuardianVerdict.DENY, "Protected or secret paths are denied."
    elif ToolRisk.SECRET in risks or ToolRisk.SYSTEM in risks:
        verdict, reason = GuardianVerdict.DENY, "Secret and system actions are denied."
    elif _targets_private_network(arguments):
        verdict, reason = GuardianVerdict.ASK, "Private or local network targets require approval."
    elif (
        paths
        and ToolRisk.READ in risks
        and not any(risk is not ToolRisk.READ for risk in risks)
        and all(
            candidate is not None
            and any(_inside(candidate, root.resolve()) for root in trusted_read_roots)
            for candidate in paths
        )
    ):
        verdict, reason = (
            GuardianVerdict.ALLOW,
            "Reading a kernel-owned artifact for this session is allowed.",
        )
    elif any(
        candidate is not None and not _inside(candidate, workspace.resolve()) for candidate in paths
    ):
        verdict, reason = GuardianVerdict.ASK, "Actions outside the workspace require approval."
    elif ToolRisk.DESTRUCTIVE in risks:
        verdict, reason = GuardianVerdict.ASK, "Destructive actions always require approval."
    elif ToolRisk.SCREEN in risks:
        verdict, reason = GuardianVerdict.ASK, "Screen capture always requires approval."
    elif ToolRisk.EXECUTE in risks:
        verdict, reason = _review_execution(arguments, mode)
    elif (ToolRisk.NETWORK in risks or ToolRisk.EXTERNAL in risks) and str(
        arguments.get("method", "GET")
    ).upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        verdict, reason = GuardianVerdict.ASK, "Mutating HTTP requests require approval."
    elif ToolRisk.WRITE in risks and mode is SecurityMode.SAFE:
        verdict, reason = GuardianVerdict.ASK, "Writes require approval in safe mode."
    elif (
        ToolRisk.WRITE in risks
        and mode is SecurityMode.LIMITED
        and any(candidate is not None and candidate.exists() for candidate in paths)
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
        path=str(path) if path else network_path,
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
    for risk in (
        ToolRisk.DESTRUCTIVE,
        ToolRisk.EXECUTE,
        ToolRisk.WRITE,
        ToolRisk.READ,
        ToolRisk.NETWORK,
    ):
        if risk in risks:
            return risk.value
    return "other"


def _review_execution(arguments: dict[str, Any], mode: SecurityMode) -> tuple[GuardianVerdict, str]:
    program = str(arguments.get("program", "")).strip()
    executable = Path(program).name.casefold()
    args = [str(value) for value in arguments.get("args", [])]
    lowered = [executable, *(value.casefold() for value in args)]
    command = " ".join(lowered)
    destructive = (
        executable in {"rm", "rmdir", "shred", "mkfs"}
        or "reset --hard" in command
        or "clean -fd" in command
        or "--force" in lowered
    )
    installs = (
        executable in {"pip", "pip3", "brew"}
        and any(value in {"install", "uninstall"} for value in lowered)
    ) or (
        executable in {"npm", "pnpm", "yarn", "bun"}
        and any(value in {"install", "add", "remove", "uninstall"} for value in lowered)
    )
    network_program = executable in {"curl", "wget", "ssh", "scp", "nc", "ncat", "telnet"}
    interpreter_escape = executable in {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
    }
    external_path_argument = any(
        value.startswith(("/", "~/", "../")) or "/../" in value for value in args
    )
    sandbox_available = platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None
    if destructive:
        return GuardianVerdict.ASK, "Potentially destructive command requires approval."
    if mode is SecurityMode.SAFE:
        return GuardianVerdict.ASK, "Command execution requires approval in safe mode."
    if mode is SecurityMode.LIMITED and (
        installs or network_program or interpreter_escape or external_path_argument
    ):
        return (
            GuardianVerdict.ASK,
            "Install, network, interpreter, or external-path commands require approval.",
        )
    if not sandbox_available:
        return (
            GuardianVerdict.ASK,
            "The execution sandbox is unavailable; explicit approval is required.",
        )
    known = {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "node",
        "python",
        "python3",
        "pytest",
        "git",
        "make",
        "cmake",
        "cargo",
        "go",
        "ruff",
        "eslint",
        "tsc",
        "vite",
    }
    if mode is SecurityMode.LIMITED and executable not in known:
        return GuardianVerdict.ASK, "Unknown executable requires approval in limited mode."
    return GuardianVerdict.ALLOW, "Command execution allowed by the active security mode."


def approval_scope(decision: GuardianDecision) -> str:
    payload = {
        "tool": decision.tool_name,
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
    timeouts: dict[str, float | None]

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
        proposed_arguments = redact(tool_args)
        if callable(getattr(deps, "secret_redactor", None)):
            proposed_arguments = deps.secret_redactor(proposed_arguments)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.proposed",
                payload={"tool_call_id": call_id, "tool": name, "arguments": proposed_arguments},
            )
        )
        decision = review_tool_call(
            tool_name=name,
            tool_call_id=call_id,
            agent_id=self.agent_id,
            arguments=tool_args,
            risks=risks,
            mode=deps.security_mode,
            workspace=deps.workspace,
            trusted_read_roots=(
                (deps.state_db.parent / "sessions" / "artifacts" / str(deps.session_id)),
            )
            if deps.state_db is not None
            else (),
        )
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="guardian.reviewed",
                payload=decision.model_dump(mode="json"),
            )
        )
        if decision.verdict is GuardianVerdict.DENY:
            return ToolResult(
                ok=False,
                error={"type": "denied", "message": decision.reason},
                metadata={"guardian": decision.model_dump(mode="json")},
            ).model_dump(mode="json")
        if decision.verdict is GuardianVerdict.ASK and not ctx.tool_call_approved:
            if not deps.is_scope_approved(decision):
                request = ApprovalRequest(
                    session_id=deps.session_id,
                    run_id=run_id,
                    agent_id=self.agent_id,
                    tool_call_id=call_id,
                    tool_name=name,
                    action_family=action_family(risks),
                    path=decision.path,
                    justification=decision.justification,
                    risks=risks,
                    reason=decision.reason,
                )
                deps.pending_approvals[call_id] = request
                deps.events.append(
                    Event(
                        session_id=deps.session_id,
                        run_id=run_id,
                        agent_id=self.agent_id,
                        type="approval.requested",
                        payload=request.model_dump(mode="json"),
                    )
                )
                raise ApprovalRequired(metadata={"approval_id": str(request.approval_id)})
        clean_args = dict(tool_args)
        clean_args.pop("justification", None)
        started = time.monotonic()
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.started",
                payload={"tool_call_id": call_id, "tool": name, "path": decision.path},
            )
        )
        try:
            timeout = self.timeouts.get(name)
            if timeout is None:
                value = await self.wrapped.call_tool(name, clean_args, ctx, tool)
            else:
                async with asyncio.timeout(timeout):
                    value = await self.wrapped.call_tool(name, clean_args, ctx, tool)
            value = ToolResult.model_validate(value).model_dump(mode="json")
        except Exception as exc:
            error_type = (
                "validation"
                if isinstance(exc, (ValueError, TypeError))
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "not_found"
                if isinstance(exc, FileNotFoundError)
                else "execution"
            )
            error_message: Any = str(exc)[:4000]
            if callable(getattr(deps, "secret_redactor", None)):
                error_message = deps.secret_redactor(error_message)
            failure = {
                "ok": False,
                "data": None,
                "error": {"type": error_type, "message": str(error_message)},
                "metadata": {},
            }
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=run_id,
                    agent_id=self.agent_id,
                    type="tool.failed",
                    payload={
                        "tool_call_id": call_id,
                        "tool": name,
                        "duration_ms": (time.monotonic() - started) * 1000,
                        "error": failure["error"],
                    },
                )
            )
            return failure
        if callable(getattr(deps, "secret_redactor", None)):
            value = deps.secret_redactor(value)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=run_id,
                agent_id=self.agent_id,
                type="tool.completed",
                payload={
                    "tool_call_id": call_id,
                    "tool": name,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "result": redact(value),
                },
            )
        )
        return value


def _run_uuid(value: str | None, fallback: UUID) -> UUID:
    try:
        return UUID(str(value)) if value else fallback
    except ValueError:
        return fallback
