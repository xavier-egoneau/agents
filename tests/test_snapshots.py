from uuid import uuid4

from agentic_kernel.snapshots import SnapshotStore


def test_snapshot_exposes_single_request_usage_for_calibration(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "sessions")
    messages = [
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "x" * 350}]},
        {
            "kind": "response",
            "parts": [{"part_kind": "text", "content": "ok"}],
            "usage": {"input_tokens": 240},
        },
    ]

    payload = store.save(uuid4(), messages)

    assert payload["latest_request_input_tokens"] == 240
    assert payload["estimated_latest_request_tokens"] > 0


def test_snapshot_does_not_invent_usage_when_provider_omits_it(tmp_path) -> None:
    payload = SnapshotStore(tmp_path / "sessions").save(
        uuid4(), [{"kind": "response", "parts": [], "usage": {}}]
    )

    assert "latest_request_input_tokens" not in payload
    assert "estimated_latest_request_tokens" not in payload
