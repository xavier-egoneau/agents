import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_kernel.api import create_app
from agentic_kernel.models import Event
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.plans import PlanService, PlanStepInput


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


def test_commands_merge_global_and_workspace_local_skills(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    global_skill = project / "content-agents" / "skills" / "plan-build"
    local_skill = project / ".amk" / "skills" / "review"
    global_skill.mkdir(parents=True)
    local_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text(
        """---
name: plan-build
description: Planifier et construire.
amk:
  commands: [/plan, /build]
---
Use the selected mode.
""",
        encoding="utf-8",
    )
    (local_skill / "SKILL.md").write_text(
        """---
name: review
description: Revoir le projet local.
amk:
  commands: [/review]
---
Review locally.
""",
        encoding="utf-8",
    )
    commands = TestClient(create_app(project)).get(
        "/api/commands", params={"workspace": str(project)}
    )
    assert commands.status_code == 200
    assert [item["command"] for item in commands.json()] == [
        "/build",
        "/compact",
        "/context",
        "/model-context",
        "/plan",
        "/reprise",
        "/review",
        "/secret",
        "/secret_list",
    ]


def test_plan_api_uses_shared_plan_service(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    session_id = uuid4()
    plan = PlanService(project / "content-agents" / "state.db").create(
        session_id,
        "Shared plan",
        [
            PlanStepInput(id="T1", title="First"),
            PlanStepInput(id="T2", title="Second", dependencies=["T1"]),
        ],
    )
    client = TestClient(create_app(project))
    current = client.get("/api/plans/current", params={"session_id": str(session_id)})
    assert current.status_code == 200
    assert current.json()["plan_id"] == plan["plan_id"]
    blocked = client.patch(
        f"/api/plans/{plan['plan_id']}/steps/T2",
        json={"status": "completed"},
    )
    assert blocked.status_code == 409
    assert (
        client.patch(
            f"/api/plans/{plan['plan_id']}/steps/T1",
            json={"status": "completed"},
        ).status_code
        == 200
    )


def test_cron_api_crud(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Review",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": True,
        "auto_resume": True,
    }
    created = client.post("/api/crons", json=body)
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert client.get("/api/crons").json()[0]["id"] == job_id
    body["enabled"] = False
    assert client.put(f"/api/crons/{job_id}", json=body).json()["enabled"] is False
    assert client.delete(f"/api/crons/{job_id}").status_code == 200


def test_session_history_api(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    session_id = uuid4()
    run_id = uuid4()
    from agentic_kernel.events import JsonlEventStore

    store = JsonlEventStore(project / "content-agents" / "sessions")
    store.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="session.started",
            payload={"prompt": "hello", "workspace": str(project)},
        )
    )
    store.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="session.completed",
            payload={"status": "success", "output": "world"},
        )
    )
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
    created = client.post("/api/admin/agents", json={"id": "helper", "content": agent_markdown})
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
    index = json.loads((project / "content-agents" / "skills" / "index.json").read_text())
    assert index["skills"][0]["id"] == "review"
    assert (
        client.put(
            "/api/admin/skills/review",
            json={"id": "review", "content": skill_markdown.replace("Check", "Inspect")},
        ).status_code
        == 200
    )
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
