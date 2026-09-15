"""Unit tests for Streamable HTTP MCP client — mocked httpx only."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from agentic_trading.rh_mcp.client import AuthFailed, McpError, RobinhoodMcpClient
from agentic_trading.rh_mcp.oauth import TokenSet, save_tokens


MCP_URL = "https://example.test/mcp/trading"


def _jsonrpc_ok(request_id: int, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class ClientRequestShapingTests(unittest.TestCase):
    def test_initialize_and_list_tools_json_rpc_shape(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            seen.append(body)
            self.assertEqual(request.headers["authorization"], "Bearer test-access")
            self.assertIn("application/json", request.headers["accept"])
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(
                    200,
                    json=_jsonrpc_ok(
                        body["id"],
                        {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "serverInfo": {"name": "rh", "version": "1"},
                        },
                    ),
                    headers={"Mcp-Session-Id": "sess-1"},
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/list":
                self.assertEqual(request.headers.get("mcp-session-id"), "sess-1")
                return httpx.Response(
                    200,
                    json=_jsonrpc_ok(
                        body["id"],
                        {
                            "tools": [
                                {
                                    "name": "get_account",
                                    "description": "acct",
                                    "inputSchema": {"type": "object"},
                                }
                            ]
                        },
                    ),
                )
            return httpx.Response(500, text=f"unexpected {method}")

        client = RobinhoodMcpClient(
            MCP_URL,
            tokens=TokenSet(access_token="test-access", refresh_token="r"),
            transport=httpx.MockTransport(handler),
        )
        init = client.initialize()
        self.assertEqual(init["serverInfo"]["name"], "rh")
        tools = client.list_tools()
        self.assertEqual(tools[0]["name"], "get_account")

        methods = [b.get("method") for b in seen]
        self.assertIn("initialize", methods)
        self.assertIn("tools/list", methods)
        init_body = next(b for b in seen if b.get("method") == "initialize")
        self.assertEqual(init_body["jsonrpc"], "2.0")
        self.assertIn("id", init_body)
        self.assertEqual(init_body["params"]["clientInfo"]["name"], "agentic-trading")

    def test_call_tool_unwraps_text_json_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(
                    200, json=_jsonrpc_ok(body["id"], {"capabilities": {}})
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/call":
                self.assertEqual(body["params"]["name"], "get_account")
                self.assertEqual(body["params"]["arguments"], {})
                return httpx.Response(
                    200,
                    json=_jsonrpc_ok(
                        body["id"],
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {"equity": "1000", "positions": []}
                                    ),
                                }
                            ]
                        },
                    ),
                )
            return httpx.Response(500, text=method)

        client = RobinhoodMcpClient(
            MCP_URL,
            tokens=TokenSet(access_token="tok"),
            transport=httpx.MockTransport(handler),
        )
        result = client.call_tool("get_account", {})
        self.assertEqual(result["equity"], "1000")

    def test_sse_response_parsed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "initialize":
                payload = _jsonrpc_ok(body["id"], {"ok": True})
                sse = f"event: message\ndata: {json.dumps(payload)}\n\n"
                return httpx.Response(
                    200,
                    text=sse,
                    headers={"content-type": "text/event-stream"},
                )
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(500)

        client = RobinhoodMcpClient(
            MCP_URL,
            tokens=TokenSet(access_token="tok"),
            transport=httpx.MockTransport(handler),
        )
        result = client.initialize()
        self.assertEqual(result, {"ok": True})


class AuthRefreshTests(unittest.TestCase):
    def test_401_refreshes_once_then_retries(self) -> None:
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "token" in url:
                data = dict(httpx.QueryParams(request.content.decode("utf-8")))
                self.assertEqual(data["grant_type"], "refresh_token")
                return httpx.Response(
                    200,
                    json={
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                    },
                )

            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")
            if method == "notifications/initialized":
                return httpx.Response(202)

            auth = request.headers.get("authorization", "")
            state["n"] += 1
            if state["n"] == 1:
                self.assertEqual(auth, "Bearer stale")
                return httpx.Response(401, text="unauthorized")
            self.assertEqual(auth, "Bearer new-access")
            return httpx.Response(
                200,
                json=_jsonrpc_ok(
                    body["id"],
                    {"tools": [{"name": "get_account"}]},
                ),
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            save_tokens(
                path,
                TokenSet(
                    access_token="stale",
                    refresh_token="refresh-me",
                    client_id="cid",
                ),
            )
            client = RobinhoodMcpClient(
                MCP_URL,
                token_path=path,
                transport=httpx.MockTransport(handler),
            )
            # Force initialized skip path: call list after marking init done via initialize
            # First RPC will be initialize (401 → refresh → retry).
            tools = client.list_tools()
            self.assertEqual(tools[0]["name"], "get_account")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["access_token"], "new-access")

    def test_401_after_refresh_raises_auth_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "token" in str(request.url):
                return httpx.Response(
                    200,
                    json={"access_token": "still-bad", "expires_in": 60},
                )
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(401, text="nope")

        client = RobinhoodMcpClient(
            MCP_URL,
            tokens=TokenSet(access_token="a", refresh_token="r"),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(AuthFailed):
            client.initialize()

    def test_is_error_tool_call_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(
                    200, json=_jsonrpc_ok(body["id"], {"capabilities": {}})
                )
            if method == "notifications/initialized":
                return httpx.Response(202)
            if method == "tools/call":
                return httpx.Response(
                    200,
                    json=_jsonrpc_ok(
                        body["id"],
                        {
                            "isError": True,
                            "content": [{"type": "text", "text": "boom"}],
                        },
                    ),
                )
            return httpx.Response(500)

        client = RobinhoodMcpClient(
            MCP_URL,
            tokens=TokenSet(access_token="tok"),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(McpError):
            client.call_tool("review_equity_order", {"symbol": "SPY"})
