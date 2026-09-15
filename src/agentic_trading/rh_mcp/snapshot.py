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

_HEURISTIC_RULES: tuple[tuple[str, str], ...] = (
    ("review", "review_equity"),
    ("place", "place_equity"),
    ("account", "get_account"),
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
    payload = {"tools": tools}
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
        lower = tool_name.lower()
        for needle, logical in _HEURISTIC_RULES:
            if needle in lower and logical not in capability_map:
                capability_map[logical] = tool_name

    return capability_map


def _tool_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    return str(name) if name else ""
