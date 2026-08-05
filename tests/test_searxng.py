from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_kernel.errors import ConfigurationError
from agentic_kernel.searxng import SEARXNG_REVISION, SearxngService


def mark_ready(service: SearxngService) -> None:
    service.root.mkdir(parents=True, exist_ok=True)
    service.python.parent.mkdir(parents=True)
    service.python.write_text("", encoding="utf-8")
    service.settings.write_text("server: {}\n", encoding="utf-8")
    service.manifest.write_text(
        json.dumps({"revision": SEARXNG_REVISION, "port": service.port}), encoding="utf-8"
    )


def test_prepare_reuses_pinned_install_and_configures_ketch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SearxngService(tmp_path, port=9888)
    mark_ready(service)
    configured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "_configure_ketch",
        lambda: configured.append(("url", service.base_url)),
    )

    assert service.prepare() is False
    assert configured == [("url", "http://127.0.0.1:9888")]


@pytest.mark.asyncio
async def test_start_rejects_a_foreign_process_on_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SearxngService(tmp_path, port=9888)
    monkeypatch.setattr(service, "_reachable", lambda: async_false())
    monkeypatch.setattr("agentic_kernel.searxng.listening_pids", lambda port: [42])
    monkeypatch.setattr("agentic_kernel.searxng.process_command", lambda pid: "foreign.exe")

    with pytest.raises(ConfigurationError, match=r"PID 42 .*foreign\.exe"):
        await service.start()


async def async_false() -> bool:
    return False
