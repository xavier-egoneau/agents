import json
import subprocess
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_kernel.api import create_app
from agentic_kernel.approvals import ApprovalStore
from agentic_kernel.events import JsonlEventStore
from agentic_kernel.kernel import Kernel
from agentic_kernel.models import ApprovalRequest, Event, RunRequest
from agentic_kernel.modules import ModuleRegistry
from agentic_kernel.plans import PlanService, PlanStepInput
from agentic_kernel.routers.crons import _workflow_basis
from agentic_kernel.scheduler import ROUTINE_INBOX_SESSION_ID, CronJobInput
from agentic_kernel.workflows import (
    AcceptedWorkflow,
    WorkflowDefinition,
    WorkflowProposalService,
    workflow_basis_hash,
)


def accepted_synthesis_workflow(project: Path, body: dict) -> AcceptedWorkflow:
    payload = CronJobInput.model_validate(body)
    basis = _workflow_basis(payload, Kernel(project), timezone="Europe/Paris")
    workflow = WorkflowDefinition.model_validate(
        {
            "schema": "amk.workflow/v1",
            "id": "routine-morning-v1",
            "title": "Morning",
            "status": "ready",
            "execution": {
                "mode": "agent_guided",
                "deviation": "stop_and_report",
                "timezone": "Europe/Paris",
            },
            "permissions": {
                "authority": "kernel_guardian",
                "unlisted": "stop_and_report",
                "declarations": [],
            },
            "missing_dependencies": [],
            "steps": [
                {
                    "id": "summary",
                    "kind": "synthesize",
                    "instructions": "Produire le résumé demandé sans outil.",
                }
            ],
            "output": {"sections": ["Résumé"]},
        }
    )
    return AcceptedWorkflow(workflow=workflow, basis_hash=workflow_basis_hash(basis))


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


def test_api_default_workspace_is_distinct_from_application_root(
    project: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "customer-project"
    workspace.mkdir()

    current = TestClient(create_app(project, workspace)).get("/api/workspaces/current")

    assert current.status_code == 200
    assert current.json()["path"] == str(workspace.resolve())


def test_git_api_is_scoped_to_the_requested_workspace(project: Path, tmp_path: Path) -> None:
    repository = tmp_path / "customer-project"
    workspace = repository / "packages" / "web"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    (workspace / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")

    client = TestClient(create_app(project))
    status = client.get("/api/git/status", params={"workspace": str(workspace)})

    assert status.status_code == 200
    payload = status.json()
    assert payload["repo_root"] == str(repository.resolve())
    assert payload["files"][0]["path"] == "packages/web/app.ts"


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


def test_cron_workflow_can_be_accepted_read_replaced_and_deleted_independently(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    accepted = accepted_synthesis_workflow(project, body)
    created = client.post(
        "/api/crons",
        json={
            **body,
            "accepted_workflow": accepted.model_dump(mode="json", by_alias=True),
        },
    )
    assert created.status_code == 200, created.text
    job = created.json()
    assert job["workflow"]["schema"] == "amk.workflow/v1"
    assert job["workflow_revision"] == 1
    assert job["workflow_basis_hash"] == accepted.basis_hash

    fetched = client.get(f"/api/crons/{job['id']}/workflow")
    assert fetched.status_code == 200
    assert fetched.json()["workflow"] == job["workflow"]
    assert fetched.json()["revision"] == 1

    toggled = client.put(f"/api/crons/{job['id']}", json={**body, "enabled": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["workflow"] == job["workflow"]
    stale_edit = client.put(
        f"/api/crons/{job['id']}",
        json={**body, "enabled": True, "prompt": "A changed request"},
    )
    assert stale_edit.status_code == 409
    assert "régénérer" in stale_edit.json()["detail"]

    changed_body = {**body, "enabled": True, "prompt": "A changed request"}
    changed_accepted = accepted_synthesis_workflow(project, changed_body)
    updated_with_workflow = client.put(
        f"/api/crons/{job['id']}",
        json={
            **changed_body,
            "accepted_workflow": changed_accepted.model_dump(mode="json", by_alias=True),
        },
    )
    assert updated_with_workflow.status_code == 200, updated_with_workflow.text
    assert updated_with_workflow.json()["prompt"] == "A changed request"
    assert updated_with_workflow.json()["workflow_revision"] == 2

    replaced = client.put(
        f"/api/crons/{job['id']}/workflow",
        json={"accepted_workflow": changed_accepted.model_dump(mode="json", by_alias=True)},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["revision"] == 3

    deleted = client.delete(f"/api/crons/{job['id']}/workflow")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert deleted.json()["workflow"] is None
    assert deleted.json()["revision"] == 4
    remaining = next(item for item in client.get("/api/crons").json() if item["id"] == job["id"])
    assert remaining["prompt"] == "A changed request"
    assert remaining["skills"] == []
    assert remaining["workflow"] is None

    session_events = Kernel(project).events.read(job["session_id"])
    assert [event.type for event in session_events].count("approval.grants.revoked") == 3


def test_cron_creation_rejects_a_stale_or_unknown_tool_workflow(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    accepted = accepted_synthesis_workflow(project, body)
    stale = accepted.model_copy(update={"basis_hash": "0" * 64})
    response = client.post(
        "/api/crons",
        json={**body, "accepted_workflow": stale.model_dump(mode="json", by_alias=True)},
    )
    assert response.status_code == 409
    assert client.get("/api/crons").json() == []

    invalid_document = accepted.workflow.model_dump(mode="json", by_alias=True)
    invalid_document["steps"] = [
        {
            "id": "invented",
            "kind": "tool",
            "tool": "invented_tool",
            "args": {"justification": "Tester un outil absent."},
        }
    ]
    invalid_document["permissions"]["declarations"] = [{"tool": "invented_tool"}]
    invalid = AcceptedWorkflow(
        workflow=WorkflowDefinition.model_validate(invalid_document),
        basis_hash=accepted.basis_hash,
    )
    response = client.post(
        "/api/crons",
        json={**body, "accepted_workflow": invalid.model_dump(mode="json", by_alias=True)},
    )
    assert response.status_code == 422
    assert "invented_tool" in response.json()["detail"]
    assert client.get("/api/crons").json() == []


def test_non_ready_workflow_can_be_saved_inactive_but_never_executed(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Calendar review",
        "schedule": "0 9 * * *",
        "prompt": "Summarize my calendar",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": True,
        "auto_resume": True,
    }
    ready = accepted_synthesis_workflow(project, body)
    document = ready.workflow.model_dump(mode="json", by_alias=True)
    document["status"] = "blocked"
    document["missing_dependencies"] = [
        {
            "capability": "calendar.events.list",
            "reason": "Aucun connecteur calendrier n’est configuré.",
        }
    ]
    blocked = AcceptedWorkflow(
        workflow=WorkflowDefinition.model_validate(document),
        basis_hash=ready.basis_hash,
    )
    accepted_payload = blocked.model_dump(mode="json", by_alias=True)

    enabled = client.post(
        "/api/crons",
        json={**body, "accepted_workflow": accepted_payload},
    )
    assert enabled.status_code == 422
    assert "ne peut pas être exécuté" in enabled.json()["detail"]

    created = client.post(
        "/api/crons",
        json={**body, "enabled": False, "accepted_workflow": accepted_payload},
    )
    assert created.status_code == 200, created.text
    job = created.json()
    assert job["workflow"]["status"] == "blocked"

    assert client.post(f"/api/crons/{job['id']}/run").status_code == 422
    reenabled = client.put(
        f"/api/crons/{job['id']}",
        json={**body, "enabled": True},
    )
    assert reenabled.status_code == 422


def test_workflow_proposal_is_returned_without_creating_a_routine_or_skill(
    project: Path,
    monkeypatch,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    creator = project / "content-agents" / "skills" / "workflow-creator"
    creator.mkdir(parents=True)
    (creator / "SKILL.md").write_text(
        """---
name: workflow-creator
description: Proposer un workflow de routine.
---
Conçois seulement le contrat demandé.
""",
        encoding="utf-8",
    )
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }

    async def propose(_self, basis):
        accepted = accepted_synthesis_workflow(project, body)
        assert accepted.basis_hash == workflow_basis_hash(basis)
        return accepted

    monkeypatch.setattr(WorkflowProposalService, "propose", propose)
    client = TestClient(create_app(project))
    response = client.post("/api/crons/workflow-proposals", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["schema"] == "amk.workflow/v1"
    assert client.get("/api/crons").json() == []
    assert sorted(path.name for path in (project / "content-agents" / "skills").iterdir()) == [
        "workflow-creator"
    ]


def test_workflow_replacement_and_deletion_refuse_pending_approvals(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    accepted = accepted_synthesis_workflow(project, body)
    job = client.post(
        "/api/crons",
        json={
            **body,
            "accepted_workflow": accepted.model_dump(mode="json", by_alias=True),
        },
    ).json()
    approval = ApprovalRequest(
        session_id=job["session_id"],
        run_id=uuid4(),
        agent_id="main",
        tool_call_id="call-1",
        tool_name="web_search",
        action_family="network",
        justification="Pending workflow test",
        reason="Approval required",
    )
    ApprovalStore(project / "content-agents" / "sessions").save_state(
        approval,
        {
            "request": RunRequest(
                prompt=body["prompt"],
                session_id=job["session_id"],
                workspace=project,
            ).model_dump(mode="json"),
            "messages": "[]",
        },
    )

    replacement = client.put(
        f"/api/crons/{job['id']}/workflow",
        json={"accepted_workflow": accepted.model_dump(mode="json", by_alias=True)},
    )
    deletion = client.delete(f"/api/crons/{job['id']}/workflow")
    atomic_replacement = client.put(
        f"/api/crons/{job['id']}",
        json={
            **body,
            "accepted_workflow": accepted.model_dump(mode="json", by_alias=True),
        },
    )
    assert replacement.status_code == 409
    assert deletion.status_code == 409
    assert atomic_replacement.status_code == 409
    assert client.get(f"/api/crons/{job['id']}/workflow").json()["workflow"] is not None


def test_accepting_workflow_atomically_supersedes_only_pending_cron_test(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    job = client.post("/api/crons", json=body).json()
    accepted = accepted_synthesis_workflow(project, body)
    request = RunRequest(
        prompt=body["prompt"],
        session_id=job["session_id"],
        workspace=project,
        trigger="cron_test",
        cron_job_id=job["id"],
    )
    run_id = uuid4()
    approvals = [
        ApprovalRequest(
            session_id=job["session_id"],
            run_id=run_id,
            agent_id="main",
            tool_call_id=f"call-{index}",
            tool_name="web_search",
            action_family="network",
            justification="Prévalider le workflow.",
            reason="Approval required",
        )
        for index in range(2)
    ]
    kernel = Kernel(project)
    state = {"request": request.model_dump(mode="json"), "messages": "[]"}
    for approval in approvals:
        kernel.approvals.save_state(approval, state)
    waiting = kernel.approval_service.resolve(approvals[0].approval_id, True)
    assert waiting.status.value == "approval_pending"
    assert client.get(f"/api/crons/{job['id']}/approval-status").json()["approved_scopes"] == [
        {
            "tool_name": "web_search",
            "action_family": "network",
            "path": None,
        }
    ]

    response = client.put(
        f"/api/crons/{job['id']}",
        json={
            **body,
            "accepted_workflow": accepted.model_dump(mode="json", by_alias=True),
        },
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["workflow_revision"] == 1
    assert updated["last_status"] == "cancelled"
    assert updated["workflow_supersession"] == {
        "run_id": str(run_id),
        "status": "cancelled",
        "approval_count": 2,
        "pending_count": 0,
    }
    assert client.get(f"/api/crons/{job['id']}/approval-status").json() == {
        "approved_scopes": [],
        "pending_count": 0,
        "pending_run_id": None,
    }
    assert client.get("/api/approvals").json() == []
    events = Kernel(project).events.read(job["session_id"])
    assert [event.type for event in events].count("approval.superseded") == 2
    assert any(
        event.type == "session.completed" and event.payload["status"] == "cancelled"
        for event in events
    )
    assert events[-1].type == "approval.grants.revoked"


def test_accepting_workflow_never_supersedes_a_scheduled_occurrence(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": [],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    job = client.post("/api/crons", json=body).json()
    accepted = accepted_synthesis_workflow(project, body)
    request = RunRequest(
        prompt=body["prompt"],
        session_id=job["session_id"],
        workspace=project,
        trigger="cron",
        cron_job_id=job["id"],
        cron_occurrence_id="real-occurrence",
    )
    approval = ApprovalRequest(
        session_id=job["session_id"],
        run_id=uuid4(),
        agent_id="main",
        tool_call_id="call-real",
        tool_name="web_search",
        action_family="network",
        justification="Exécuter l'occurrence réelle.",
        reason="Approval required",
    )
    store = ApprovalStore(project / "content-agents" / "sessions")
    store.save_state(
        approval,
        {"request": request.model_dump(mode="json"), "messages": "[]"},
    )
    accepted_payload = accepted.model_dump(mode="json", by_alias=True)

    atomic = client.put(
        f"/api/crons/{job['id']}",
        json={**body, "accepted_workflow": accepted_payload},
    )
    dedicated = client.put(
        f"/api/crons/{job['id']}/workflow",
        json={"accepted_workflow": accepted_payload},
    )

    assert atomic.status_code == 409
    assert dedicated.status_code == 409
    assert store.load_state(approval.approval_id) is not None
    persisted = client.get(f"/api/crons/{job['id']}/workflow").json()
    assert persisted["workflow"] is None
    assert persisted["revision"] == 0


def test_cron_workflow_is_revalidated_before_an_approval_is_consumed(
    project: Path,
) -> None:
    skill_dir = project / "content-agents" / "skills" / "routine-local"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: routine-local
description: Routine-local guidance.
---
Use the original guidance.
""",
        encoding="utf-8",
    )
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))
    body = {
        "name": "Morning",
        "schedule": "0 9 * * *",
        "prompt": "Summarize",
        "workspace": str(project),
        "agent_id": "main",
        "skills": ["routine-local"],
        "security_mode": "limited",
        "enabled": False,
        "auto_resume": True,
    }
    accepted = accepted_synthesis_workflow(project, body)
    job = client.post(
        "/api/crons",
        json={
            **body,
            "accepted_workflow": accepted.model_dump(mode="json", by_alias=True),
        },
    ).json()
    approval = ApprovalRequest(
        session_id=job["session_id"],
        run_id=uuid4(),
        agent_id="main",
        tool_call_id="call-drift",
        tool_name="web_search",
        action_family="network",
        justification="Tester la dérive avant reprise.",
        reason="Approval required",
    )
    store = ApprovalStore(project / "content-agents" / "sessions")
    store.save_state(
        approval,
        {
            "request": RunRequest(
                prompt=body["prompt"],
                session_id=job["session_id"],
                workspace=project,
                skills=["routine-local"],
                trigger="cron_test",
                cron_job_id=job["id"],
                workflow=job["workflow"],
                tool_allowlist=[],
            ).model_dump(mode="json"),
            "messages": "[]",
        },
    )
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace("original", "changed"),
        encoding="utf-8",
    )

    response = client.post(
        f"/api/approvals/{approval.approval_id}/resolve",
        json={"approved": True},
    )

    assert response.status_code == 409
    assert "régénérer" in response.json()["detail"]
    assert store.load_state(approval.approval_id) is not None


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
        assert client.get(f"/api/crons/{job_id}/approval-status").json()["approved_scopes"] == []

    assert [event.type for event in events.read(session_id)].count("approval.grants.revoked") == 3


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


def test_hidden_telegram_session_is_excluded_from_history(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    from agentic_kernel.events import JsonlEventStore

    session_id = uuid4()
    JsonlEventStore(project / "content-agents" / "sessions").append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="session.started",
            payload={
                "prompt": "telegram privé",
                "workspace": None,
                "trigger": "telegram",
                "hidden": True,
            },
        )
    )
    client = TestClient(app)
    assert all(
        item["session_id"] != str(session_id)
        for item in client.get(
            "/api/sessions",
            params={"workspace": str(project), "include_channels": True},
        ).json()
    )
    assert client.get(f"/api/sessions/{session_id}").json()["hidden"] == 1


def test_visible_telegram_session_can_be_aggregated_into_app_catalog(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    from agentic_kernel.events import JsonlEventStore

    session_id = uuid4()
    JsonlEventStore(project / "content-agents" / "sessions").append(
        Event(
            session_id=session_id,
            run_id=uuid4(),
            agent_id="main",
            type="session.started",
            payload={
                "prompt": "telegram visible",
                "workspace": None,
                "trigger": "telegram",
                "hidden": False,
            },
        )
    )
    client = TestClient(app)
    unfiltered = client.get("/api/sessions").json()
    filtered = client.get("/api/sessions", params={"workspace": str(project)}).json()
    app_catalog = client.get(
        "/api/sessions",
        params={"workspace": str(project), "include_channels": True},
    ).json()
    default_workspace = project / "content-agents" / "workspaces" / "main"
    default_filtered = client.get(
        "/api/sessions", params={"workspace": str(default_workspace)}
    ).json()
    assert any(item["session_id"] == str(session_id) for item in unfiltered)
    assert all(item["session_id"] != str(session_id) for item in filtered)
    assert any(item["session_id"] == str(session_id) for item in app_catalog)
    assert any(item["session_id"] == str(session_id) for item in default_filtered)
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["workspace"] is None
    assert detail["workspace_kind"] == "agent_default"
    assert detail["effective_workspace"] == str(
        default_workspace.resolve()
    )


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
    sessions = TestClient(app).get("/api/sessions", params={"workspace": str(project)}).json()
    assert any(
        item["trigger"] == "routine_inbox" and item["workspace"] is None for item in sessions
    )
    inbox = next(item for item in sessions if item["trigger"] == "routine_inbox")
    assert inbox["workspace_kind"] == "agent_default"
    assert inbox["effective_workspace"] == str(
        (project / "content-agents" / "workspaces" / "main").resolve()
    )
    assert all(item["session_id"] != str(automation_session) for item in sessions)


def test_routine_inbox_keeps_its_stable_name_after_a_user_reply(project: Path) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    from agentic_kernel.events import JsonlEventStore

    JsonlEventStore(project / "content-agents" / "sessions").append(
        Event(
            session_id=ROUTINE_INBOX_SESSION_ID,
            run_id=uuid4(),
            agent_id="main",
            type="session.started",
            payload={"prompt": "pourquoi ?", "trigger": "user", "workspace": str(project)},
        )
    )

    sessions = TestClient(app).get("/api/sessions").json()
    inbox = next(item for item in sessions if item["session_id"] == str(ROUTINE_INBOX_SESSION_ID))
    assert inbox["prompt"] == "Routines"
    assert inbox["trigger"] == "routine_inbox"
    assert inbox["workspace"] is None


def test_routine_inbox_can_be_cleared_without_deleting_its_stable_session(
    project: Path,
) -> None:
    ModuleRegistry(project / "tools").build_index()
    app = create_app(project)
    store = JsonlEventStore(project / "content-agents" / "sessions")
    run_id = uuid4()
    store.append(
        Event(
            session_id=ROUTINE_INBOX_SESSION_ID,
            run_id=run_id,
            agent_id="main",
            type="session.started",
            payload={"prompt": "ancien message", "trigger": "user"},
        )
    )
    store.append(
        Event(
            session_id=ROUTINE_INBOX_SESSION_ID,
            run_id=run_id,
            agent_id="main",
            type="session.completed",
            payload={"status": "success", "output": "ancienne réponse"},
        )
    )
    snapshots = (
        project / "content-agents" / "sessions" / "blobs" / str(ROUTINE_INBOX_SESSION_ID)
    )
    snapshots.mkdir(parents=True)
    (snapshots / "obsolete.json.gz").write_bytes(b"obsolete")
    artifacts = (
        project / "content-agents" / "sessions" / "artifacts" / str(ROUTINE_INBOX_SESSION_ID)
    )
    artifacts.mkdir(parents=True)
    (artifacts / "obsolete.png").write_bytes(b"obsolete")

    client = TestClient(app)
    response = client.post(f"/api/sessions/{ROUTINE_INBOX_SESSION_ID}/clear")

    assert response.status_code == 200
    assert response.json()["status"] == "cleared"
    detail = client.get(f"/api/sessions/{ROUTINE_INBOX_SESSION_ID}").json()
    assert detail["trigger"] == "routine_inbox"
    assert detail["prompt"] == "Routines"
    assert detail["messages"] == []
    assert [event["type"] for event in detail["events"]] == ["routine.inbox.created"]
    assert not snapshots.exists()
    assert not artifacts.exists()


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
        "/api/admin/agents",
        json={
            "id": "helper",
            "content": agent_markdown,
            "telegram": {
                "enabled": True,
                "hide_session": True,
                "user_id": "123456",
                "bot_token": "123456:abcdefghijklmnopqrstuvwxyz_ABCD",
            },
        },
    )
    assert created.status_code == 200
    helper = next(
        item for item in client.get("/api/admin/agents").json() if item["id"] == "helper"
    )
    assert helper["telegram"]["enabled"] is True
    assert helper["telegram"]["hide_session"] is True
    assert helper["telegram"]["user_id_configured"] is True
    assert helper["telegram"]["bot_token_configured"] is True
    assert "123456:abcdefghijklmnopqrstuvwxyz_ABCD" not in json.dumps(helper)
    assert client.delete("/api/admin/agents/main").status_code == 403
    assert client.delete("/api/admin/agents/helper").status_code == 200
    secret_names = json.loads(
        (project / "content-agents" / "secrets.json").read_text(encoding="utf-8")
    )
    assert "TELEGRAM_HELPER_USER_ID" not in secret_names
    assert "TELEGRAM_HELPER_BOT_TOKEN" not in secret_names

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


def test_module_settings_are_schema_driven_and_secrets_are_write_only(
    project: Path,
) -> None:
    manifest_path = project / "tools" / "modules" / "clock" / "module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["tools", "config"]
    manifest["config"] = {
        "title": "Clock settings",
        "fields": [
            {
                "name": "timezone",
                "label": "Timezone",
                "type": "text",
                "required": True,
                "default": "UTC",
            },
            {
                "name": "token",
                "label": "Token",
                "type": "secret",
                "secret_name": "CLOCK_TOKEN",
                "required": True,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ModuleRegistry(project / "tools").build_index()
    client = TestClient(create_app(project))

    initial = client.get("/api/admin/module-settings").json()
    assert [item["id"] for item in initial] == ["clock"]
    assert initial[0]["state"] == "required"
    secret_field = next(item for item in initial[0]["fields"] if item["name"] == "token")
    assert secret_field["configured"] is False
    assert "value" not in secret_field

    updated = client.put(
        "/api/admin/module-settings/clock",
        json={"values": {"timezone": "Europe/Paris", "token": "very-secret"}},
    )
    assert updated.status_code == 200
    assert updated.json()["state"] == "configured"
    assert "very-secret" not in updated.text
    assert "very-secret" not in (
        project / "content-agents" / "tool-settings.json"
    ).read_text(encoding="utf-8")
    assert json.loads(
        (project / "content-agents" / "secrets.json").read_text(encoding="utf-8")
    )["CLOCK_TOKEN"] == "very-secret"
    assert client.put(
        "/api/admin/module-settings/clock", json={"values": {"unknown": True}}
    ).status_code == 422
