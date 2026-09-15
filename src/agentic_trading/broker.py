"""Broker facade over MCP tool calls."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from agentic_trading.rh_mcp.snapshot import build_capability_map
from agentic_trading.risk import PortfolioSnapshot


class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def list_tools(self) -> list[dict[str, Any]]: ...


class Broker:
    def __init__(
        self,
        client: McpClient,
        tools: list[dict[str, Any]],
        capability_map: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._tools = tools
        self._capability_map = capability_map or build_capability_map(tools)

    def get_equity(self) -> Decimal:
        tool = self._require_tool("get_account")
        result = self._client.call_tool(tool, {})
        return Decimal(str(result.get("equity", "0")))

    def get_positions(self) -> PortfolioSnapshot:
        tool = self._require_tool("get_account")
        result = self._client.call_tool(tool, {})
        positions = result.get("positions")
        if not isinstance(positions, list):
            return PortfolioSnapshot(
                open_positions=0, held={}, positions_read_failed=True
            )

        held: dict[str, Decimal] = {}
        for item in positions:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            qty_raw = item.get("quantity", item.get("qty"))
            if not symbol or qty_raw is None:
                continue
            qty = Decimal(str(qty_raw))
            if qty > 0:
                held[symbol] = qty

        return PortfolioSnapshot(open_positions=len(held), held=held)

    def review_order(self, **kwargs: Any) -> dict[str, Any]:
        tool = self._require_tool("review_equity")
        return self._client.call_tool(tool, dict(kwargs))

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        tool = self._require_tool("place_equity")
        return self._client.call_tool(tool, dict(kwargs))

    def _require_tool(self, capability: str) -> str:
        tool = self._capability_map.get(capability)
        if not tool:
            raise KeyError(f"no MCP tool mapped for capability {capability!r}")
        return tool
