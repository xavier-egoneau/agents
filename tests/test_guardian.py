from pathlib import Path

from agentic_kernel.guardian import review_tool_call
from agentic_kernel.models import GuardianVerdict, SecurityMode, ToolRisk


def decide(tmp_path: Path, tool: str, risks: list[ToolRisk], mode: SecurityMode, path: str):
    return review_tool_call(
        tool_name=tool,
        tool_call_id="call-1",
        agent_id="main",
        arguments={"path": path, "justification": "Required by the user."},
        risks=risks,
        mode=mode,
        workspace=tmp_path,
    )


def test_filesystem_permission_matrix(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("old")
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.SAFE, "existing.txt").verdict
        == GuardianVerdict.ALLOW
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.SAFE, "new.txt").verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.LIMITED, "new.txt").verdict
        == GuardianVerdict.ALLOW
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.LIMITED, "existing.txt").verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "write", [ToolRisk.WRITE], SecurityMode.POWER, "existing.txt").verdict
        == GuardianVerdict.ALLOW
    )
    for mode in SecurityMode:
        assert (
            decide(tmp_path, "delete", [ToolRisk.DESTRUCTIVE], mode, "existing.txt").verdict
            == GuardianVerdict.ASK
        )


def test_outside_and_protected_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, str(outside)).verdict
        == GuardianVerdict.ASK
    )
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, ".ssh/id_rsa").verdict
        == GuardianVerdict.DENY
    )
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    assert (
        decide(tmp_path, "read", [ToolRisk.READ], SecurityMode.POWER, "link").verdict
        == GuardianVerdict.DENY
    )


def test_kernel_owned_session_artifact_is_readable_without_ask(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    artifact_root = tmp_path / "content-agents" / "sessions" / "artifacts" / "session-1"
    artifact = artifact_root / "run-1" / "screen.png"
    workspace.mkdir()
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"png")

    decision = review_tool_call(
        tool_name="image_inspect",
        tool_call_id="call-image",
        agent_id="main",
        arguments={"path": str(artifact), "justification": "Inspect generated screenshot."},
        risks=[ToolRisk.READ],
        mode=SecurityMode.POWER,
        workspace=workspace,
        trusted_read_roots=(artifact_root,),
    )

    assert decision.verdict == GuardianVerdict.ALLOW


def test_missing_justification_is_denied(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="read",
        tool_call_id="call-1",
        agent_id="main",
        arguments={"path": "file.txt"},
        risks=[ToolRisk.READ],
        mode=SecurityMode.LIMITED,
        workspace=tmp_path,
    )
    assert decision.verdict == GuardianVerdict.DENY


def test_private_web_target_always_requires_approval(tmp_path: Path) -> None:
    decision = review_tool_call(
        tool_name="web",
        tool_call_id="call-web",
        agent_id="main",
        arguments={
            "action": "scrape",
            "url": "http://127.0.0.1:8080/private",
            "justification": "Inspect a local service.",
        },
        risks=[ToolRisk.NETWORK, ToolRisk.EXTERNAL],
        mode=SecurityMode.POWER,
        workspace=tmp_path,
    )
    assert decision.verdict == GuardianVerdict.ASK
    assert decision.path == "http://127.0.0.1:8080"
