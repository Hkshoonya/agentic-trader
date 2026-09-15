"""Streamable HTTP JSON-RPC client for Robinhood Trading MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import httpx

from agentic_trading.rh_mcp.oauth import (
    AuthFailed,
    TokenSet,
    load_tokens,
    refresh_access_token,
)

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_ACCEPT = "application/json, text/event-stream"


class McpError(Exception):
    """JSON-RPC or MCP transport error."""


class RobinhoodMcpClient:
    """Minimal Streamable HTTP MCP client: initialize / tools/list / tools/call."""

    def __init__(
        self,
        mcp_url: str,
        *,
        token_path: Path | str | None = None,
        tokens: TokenSet | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
        client_name: str = "agentic-trading",
        client_version: str = "0.1.0",
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        if tokens is None and token_path is None:
            raise ValueError("tokens or token_path required")
        self.mcp_url = mcp_url
        self.token_path = Path(token_path).expanduser() if token_path else None
        self._tokens = tokens or load_tokens(self.token_path)  # type: ignore[arg-type]
        if self._tokens is None:
            raise AuthFailed(f"no tokens at {self.token_path}")
        self._transport = transport
        self._timeout = timeout
        self._client_name = client_name
        self._client_version = client_version
        self._protocol_version = protocol_version
        self._request_id = 0
        self._session_id: str | None = None
        self._initialized = False

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            },
        )
        # Best-effort initialized notification (no id).
        try:
            self._post_notification("notifications/initialized", {})
        except Exception:  # noqa: BLE001 — non-fatal for hosts that ignore it
            pass
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        result = self._rpc("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpError(f"tools/list missing tools array: {result!r}")
        return list(tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._ensure_initialized()
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )
        if isinstance(result, dict) and result.get("isError"):
            raise McpError(f"tools/call isError for {name}: {result!r}")
        return _unwrap_tool_result(result)

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._tokens.access_token}",
            "Content-Type": "application/json",
            "Accept": DEFAULT_ACCEPT,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _refresh_once(self) -> None:
        self._tokens = refresh_access_token(
            self._tokens,
            token_path=self.token_path,
            transport=self._transport,
            timeout=self._timeout,
        )

    def _post_notification(self, method: str, params: Mapping[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            response = client.post(self.mcp_url, headers=self._headers(), json=payload)
        session = response.headers.get("mcp-session-id") or response.headers.get(
            "Mcp-Session-Id"
        )
        if session:
            self._session_id = session

    def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._rpc_with_auth_retry(method, params, retried=False)

    def _rpc_with_auth_retry(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        retried: bool,
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": dict(params),
        }
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            response = client.post(self.mcp_url, headers=self._headers(), json=payload)

        session = response.headers.get("mcp-session-id") or response.headers.get(
            "Mcp-Session-Id"
        )
        if session:
            self._session_id = session

        if response.status_code == 401:
            if retried:
                raise AuthFailed("MCP request unauthorized after refresh")
            try:
                self._refresh_once()
            except AuthFailed:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AuthFailed(f"token refresh failed: {exc}") from exc
            return self._rpc_with_auth_retry(method, params, retried=True)

        if response.status_code >= 400:
            raise McpError(
                f"MCP HTTP {response.status_code} for {method}: {response.text}"
            )

        body = _parse_streamable_body(response)
        if "error" in body:
            raise McpError(f"JSON-RPC error for {method}: {body['error']!r}")
        result = body.get("result")
        if not isinstance(result, dict):
            # Some servers return non-object results; normalize.
            if result is None:
                raise McpError(f"JSON-RPC missing result for {method}: {body!r}")
            return {"value": result}
        return result


def _parse_streamable_body(response: httpx.Response) -> dict[str, Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    text = response.text.strip()
    if not text:
        raise McpError("empty MCP response body")

    if (
        "text/event-stream" in content_type
        or text.startswith("event:")
        or "data:" in text[:20]
    ):
        return _parse_sse_json(text)

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _parse_sse_json(text)
    if not isinstance(payload, dict):
        raise McpError(f"expected JSON object, got {type(payload)!r}")
    return payload


def _parse_sse_json(text: str) -> dict[str, Any]:
    """Take the last JSON ``data:`` payload from an SSE stream."""
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            last = parsed
    if last is None:
        raise McpError(f"could not parse SSE JSON-RPC body: {text[:200]!r}")
    return last


def _unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize MCP tools/call result into a plain dict for Broker."""
    if "content" not in result:
        return dict(result)

    content = result.get("content")
    if not isinstance(content, list):
        return dict(result)

    texts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text", "")))
    if not texts:
        return dict(result)

    joined = "\n".join(texts).strip()
    try:
        parsed = json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined, **{k: v for k, v in result.items() if k != "content"}}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed, "text": joined}
