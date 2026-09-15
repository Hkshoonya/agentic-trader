"""Operator CLI for agentic-trading (run / status / mode / kill-switch / auth stub)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from agentic_trading.broker import Broker
from agentic_trading.config import Config, load_config
from agentic_trading.journal import DecisionJournal
from agentic_trading.runtime import (
    build_guard,
    effective_mode,
    run_loop,
    write_mode,
)
from agentic_trading.strategies.fixture import FixtureStrategy

# Minimal tools list when no snapshot/token is available (Phase 0 local / CI).
_FALLBACK_TOOLS: list[dict[str, Any]] = [
    {
        "name": "review_equity_order",
        "description": "Simulate an equity order before placement",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_account",
        "description": "Read Agentic account equity and positions",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "place_equity_order",
        "description": "Place an equity order",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _load_tools(config: Config) -> list[dict[str, Any]]:
    path = Path(config.tools_snapshot_path)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "tools" in payload:
            return list(payload["tools"])
        if isinstance(payload, list):
            return payload
    return list(_FALLBACK_TOOLS)


def _token_available(config: Config) -> bool:
    return Path(config.token_path).expanduser().is_file()


class _Phase0FakeMcpClient:
    """Local stand-in until Task 8 ships the real MCP client."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self._tools = list(tools)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "get_account":
            return {"equity": "1000", "positions": []}
        if name == "review_equity_order":
            return {"ok": True}
        if name == "place_equity_order":
            return {"ok": True, "placed": True}
        return {}

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)


def build_broker(
    config: Config,
    *,
    client: Any = None,
    tools: Optional[list[dict[str, Any]]] = None,
) -> tuple[Broker, list[dict[str, Any]]]:
    """Build a Broker; inject ``client``/``tools`` for tests, else Fake when no token."""
    resolved_tools = list(tools) if tools is not None else _load_tools(config)
    if client is not None:
        return Broker(client, resolved_tools), resolved_tools

    if not _token_available(config):
        print(
            "warning: no MCP token at "
            f"{config.token_path}; using FakeMcpClient "
            "(OAuth/real client lands in Task 8)",
            file=sys.stderr,
        )
    else:
        print(
            "warning: MCP OAuth client not implemented yet (Task 8); "
            "using FakeMcpClient despite token file",
            file=sys.stderr,
        )
    fake = _Phase0FakeMcpClient(resolved_tools)
    return Broker(fake, resolved_tools), resolved_tools


def cmd_run(
    config_path: str,
    *,
    broker: Optional[Broker] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    strategy: Any = None,
) -> int:
    config = load_config(config_path)
    if broker is None:
        broker, tools = build_broker(config, tools=tools)
    run_loop(
        config,
        broker=broker,
        strategy=strategy or FixtureStrategy(),
        tools=tools,
    )
    return 0


def cmd_status(config_path: str) -> int:
    config = load_config(config_path)
    mode = effective_mode(config)
    journal = DecisionJournal(Path(config.journal_dir))
    guard = build_guard(config, mode)
    guard.load(config.state_dir, journal)
    print(f"mode: {mode}")
    print(f"kill_switch: {guard._kill_switch}")  # noqa: SLF001
    print(f"kill_reason: {guard._kill_reason}")  # noqa: SLF001
    print(f"daily_notional: {guard._daily_notional}")  # noqa: SLF001
    print(f"baseline_equity: {guard.baseline_equity}")
    print(f"current_equity: {guard.current_equity}")
    return 0


def cmd_flip_mode(config_path: str, mode: str) -> int:
    config = load_config(config_path)
    write_mode(config.state_dir, mode)
    print(f"mode set to {mode} ({config.state_dir / 'mode'})")
    return 0


def cmd_reset_kill_switch(config_path: str) -> int:
    config = load_config(config_path)
    mode = effective_mode(config)
    journal = DecisionJournal(Path(config.journal_dir))
    guard = build_guard(config, mode)
    guard.load(config.state_dir, journal)
    guard.reset_kill_switch()
    guard.persist(config.state_dir)
    print("kill switch reset")
    return 0


def cmd_auth() -> int:
    print(
        "OAuth implemented in Task 8; run agentic-trading auth after Task 8",
        file=sys.stderr,
    )
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-trading")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run quote loop (shadow by default)")
    run_p.add_argument("--config", required=True, help="Path to agentic TOML config")

    status_p = sub.add_parser(
        "status", help="Print mode, kill, daily notional, baseline"
    )
    status_p.add_argument("--config", required=True)

    flip_p = sub.add_parser(
        "flip-mode", help="Persist shadow|live under state_dir/mode"
    )
    flip_p.add_argument("mode", choices=["shadow", "live"])
    flip_p.add_argument("--config", required=True)

    reset_p = sub.add_parser("reset-kill-switch", help="Clear RiskGuard kill switch")
    reset_p.add_argument("--config", required=True)

    sub.add_parser("auth", help="OAuth (Task 8)")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "run":
        return cmd_run(args.config)
    if args.command == "status":
        return cmd_status(args.config)
    if args.command == "flip-mode":
        return cmd_flip_mode(args.config, args.mode)
    if args.command == "reset-kill-switch":
        return cmd_reset_kill_switch(args.config)
    if args.command == "auth":
        return cmd_auth()
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
