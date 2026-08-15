from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import keyring

from .errors import AuthenticationError

KEYRING_SERVICE = "agentic-markdown-kernel"


@dataclass(frozen=True)
class OAuthSpec:
    provider_id: str
    client_id: str
    authorize_url: str
    token_url: str
    redirect_uri: str
    scopes: str
    json_token_request: bool = False


OPENAI_CODEX = OAuthSpec(
    provider_id="openai-codex",
    client_id="app_EMoamEEZ73f0CkXaXp7hrann",
    authorize_url="https://auth.openai.com/oauth/authorize",
    token_url="https://auth.openai.com/oauth/token",  # noqa: S106 - URL d'endpoint, pas un secret
    redirect_uri="http://localhost:1455/auth/callback",
    scopes="openid profile email offline_access",
)

CLAUDE = OAuthSpec(
    provider_id="claude",
    client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    authorize_url="https://claude.ai/oauth/authorize",
    token_url="https://platform.claude.com/v1/oauth/token",  # noqa: S106 - URL d'endpoint, pas un secret
    redirect_uri="http://localhost:53692/callback",
    scopes=(
        "org:create_api_key user:profile user:inference user:sessions:claude_code "
        "user:mcp_servers user:file_upload"
    ),
    json_token_request=True,
)

SPECS = {spec.provider_id: spec for spec in (OPENAI_CODEX, CLAUDE)}


class OAuthCredential(dict):
    @property
    def access(self) -> str:
        return str(self["access"])

    @property
    def refresh(self) -> str:
        return str(self["refresh"])

    @property
    def expires(self) -> float:
        return float(self["expires"])


class CredentialStore:
    def get(self, provider_id: str) -> OAuthCredential | None:
        try:
            value = keyring.get_password(KEYRING_SERVICE, provider_id)
        except keyring.errors.KeyringError as exc:
            raise AuthenticationError(f"system keyring unavailable: {exc}") from exc
        return OAuthCredential(json.loads(value)) if value else None

    def set(self, provider_id: str, credential: OAuthCredential) -> None:
        try:
            keyring.set_password(KEYRING_SERVICE, provider_id, json.dumps(credential))
        except keyring.errors.KeyringError as exc:
            raise AuthenticationError(f"cannot write system keyring: {exc}") from exc

    def delete(self, provider_id: str) -> None:
        try:
            keyring.delete_password(KEYRING_SERVICE, provider_id)
        except keyring.errors.PasswordDeleteError:
            return
        except keyring.errors.KeyringError as exc:
            raise AuthenticationError(f"cannot update system keyring: {exc}") from exc


class _CallbackHandler(BaseHTTPRequestHandler):
    result: ClassVar[dict[str, str] | None] = None
    expected_path: ClassVar[str] = "/"
    expected_state: ClassVar[str] = ""

    def do_GET(self) -> None:  # nom imposé par BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        valid = (
            parsed.path == self.expected_path
            and query.get("state") == self.expected_state
            and bool(query.get("code"))
        )
        if valid:
            type(self).result = query
            self.send_response(200)
            message = "Authentication completed. You can close this window."
        else:
            self.send_response(400)
            message = "Authentication failed: invalid callback or state."
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, _format: str, *args: object) -> None:
        return


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _parse_manual(value: str) -> dict[str, str]:
    value = value.strip()
    if not value:
        return {}
    parsed = urlparse(value)
    if parsed.scheme:
        return {key: values[0] for key, values in parse_qs(parsed.query).items()}
    if "#" in value:
        code, state = value.split("#", 1)
        return {"code": code, "state": state}
    return {"code": value}


class OAuthManager:
    def __init__(self, store: CredentialStore | None = None) -> None:
        self.store = store or CredentialStore()

    def login(
        self,
        provider_id: str,
        timeout_seconds: float = 180,
        *,
        allow_manual: bool = True,
    ) -> OAuthCredential:
        spec = self._spec(provider_id)
        verifier, challenge = _pkce()
        # `state` protège le callback contre le CSRF, `verifier` porte le PKCE :
        # les coupler (state = verifier) exposait le secret PKCE dès que l'URL
        # d'autorisation fuit — historique du navigateur, referrer, logs.
        state = secrets.token_hex(16)
        redirect = urlparse(spec.redirect_uri)
        params = {
            "client_id": spec.client_id,
            "response_type": "code",
            "redirect_uri": spec.redirect_uri,
            "scope": spec.scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if provider_id == "openai-codex":
            params.update(
                {
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "originator": "pi",
                }
            )
        else:
            params["code"] = "true"
        url = f"{spec.authorize_url}?{urlencode(params)}"

        handler = type("CallbackHandler", (_CallbackHandler,), {})
        handler.result = None
        handler.expected_path = redirect.path
        handler.expected_state = state
        result: dict[str, str] | None = None
        try:
            server = HTTPServer(("127.0.0.1", redirect.port or 80), handler)
            server.timeout = 1.0
            print(f"Open this URL to authenticate:\n{url}")
            webbrowser.open(url)
            # `handle_request()` unique était consommé par n'importe quelle
            # requête parasite (favicon, double GET du provider) : on écoute
            # jusqu'au callback valide ou au délai global.
            deadline = time.monotonic() + timeout_seconds
            while handler.result is None and time.monotonic() < deadline:
                server.handle_request()
            result = handler.result
            server.server_close()
        except OSError:
            print(f"Local callback unavailable. Open this URL:\n{url}")

        if result is None and allow_manual:
            result = _parse_manual(input("Paste the callback URL or authorization code: "))
        if result is None:
            raise AuthenticationError("OAuth callback not received; start the connection again")
        if result.get("state") and result["state"] != state:
            raise AuthenticationError("OAuth state mismatch")
        code = result.get("code")
        if not code:
            raise AuthenticationError("OAuth callback did not contain an authorization code")
        credential = self._exchange(spec, code, verifier, state)
        self.store.set(provider_id, credential)
        return credential

    def logout(self, provider_id: str) -> None:
        self._spec(provider_id)
        self.store.delete(provider_id)

    def status(self, provider_id: str) -> bool:
        self._spec(provider_id)
        return self.store.get(provider_id) is not None

    def access_token(self, provider_id: str) -> OAuthCredential:
        spec = self._spec(provider_id)
        credential = self.store.get(provider_id)
        if credential is None:
            raise AuthenticationError(f"not authenticated with {provider_id}; run amk auth login")
        if credential.expires <= time.time() + 300:
            credential = self._refresh(spec, credential.refresh)
            self.store.set(provider_id, credential)
        return credential

    @staticmethod
    def _spec(provider_id: str) -> OAuthSpec:
        try:
            return SPECS[provider_id]
        except KeyError as exc:
            raise AuthenticationError(f"unsupported OAuth provider: {provider_id}") from exc

    def _exchange(self, spec: OAuthSpec, code: str, verifier: str, state: str) -> OAuthCredential:
        payload = {
            "grant_type": "authorization_code",
            "client_id": spec.client_id,
            "code": code,
            "redirect_uri": spec.redirect_uri,
            "code_verifier": verifier,
        }
        if spec.provider_id == "claude":
            payload["state"] = state
        return self._token_request(spec, payload)

    def _refresh(self, spec: OAuthSpec, refresh_token: str) -> OAuthCredential:
        return self._token_request(
            spec,
            {
                "grant_type": "refresh_token",
                "client_id": spec.client_id,
                "refresh_token": refresh_token,
            },
        )

    @staticmethod
    def _token_request(spec: OAuthSpec, payload: dict[str, str]) -> OAuthCredential:
        try:
            with httpx.Client(timeout=30) as client:
                response = (
                    client.post(spec.token_url, json=payload)
                    if spec.json_token_request
                    else client.post(spec.token_url, data=payload)
                )
            response.raise_for_status()
            data = response.json()
            access = data["access_token"]
            refresh = data["refresh_token"]
            expires_in = float(data["expires_in"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError(
                f"OAuth token request failed for {spec.provider_id}: {exc}"
            ) from exc
        credential = OAuthCredential(
            access=access,
            refresh=refresh,
            expires=time.time() + expires_in,
        )
        if spec.provider_id == "openai-codex":
            credential["account_id"] = _codex_account_id(access)
        return credential


def _codex_account_id(access_token: str) -> str:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    except (IndexError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("OpenAI token does not contain a ChatGPT account id") from exc
