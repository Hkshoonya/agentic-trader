"""Operator CLI for agentic-trading (run / status / mode / kill-switch / auth)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import agentic_trading  # noqa: F401 — repo-root path for paper_scalper
import paper_scalper
from agentic_trading.broker import Broker
from agentic_trading.config import Config, load_config
from agentic_trading.journal import DecisionJournal
from agentic_trading.rh_mcp.client import RobinhoodMcpClient
from agentic_trading.rh_mcp.oauth import AuthFailed, load_tokens, run_desktop_oauth
from agentic_trading.rh_mcp.snapshot import write_tools_snapshot
from agentic_trading.runtime import (
    build_guard,
    effective_mode,
    run_loop,
    write_mode,
)
from agentic_trading.llm import FakeLlmClient, build_llm_client
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.strategies.llm_multi_asset import LlmMultiAssetStrategy
from agentic_trading.strategies.spy_scalper import SpyScalperStrategy

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
    path = Path(config.token_path).expanduser()
    if not path.is_file():
        return False
    return load_tokens(path) is not None


class _Phase0FakeMcpClient:
    """Local stand-in when tokens are missing (CI / offline)."""

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
    """Build a Broker; inject ``client``/``tools`` for tests.

    Uses real ``RobinhoodMcpClient`` when tokens exist; otherwise Fake.
    """
    resolved_tools = list(tools) if tools is not None else _load_tools(config)
    if client is not None:
        return Broker(client, resolved_tools), resolved_tools

    if _token_available(config):
        try:
            real = RobinhoodMcpClient(
                config.mcp_url,
                token_path=config.token_path,
            )
            # Prefer live tools/list when authenticated.
            if tools is None:
                try:
                    resolved_tools = real.list_tools()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"warning: tools/list failed ({exc}); "
                        "falling back to snapshot/fixture tools",
                        file=sys.stderr,
                    )
            return Broker(real, resolved_tools), resolved_tools
        except AuthFailed as exc:
            print(
                f"warning: MCP auth failed ({exc}); using FakeMcpClient",
                file=sys.stderr,
            )

    print(
        "warning: no usable MCP token at "
        f"{config.token_path}; using FakeMcpClient "
        "(run: agentic-trading auth --config …)",
        file=sys.stderr,
    )
    fake = _Phase0FakeMcpClient(resolved_tools)
    return Broker(fake, resolved_tools), resolved_tools


def load_scalper_config(path: Path | None) -> paper_scalper.Config:
    """Load paper_scalper.Config from JSON path, or defaults."""
    if path is not None and Path(path).is_file():
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return paper_scalper.Config(**payload)
    return paper_scalper.Config()


def build_strategy(
    config: Config,
    strategy_name: Optional[str] = None,
) -> FixtureStrategy | SpyScalperStrategy | LlmMultiAssetStrategy:
    """Select strategy plugin from config / CLI override."""
    name = (strategy_name or config.strategy or "fixture").strip().lower()
    if name == "fixture":
        return FixtureStrategy()
    if name == "spy_scalper":
        return SpyScalperStrategy(load_scalper_config(config.scalper_config))
    if name == "llm":
        client = build_llm_client()
        if isinstance(client, FakeLlmClient):
            print(
                "warning: no AGENTIC_LLM_API_KEY; using FakeLlmClient "
                "(set AGENTIC_LLM_BASE_URL / AGENTIC_LLM_API_KEY / "
                "AGENTIC_LLM_MODEL for OpenAI-compatible HTTP)",
                file=sys.stderr,
            )
        return LlmMultiAssetStrategy(
            client=client,
            whitelist=config.symbol_whitelist,
        )
    raise ValueError(f"unknown strategy: {name}")


def cmd_run(
    config_path: str,
    *,
    broker: Optional[Broker] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    strategy: Any = None,
    strategy_name: Optional[str] = None,
) -> int:
    config = load_config(config_path)
    if broker is None:
        broker, tools = build_broker(config, tools=tools)
    resolved = (
        strategy if strategy is not None else build_strategy(config, strategy_name)
    )
    run_loop(
        config,
        broker=broker,
        strategy=resolved,
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


def cmd_auth(config_path: str) -> int:
    config = load_config(config_path)
    try:
        run_desktop_oauth(config.token_path)
    except AuthFailed as exc:
        print(f"auth failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("auth cancelled", file=sys.stderr)
        return 130
    return 0


def cmd_snapshot_tools(config_path: str) -> int:
    config = load_config(config_path)
    if not _token_available(config):
        print(
            f"no tokens at {config.token_path}; run: agentic-trading auth --config …",
            file=sys.stderr,
        )
        return 1
    try:
        client = RobinhoodMcpClient(config.mcp_url, token_path=config.token_path)
        tools = client.list_tools()
    except AuthFailed as exc:
        print(f"auth failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"snapshot-tools failed: {exc}", file=sys.stderr)
        return 1
    write_tools_snapshot(tools, config.tools_snapshot_path, dated_copy=True)
    print(f"wrote {len(tools)} tools to {config.tools_snapshot_path} (+ dated copy)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-trading")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run quote loop (shadow by default)")
    run_p.add_argument("--config", required=True, help="Path to agentic TOML config")
    run_p.add_argument(
        "--strategy",
        choices=["fixture", "spy_scalper", "llm"],
        default=None,
        help="Override strategy from config (default: config or fixture)",
    )

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

    auth_p = sub.add_parser("auth", help="OAuth 2.1 PKCE desktop flow; save tokens")
    auth_p.add_argument("--config", required=True)

    snap_p = sub.add_parser(
        "snapshot-tools",
        help="Authenticated tools/list → tools_snapshot_path (+ dated copy)",
    )
    snap_p.add_argument("--config", required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "run":
        return cmd_run(args.config, strategy_name=args.strategy)
    if args.command == "status":
        return cmd_status(args.config)
    if args.command == "flip-mode":
        return cmd_flip_mode(args.config, args.mode)
    if args.command == "reset-kill-switch":
        return cmd_reset_kill_switch(args.config)
    if args.command == "auth":
        return cmd_auth(args.config)
    if args.command == "snapshot-tools":
        return cmd_snapshot_tools(args.config)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
