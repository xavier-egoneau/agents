import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_kernel.api import create_app
from agentic_kernel.approvals import ApprovalStore
from agentic_kernel.models import ApprovalRequest, Event, RunRequest
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.plans import PlanService, PlanStepInput


def test_health_and_catalog(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert set(health["scheduler"]) == {
        "running",
        "last_tick_at",
        "last_tick_age_seconds",
        "due_count",
    }
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


def test_completed_plan_is_archived_from_composer_state(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    session_id = uuid4()
    service = PlanService(project / "content-agents" / "state.db")
    plan = service.create(
        session_id,
        "Finished iteration",
        [PlanStepInput(id="T1", title="Done")],
    )
    service.update(plan["plan_id"], "T1", "completed", session_id=session_id)

    response = TestClient(create_app(project)).get(
        "/api/plans/current", params={"session_id": str(session_id)}
    )

    assert response.status_code == 200
    assert response.json() is None


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


def test_cron_permission_changes_revoke_durable_approval_grants(project: Path) -> None:
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
    session_id = created.json()["session_id"]

    from agentic_kernel.events import JsonlEventStore

    events = JsonlEventStore(project / "content-agents" / "sessions")

    def grant_network_scope() -> None:
        events.append(
            Event(
                session_id=session_id,
                run_id=uuid4(),
                agent_id="main",
                type="approval.resolved",
                payload={
                    "approval_id": str(uuid4()),
                    "approved": True,
                    "tool_name": "web_search",
                    "action_family": "network",
                    "path": None,
                },
            )
        )
        status = client.get(f"/api/crons/{job_id}/approval-status")
        assert status.status_code == 200
        assert status.json()["approved_scopes"] == [
            {
                "tool_name": "web_search",
                "action_family": "network",
                "path": None,
            }
        ]

    other_workspace = project / "other-workspace"
    other_workspace.mkdir()
    for change in (
        {"prompt": "Review with a new instruction"},
        {"workspace": str(other_workspace)},
        {"model": "another-model"},
    ):
        grant_network_scope()
        body.update(change)
        updated = client.put(f"/api/crons/{job_id}", json=body)
        assert updated.status_code == 200
        assert (
            client.get(f"/api/crons/{job_id}/approval-status")
            .json()["approved_scopes"]
            == []
        )

    assert [event.type for event in events.read(session_id)].count(
        "approval.grants.revoked"
    ) == 3


def test_mixed_run_approval_batch_returns_structured_conflict(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    store = ApprovalStore(project / "content-agents" / "sessions")
    request = RunRequest(prompt="Test routine", workspace=project)
    approvals = [
        ApprovalRequest(
            session_id=request.session_id,
            run_id=uuid4(),
            agent_id="main",
            tool_call_id=f"call-{index}",
            tool_name="web_search",
            action_family="network",
            justification="Tester la prévalidation",
            reason="Network approval required",
        )
        for index in range(2)
    ]
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    for approval in approvals:
        store.save_state(approval, state)

    response = TestClient(create_app(project)).post(
        "/api/approvals/resolve-batch",
        json={
            "approval_ids": [str(item.approval_id) for item in approvals],
            "approved": True,
        },
    )
    assert response.status_code == 409
    assert "same run" in response.json()["detail"]


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


def test_session_history_has_global_routine_inbox_and_hides_execution_sessions(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    from agentic_kernel.events import JsonlEventStore

    store = JsonlEventStore(project / "content-agents" / "sessions")
    automation_session = uuid4()
    store.append(
        Event(
            session_id=automation_session,
            run_id=uuid4(),
            agent_id="main",
            type="session.started",
            payload={
                "prompt": "hidden routine execution",
                "workspace": str(project),
                "trigger": "cron",
            },
        )
    )
    sessions = TestClient(app).get(
        "/api/sessions", params={"workspace": str(project)}
    ).json()
    assert any(
        item["trigger"] == "routine_inbox" and item["workspace"] is None
        for item in sessions
    )
    assert all(item["session_id"] != str(automation_session) for item in sessions)


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
