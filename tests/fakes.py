"""Test doubles for MCP client interactions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict


class FakeMcpClient:
    """Records tool calls and returns sensible fake payloads."""

    def __init__(self, tools: list[dict]) -> None:
        self._tools = list(tools)
        self.calls: list[Call] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append(Call(name=name, arguments=dict(arguments)))
        if name == "get_account":
            return {"equity": "1000", "positions": []}
        if name == "review_equity_order":
            return {"ok": True}
        if name == "place_equity_order":
            return {"ok": True, "placed": True}
        return {}

    def list_tools(self) -> list[dict]:
        return list(self._tools)
