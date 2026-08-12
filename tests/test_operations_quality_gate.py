import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agentic_kernel.events import JsonlEventStore
from agentic_kernel.models import Event
from agentic_kernel.plans import PlanService, PlanStepInput


def _operations_module():
    source = Path(__file__).parents[1] / "tools/modules/operations/module.py"
    spec = importlib.util.spec_from_file_location("test_operations_module", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(tmp_path, *, with_visual_check: bool, observation: str = "rendered game"):
    workspace = tmp_path / "web"
    workspace.mkdir()
    (workspace / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
    content = tmp_path / "content"
    events = JsonlEventStore(content / "sessions")
    session_id, root_run_id, child_run_id = uuid4(), uuid4(), uuid4()
    events.append(
        Event(
            session_id=session_id,
            run_id=child_run_id,
            parent_run_id=root_run_id,
            agent_id="dev",
            type="tool.completed",
            payload={
                "tool": "browser_open",
                "result": {"ok": True, "data": {"status_code": 200}},
            },
        )
    )
    if with_visual_check:
        for tool, data in (
            ("browser_screenshot", {"path": "screenshot.png"}),
            ("image_inspect", {"observation": observation}),
        ):
            events.append(
                Event(
                    session_id=session_id,
                    run_id=child_run_id,
                    parent_run_id=root_run_id,
                    agent_id="dev",
                    type="tool.completed",
                    payload={"tool": tool, "result": {"ok": True, "data": data}},
                )
            )
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            state_db=content / "state.db",
            events=events,
            session_id=session_id,
            root_run_id=root_run_id,
            workspace=workspace,
        )
    )
    service = PlanService(ctx.deps.state_db)
    plan = service.create(
        session_id,
        "Web build",
        [PlanStepInput(id="T1", title="Vérification finale et tests")],
    )
    service.claim(
        plan["plan_id"],
        "T1",
        session_id=session_id,
        claimed_by="agent:dev",
        run_id=str(child_run_id),
    )
    service.update(plan["plan_id"], "T1", "validating", session_id=session_id)
    return ctx, plan


async def test_web_quality_gate_rejects_unobserved_render(tmp_path) -> None:
    ctx, plan = _context(tmp_path, with_visual_check=False)

    result = await _operations_module().plan_validate(
        ctx,
        plan["plan_id"],
        "T1",
        passed=True,
        evidence=["page opened"],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "verification_required"
    assert "browser_screenshot" in result["error"]["message"]
    assert "image_inspect" in result["error"]["message"]


async def test_web_quality_gate_accepts_observed_healthy_render(tmp_path) -> None:
    ctx, plan = _context(tmp_path, with_visual_check=True)

    result = await _operations_module().plan_validate(
        ctx,
        plan["plan_id"],
        "T1",
        passed=True,
        evidence=["browser screenshot inspected"],
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"


async def test_web_quality_gate_rejects_black_render(tmp_path) -> None:
    ctx, plan = _context(
        tmp_path,
        with_visual_check=True,
        observation="L'image est presque entièrement noire et le contenu attendu est absent.",
    )

    result = await _operations_module().plan_validate(
        ctx,
        plan["plan_id"],
        "T1",
        passed=True,
        evidence=["capture inspected"],
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "verification_required"
    assert "rendu vide" in result["error"]["message"]
