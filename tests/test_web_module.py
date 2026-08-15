import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


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
        binary="ketch",
        action="search",
        query="python agents",
        url=None,
        library=None,
        language=None,
        backend=None,
        limit=3,
        max_chars=4000,
        depth=1,
        scrape_results=True,
    )
    assert search == [
        "ketch",
        "search",
        "python agents",
        "--limit",
        "3",
        "--scrape",
        "--max-chars",
        "4000",
        "--json",
    ]


async def test_web_search_falls_back_after_configured_backend_failure(monkeypatch) -> None:
    module = load_web_module()
    call = AsyncMock(
        side_effect=[
            {
                "ok": False,
                "error": {"type": "upstream", "message": "rate limited"},
                "metadata": {},
            },
            {"ok": True, "data": [{"url": "https://example.com"}], "metadata": {}},
        ]
    )
    monkeypatch.setattr(module, "web", call)

    result = await module.web_search(None, "amiante maison")

    assert result["ok"] is True
    assert result["metadata"]["search_backends_attempted"] == [
        "configured-default",
        "exa",
    ]
    assert result["metadata"]["search_backend_selected"] == "exa"
    assert call.await_args_list[0].kwargs["backend"] is None
    assert call.await_args_list[1].kwargs["backend"] == "exa"
    crawl = module._build_command(
        binary="ketch",
        action="crawl",
        query=None,
        url="https://example.com",
        library=None,
        language=None,
        backend=None,
        limit=5,
        max_chars=4000,
        depth=2,
        scrape_results=False,
    )
    assert crawl == [
        "ketch",
        "crawl",
        "https://example.com",
        "--depth",
        "2",
        "--concurrency",
        "4",
        "--json",
    ]


def test_decodes_json_and_json_lines_with_output_metadata() -> None:
    module = load_web_module()
    assert (
        module._decode_output(b'{"url":"https://example.com"}')["data"]["url"]
        == "https://example.com"
    )
    decoded = module._decode_output(b'{"url":"https://a.test"}\n{"url":"https://b.test"}\n')
    assert len(decoded["data"]) == 2
    assert decoded["metadata"]["truncated"] is False


def test_web_results_follow_the_kernel_tool_contract() -> None:
    from agentic_kernel.models import ToolResult

    module = load_web_module()
    decoded = module._decode_output(b'{"url":"https://example.com"}')
    assert ToolResult.model_validate(decoded).ok


async def test_missing_ketch_is_a_structured_precondition(monkeypatch) -> None:
    from agentic_kernel.models import ToolResult

    module = load_web_module()
    monkeypatch.setenv("AMK_KETCH_BIN", "/definitely/missing/ketch")
    result = await module.web(None, action="search", query="test")
    assert result["ok"] is False
    assert result["error"]["type"] == "precondition"
    assert ToolResult.model_validate(result).error.details["remedy"].startswith("brew install")


async def test_ketch_nonzero_exit_follows_the_tool_contract(monkeypatch) -> None:
    from agentic_kernel.models import ToolResult

    module = load_web_module()

    class FailedProcess:
        returncode = 4

        async def communicate(self):
            return b"", b"upstream unavailable"

    async def create_process(*args, **kwargs):
        return FailedProcess()

    monkeypatch.setattr(module, "_resolve_binary", lambda: "ketch")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=Path.cwd()))

    result = await module.web(ctx, action="search", query="latest news")
    validated = ToolResult.model_validate(result)

    assert validated.ok is False
    assert validated.error is not None
    assert validated.error.type == "upstream"
    assert validated.error.details == {"exit_code": 4}


async def test_ketch_timeout_follows_the_tool_contract(monkeypatch) -> None:
    from agentic_kernel.models import ToolResult

    module = load_web_module()

    class TimedOutProcess:
        returncode = None
        terminated = False
        waited = False

        async def communicate(self):
            raise TimeoutError

        def terminate(self) -> None:
            self.terminated = True

        async def wait(self) -> None:
            self.waited = True

    process = TimedOutProcess()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(module, "_resolve_binary", lambda: "ketch")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    ctx = SimpleNamespace(deps=SimpleNamespace(workspace=Path.cwd()))

    result = await module.web(ctx, action="search", query="latest news")
    validated = ToolResult.model_validate(result)

    assert validated.ok is False
    assert validated.error is not None
    assert validated.error.type == "cancelled"
    assert process.terminated is True
    assert process.waited is True


async def test_private_scrape_target_is_blocked_before_ketch_runs(monkeypatch) -> None:
    """La classification du Guardian ne résout pas le DNS : un hôte privé
    mono-label passait pour public et partait tel quel vers Ketch. La
    vérification au niveau du module est la vraie frontière."""
    module = load_web_module()
    monkeypatch.setattr(module, "_resolve_binary", lambda: "ketch")
    spawned: list[list[str]] = []

    async def create_process(*args, **kwargs):
        spawned.append(list(args))
        raise AssertionError("ketch must not be spawned for blocked targets")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=Path.cwd(), approved_scopes=set()),
        tool_call_approved=False,
    )

    result = await module.web_scrape(ctx, "http://127.0.0.1:8000/secret")

    assert result["ok"] is False
    assert result["error"]["type"] == "ssrf_blocked"
    assert spawned == []


async def test_approved_private_scope_lets_ketch_proceed(monkeypatch) -> None:
    """Le même contrat que http_request : la portée exacte approuvée autorise."""
    from agentic_kernel.models import ToolResult

    module = load_web_module()
    monkeypatch.setattr(module, "_resolve_binary", lambda: "ketch")

    class QuietProcess:
        returncode = 0

        async def communicate(self):
            return b'{"url":"http://127.0.0.1:8000"}', b""

    async def create_process(*args, **kwargs):
        return QuietProcess()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            workspace=Path.cwd(),
            approved_scopes={("web_scrape", "network", "http://127.0.0.1:8000")},
        ),
        tool_call_approved=False,
    )

    result = await module.web_scrape(ctx, "http://127.0.0.1:8000/")

    assert ToolResult.model_validate(result).ok is True
