from pathlib import Path

from agentic_kernel.workspace_map import WorkspaceMapService


def test_workspace_map_is_bounded_useful_and_secret_safe(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("SECRET = True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    result = WorkspaceMapService(ttl_seconds=0).build(tmp_path)
    rendered = result.render()

    assert result.languages == ("python",)
    assert "main.py" in result.entrypoints
    assert "tests/test_main.py" in result.tests
    assert "uv run pytest" in result.commands
    assert ".env" not in rendered
    assert "ignored.py" not in rendered
    assert "not proof that files were read" in rendered


def test_workspace_map_reads_package_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"vite","test":"vitest"}}', encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.tsx").write_text("export {};\n", encoding="utf-8")

    result = WorkspaceMapService().build(tmp_path)

    assert result.package_managers == ("npm",)
    assert result.commands == ("npm run dev", "npm run test")
    assert "src/main.tsx" in result.entrypoints
