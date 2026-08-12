from pathlib import Path

import httpx

from agentic_kernel.vision import LocalVisionService


async def test_local_vision_disables_reasoning_for_tool_observation(
    tmp_path: Path, monkeypatch
) -> None:
    service = LocalVisionService(tmp_path)
    monkeypatch.setattr(
        service,
        "_config",
        lambda: {
            "base_url": "http://127.0.0.1:8081/v1",
            "model": "vision-test",
            "timeout_seconds": 1,
            "max_tokens": 2048,
        },
    )

    async def ready(_config) -> None:
        return None

    monkeypatch.setattr(service, "_ensure_server", ready)
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Écran noir."}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json):
            captured.update(json)
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())

    observation = await service.analyze_bytes(b"png", "image/png", "Décris.", "fast")

    assert observation == "Écran noir."
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
