import importlib.util
from pathlib import Path


def load_web_module():
    path = Path(__file__).parents[1] / "tools" / "modules" / "web" / "module.py"
    spec = importlib.util.spec_from_file_location("test_amk_web_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builds_bounded_ketch_commands() -> None:
    module = load_web_module()
    search = module._build_command(
        binary="ketch", action="search", query="python agents", url=None,
        library=None, language=None, backend=None, limit=3, max_chars=4000,
        depth=1, scrape_results=True,
    )
    assert search == [
        "ketch", "search", "python agents", "--backend", "ddg", "--limit", "3",
        "--scrape", "--max-chars", "4000", "--json",
    ]
    crawl = module._build_command(
        binary="ketch", action="crawl", query=None, url="https://example.com",
        library=None, language=None, backend=None, limit=5, max_chars=4000,
        depth=2, scrape_results=False,
    )
    assert crawl == [
        "ketch", "crawl", "https://example.com", "--depth", "2",
        "--concurrency", "4", "--json",
    ]


def test_decodes_json_and_json_lines_with_output_metadata() -> None:
    module = load_web_module()
    assert module._decode_output(b'{"url":"https://example.com"}')["data"]["url"] == "https://example.com"
    decoded = module._decode_output(b'{"url":"https://a.test"}\n{"url":"https://b.test"}\n')
    assert len(decoded["data"]) == 2
    assert decoded["truncated"] is False


async def test_missing_ketch_is_a_structured_precondition(monkeypatch) -> None:
    module = load_web_module()
    monkeypatch.setenv("AMK_KETCH_BIN", "/definitely/missing/ketch")
    result = await module.web(None, action="search", query="test")
    assert result["ok"] is False
    assert result["error"]["type"] == "precondition"
