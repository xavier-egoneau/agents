import base64
import json
import time

import httpx

from agentic_kernel.auth import OPENAI_CODEX, OAuthCredential, OAuthManager


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get(self, provider_id):
        return self.data.get(provider_id)

    def set(self, provider_id, credential):
        self.data[provider_id] = credential

    def delete(self, provider_id):
        self.data.pop(provider_id, None)


def _jwt(account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
    ).rstrip(b"=")
    return f"x.{payload.decode()}.x"


def test_refreshes_expired_oauth_token(monkeypatch) -> None:
    store = MemoryStore()
    store.set(
        "openai-codex",
        OAuthCredential(access="old", refresh="refresh", expires=time.time() - 1),
    )
    manager = OAuthManager(store=store)
    refreshed = OAuthCredential(
        access=_jwt("account"),
        refresh="new-refresh",
        expires=time.time() + 3600,
        account_id="account",
    )
    monkeypatch.setattr(manager, "_refresh", lambda spec, token: refreshed)
    assert manager.access_token("openai-codex")["account_id"] == "account"
    assert store.get("openai-codex") == refreshed


def test_token_exchange_uses_pkce_payload(monkeypatch) -> None:
    token = _jwt("account")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == OPENAI_CODEX.token_url
        assert b"code_verifier=verifier" in request.content
        return httpx.Response(
            200,
            json={"access_token": token, "refresh_token": "r", "expires_in": 3600},
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    credential = OAuthManager(MemoryStore())._exchange(OPENAI_CODEX, "code", "verifier", "state")
    assert credential["account_id"] == "account"
