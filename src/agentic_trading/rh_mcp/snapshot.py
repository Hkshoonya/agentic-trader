"""Persist MCP tools/list snapshots and derive capability maps."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

# Exact tool names → logical capabilities (verified against the live MCP tool
# list on 2026-09-16). Heuristics below are a fallback for renames.
_EXACT_CAPABILITY_MAP: dict[str, str] = {
    "get_accounts": "list_accounts",
    "get_portfolio": "get_portfolio",
    "get_equity_positions": "get_positions",
    "get_equity_quotes": "get_quotes",
    "get_equity_price_book": "get_price_book",
    "get_equity_tradability": "get_tradability",
    "get_equity_historicals": "get_historicals",
    "get_equity_technical_indicators": "get_indicators",
    "review_equity_order": "review_equity",
    "place_equity_order": "place_equity",
    "get_equity_orders": "get_orders",
    "cancel_equity_order": "cancel_equity",
    # Crypto is a separate namespace on purpose: an equity capability can never
    # bind to a crypto tool, and vice versa. Crypto orders need their own
    # capabilities because the live crypto tools reject the equity argument
    # shape outright ("unexpected additional properties [rhs_account_number]").
    "get_crypto_quotes": "get_crypto_quotes",
    "get_crypto_positions": "get_crypto_positions",
    "get_crypto_orders": "get_crypto_orders",
    "get_pnl_trade_history": "get_trade_history",
    "preview_crypto_order": "preview_crypto",
    "place_crypto_order": "place_crypto",
    "cancel_crypto_order": "cancel_crypto",
}

# Asset classes this module must never bind to equity capabilities.
_NON_EQUITY_TOKENS = frozenset(
    {"option", "options", "crypto", "advanced", "index", "indexes"}
)


def write_tools_snapshot(
    tools: list[dict[str, Any]],
    path: Path | str,
    *,
    dated_copy: bool = False,
) -> None:
    """Write tools list to ``path``; optionally also write a dated sibling copy."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    capability_map = build_capability_map(tools)
    payload = {"tools": tools, "capability_map": capability_map}
    text = json.dumps(payload, indent=2) + "\n"
    dest.write_text(text, encoding="utf-8")
    if dated_copy:
        dated = dest.with_name(f"{dest.stem}_{date.today().isoformat()}{dest.suffix}")
        dated.write_text(text, encoding="utf-8")


def build_capability_map(
    tools: list[dict[str, Any]],
    existing: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map logical broker capabilities to concrete MCP tool names."""
    capability_map: dict[str, str] = dict(existing or {})
    tool_names = [_tool_name(tool) for tool in tools]
    tool_names = [name for name in tool_names if name]

    for tool_name in tool_names:
        logical = _EXACT_CAPABILITY_MAP.get(tool_name)
        if logical and logical not in capability_map:
            capability_map[logical] = tool_name

    for tool_name in tool_names:
        if _tokens(tool_name) & _NON_EQUITY_TOKENS:
            # Never let an option/crypto tool satisfy an equity capability.
            continue
        if "review_equity" not in capability_map and _matches_review_equity(tool_name):
            capability_map["review_equity"] = tool_name
        if "place_equity" not in capability_map and _matches_place_equity(tool_name):
            capability_map["place_equity"] = tool_name
        if "list_accounts" not in capability_map and _matches_accounts(tool_name):
            capability_map["list_accounts"] = tool_name
        if "get_portfolio" not in capability_map and _matches_portfolio(tool_name):
            capability_map["get_portfolio"] = tool_name
        if "get_positions" not in capability_map and _matches_positions(tool_name):
            capability_map["get_positions"] = tool_name
        if "get_quotes" not in capability_map and _matches_quotes(tool_name):
            capability_map["get_quotes"] = tool_name
        if "get_orders" not in capability_map and _matches_orders(tool_name):
            capability_map["get_orders"] = tool_name
        if "cancel_equity" not in capability_map and _matches_cancel(tool_name):
            capability_map["cancel_equity"] = tool_name
        if "get_historicals" not in capability_map and _matches_historicals(tool_name):
            capability_map["get_historicals"] = tool_name

    return capability_map


def _tokens(name: str) -> frozenset[str]:
    return frozenset(name.lower().replace("-", "_").split("_"))


def _matches_review_equity(name: str) -> bool:
    tokens = _tokens(name)
    return "review" in tokens and bool(tokens & {"equity", "stock", "order"})


def _matches_place_equity(name: str) -> bool:
    tokens = _tokens(name)
    return "place" in tokens and ("equity" in tokens or "order" in tokens)


def _matches_accounts(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens & {"accounts", "account"}) and bool(tokens & {"get", "list"})


def _matches_portfolio(name: str) -> bool:
    tokens = _tokens(name)
    return "portfolio" in tokens


def _matches_positions(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens & {"positions", "position"}) and bool(
        tokens & {"get", "list"}
    )


def _matches_quotes(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens & {"quotes", "quote"}) and bool(tokens & {"get", "list"})


def _matches_orders(name: str) -> bool:
    tokens = _tokens(name)
    return "orders" in tokens and bool(tokens & {"get", "list"})


def _matches_cancel(name: str) -> bool:
    tokens = _tokens(name)
    return "cancel" in tokens and bool(tokens & {"order", "orders", "equity"})


def _matches_historicals(name: str) -> bool:
    tokens = _tokens(name)
    return bool(tokens & {"historicals", "historical", "history", "bars", "candles"})


def _tool_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    return str(name) if name else ""
