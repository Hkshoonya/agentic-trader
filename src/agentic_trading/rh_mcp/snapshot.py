"""Persist MCP tools/list snapshots and derive capability maps."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

_EXACT_CAPABILITY_MAP: dict[str, str] = {
    "review_equity_order": "review_equity",
    "place_equity_order": "place_equity",
    "get_account": "get_account",
}


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
        if "review_equity" not in capability_map and _matches_review_equity(tool_name):
            capability_map["review_equity"] = tool_name
        if "place_equity" not in capability_map and _matches_place_equity(tool_name):
            capability_map["place_equity"] = tool_name
        if "get_account" not in capability_map and _matches_get_account(tool_name):
            capability_map["get_account"] = tool_name

    return capability_map


def _tokens(name: str) -> frozenset[str]:
    return frozenset(name.lower().replace("-", "_").split("_"))


def _matches_review_equity(name: str) -> bool:
    tokens = _tokens(name)
    return "review" in tokens and "equity" in tokens


def _matches_place_equity(name: str) -> bool:
    tokens = _tokens(name)
    return "place" in tokens and ("equity" in tokens or "order" in tokens)


def _matches_get_account(name: str) -> bool:
    tokens = _tokens(name)
    return "account" in tokens or ("equity" in tokens and "portfolio" in tokens)


def _tool_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    return str(name) if name else ""
