import json
from pathlib import Path

from agentic_kernel.module_settings import stored_module_settings


def test_stored_module_settings_returns_only_requested_namespace(tmp_path: Path) -> None:
    (tmp_path / "tool-settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "modules": {
                    "perception": {"model_path": "model.gguf"},
                    "other": {"value": "ignored"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert stored_module_settings(tmp_path, "perception") == {
        "model_path": "model.gguf"
    }
