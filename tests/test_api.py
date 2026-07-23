import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_kernel.api import create_app
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.models import Event


def test_health_and_catalog(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    assert client.get("/api/health").json() == {"status": "ok"}
    response = client.get("/api/catalog")
    assert response.status_code == 200
    assert response.json()["agents"][0]["id"] == "main"
    assert response.json()["default_provider"] == "test"
    assert client.get("/api/approvals").json() == []
    current = client.get("/api/workspaces/current")
    assert current.status_code == 200
    assert current.json()["path"] == str(project.resolve())
    validated = client.post("/api/workspaces/validate", json={"path": str(project)})
    assert validated.status_code == 200
    missing = client.post("/api/workspaces/validate", json={"path": str(project / "missing")})
    assert missing.status_code == 422


def test_session_history_api(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    session_id = uuid4()
    run_id = uuid4()
    app.state  # keep a concrete app before entering the client context
    from agentic_kernel.events import JsonlEventStore
    store = JsonlEventStore(project / "content-agents" / "sessions")
    store.append(Event(session_id=session_id, run_id=run_id, agent_id="main",
                       type="session.started", payload={"prompt": "hello", "workspace": str(project)}))
    store.append(Event(session_id=session_id, run_id=run_id, agent_id="main",
                       type="session.completed", payload={"status": "success", "output": "world"}))
    client = TestClient(app)
    history = client.get("/api/sessions").json()
    assert history[0]["session_id"] == str(session_id)
    assert history[0]["prompt"] == "hello"
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["output"] == "world"
    assert len(detail["events"]) == 2
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_markdown_agent_and_skill_crud(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    agent_markdown = """---
id: helper
description: Helper agent
provider: test
modules: []
skills: []
delegates: []
---
Help carefully.
"""
    created = client.post(
        "/api/admin/agents", json={"id": "helper", "content": agent_markdown}
    )
    assert created.status_code == 200
    assert any(item["id"] == "helper" for item in client.get("/api/admin/agents").json())
    assert client.delete("/api/admin/agents/main").status_code == 403
    assert client.delete("/api/admin/agents/helper").status_code == 200

    skill_markdown = """---
name: review
description: Review carefully
---
Check the work.
"""
    created_skill = client.post(
        "/api/admin/skills", json={"id": "review", "content": skill_markdown}
    )
    assert created_skill.status_code == 200
    index = json.loads(
        (project / "content-agents" / "skills" / "index.json").read_text()
    )
    assert index["skills"][0]["id"] == "review"
    assert client.put(
        "/api/admin/skills/review",
        json={"id": "review", "content": skill_markdown.replace("Check", "Inspect")},
    ).status_code == 200
    assert client.delete("/api/admin/skills/review").status_code == 200


def test_provider_crud_masks_keys_and_protects_default(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    created = client.post(
        "/api/admin/providers",
        json={
            "id": "secondary",
            "config": {
                "kind": "deepseek",
                "connection_type": "api_key",
                "model": "model-a",
                "models": ["model-a", "model-b"],
                "api_key": "secret-value",
            },
        },
    )
    assert created.status_code == 200
    admin = client.get("/api/admin/providers").json()
    secondary = next(item for item in admin["providers"] if item["id"] == "secondary")
    assert secondary["api_key"] == ""
    assert secondary["api_key_configured"] is True
    assert client.delete("/api/admin/providers/test").status_code == 409
    assert client.delete("/api/admin/providers/secondary").status_code == 200
