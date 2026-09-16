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
    rhs_account_number: str = "90000001"
    agentic_allowed: bool = True
    positions: list[dict] = field(default_factory=list)
    crypto_holdings: dict[str, str] = field(default_factory=dict)
    crypto_orders: list[dict] = field(default_factory=list)
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
                        # Crypto tools want this numeric id, not the alphanumeric
                        # one; the live get_accounts payload carries both.
                        "rhs_account_number": self.rhs_account_number,
                        "agentic_allowed": self.agentic_allowed,
                        "type": "cash",
                    }
                ]
            }
        if name == "get_portfolio":
            return {"total_equity": self.equity, "buying_power": self.equity}
        if name == "get_equity_positions":
            return {"results": list(self.positions)}
        if name == "get_crypto_positions":
            # Recorded live shape: rows carry currency.code, not a pair symbol.
            return {
                "data": {
                    "results": [
                        {
                            "currency": {
                                "code": code,
                                "name": code,
                                "type": "cryptocurrency",
                            },
                            "quantity": quantity,
                        }
                        for code, quantity in self.crypto_holdings.items()
                    ],
                    "next": "",
                }
            }
        if name == "get_crypto_orders":
            return {"data": {"results": list(self.crypto_orders), "next": ""}}
        if name == "get_equity_quotes":
            return self.quotes or {"quotes": []}
        if name == "get_equity_orders":
            return {"orders": list(self.orders)}
        if name in ("review_equity_order", "preview_crypto_order"):
            return dict(self.review_result)
        if name in ("place_equity_order", "place_crypto_order"):
            if self.place_error is not None:
                raise self.place_error
            return dict(self.place_result)
        if name == "cancel_equity_order":
            return {"ok": True}
        return {}

    def calls_named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]
