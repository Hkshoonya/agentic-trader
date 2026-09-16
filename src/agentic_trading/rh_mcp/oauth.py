"""OAuth 2.1 + PKCE (S256) for Robinhood Trading MCP (desktop / public client).

Candidate endpoints (confirm against live discovery before relying on them):

- authorize: ``https://robinhood.com/oauth``
- token: ``https://api.robinhood.com/oauth2/token/``
- register: ``https://agent.robinhood.com/oauth/trading/register``

Verified against public OAuth metadata notes (2026-07-20). Re-check
``/.well-known/oauth-authorization-server`` if auth fails.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

# Candidate endpoints — confirm via OAuth discovery / Robinhood docs.
AUTHORIZE_URL = "https://robinhood.com/oauth"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
REGISTER_URL = "https://agent.robinhood.com/oauth/trading/register"
DEFAULT_SCOPE = "internal"
DEFAULT_CLIENT_NAME = "agentic-trading"


class AuthFailed(Exception):
    """Authentication or token refresh failed."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    client_id: str | None = None
    token_type: str = "Bearer"
    raw: dict[str, Any] | None = None

    def is_expired(self, *, skew_seconds: float = 60.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - skew_seconds)


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def load_tokens(path: Path | str) -> TokenSet | None:
    token_path = Path(path).expanduser()
    if not token_path.is_file():
        return None
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return None
    return TokenSet(
        access_token=str(payload["access_token"]),
        refresh_token=(
            str(payload["refresh_token"]) if payload.get("refresh_token") else None
        ),
        expires_at=(
            float(payload["expires_at"])
            if payload.get("expires_at") is not None
            else None
        ),
        client_id=str(payload["client_id"]) if payload.get("client_id") else None,
        token_type=str(payload.get("token_type") or "Bearer"),
        raw=payload,
    )


def save_tokens(path: Path | str, tokens: TokenSet | Mapping[str, Any]) -> Path:
    """Write tokens JSON with mode ``0600`` (owner read/write only)."""
    token_path = Path(path).expanduser()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(tokens, TokenSet):
        payload: dict[str, Any] = {
            "access_token": tokens.access_token,
            "token_type": tokens.token_type,
        }
        if tokens.refresh_token:
            payload["refresh_token"] = tokens.refresh_token
        if tokens.expires_at is not None:
            payload["expires_at"] = tokens.expires_at
        if tokens.client_id:
            payload["client_id"] = tokens.client_id
        if tokens.raw:
            for key, value in tokens.raw.items():
                payload.setdefault(key, value)
    else:
        payload = dict(tokens)

    tmp = token_path.with_suffix(token_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(token_path)
    token_path.chmod(0o600)
    return token_path


def _expires_at_from_response(payload: Mapping[str, Any]) -> float | None:
    if "expires_at" in payload and payload["expires_at"] is not None:
        return float(payload["expires_at"])
    if "expires_in" in payload and payload["expires_in"] is not None:
        return time.time() + float(payload["expires_in"])
    return None


def token_set_from_response(
    payload: Mapping[str, Any],
    *,
    client_id: str | None = None,
) -> TokenSet:
    access = payload.get("access_token")
    if not access:
        raise AuthFailed(f"token response missing access_token: {payload!r}")
    return TokenSet(
        access_token=str(access),
        refresh_token=(
            str(payload["refresh_token"]) if payload.get("refresh_token") else None
        ),
        expires_at=_expires_at_from_response(payload),
        client_id=client_id
        or (str(payload["client_id"]) if payload.get("client_id") else None),
        token_type=str(payload.get("token_type") or "Bearer"),
        raw=dict(payload),
    )


def register_client(
    *,
    redirect_uri: str,
    client_name: str = DEFAULT_CLIENT_NAME,
    register_url: str = REGISTER_URL,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> str:
    """Dynamic client registration (public client, no secret). Returns client_id."""
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.post(register_url, json=body)
    if response.status_code >= 400:
        raise AuthFailed(
            f"client registration failed ({response.status_code}): {response.text}"
        )
    payload = response.json()
    client_id = payload.get("client_id")
    if not client_id:
        raise AuthFailed(f"registration response missing client_id: {payload!r}")
    return str(client_id)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
    authorize_url: str = AUTHORIZE_URL,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{authorize_url}?{query}"


def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    token_url: str = TOKEN_URL,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> TokenSet:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.post(token_url, data=data)
    if response.status_code >= 400:
        raise AuthFailed(
            f"token exchange failed ({response.status_code}): {response.text}"
        )
    return token_set_from_response(response.json(), client_id=client_id)


def refresh_access_token(
    tokens: TokenSet,
    *,
    token_path: Path | str | None = None,
    token_url: str = TOKEN_URL,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> TokenSet:
    """Refresh once; optionally persist the new token set."""
    if not tokens.refresh_token:
        raise AuthFailed("no refresh_token available")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
    }
    if tokens.client_id:
        data["client_id"] = tokens.client_id
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.post(token_url, data=data)
    if response.status_code >= 400:
        raise AuthFailed(
            f"token refresh failed ({response.status_code}): {response.text}"
        )
    refreshed = token_set_from_response(response.json(), client_id=tokens.client_id)
    if not refreshed.refresh_token:
        refreshed = TokenSet(
            access_token=refreshed.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=refreshed.expires_at,
            client_id=refreshed.client_id or tokens.client_id,
            token_type=refreshed.token_type,
            raw=refreshed.raw,
        )
    if token_path is not None:
        save_tokens(token_path, refreshed)
    return refreshed


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Capture ``?code=`` (and state) from the loopback redirect."""

    result: dict[str, str] | None = None
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/callback", "/oauth/callback"):
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        if error:
            desc = params.get("error_description", [""])[0]
            _OAuthCallbackHandler.result = {
                "error": str(error),
                "error_description": str(desc),
            }
            self._respond(400, f"Auth error: {error}")
            return
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if not code:
            self._respond(400, "Missing authorization code")
            return
        if self.expected_state and state != self.expected_state:
            _OAuthCallbackHandler.result = {"error": "state_mismatch"}
            self._respond(400, "State mismatch")
            return
        _OAuthCallbackHandler.result = {"code": str(code), "state": str(state or "")}
        self._respond(200, "Authorization complete. You can close this window.")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _wait_for_redirect_code(
    *,
    port: int,
    state: str,
    timeout_seconds: float = 300.0,
) -> str:
    _OAuthCallbackHandler.result = None
    _OAuthCallbackHandler.expected_state = state
    server = http.server.HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _OAuthCallbackHandler.result is not None:
            break
        time.sleep(0.05)
    server.server_close()
    thread.join(timeout=1.0)
    result = _OAuthCallbackHandler.result
    if result is None:
        raise AuthFailed("timed out waiting for OAuth redirect")
    if "error" in result:
        raise AuthFailed(f"OAuth redirect error: {result}")
    return result["code"]


def run_desktop_oauth(
    token_path: Path | str,
    *,
    port: int = 8765,
    open_browser: bool = True,
    open_url: Callable[[str], None] | None = None,
    paste_fallback: bool = True,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
    authorize_url: str = AUTHORIZE_URL,
    token_url: str = TOKEN_URL,
    register_url: str = REGISTER_URL,
    client_name: str = DEFAULT_CLIENT_NAME,
    scope: str = DEFAULT_SCOPE,
) -> TokenSet:
    """Interactive desktop PKCE flow; stores tokens at ``token_path`` (mode 0600)."""
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    client_id = register_client(
        redirect_uri=redirect_uri,
        client_name=client_name,
        register_url=register_url,
        transport=transport,
    )
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state=state,
        scope=scope,
        authorize_url=authorize_url,
    )
    print_fn("Open this URL to authorize (desktop browser required):")
    print_fn(url)
    if open_browser:
        opener = open_url or webbrowser.open
        try:
            opener(url)
        except Exception:  # noqa: BLE001 — best-effort only
            print_fn("(could not open browser automatically)")

    code: str | None = None
    try:
        print_fn(f"Waiting for redirect on {redirect_uri} …")
        code = _wait_for_redirect_code(port=port, state=state)
    except AuthFailed as exc:
        if not paste_fallback:
            raise
        print_fn(f"Redirect listener failed ({exc}); paste-code fallback.")
        pasted = input_fn(
            "Paste the authorization code (or full redirect URL): "
        ).strip()
        if not pasted:
            raise AuthFailed("empty authorization code") from exc
        if "://" in pasted or pasted.startswith("/"):
            parsed = urllib.parse.urlparse(
                pasted if "://" in pasted else f"http://x{pasted}"
            )
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            pasted_state = params.get("state", [None])[0]
            if pasted_state and pasted_state != state:
                raise AuthFailed("state mismatch in pasted URL") from exc
        else:
            code = pasted
        if not code:
            raise AuthFailed("could not parse authorization code") from exc

    assert code is not None
    tokens = exchange_code(
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        code_verifier=verifier,
        token_url=token_url,
        transport=transport,
    )
    saved = save_tokens(token_path, tokens)
    print_fn(f"Tokens saved to {saved} (mode 0600)")
    return tokens
