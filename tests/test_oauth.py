"""Unit tests for OAuth helpers — mocked HTTP only (no Robinhood network)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agentic_trading.rh_mcp.oauth import (
    AUTHORIZE_URL,
    AuthFailed,
    TokenSet,
    build_authorize_url,
    exchange_code,
    generate_pkce,
    load_tokens,
    refresh_access_token,
    register_client,
    save_tokens,
    token_set_from_response,
)


class PkceTests(unittest.TestCase):
    def test_generate_pkce_s256_shape(self) -> None:
        verifier, challenge = generate_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        import base64

        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)


class TokenStorageTests(unittest.TestCase):
    def test_save_tokens_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            tokens = TokenSet(
                access_token="access",
                refresh_token="refresh",
                client_id="cid",
                expires_at=1_700_000_000.0,
            )
            save_tokens(path, tokens)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_tokens(path)
            assert loaded is not None
            self.assertEqual(loaded.access_token, "access")
            self.assertEqual(loaded.refresh_token, "refresh")
            self.assertEqual(loaded.client_id, "cid")


class AuthorizeUrlTests(unittest.TestCase):
    def test_build_authorize_url_includes_pkce(self) -> None:
        url = build_authorize_url(
            client_id="cid",
            redirect_uri="http://127.0.0.1:8765/callback",
            code_challenge="challenge",
            state="state123",
        )
        self.assertTrue(url.startswith(AUTHORIZE_URL + "?"))
        self.assertIn("code_challenge=challenge", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("response_type=code", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("scope=internal", url)


class RegisterAndTokenHttpTests(unittest.TestCase):
    def test_register_client_posts_public_client_body(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(201, json={"client_id": "registered-id"})

        transport = httpx.MockTransport(handler)
        client_id = register_client(
            redirect_uri="http://127.0.0.1:8765/callback",
            transport=transport,
        )
        self.assertEqual(client_id, "registered-id")
        body = seen["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertIn("authorization_code", body["grant_types"])
        self.assertEqual(body["redirect_uris"], ["http://127.0.0.1:8765/callback"])

    def test_exchange_code_form_body(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["data"] = dict(httpx.QueryParams(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "access_token": "a1",
                    "refresh_token": "r1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        tokens = exchange_code(
            code="authcode",
            redirect_uri="http://127.0.0.1:8765/callback",
            client_id="cid",
            code_verifier="verifier",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(tokens.access_token, "a1")
        self.assertEqual(tokens.refresh_token, "r1")
        self.assertEqual(tokens.client_id, "cid")
        data = seen["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["grant_type"], "authorization_code")
        self.assertEqual(data["code_verifier"], "verifier")
        self.assertEqual(data["client_id"], "cid")

    def test_refresh_access_token_persists(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            data = dict(httpx.QueryParams(request.content.decode("utf-8")))
            self.assertEqual(data["grant_type"], "refresh_token")
            self.assertEqual(data["refresh_token"], "old-refresh")
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "expires_in": 1800,
                    "token_type": "Bearer",
                },
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            old = TokenSet(
                access_token="old",
                refresh_token="old-refresh",
                client_id="cid",
            )
            refreshed = refresh_access_token(
                old,
                token_path=path,
                transport=httpx.MockTransport(handler),
            )
            self.assertEqual(refreshed.access_token, "new-access")
            self.assertEqual(refreshed.refresh_token, "old-refresh")
            loaded = load_tokens(path)
            assert loaded is not None
            self.assertEqual(loaded.access_token, "new-access")

    def test_refresh_without_refresh_token_raises(self) -> None:
        with self.assertRaises(AuthFailed):
            refresh_access_token(TokenSet(access_token="only"))

    def test_token_set_from_response_requires_access(self) -> None:
        with self.assertRaises(AuthFailed):
            token_set_from_response({"token_type": "Bearer"})


class DesktopOauthPasteFallbackTests(unittest.TestCase):
    def test_paste_fallback_exchanges_code(self) -> None:
        from agentic_trading.rh_mcp.oauth import run_desktop_oauth

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url.path))
            if request.url.path.endswith("/register"):
                return httpx.Response(201, json={"client_id": "cid"})
            if "token" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "access_token": "pasted-access",
                        "refresh_token": "pasted-refresh",
                        "expires_in": 60,
                    },
                )
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            with mock.patch(
                "agentic_trading.rh_mcp.oauth._wait_for_redirect_code",
                side_effect=AuthFailed("no listener"),
            ):
                tokens = run_desktop_oauth(
                    path,
                    open_browser=False,
                    paste_fallback=True,
                    input_fn=lambda _prompt: "paste-code-xyz",
                    print_fn=lambda _msg: None,
                    transport=httpx.MockTransport(handler),
                    register_url="https://example.test/oauth/trading/register",
                    token_url="https://example.test/oauth2/token/",
                )
            self.assertEqual(tokens.access_token, "pasted-access")
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
