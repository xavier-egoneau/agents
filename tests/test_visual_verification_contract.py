from pathlib import Path


ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_main_routes_rendering_work_to_uifront_with_visual_evidence() -> None:
    prompt = _read("src/agentic_kernel/defaults/agents/main.md")

    assert "doit passer par\n`uifront`" in prompt
    assert "browser_open" in prompt
    assert "capture effectivement inspectée" in prompt
    assert "reprendre le run" in prompt


def test_uifront_requires_screenshot_and_vision_inspection() -> None:
    prompt = _read("src/agentic_kernel/defaults/agents/uifront.md")

    required_tools = (
        "browser_open",
        "browser_snapshot",
        "browser_screenshot",
        "image_inspect",
    )
    assert all(tool in prompt for tool in required_tools)
    assert "chemin de chaque capture inspectée" in prompt
    assert "vérification visuelle comme incomplète" in prompt
