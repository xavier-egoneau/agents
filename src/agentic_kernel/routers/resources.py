from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import base64
import binascii

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..avatars import AvatarError, AvatarStore
from ..config import ProjectConfig
from ..errors import ConfigurationError
from ..models import ProviderRegistry
from ..module_settings import ModuleSettingsStore
from ..modules import ModuleRegistry
from ..platform.secure_files import secure_file
from ..telegram import TelegramAgentInput, TelegramConfigStore


class MarkdownResourceBody(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    content: str = Field(min_length=1)
    telegram: TelegramAgentInput | None = None


class AvatarBody(BaseModel):
    """Image en base64, comme les pièces jointes du composer.

    Évite `python-multipart`, absent des dépendances, pour un surcoût de 33 %
    sur une image déjà plafonnée à 1 Mo.
    """

    data_base64: str = Field(min_length=1)


class ProviderResourceBody(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    config: dict[str, object]


class ContentHomeBody(BaseModel):
    """Dossier parent de `content-agents`. Vide rétablit l'emplacement par défaut."""

    path: str | None = None


class ModuleSettingsBody(BaseModel):
    values: dict[str, object]


def _atomic_markdown_write(
    target: Path,
    content: str,
    validate: Callable[[], None],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.read_bytes() if target.exists() else None
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, target)
    try:
        validate()
    except Exception:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            rollback = target.with_suffix(target.suffix + ".rollback")
            rollback.write_bytes(previous)
            os.replace(rollback, target)
        raise


def _knowledge_library(project: ProjectConfig, agent_id: str):
    """Bibliothèque d'un orchestrateur, résolue comme côté outils.

    Même règle que dans le module `memory` : sans identifiant utilisable, on
    retombe sur le dossier partagé plutôt que de bâtir un chemin depuis une
    chaîne arbitraire.
    """
    from ..knowledge import KnowledgeLibrary

    racine = project.content_root
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", agent_id or ""):
        return KnowledgeLibrary(racine / "knowledge")
    return KnowledgeLibrary(racine / "workspaces" / agent_id / "knowledge")


def _validate_agent(project: ProjectConfig, agent_id: str, target: Path) -> None:
    agent = project.agents().get(agent_id)
    if agent is None or Path(agent.source).resolve() != target.resolve():
        raise ConfigurationError(f"Le front matter doit déclarer exactement id: {agent_id}")


def _validate_skill(project: ProjectConfig, skill_id: str, target: Path) -> None:
    skill = project.skills().get(skill_id)
    if skill is None or Path(skill.source).resolve() != target.resolve():
        raise ConfigurationError(f"Le front matter doit déclarer exactement name: {skill_id}")


def create_resource_router(
    project: ProjectConfig,
    telegram_store: TelegramConfigStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["resources"])
    telegram = telegram_store or TelegramConfigStore(project.content_root)
    module_settings = ModuleSettingsStore(
        project.content_root,
        ModuleRegistry(project.tools_root),
    )

    @router.get("/content-home")
    async def content_home() -> dict[str, object]:
        from ..paths import configured_home, data_home

        return {
            "current": str(project.content_root),
            "configured": str(configured_home()) if configured_home() else None,
            "default": str(data_home().resolve()),
            # Une variable d'environnement prime sur le réglage : le dire évite
            # de chercher pourquoi une modification ne prend jamais effet.
            "environment_override": os.environ.get("AMK_HOME"),
        }

    @router.put("/content-home")
    async def set_content_home(payload: ContentHomeBody) -> dict[str, object]:
        from ..paths import normalized_home, store_home

        target = (payload.path or "").strip()
        if not target:
            store_home(None)
            return {"path": None, "restart_required": True}
        selected = Path(target).expanduser()
        try:
            selected = selected.resolve()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail="Chemin illisible") from exc
        if not selected.is_dir():
            raise HTTPException(status_code=422, detail=f"Dossier introuvable : {selected}")
        # Pointer sur un `content-agents` existant est le geste naturel : on le
        # ramène à son parent au lieu de créer `content-agents/content-agents`.
        selected = normalized_home(selected)
        # Le dossier doit être inscriptible : découvrir l'inverse au prochain
        # démarrage laisserait AMK sans données et sans explication.
        probe = selected / ".amk-write-test"
        try:
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=422, detail=f"Dossier non inscriptible : {selected}"
            ) from exc
        store_home(selected)
        return {"path": str(selected), "restart_required": True}

    @router.get("/knowledge")
    async def knowledge_inventory(agent_id: str = "main") -> dict[str, object]:
        """Index de la bibliothèque d'un orchestrateur.

        `agent_id` désigne la racine du run à venir : chaque orchestrateur a son
        corpus, et un sous-agent n'en a jamais un à lui.
        """
        from ..knowledge import KnowledgeLibrary

        if agent_id not in project.agents():
            raise HTTPException(status_code=404, detail=f"Agent inconnu : {agent_id}")
        library = _knowledge_library(project, agent_id)
        pages = library.inventory()
        return {
            "agent_id": agent_id,
            "pages": pages,
            "tags": sorted(KnowledgeLibrary(library.root).known_tags()),
            # La somme sert à prévenir avant d'injecter : une sélection large
            # peut peser plus que la fenêtre du modèle.
            "total_bytes": sum(int(page["bytes"]) for page in pages),
        }

    @router.get("/module-settings")
    async def configurable_modules() -> list[dict[str, object]]:
        try:
            return module_settings.list()
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/module-settings/{module_id}")
    async def update_module_settings(
        module_id: str, payload: ModuleSettingsBody
    ) -> dict[str, object]:
        try:
            return module_settings.update(module_id, payload.values)
        except (ConfigurationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    avatars = AvatarStore(project.content_root)

    @router.get("/agents")
    async def agents() -> list[dict[str, object]]:
        managed_root = (project.content_root / "agents").resolve()
        with_avatar = avatars.agents_with_avatar()
        return [
            {
                "id": agent.id,
                "description": agent.description,
                "content": Path(agent.source).read_text(encoding="utf-8"),
                "telegram": telegram.view(agent.id),
                "has_avatar": agent.id in with_avatar,
                "subagent": agent.subagent,
            }
            for agent in project.agents().values()
            if Path(agent.source).resolve().parent == managed_root
        ]

    @router.get("/agents/{agent_id}/avatar")
    async def get_agent_avatar(agent_id: str) -> FileResponse:
        stored = avatars.find(agent_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Aucun avatar pour cet agent")
        return FileResponse(
            stored.path,
            media_type=stored.media_type,
            # Le nom de fichier ne change pas quand l'image change : sans
            # revalidation, le navigateur servirait l'ancienne indéfiniment.
            headers={"Cache-Control": "no-cache"},
        )

    @router.put("/agents/{agent_id}/avatar")
    async def set_agent_avatar(agent_id: str, payload: AvatarBody) -> dict[str, object]:
        if agent_id not in project.agents():
            raise HTTPException(status_code=404, detail=f"Agent inconnu : {agent_id}")
        try:
            raw = base64.b64decode(payload.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="Image illisible") from exc
        try:
            stored = avatars.save(agent_id, raw)
        except AvatarError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": agent_id, "media_type": stored.media_type}

    @router.delete("/agents/{agent_id}/avatar")
    async def delete_agent_avatar(agent_id: str) -> dict[str, object]:
        try:
            removed = avatars.delete(agent_id)
        except AvatarError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": agent_id, "deleted": removed}

    @router.post("/agents")
    async def create_agent(payload: MarkdownResourceBody) -> dict[str, str]:
        target = project.content_root / "agents" / f"{payload.id}.md"
        if target.exists():
            raise HTTPException(status_code=409, detail=f"L’agent {payload.id} existe déjà")
        try:
            if payload.telegram is not None:
                telegram.validate(payload.id, payload.telegram)
            _atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_agent(project, payload.id, target),
            )
            if project.agents()[payload.id].user_memory:
                project.ensure_agent_memory(payload.id)
            if payload.telegram is not None:
                telegram.update(payload.id, payload.telegram)
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @router.put("/agents/{agent_id}")
    async def update_agent(
        agent_id: str, payload: MarkdownResourceBody
    ) -> dict[str, str]:
        if payload.id != agent_id:
            raise HTTPException(
                status_code=422,
                detail="Le renommage d’un agent n’est pas implicite",
            )
        target = project.content_root / "agents" / f"{agent_id}.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Agent introuvable")
        try:
            if payload.telegram is not None:
                telegram.validate(agent_id, payload.telegram)
            _atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_agent(project, agent_id, target),
            )
            if project.agents()[agent_id].user_memory:
                project.ensure_agent_memory(agent_id)
            if payload.telegram is not None:
                telegram.update(agent_id, payload.telegram)
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": agent_id, "status": "updated"}

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> dict[str, str]:
        if agent_id == "main":
            raise HTTPException(status_code=403, detail="L’agent main ne peut pas être supprimé")
        references = [
            agent.id for agent in project.agents().values() if agent_id in agent.delegates
        ]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Agent encore référencé par : {', '.join(references)}",
            )
        target = project.content_root / "agents" / f"{agent_id}.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Agent introuvable")
        target.unlink()
        telegram.delete(agent_id)
        # Sans ça, recréer un agent du même nom hériterait de l'ancienne image.
        avatars.delete(agent_id)
        (project.content_root / "agents" / f"{agent_id}.tools-disabled.json").unlink(
            missing_ok=True
        )
        return {"id": agent_id, "status": "deleted"}

    @router.get("/skills")
    async def skills() -> list[dict[str, str]]:
        return [
            {
                "id": skill.name,
                "description": skill.description,
                "content": Path(skill.source).read_text(encoding="utf-8"),
            }
            for skill in project.skills().values()
        ]

    @router.post("/skills")
    async def create_skill(payload: MarkdownResourceBody) -> dict[str, str]:
        target = project.content_root / "skills" / payload.id / "SKILL.md"
        if target.exists():
            raise HTTPException(status_code=409, detail=f"La skill {payload.id} existe déjà")
        try:
            _atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_skill(project, payload.id, target),
            )
            project.build_skills_index()
        except ConfigurationError as exc:
            if target.parent.exists() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @router.put("/skills/{skill_id}")
    async def update_skill(
        skill_id: str, payload: MarkdownResourceBody
    ) -> dict[str, str]:
        if payload.id != skill_id:
            raise HTTPException(
                status_code=422,
                detail="Le renommage d’une skill n’est pas implicite",
            )
        target = project.content_root / "skills" / skill_id / "SKILL.md"
        if not target.exists():
            raise HTTPException(status_code=404, detail="Skill introuvable")
        try:
            _atomic_markdown_write(
                target,
                payload.content,
                lambda: _validate_skill(project, skill_id, target),
            )
            project.build_skills_index()
        except ConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": skill_id, "status": "updated"}

    @router.delete("/skills/{skill_id}")
    async def delete_skill(skill_id: str) -> dict[str, str]:
        references = [
            agent.id for agent in project.agents().values() if skill_id in agent.skills
        ]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Skill encore référencée par : {', '.join(references)}",
            )
        target = project.content_root / "skills" / skill_id
        if not (target / "SKILL.md").exists():
            raise HTTPException(status_code=404, detail="Skill introuvable")
        shutil.rmtree(target)
        project.build_skills_index()
        return {"id": skill_id, "status": "deleted"}

    def provider_document() -> dict[str, object]:
        return json.loads(
            (project.content_root / "providers.json").read_text(encoding="utf-8")
        )

    def save_provider_document(document: dict[str, object]) -> None:
        ProviderRegistry.model_validate(document)
        target = project.content_root / "providers.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        secure_file(target)

    @router.get("/providers")
    async def providers() -> dict[str, object]:
        document = provider_document()
        sanitized = []
        for raw in document.get("providers", []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["api_key_configured"] = bool(item.get("api_key"))
            item["api_key"] = ""
            sanitized.append(item)
        return {
            "default_provider": document.get("default_provider"),
            "providers": sanitized,
        }

    @router.post("/providers")
    async def create_provider(payload: ProviderResourceBody) -> dict[str, str]:
        document = provider_document()
        providers = document.get("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=422, detail="Registre providers invalide")
        if any(isinstance(item, dict) and item.get("id") == payload.id for item in providers):
            raise HTTPException(status_code=409, detail="Provider déjà existant")
        config = {**payload.config, "id": payload.id}
        config.pop("api_key_configured", None)
        providers.append(config)
        try:
            save_provider_document(document)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": payload.id, "status": "created"}

    @router.put("/providers/{provider_id}")
    async def update_provider(
        provider_id: str, payload: ProviderResourceBody
    ) -> dict[str, str]:
        if payload.id != provider_id:
            raise HTTPException(status_code=422, detail="Le renommage n’est pas implicite")
        document = provider_document()
        providers = document.get("providers", [])
        for index, existing in enumerate(providers):
            if isinstance(existing, dict) and existing.get("id") == provider_id:
                update = dict(payload.config)
                update.pop("api_key_configured", None)
                if not update.get("api_key"):
                    update["api_key"] = existing.get("api_key")
                providers[index] = {**existing, **update, "id": provider_id}
                break
        else:
            raise HTTPException(status_code=404, detail="Provider introuvable")
        try:
            save_provider_document(document)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"id": provider_id, "status": "updated"}

    @router.delete("/providers/{provider_id}")
    async def delete_provider(provider_id: str) -> dict[str, str]:
        document = provider_document()
        if document.get("default_provider") == provider_id:
            raise HTTPException(
                status_code=409,
                detail="Le provider par défaut ne peut pas être supprimé",
            )
        references = [
            agent.id for agent in project.agents().values() if agent.provider == provider_id
        ]
        if references:
            raise HTTPException(
                status_code=409,
                detail=f"Provider encore utilisé par : {', '.join(references)}",
            )
        providers = document.get("providers", [])
        remaining = [
            item
            for item in providers
            if not isinstance(item, dict) or item.get("id") != provider_id
        ]
        if len(remaining) == len(providers):
            raise HTTPException(status_code=404, detail="Provider introuvable")
        document["providers"] = remaining
        save_provider_document(document)
        return {"id": provider_id, "status": "deleted"}

    return router
