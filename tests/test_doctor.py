from __future__ import annotations

import json
from pathlib import Path

from agentic_kernel.doctor import _providers_diagnostic


def test_doctor_reports_an_output_budget_that_leaves_no_final_answer(
    tmp_path: Path,
) -> None:
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "default_provider": "local",
                "providers": [
                    {
                        "id": "local",
                        "kind": "llama-cpp",
                        "connection_type": "local",
                        "model": "qwen",
                        "port": 8123,
                        "num_predict": 8192,
                        "reasoning_budget": 8192,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    diagnostic = _providers_diagnostic(tmp_path)

    assert diagnostic.status == "error"
    assert diagnostic.required is True
    assert "leave at least 25%" in diagnostic.detail


def test_doctor_accepts_a_reasoning_budget_with_output_headroom(tmp_path: Path) -> None:
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "default_provider": "local",
                "providers": [
                    {
                        "id": "local",
                        "kind": "llama-cpp",
                        "connection_type": "local",
                        "model": "qwen",
                        "port": 8123,
                        "num_predict": 8192,
                        "reasoning_budget": 4096,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    diagnostic = _providers_diagnostic(tmp_path)

    assert diagnostic.status == "ok"
    assert diagnostic.detail == "local"
