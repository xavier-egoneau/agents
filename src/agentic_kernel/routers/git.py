from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pydantic_ai import Agent, PromptedOutput

from ..config import ProjectConfig
from ..git_service import GitError, GitService
from ..providers import ProviderFactory
from ..secrets import SecretStore


class WorkspacePayload(BaseModel):
    workspace: str = Field(min_length=1)


class SwitchPayload(WorkspacePayload):
    branch: str = Field(min_length=1, max_length=240)


class ProposalPayload(WorkspacePayload):
    provider_id: str | None = None
    model: str | None = None


class CommitPayload(WorkspacePayload):
    message: str = Field(min_length=1, max_length=20_000)
    fingerprint: str = Field(min_length=1)


class GeneratedCommit(BaseModel):
    subject: str = Field(min_length=1, max_length=72)
    body: str = Field(default="", max_length=4_000)


def create_git_router(project: ProjectConfig, service: GitService) -> APIRouter:
    router = APIRouter(prefix="/api/git", tags=["git"])

    @router.get("/status")
    async def status(workspace: str) -> dict:
        return _snapshot(service, workspace).model_dump()

    @router.get("/branches")
    async def branches(workspace: str) -> dict[str, object]:
        try:
            current, names = service.branches(workspace)
            return {"current": current, "branches": names}
        except GitError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/switch")
    async def switch(payload: SwitchPayload) -> dict:
        try:
            return service.switch(payload.workspace, payload.branch).model_dump()
        except GitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/commit-proposal")
    async def proposal(payload: ProposalPayload) -> dict[str, str]:
        snapshot = _snapshot(service, payload.workspace)
        if not snapshot.available or not snapshot.files:
            raise HTTPException(status_code=409, detail="Aucune modification à valider.")
        fallback = service.fallback_message(snapshot)
        try:
            registry = project.providers()
            provider_id = payload.provider_id or registry.default_provider
            configured = next(item for item in registry.providers if item.id == provider_id)
            model = ProviderFactory(
                registry,
                runtime_dir=project.content_root / "runtime" / "providers",
            ).build(provider_id, payload.model or configured.model)
            summary = "\n\n".join(
                (
                    f"FICHIER: {item.path} "
                    f"({item.status}, +{item.additions} -{item.deletions})\n"
                    f"{_prompt_patch(item.path, item.patch)}"
                )
                for item in snapshot.files
            )[:50_000]
            summary = SecretStore(project.content_root / "secrets.json").redact(summary)
            agent = Agent(
                model,
                output_type=PromptedOutput(GeneratedCommit),
                instructions=(
                    "Rédige un message Git couvrant toutes les modifications fournies. "
                    "Sujet impératif présent, concis, sans point final, 72 caractères maximum. "
                    "Le corps explique les groupes de changements importants. Aucun markdown."
                ),
                retries=1,
            )
            generated = (await agent.run(summary)).output
            message = generated.subject.strip()
            if generated.body.strip():
                message += "\n\n" + generated.body.strip()
            source = "model"
        except Exception:
            message, source = fallback, "fallback"
        return {"message": message, "fingerprint": snapshot.fingerprint, "source": source}

    @router.post("/commit")
    async def commit(payload: CommitPayload) -> dict:
        try:
            return service.commit(
                payload.workspace, payload.message, payload.fingerprint
            ).model_dump()
        except GitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


def _snapshot(service: GitService, workspace: str):
    try:
        return service.snapshot(Path(workspace))
    except GitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _prompt_patch(path: str, patch: str) -> str:
    name = Path(path).name.lower()
    sensitive = (
        name.startswith(".env")
        or "secret" in name
        or "credential" in name
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )
    return "[contenu sensible masqué]" if sensitive else patch[:6000]
