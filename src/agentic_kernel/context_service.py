from __future__ import annotations

import json
import math
import os
from collections.abc import Collection
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from pydantic_ai import BinaryContent, ModelMessagesTypeAdapter
from pydantic_ai.messages import ImageUrl, ModelRequest, ModelResponse, TextPart, UserPromptPart

from .errors import ConfigurationError
from .models import Event, ModuleIndex, RunError, RunResult, RunStatus


def effective_context_window(declared: int | None, cap: int | None) -> int | None:
    """Borne la fenêtre déclarée par ce que le serveur alloue vraiment."""
    if cap is None:
        return declared
    if declared is None:
        return cap
    return min(declared, cap)


class ModelContextRegistry:
    """Durable registry for provider/model context-window metadata."""

    def __init__(self, content_root: Path) -> None:
        self.path = content_root / "models-infos.json"

    @staticmethod
    def parse_size(value: str) -> int:
        normalized = value.strip().lower().replace("_", "").replace(" ", "")
        multiplier = 1
        if normalized.endswith("k"):
            normalized, multiplier = normalized[:-1], 1_000
        elif normalized.endswith("m"):
            normalized, multiplier = normalized[:-1], 1_000_000
        try:
            result = int(float(normalized) * multiplier)
        except ValueError as exc:
            raise ValueError("taille absente ou invalide") from exc
        if not 1_024 <= result <= 10_000_000:
            raise ValueError("la taille doit être comprise entre 1 024 et 10 000 000")
        return result

    def get(self, provider_id: str, model_name: str | None) -> int | None:
        if not model_name:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for item in data.get("models", []):
            if (
                isinstance(item, dict)
                and item.get("provider_id") == provider_id
                and item.get("model") == model_name
                and isinstance(item.get("context_window_tokens"), int)
            ):
                return item["context_window_tokens"]
        return None

    def set(self, provider_id: str, model_name: str | None, size: int) -> None:
        if not model_name:
            raise ConfigurationError("le modèle actif n’est pas résolu")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"schema_version": 1, "models": []}
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"registre invalide : {exc}") from exc
        models = [
            item
            for item in data.get("models", [])
            if not (
                item.get("provider_id") == provider_id
                and item.get("model") == model_name
            )
        ]
        models.append(
            {
                "provider_id": provider_id,
                "model": model_name,
                "context_window_tokens": size,
                "source": "user-confirmed-rppl",
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )
        document = {
            "schema_version": 1,
            "models": sorted(
                models,
                key=lambda item: (item["provider_id"], item["model"]),
            ),
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def without_images(messages: list[Any]) -> list[Any]:
    """Keep text history usable when switching from a vision to a text model."""
    sanitized: list[Any] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            sanitized.append(message)
            continue
        parts: list[Any] = []
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, list):
                parts.append(part)
                continue
            content: list[Any] = []
            for item in part.content:
                if isinstance(item, ImageUrl) or (
                    isinstance(item, BinaryContent) and item.is_image
                ):
                    content.append(
                        "[Image jointe omise : le modèle actif ne prend pas en charge la vision.]"
                    )
                else:
                    content.append(item)
            parts.append(replace(part, content=content))
        sanitized.append(replace(message, parts=parts))
    return sanitized


class ContextService:
    """Context measurement and history reconstruction behind the Kernel facade."""

    def __init__(
        self,
        *,
        config,
        events,
        snapshots,
        secrets,
        module_registry,
        workspace_maps,
        registry: ModelContextRegistry,
    ) -> None:
        self.config = config
        self.events = events
        self.snapshots = snapshots
        self.secrets = secrets
        self.module_registry = module_registry
        self.workspace_maps = workspace_maps
        self.registry = registry

    @staticmethod
    def estimate_tokens(value: str) -> int:
        return max(1, math.ceil(len(value.encode("utf-8")) / 3.5))

    def secret_catalog_instruction(self) -> str:
        names = self.secrets.names()
        if not names:
            return ""
        return "\n".join(
            [
                "# Available secret references",
                (
                    "Only these variable names are visible. Their values are held by "
                    "the kernel and must never be requested from secrets.json or "
                    "repeated in arguments, output, or traces."
                ),
                *(f"- `{name}`" for name in names),
            ]
        )

    def overhead_tokens(
        self,
        agent_config,
        skills,
        prompt: str,
        workspace: Path | None = None,
        inlined_skill_names: Collection[str] | None = None,
    ) -> int:
        """Tokens occupés par le prompt avant le premier message de la conversation.

        `inlined_skill_names` dit quelles skills entrent réellement dans le
        prompt de ce run. Compter toutes celles rattachées à l'agent surestimait
        l'occupation dès lors que leur corps est chargé à la demande, et faisait
        déclencher la compaction plus tôt que nécessaire.
        """
        components = [
            self.config.system_instructions(),
            agent_config.instructions,
            prompt,
            self.workspace_maps.build(workspace or self.config.root).render(),
            self.secret_catalog_instruction(),
        ]
        noms = agent_config.skills if inlined_skill_names is None else inlined_skill_names
        components.extend(skills[name].instructions for name in noms if name in skills)
        return self.estimate_tokens("\n".join(components)) + self._tool_schema_tokens(agent_config)

    def _tool_schema_tokens(self, agent_config) -> int:
        """Ce que pèsent les définitions d'outils envoyées au modèle.

        On comptait ici `tools/index.json` en entier : 246 ko, soit 70 500
        tokens. Ce fichier est un catalogue interne — sources, catégories,
        schémas de sortie, métadonnées — dont le modèle ne voit rien. Ce qui
        part sur le fil, c'est le nom, la description et le schéma d'entrée de
        chaque outil exposé : 8 800 tokens pour les 76 outils du dépôt.

        L'écart de 61 700 tokens ne restait pas théorique. Sur une fenêtre de
        65 536, il suffisait à faire dépasser le seuil de compaction avant même
        le premier message, à rendre la cible de réduction négative — donc
        inatteignable — et à faire payer à chaque run un instantané et une passe
        de compaction qui ne réduisaient rien.
        """
        declares = set(agent_config.declared_tools or ())
        desactives = self.config.disabled_tools(agent_config.id)
        # L'index porte déjà les schémas d'entrée résolus depuis les fonctions
        # réelles : c'est la même forme que celle transmise au modèle.
        try:
            index = ModuleIndex.model_validate_json(
                self.module_registry.index_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            return 0
        expose = [
            tool
            for manifest in index.modules
            if manifest.enabled
            for tool in manifest.tools
            if tool.name not in desactives and (not declares or tool.name in declares)
        ]
        return sum(
            self.estimate_tokens(
                json.dumps(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                    ensure_ascii=False,
                )
            )
            for tool in expose
        )

    def status(
        self,
        *,
        session_id,
        provider_id: str,
        model_name: str | None,
        context_window_tokens: int | None = None,
        compaction_threshold_ratio: float = 0.7,
    ) -> dict[str, Any]:
        window = (
            context_window_tokens
            if context_window_tokens is not None
            else self.registry.get(provider_id, model_name)
        )
        estimated = 0
        observed = None
        compaction_count = 0
        calibration_factor = 1.0
        calibration_samples = 0
        session = None
        if session_id is not None:
            projected = self.events.projection.context(session_id)
            if projected is not None:
                estimated = int(projected["estimated_history_tokens"])
                observed = projected["observed_input_tokens"]
                compaction_count = int(projected["compaction_count"])
                calibration_factor = float(projected["calibration_factor"])
                calibration_samples = int(projected["calibration_samples"])
            session = self.events.projection.session(session_id)
        try:
            agent = self.config.agents()["main"]
            workspace = (
                Path(session["workspace"])
                if session and session.get("workspace")
                else None
            )
            skills = self.config.skills(workspace)
            overhead = self.overhead_tokens(agent, skills, "", workspace)
        except (KeyError, OSError, ValueError):
            overhead = 0
        calibrated_history = round(estimated * calibration_factor)
        complete_estimate = calibrated_history + overhead
        gauge_value = int(observed) if isinstance(observed, int) else complete_estimate
        ratio = gauge_value / window if window else None
        return {
            "provider_id": provider_id,
            "model": model_name,
            "context_window_tokens": window,
            "estimated_history_tokens": estimated,
            "estimated_request_tokens": complete_estimate,
            "observed_input_tokens": observed,
            "estimated_ratio": ratio,
            "compaction_threshold_ratio": compaction_threshold_ratio,
            "compaction_count": compaction_count,
            "measurement": "observed" if observed is not None else "estimated",
            "calibration_factor": calibration_factor,
            "calibration_samples": calibration_samples,
        }

    def latest_history(
        self,
        session_id,
        run_id=None,
        agent_id="kernel",
        *,
        context_window_tokens: int | None = None,
        compaction_threshold_ratio: float = 0.7,
        supports_vision: bool = True,
    ):
        events = self.events.read(session_id)
        history: list[Any] = []
        snapshot_index = -1
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            messages = (
                self.snapshots.load(session_id, event.payload)
                if event.type == "messages.snapshot"
                else None
            )
            if messages is not None:
                history = list(ModelMessagesTypeAdapter.validate_python(messages))
                if not supports_vision:
                    history = without_images(history)
                snapshot_index = index
                break
        for event in events[snapshot_index + 1 :]:
            if (
                event.type == "session.started"
                and event.run_id != run_id
                and isinstance(event.payload.get("prompt"), str)
            ):
                history.append(
                    ModelRequest(parts=[UserPromptPart(content=event.payload["prompt"])])
                )
            elif event.type == "routine.notification":
                content = event.payload.get("content")
                if isinstance(content, str) and content:
                    history.append(ModelResponse(parts=[TextPart(content=content)]))
        if context_window_tokens is None and run_id is not None:
            self.events.append(
                Event(
                    session_id=session_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    type="context.window_unknown",
                    payload={"compaction_threshold": compaction_threshold_ratio},
                )
            )
        return history or None

    def run_native_command(
        self,
        *,
        request,
        display_prompt: str,
        command: str,
        provider_id: str,
        model_name: str | None,
        context_window_tokens: int | None,
        workspace: Path,
        server_cap: int | None = None,
        trigger_ratio: float = 0.7,
    ) -> RunResult:
        """Execute deterministic context commands without calling a provider."""
        run_id = uuid4()
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.started",
                payload={
                    "prompt": display_prompt,
                    "skills": request.skills,
                    "resolved_command": command,
                    "workspace": str(workspace),
                    "security_mode": request.security_mode,
                    "provider_id": provider_id,
                    "model": model_name,
                    "reasoning": request.reasoning,
                    "trigger": request.trigger,
                    "cron_job_id": request.cron_job_id,
                    "images": [],
                },
            )
        )
        if command == "/model-context":
            status, output, event_type, event_payload = self._update_window(
                display_prompt=display_prompt,
                command=command,
                provider_id=provider_id,
                model_name=model_name,
                server_cap=server_cap,
                trigger_ratio=trigger_ratio,
            )
        else:
            status = RunStatus.SUCCESS
            output, event_payload = self._inspect(
                session_id=request.session_id,
                provider_id=provider_id,
                model_name=model_name,
                context_window_tokens=context_window_tokens,
                trigger_ratio=trigger_ratio,
            )
            event_type = "context.inspected"
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id="kernel",
                type=event_type,
                payload=event_payload,
            )
        )
        result = RunResult(
            session_id=request.session_id,
            run_id=run_id,
            agent_id=request.agent_id,
            status=status,
            output=output,
            errors=(
                [RunError(type="validation", message=event_payload["error"])]
                if status is RunStatus.FAILED
                else []
            ),
        )
        self.events.append(
            Event(
                session_id=request.session_id,
                run_id=run_id,
                agent_id=request.agent_id,
                type="session.completed",
                payload=result.model_dump(mode="json"),
            )
        )
        return result

    def _update_window(
        self,
        *,
        display_prompt: str,
        command: str,
        provider_id: str,
        model_name: str | None,
        server_cap: int | None = None,
        trigger_ratio: float = 0.7,
    ) -> tuple[RunStatus, str, str, dict[str, Any]]:
        raw_value = display_prompt.lstrip()[len(command) :].strip()
        try:
            size = self.registry.parse_size(raw_value)
            self.registry.set(provider_id, model_name, size)
        except (ValueError, ConfigurationError) as exc:
            return (
                RunStatus.FAILED,
                (
                    f"Impossible de définir la fenêtre : {exc}. "
                    "Utilise par exemple `/model-context 128000`."
                ),
                "context.window_update_failed",
                {"error": str(exc)},
            )
        effective = effective_context_window(size, server_cap)
        output = (
            f"Fenêtre enregistrée pour `{provider_id}/{model_name}` : "
            f"**{size:,} tokens**. La compaction automatique se déclenchera "
            f"à **{math.floor(effective * trigger_ratio):,} tokens** "
            f"({trigger_ratio:.0%} de la fenêtre effective)."
        )
        if effective != size:
            output += (
                f" Attention : le serveur llama.cpp n'alloue que **{server_cap:,} tokens** "
                "(champ « fenêtre de contexte » du provider) et c'est ce plafond qui "
                "s'applique. Augmente-le pour utiliser tout l'espace déclaré."
            )
        return (
            RunStatus.SUCCESS,
            output,
            "context.window_updated",
            {
                "provider_id": provider_id,
                "model": model_name,
                "context_window_tokens": size,
                "source": "rppl:/model-context",
            },
        )

    def _inspect(
        self,
        *,
        session_id,
        provider_id: str,
        model_name: str | None,
        context_window_tokens: int | None,
        trigger_ratio: float = 0.7,
    ) -> tuple[str, dict[str, Any]]:
        events = self.events.read(session_id)
        snapshot = next(
            (
                event
                for event in reversed(events)
                if event.type == "messages.snapshot"
                and self.snapshots.load(session_id, event.payload) is not None
            ),
            None,
        )
        snapshot_messages = (
            self.snapshots.load(session_id, snapshot.payload) if snapshot else []
        )
        estimated = self.estimate_tokens(
            json.dumps(snapshot_messages, ensure_ascii=False)
        )
        ratio = estimated / context_window_tokens if context_window_tokens else None
        compactions = [event for event in events if event.type == "context.compacted"]
        output = "\n".join(
            [
                "### État du contexte",
                "",
                f"- Provider : `{provider_id}`",
                f"- Modèle : `{model_name or 'non résolu'}`",
                (
                    f"- Fenêtre connue : **{context_window_tokens:,} tokens**"
                    if context_window_tokens
                    else "- Fenêtre connue : **non**"
                ),
                f"- Historique estimé : **{estimated:,} tokens**",
                (
                    f"- Occupation estimée : **{ratio:.1%}**"
                    if ratio is not None
                    else "- Occupation estimée : indisponible"
                ),
                (
                    f"- Seuil automatique ({trigger_ratio:.0%}) : "
                    f"**{math.floor(context_window_tokens * trigger_ratio):,} tokens**"
                    if context_window_tokens
                    else "- Seuil automatique : inconnu — utilise `/model-context <tokens>`"
                ),
                f"- Compactions enregistrées : **{len(compactions)}**",
                "- Source de vérité complète : journal JSONL append-only",
            ]
        )
        return output, {
            "provider_id": provider_id,
            "model": model_name,
            "context_window_tokens": context_window_tokens,
            "estimated_history_tokens": estimated,
            "estimated_ratio": ratio,
            "compaction_count": len(compactions),
        }
