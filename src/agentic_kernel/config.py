from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import ConfigurationError
from .models import AgentConfig, ProviderRegistry, SkillConfig
from .paths import content_root
from .platform.secure_files import secure_file

NATIVE_RPPL_COMMANDS: dict[str, dict[str, str]] = {
    "/compact": {
        "description": "Compacter manuellement le contexte de la session.",
        "prompt": (
            "Le kernel effectue la compaction manuelle avant cette réponse. "
            "Ne tente pas de compacter ou résumer l’historique toi-même et ne prétends "
            "pas que cette opération est indisponible."
        ),
    },
    "/context": {
        "description": "Afficher l’état mesuré du contexte de la session.",
        "prompt": "",
    },
    "/model-context": {
        "description": "Définir la fenêtre du modèle actif, en tokens.",
        "prompt": "",
    },
    "/reprise": {
        "description": "Reprendre où l’agent en était dans la session courante.",
        "prompt": (
            "Reprends où tu en étais dans cette session. Appuie-toi sur le plan, "
            "les traces, les artefacts et l’état réel du workspace. Ne rejoue pas "
            "les actions déjà terminées. Identifie la dernière étape inachevée, "
            "vérifie ses préconditions puis poursuis jusqu’au prochain résultat "
            "utile. S’il n’existe rien à reprendre, explique-le clairement."
        ),
    },
    "/secret": {
        "description": "Enregistrer localement un secret sans l’envoyer au modèle.",
        "prompt": "",
    },
    "/secret_list": {
        "description": "Lister les noms des secrets disponibles, jamais leurs valeurs.",
        "prompt": "",
    },
}

_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ConfigurationError("agent definition must start with YAML front matter")
    try:
        _, raw_header, body = text.split("---", 2)
    except ValueError as exc:
        raise ConfigurationError("agent definition has unterminated front matter") from exc
    header = yaml.safe_load(raw_header) or {}
    if not isinstance(header, dict):
        raise ConfigurationError("agent front matter must be an object")
    return header, body.strip()


class ProjectConfig:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.content_root = content_root(self.root)
        self.tools_root = self.root / "tools"
        for sensitive in ("providers.json", "secrets.json"):
            path = self.content_root / sensitive
            if path.exists():
                secure_file(path)

    def agent_workspace(self, agent_id: str) -> Path:
        """Return the durable personal workspace owned by an agent.

        A missing logical workspace is not the application source tree.  It is
        resolved here to a visible directory under the user's content root.
        """
        if not _AGENT_ID_PATTERN.fullmatch(agent_id):
            raise ConfigurationError(f"invalid agent id: {agent_id}")
        base = (self.content_root / "workspaces").resolve()
        workspace = (base / agent_id).resolve()
        try:
            workspace.relative_to(base)
        except ValueError as exc:
            raise ConfigurationError(f"invalid agent workspace: {agent_id}") from exc
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolve_workspace(self, agent_id: str, workspace: Path | str | None) -> Path:
        if workspace is None:
            return self.agent_workspace(agent_id)
        return Path(workspace).expanduser().resolve()

    def system_instructions(self) -> str:
        path = self.content_root / "system.md"
        if not path.is_file():
            raise ConfigurationError(f"missing system instructions: {path}")
        return path.read_text(encoding="utf-8").strip()

    def providers(self) -> ProviderRegistry:
        path = self.content_root / "providers.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ProviderRegistry.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ConfigurationError(f"invalid providers file {path}: {exc}") from exc

    def agents(self) -> dict[str, AgentConfig]:
        result: dict[str, AgentConfig] = {}
        default_provider = self.providers().default_provider
        markdown_roots = (
            self.content_root / "agents",
            self.root / ".agents" / "agents",
            self.root / ".claude" / "agents",
        )
        for directory in markdown_roots:
            for path in sorted(directory.glob("*.md")):
                if path.name.endswith("-disabled.md") or path.name == "README.md":
                    continue
                agent = self._markdown_agent(path, default_provider)
                result.setdefault(agent.id, agent)
        for path in sorted((self.root / ".codex" / "agents").glob("*.toml")):
            agent = self._codex_agent(path, default_provider)
            result.setdefault(agent.id, agent)
        self._validate_agent_graph(result)
        return result

    def skills(self, workspace: Path | str | None = None) -> dict[str, SkillConfig]:
        """Discover AMK skills from the project's explicit skill directory only.

        OpenAI and Claude skill packages remain format-compatible, but must be
        copied or installed into content-agents/skills before AMK exposes them.
        This prevents unrelated user-level skills from leaking into a project.
        """
        result: dict[str, SkillConfig] = {}
        roots = [self.content_root / "skills"]
        if workspace is not None:
            local = Path(workspace).expanduser().resolve()
            roots.extend(
                [
                    local / "content-agents" / "skills",
                    local / ".amk" / "skills",
                    local / ".agents" / "skills",
                    local / ".claude" / "skills",
                    local / ".codex" / "skills",
                ]
            )
        seen_roots: set[Path] = set()
        for root in roots:
            resolved_root = root.resolve()
            if resolved_root in seen_roots:
                continue
            seen_roots.add(resolved_root)
            for path in sorted(root.glob("*/SKILL.md")):
                skill = self._skill_from_markdown(path)
                # Global skills load first; a project-local definition with the
                # same name deliberately shadows it for that workspace.
                result[skill.name] = skill
        return result

    def _skill_from_markdown(self, path: Path) -> SkillConfig:
        header, body = split_front_matter(path.read_text(encoding="utf-8"))
        name = header.get("name") or path.parent.name
        description = header.get("description")
        if not description:
            raise ConfigurationError(f"skill {path} is missing description")
        raw_tools = header.get("allowed-tools", [])
        allowed_tools = _string_list(raw_tools)
        try:
            skill = SkillConfig.model_validate(
                {
                    **header,
                    "name": name,
                    "description": description,
                    "instructions": body,
                    "source": str(path),
                    "root": str(path.parent),
                    "allowed_tools": allowed_tools,
                }
            )
        except ValidationError as exc:
            raise ConfigurationError(f"invalid skill {path}: {exc}") from exc
        return skill

    def commands(self, workspace: Path | str | None = None) -> list[dict[str, str]]:
        """Discover global then project-local slash/RPPL commands."""
        by_command: dict[str, dict[str, str]] = {
            command: {
                "command": command,
                "description": definition["description"],
                "kind": "native",
                "skill": "",
                "source": "kernel",
            }
            for command, definition in NATIVE_RPPL_COMMANDS.items()
        }
        for skill in self.skills(workspace).values():
            metadata = skill.model_extra or {}
            amk = metadata.get("amk")
            cody = metadata.get("cody")
            raw = metadata.get("commands")
            if isinstance(amk, dict):
                raw = amk.get("commands", raw)
            if isinstance(cody, dict):
                raw = cody.get("commands", raw)
            descriptions = amk.get("command_descriptions", {}) if isinstance(amk, dict) else {}
            for value in _string_list(raw):
                command = "/" + value.strip().lstrip("/").lower()
                if command == "/":
                    continue
                by_command[command] = {
                    "command": command,
                    "description": str(descriptions.get(command, skill.description)),
                    "kind": "skill",
                    "skill": skill.name,
                    "source": skill.source,
                }
        return [by_command[key] for key in sorted(by_command)]

    def resolve_command(
        self, prompt: str, workspace: Path | str | None = None
    ) -> dict[str, str] | None:
        prefix = prompt.lstrip().split(maxsplit=1)[0].lower() if prompt.strip() else ""
        return next(
            (item for item in self.commands(workspace) if item["command"] == prefix),
            None,
        )

    @staticmethod
    def expand_native_command(prompt: str, command: str) -> str:
        definition = NATIVE_RPPL_COMMANDS.get(command)
        if definition is None:
            return prompt
        stripped = prompt.lstrip()
        suffix = stripped[len(command) :].strip()
        return definition["prompt"] + (
            f"\n\nPrécision de l’utilisateur : {suffix}" if suffix else ""
        )

    def build_skills_index(self) -> dict[str, Any]:
        skills = self.skills()
        payload = {
            "schema_version": 1,
            "skills": [
                {
                    "id": skill.name,
                    "description": skill.description,
                    "source": str(Path(skill.source).relative_to(self.root)),
                    "sha256": hashlib.sha256(Path(skill.source).read_bytes()).hexdigest(),
                }
                for skill in sorted(skills.values(), key=lambda item: item.name)
            ],
        }
        target = self.content_root / "skills" / "index.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return payload

    def disabled_tools(self, agent_id: str) -> set[str]:
        path = self.content_root / "agents" / f"{agent_id}.tools-disabled.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"invalid disabled-tools file {path}: {exc}") from exc
        values = data.get("tools") if isinstance(data, dict) else data
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ConfigurationError(f'{path} must contain a string list or {{"tools": [...]}}')
        return set(values)

    def _markdown_agent(self, path: Path, default_provider: str) -> AgentConfig:
        header, body = split_front_matter(path.read_text(encoding="utf-8"))
        agent_id = header.get("id") or header.get("name") or path.stem
        model = header.get("model")
        provider = header.get("provider") or _provider_for_model(model, default_provider)
        description = header.get("description") or f"Agent loaded from {path.name}"
        declared_tools = _string_list(header.get("tools", []))
        disallowed = _string_list(header.get("disallowedTools", header.get("disallowed-tools", [])))
        compatibility_notes = []
        if declared_tools:
            compatibility_notes.append(
                "Tools requested by the imported agent: " + ", ".join(declared_tools) + "."
            )
        if disallowed:
            compatibility_notes.append(
                "Tools disallowed by the imported agent: " + ", ".join(disallowed) + "."
            )
        if header.get("permissionMode"):
            compatibility_notes.append(f"Imported permission mode: {header['permissionMode']}.")
        instructions = "\n\n".join([body, *compatibility_notes])
        data = {
            "id": agent_id,
            "description": description,
            "provider": provider,
            "model": _normalize_model(model),
            "modules": _string_list(header.get("modules", [])),
            "skills": _string_list(header.get("skills", [])),
            "declared_tools": declared_tools,
            "delegates": _string_list(header.get("delegates", [])),
            "budgets": header.get("budgets"),
            "instructions": instructions,
            "source": str(path),
        }
        try:
            return AgentConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigurationError(f"invalid agent {path}: {exc}") from exc

    def _codex_agent(self, path: Path, default_provider: str) -> AgentConfig:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"invalid Codex agent {path}: {exc}") from exc
        model = data.get("model")
        normalized = {
            "id": data.get("id") or data.get("name") or path.stem,
            "description": data.get("description") or f"Codex agent loaded from {path.name}",
            "provider": data.get("provider") or _provider_for_model(model, default_provider),
            "model": _normalize_model(model),
            "modules": _string_list(data.get("modules", [])),
            "skills": _string_list(data.get("skills", [])),
            "declared_tools": [],
            "delegates": _string_list(data.get("delegates", [])),
            "budgets": data.get("budgets"),
            "instructions": data.get("developer_instructions", ""),
            "source": str(path),
        }
        try:
            return AgentConfig.model_validate(normalized)
        except ValidationError as exc:
            raise ConfigurationError(f"invalid Codex agent {path}: {exc}") from exc

    @staticmethod
    def _validate_agent_graph(agents: dict[str, AgentConfig]) -> None:
        for agent in agents.values():
            missing = set(agent.delegates) - agents.keys()
            if missing:
                raise ConfigurationError(
                    f"agent {agent.id} references unknown delegates: {sorted(missing)}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(agent_id: str) -> None:
            if agent_id in visiting:
                raise ConfigurationError(f"cycle detected at agent {agent_id}")
            if agent_id in visited:
                return
            visiting.add(agent_id)
            for child in agents[agent_id].delegates:
                visit(child)
            visiting.remove(agent_id)
            visited.add(agent_id)

        for agent_id in agents:
            visit(agent_id)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ConfigurationError(f"expected a string or list, got {type(value).__name__}")


def _provider_for_model(model: Any, default: str) -> str:
    if not isinstance(model, str):
        return default
    lowered = model.lower()
    if lowered in {"sonnet", "opus", "haiku"} or lowered.startswith("claude"):
        return "claude"
    if lowered.startswith(("gpt-", "codex", "o1", "o3", "o4")):
        return "openai-codex"
    if ":" in lowered:
        return lowered.split(":", 1)[0]
    return default


def _normalize_model(model: Any) -> str | None:
    if not isinstance(model, str) or model in {"inherit", "default"}:
        return None
    aliases = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-6",
        "haiku": "claude-haiku-4-5",
    }
    normalized = aliases.get(model.lower(), model)
    return normalized.split(":", 1)[1] if ":" in normalized else normalized
