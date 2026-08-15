from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_kernel.kanban import KanbanService, KanbanTaskInput, KanbanTaskPatch
from agentic_kernel.routers.kanban import create_kanban_router


def task(workspace: str, title: str, **extra):
    return KanbanTaskInput(
        workspace=workspace,
        title=title,
        prompt=extra.pop("prompt", f"Fais {title}"),
        **extra,
    )


def test_board_is_scoped_by_workspace(tmp_path) -> None:
    service = KanbanService(tmp_path / "state.db")
    service.create(task("a", "A"))
    service.create(task("b", "B"))
    assert [item["title"] for item in service.list("a")] == ["A"]


def test_claim_is_atomic_and_honors_dependencies(tmp_path) -> None:
    service = KanbanService(tmp_path / "state.db")
    first = service.create(task("a", "A", status="ready", auto_run=True))
    second = service.create(
        task("a", "B", status="ready", auto_run=True, dependencies=[first["id"]])
    )
    claimed = service.claim_next("a", "cron")
    assert claimed and claimed["id"] == first["id"]
    assert service.claim_next("a", "other") is None
    service.finish(first["id"], "done")
    assert service.claim_next("a", "other")["id"] == second["id"]


def test_manual_cards_are_not_claimed_by_automatic_dispatcher(tmp_path) -> None:
    service = KanbanService(tmp_path / "state.db")
    service.create(task("a", "A", status="ready", auto_run=False))
    assert service.claim_next("a", "cron") is None
    assert service.claim_next("a", "user", automatic_only=False) is not None


def test_task_can_move_to_ready_after_prompt_is_written(tmp_path) -> None:
    service = KanbanService(tmp_path / "state.db")
    created = service.create(KanbanTaskInput(workspace="a", title="A"))
    updated = service.update(
        created["id"], KanbanTaskPatch(prompt="Fais A", status="ready")
    )
    assert updated["status"] == "ready"


def test_http_board_flow(tmp_path) -> None:
    app = FastAPI()
    app.include_router(create_kanban_router(tmp_path / "state.db"))
    client = TestClient(app)

    created = client.post(
        "/api/kanban/tasks",
        json={
            "workspace": "C:/project",
            "title": "Préparer le prompt",
            "prompt": "Implémente la fonctionnalité",
            "status": "ready",
            "auto_run": True,
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client.get(
        "/api/kanban/tasks", params={"workspace": "C:/project"}
    ).json()[0]["id"] == task_id

    claimed = client.post(
        "/api/kanban/claim-next",
        json={"workspace": "C:/project", "worker_id": "cron"},
    )
    assert claimed.json()["status"] == "running"
    finished = client.post(
        f"/api/kanban/tasks/{task_id}/finish",
        json={"status": "review", "result": "Terminé"},
    )
    assert finished.json()["status"] == "review"
