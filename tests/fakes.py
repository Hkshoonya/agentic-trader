"""Test doubles for MCP client interactions.

Payload shapes mirror the live Robinhood Trading MCP tools (see
``tests/fixtures/tools_snapshot.json``). Keeping these faithful is what makes
the broker tests meaningful — an invented ``get_account`` tool previously hid a
real integration break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict


@dataclass
class FakeMcpClient:
    """Records tool calls and returns live-shaped fake payloads."""

    tools: list[dict]
    equity: str = "1000"
    account_number: str = "FAKE0001"
    agentic_allowed: bool = True
    positions: list[dict] = field(default_factory=list)
    quotes: dict[str, Any] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    review_result: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    place_result: dict[str, Any] = field(
        default_factory=lambda: {"ok": True, "placed": True, "id": "order-1"}
    )
    place_error: Exception | None = None
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.tools = list(self.tools)

    def list_tools(self) -> list[dict]:
        return list(self.tools)

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append(Call(name=name, arguments=dict(arguments)))
        if name == "get_accounts":
            return {
                "results": [
                    {
                        "account_number": self.account_number,
                        "agentic_allowed": self.agentic_allowed,
                        "type": "cash",
                    }
                ]
            }
        if name == "get_portfolio":
            return {"total_equity": self.equity, "buying_power": self.equity}
        if name == "get_equity_positions":
            return {"results": list(self.positions)}
        if name == "get_equity_quotes":
            return self.quotes or {"quotes": []}
        if name == "get_equity_orders":
            return {"orders": list(self.orders)}
        if name == "review_equity_order":
            return dict(self.review_result)
        if name == "place_equity_order":
            if self.place_error is not None:
                raise self.place_error
            return dict(self.place_result)
        if name == "cancel_equity_order":
            return {"ok": True}
        return {}

    def calls_named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]
