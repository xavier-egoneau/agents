from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_kernel.api import create_app
from agentic_kernel.kernel import Kernel
from agentic_kernel.models import Event, RunResult, RunStatus
from agentic_kernel.modules import ModuleRegistry


def _client(project: Path) -> TestClient:
    ModuleRegistry(project / "tools").build_index()
    return TestClient(create_app(project))


def test_api_requires_the_token_when_configured(project: Path, monkeypatch) -> None:
    """Sans jeton, n'importe quel processus local pilote runs, fichiers et purge."""
    monkeypatch.setenv("AMK_API_TOKEN", "jeton-local-de-test")
    client = _client(project)

    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"x-amk-token": "mauvais"}).status_code == 401
    assert client.get("/api/catalog").status_code == 401
    valid = client.get("/api/health", headers={"x-amk-token": "jeton-local-de-test"})
    assert valid.status_code == 200


def test_api_is_open_when_no_token_is_configured(project: Path) -> None:
    client = _client(project)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/catalog").status_code == 200


def test_web_run_carries_the_knowledge_selection(project: Path, monkeypatch) -> None:
    """La surface envoie knowledge_mode/knowledge_pages : le routeur les portait
    nulle part, Pydantic les ignorait sans bruit et la bibliothèque restait `off`."""
    from agentic_kernel import kernel as kernel_module

    captured: dict[str, object] = {}

    async def fake_run(self, request):
        captured["knowledge_mode"] = request.knowledge_mode
        captured["knowledge_pages"] = request.knowledge_pages
        return RunResult(
            session_id=request.session_id,
            run_id=uuid4(),
            agent_id=request.agent_id,
            status=RunStatus.SUCCESS,
            output="ok",
        )

    monkeypatch.setattr(kernel_module.Kernel, "run", fake_run)
    client = _client(project)

    response = client.post(
        "/api/runs",
        json={
            "prompt": "Résume la page",
            "knowledge_mode": "manual",
            "knowledge_pages": ["page-un"],
        },
    )

    assert response.status_code == 200
    assert captured == {"knowledge_mode": "manual", "knowledge_pages": ["page-un"]}


def test_resume_without_a_start_event_is_a_conflict_not_a_crash(project: Path) -> None:
    """`next(...)` sur une session sans `session.started` levait StopIteration :
    un 500 sans diagnostic au lieu d'un refus expliqué."""
    session_id = uuid4()
    kernel = Kernel(project)
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="session.completed",
            payload={"status": "failed"},
        )
    )
    client = _client(project)

    response = client.post(f"/api/runs/{session_id}/resume", json={})

    assert response.status_code == 409
    assert "démarrage" in response.json()["detail"]
