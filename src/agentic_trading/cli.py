"""Operator CLI for agentic-trading (run / status / mode / kill-switch / auth)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
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
    run_daemon,
    run_loop,
    write_mode,
)
from agentic_trading.llm import FakeLlmClient, build_llm_client
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.strategies.llm_multi_asset import LlmMultiAssetStrategy
from agentic_trading.strategies.spy_scalper import SpyScalperStrategy

# Minimal tools list when no snapshot/token is available (offline / CI).
# Names and required arguments mirror the live tool schema (2026-09-16).
_FALLBACK_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_accounts",
        "description": "List brokerage accounts",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_portfolio",
        "description": "Account market value and buying power",
        "inputSchema": {
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
        },
    },
    {
        "name": "get_equity_positions",
        "description": "List open equity positions",
        "inputSchema": {
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
        },
    },
    {
        "name": "get_equity_quotes",
        "description": "Real-time stock quotes",
        "inputSchema": {
            "type": "object",
            "properties": {"symbols": {"type": "array"}},
            "required": ["symbols"],
        },
    },
    {
        "name": "review_equity_order",
        "description": "Simulate an equity order before placement",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_number": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string"},
                "type": {"type": "string"},
            },
            "required": ["account_number", "symbol", "side", "type"],
        },
    },
    {
        "name": "place_equity_order",
        "description": "Place an equity order",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_number": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string"},
                "type": {"type": "string"},
            },
            "required": ["account_number", "symbol", "side", "type"],
        },
    },
    {
        "name": "get_equity_orders",
        "description": "List equity orders",
        "inputSchema": {
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
        },
    },
    {
        "name": "cancel_equity_order",
        "description": "Cancel an open equity order",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_number": {"type": "string"},
                "order_id": {"type": "string"},
            },
            "required": ["account_number", "order_id"],
        },
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


class _OfflineFakeMcpClient:
    """Local stand-in when tokens are missing (CI / offline).

    Mirrors the real payload *shapes* closely enough that capability mapping and
    parsing are exercised the same way they are against the live broker.
    """

    def __init__(self, tools: list[dict[str, Any]], *, equity: str = "1000") -> None:
        self._tools = list(tools)
        self._equity = equity
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "get_accounts":
            return {
                "results": [
                    {"account_number": "OFFLINE0001", "agentic_allowed": True}
                ]
            }
        if name == "get_portfolio":
            return {"total_equity": self._equity}
        if name == "get_equity_positions":
            return {"results": []}
        if name == "get_equity_quotes":
            return {"quotes": []}
        if name == "get_equity_orders":
            return {"orders": []}
        if name == "review_equity_order":
            return {"ok": True}
        if name == "place_equity_order":
            return {"ok": True, "placed": True}
        if name == "cancel_equity_order":
            return {"ok": True}
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
            return (
                Broker(real, resolved_tools, account_number=config.account_number),
                resolved_tools,
            )
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
    fake = _OfflineFakeMcpClient(resolved_tools)
    return (
        Broker(fake, resolved_tools, account_number=config.account_number),
        resolved_tools,
    )


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
            max_quote_age_seconds=config.max_quote_age_seconds,
        )
    if name == "trend_crypto":
        from agentic_trading.strategies.trend_crypto import TrendCryptoStrategy

        bar_dir = (
            Path(config.history_path)
            if config.history_path
            else Path("data/bars")
        )
        # Pass the broker form (BTC-USD) so emitted intents pass the RiskGuard
        # whitelist; the strategy maps to the bar-file name (BTCUSD) internally.
        symbols = [
            symbol.upper()
            for symbol in sorted(config.symbol_whitelist)
            if "-" in symbol or symbol.upper().endswith("USD")
        ]
        if not symbols:
            raise ValueError(
                "trend_crypto needs crypto pairs in symbol_whitelist, e.g. BTC-USD"
            )
        return TrendCryptoStrategy(
            bar_dir=bar_dir,
            symbols=symbols,
            max_positions=config.max_open_positions * 5,
        )
    raise ValueError(f"unknown strategy: {name}")


def cmd_run(
    config_path: str,
    *,
    broker: Optional[Broker] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    strategy: Any = None,
    strategy_name: Optional[str] = None,
    daemon: bool = False,
    once: bool = False,
    duration_seconds: Optional[float] = None,
    dry_run: bool = False,
    session_policy: Optional[str] = None,
) -> int:
    config = load_config(config_path)
    if session_policy is not None:
        config = replace(config, session_policy=session_policy)
    if broker is None:
        broker, tools = build_broker(config, tools=tools)
    resolved = (
        strategy if strategy is not None else build_strategy(config, strategy_name)
    )
    if daemon or once:
        run_daemon(
            config,
            broker=broker,
            strategy=resolved,
            tools=tools,
            duration_seconds=duration_seconds,
            once=once,
            force_shadow=dry_run,
        )
    else:
        run_loop(
            config,
            broker=broker,
            strategy=resolved,
            tools=tools,
            force_shadow=dry_run,
        )
    return 0


def cmd_accounts(config_path: str) -> int:
    """Read-only: list accounts and show which one the bot would use."""
    config = load_config(config_path)
    broker, _ = build_broker(config)
    accounts = broker.list_accounts()
    for account in accounts:
        trader = account.get("agentic_allowed") is True
        print(
            f"{account.get('account_number')}\t"
            f"agentic_allowed={str(trader).lower()}\t"
            f"type={account.get('type') or account.get('account_type') or '?'}"
        )
    try:
        selected = broker.resolve_account_number()
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostics
        print(f"no account selected: {exc}", file=sys.stderr)
        return 1
    print(f"selected for trading: {selected}")
    return 0


def cmd_probe(config_path: str, *, out_path: Optional[str] = None) -> int:
    """Read-only: dump raw account/quote payloads to confirm parser shapes.

    Places no orders and changes no broker state.
    """
    config = load_config(config_path)
    broker, tools = build_broker(config)
    report: dict[str, Any] = {
        "mcp_url": config.mcp_url,
        "capability_map": broker._capability_map,  # noqa: SLF001 — operator view
        "tools": [t.get("name") for t in tools],
    }
    try:
        report["accounts"] = broker.list_accounts()
    except Exception as exc:  # noqa: BLE001
        report["accounts_error"] = str(exc)

    if "accounts_error" not in report:
        try:
            account = broker.resolve_account_number()
            report["resolved_account"] = account
            report["portfolio"] = broker.get_portfolio(account)
            positions = broker.get_positions(account)
            report["positions"] = {
                "held": {k: str(v) for k, v in positions.held.items()},
                "positions_read_failed": positions.positions_read_failed,
            }
        except Exception as exc:  # noqa: BLE001
            report["account_error"] = str(exc)

    try:
        symbols = sorted(config.symbol_whitelist)[:20]
        report["quotes_request"] = symbols
        report["quotes"] = broker.get_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        report["quotes_error"] = str(exc)

    text = json.dumps(report, indent=2, default=str)
    if out_path is None:
        destination = Path(config.state_dir) / "probe.json"
    else:
        destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    destination.chmod(0o600)  # contains real account identifiers
    print(text)
    print(
        f"\nwrote {destination} (contains account data; keep it out of git)",
        file=sys.stderr,
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
    from agentic_trading.promotion import load_state

    promotion = load_state(config.state_dir)
    print(f"stage: {promotion.stage} (streak {promotion.streak}"
          f"/{config.promotion_cycles_required})")
    print(f"autonomy: {config.autonomy} (auto-promotion "
          f"{'enabled' if config.autonomy == 'auto' else 'disabled'})")
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


def _profile_opener(profile: str):
    """Open the authorize URL in a specific Chrome profile (multi-profile hosts)."""
    import shutil
    import subprocess

    binary = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if binary is None:
        raise FileNotFoundError("no Chrome/Chromium binary found")

    def open_in_profile(url: str) -> None:
        subprocess.Popen(  # noqa: S603 — fixed binary, operator-supplied profile
            [binary, f"--profile-directory={profile}", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return open_in_profile


def cmd_auth(config_path: str, *, profile: Optional[str] = None) -> int:
    config = load_config(config_path)
    try:
        run_desktop_oauth(
            config.token_path,
            open_url=_profile_opener(profile) if profile else None,
        )
    except AuthFailed as exc:
        print(f"auth failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("auth cancelled", file=sys.stderr)
        return 130
    return 0


def cmd_fetch_history(
    config_path: str,
    *,
    symbol: str,
    start: str,
    end: Optional[str] = None,
    interval: Optional[str] = None,
    out: Optional[str] = None,
    print_shape: bool = False,
) -> int:
    """Read-only: fetch OHLCV bars through the authenticated MCP connection."""
    from agentic_trading.history import parse_bars_payload, save_bars

    config = load_config(config_path)
    broker, _ = build_broker(config)
    try:
        payload = broker.get_historicals(
            [symbol], start_time=start, end_time=end, interval=interval
        )
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostics
        print(f"fetch-history failed: {exc}", file=sys.stderr)
        return 1
    if print_shape:
        print(json.dumps(payload, indent=2, default=str)[:4000])
    try:
        bars = parse_bars_payload(payload, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        print(
            f"could not parse bars ({exc}). Re-run with --print-shape and "
            "report the payload keys.",
            file=sys.stderr,
        )
        return 1
    destination = Path(out) if out else Path(f"data/bars/{symbol}_{interval or 'auto'}.jsonl")
    count = save_bars(destination, bars)
    first = bars[0].start.isoformat() if bars else "-"
    last = bars[-1].start.isoformat() if bars else "-"
    print(f"wrote {count} bars to {destination} ({first} .. {last})")
    return 0


def cmd_import_history(
    csv_path: str, *, symbol: str, out: Optional[str] = None
) -> int:
    """Import bars from a CSV, or convert recorded quotes (.jsonl) into bars."""
    from agentic_trading.history import bars_from_quotes, load_bars_csv, save_bars

    source = Path(csv_path)
    if source.suffix.lower() in (".jsonl", ".ndjson"):
        bars = bars_from_quotes(source)
    else:
        bars = load_bars_csv(source, symbol=symbol)
    if not bars:
        print(f"no usable bars in {csv_path}", file=sys.stderr)
        return 1
    destination = Path(out) if out else Path(f"data/bars/{symbol}_imported.jsonl")
    print(f"wrote {save_bars(destination, bars)} bars to {destination}")
    return 0


def cmd_evolve(
    config_path: str,
    *,
    population: Optional[int] = None,
    generations: Optional[int] = None,
    seed: int = 42,
    symbols: Optional[str] = None,
    interval: str = "5minute",
    families: Optional[str] = None,
) -> int:
    """Run an evolution cycle, assess it, and record the verdict."""
    from agentic_trading import selfimprove

    config = load_config(config_path)
    try:
        if symbols:
            from agentic_trading.families import evolve_with_families, families_for
            from agentic_trading.multisymbol import load_symbol_bars
            from agentic_trading.promotion import assess, load_state
            from agentic_trading.promotion import policy_from_config, save_state
            from agentic_trading.promotion import apply_assessment

            wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
            history = (
                Path(config.history_path)
                if config.history_path
                else Path("data/bars")
            )
            bar_dir = history if history.is_dir() else history.parent
            symbol_bars = load_symbol_bars(bar_dir, wanted, interval=interval)
            if not symbol_bars:
                print(
                    f"no bar files found in {bar_dir} for {wanted} "
                    f"(interval={interval})",
                    file=sys.stderr,
                )
                return 1
            allowed = families_for(interval)
            requested = (
                tuple(f.strip() for f in families.split(",") if f.strip())
                if families
                else allowed
            )
            usable = tuple(name for name in requested if name in allowed)
            skipped = [name for name in requested if name not in allowed]
            if skipped:
                print(
                    f"skipping intraday-only families for interval={interval}: {skipped}",
                    file=sys.stderr,
                )
            result = evolve_with_families(
                symbol_bars,
                families=usable or (allowed[0],),
                population=population or config.evolution_population,
                generations=generations or config.evolution_generations,
                seed=seed,
                min_oos_trades=config.min_oos_trades,
            )
            selfimprove.write_evolution(result, config.state_dir)
            assessment = assess(result, policy_from_config(config))
            state = load_state(config.state_dir)
            events = apply_assessment(
                state, assessment, policy_from_config(config)
            )
            save_state(config.state_dir, state)
            events.extend(
                selfimprove.update_limits(config, eligible=assessment.eligible)
            )
            print(f"symbols pooled: {', '.join(sorted(symbol_bars))}")
        else:
            assessment, state, events = selfimprove.evaluate_and_record(
                config, population=population, generations=generations, seed=seed
            )
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostics
        print(f"evolve failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(assessment.to_dict(), indent=2))
    print(f"stage: {state.stage} (streak {state.streak})")
    for event in events:
        print(json.dumps(event, default=str))
    if not assessment.eligible:
        print(
            "\nNOT eligible for promotion. The bot stays in shadow. "
            "Reasons above are the evidence gap.",
            file=sys.stderr,
        )
    return 0


def cmd_promote(config_path: str, stage: str) -> int:
    """Operator override: set the promotion stage explicitly."""
    from agentic_trading import selfimprove
    from agentic_trading.promotion import load_state, save_state

    config = load_config(config_path)
    state = load_state(config.state_dir)
    state.stage = stage
    state.streak = 0
    state.updated_at = datetime.now(timezone.utc).isoformat()
    state.stage_equity = ""
    save_state(config.state_dir, state)
    events = selfimprove.apply_stage(config, state)
    for event in events:
        print(json.dumps(event))
    print(f"stage set to {stage} (operator override)")
    return 0


def cmd_dashboard(
    config_path: str, *, host: str, port: int, open_browser: bool
) -> int:
    """Serve the read-only animated console (Ctrl+C to stop)."""
    from agentic_trading.dashboard import serve

    config = load_config(config_path)
    server = serve(
        config,
        host=host,
        port=port,
        open_browser=open_browser,
        config_path=config_path,
    )
    print(f"dashboard: http://{host}:{port}/  (read-only, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        server.server_close()
    return 0


def cmd_selfcheck(config_path: str, *, offline: bool = False) -> int:
    """Read-only: verify state, data, analysis path, broker and plumbing."""
    from agentic_trading.selfcheck import run_checks, write_report

    config = load_config(config_path)
    broker = None
    if not offline:
        try:
            broker, _ = build_broker(config)
        except Exception as exc:  # noqa: BLE001 — report rather than crash
            print(f"broker unavailable ({exc}); running the offline checks only")
    report = run_checks(config, broker, include_broker=broker is not None)
    for check in report.checks:
        print(f"{check.status.upper():5s} {check.name:9s} {check.ms:7.0f}ms  {check.detail}")
    write_report(config, report)
    print()
    print("healthy" if report.healthy else "ATTENTION REQUIRED")
    return 0 if report.healthy else 1


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
    run_p.add_argument(
        "--daemon",
        action="store_true",
        help="Continuous autonomous loop over the configured quote source",
    )
    run_p.add_argument(
        "--once",
        action="store_true",
        help="Run a single daemon cycle (useful for a live smoke test)",
    )
    run_p.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="Stop the daemon after this many seconds",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Force shadow mode even if state_dir/mode says live",
    )
    run_p.add_argument(
        "--session-policy",
        choices=["regular", "extended", "all", "any"],
        default=None,
        help="Override config session_policy for this run",
    )

    accounts_p = sub.add_parser(
        "accounts", help="Read-only: list accounts and the one used for trading"
    )
    accounts_p.add_argument("--config", required=True)

    probe_p = sub.add_parser(
        "probe",
        help="Read-only: dump raw account/quote payloads to confirm parser shapes",
    )
    probe_p.add_argument("--config", required=True)
    probe_p.add_argument(
        "--out", default=None, help="Destination path (default: state_dir/probe.json)"
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
    check_p = sub.add_parser(
        "selfcheck",
        help="Read-only: verify state, data, analysis path, broker and plumbing",
    )
    check_p.add_argument("--config", required=True)
    check_p.add_argument(
        "--offline",
        action="store_true",
        help="Skip the broker round trips (no network)",
    )

    auth_p = sub.add_parser("auth", help="OAuth 2.1 PKCE desktop flow; save tokens")
    auth_p.add_argument("--config", required=True)
    auth_p.add_argument(
        "--profile",
        default=None,
        help='Chrome profile directory to open, e.g. "Profile 1" (default: system browser)',
    )

    snap_p = sub.add_parser(
        "snapshot-tools",
        help="Authenticated tools/list → tools_snapshot_path (+ dated copy)",
    )
    snap_p.add_argument("--config", required=True)

    fetch_p = sub.add_parser(
        "fetch-history",
        help="Read-only: fetch OHLCV bars for backtesting via the MCP connection",
    )
    fetch_p.add_argument("--config", required=True)
    fetch_p.add_argument("--symbol", required=True)
    fetch_p.add_argument("--start", required=True, help="RFC3339 UTC start")
    fetch_p.add_argument("--end", default=None)
    fetch_p.add_argument(
        "--interval",
        default=None,
        help="minute, 5minute, hour, day, ... (omit to let the server choose)",
    )
    fetch_p.add_argument("--out", default=None, help="Destination JSONL path")
    fetch_p.add_argument(
        "--print-shape",
        action="store_true",
        help="Print the raw payload (truncated) to confirm the parser shape",
    )

    import_p = sub.add_parser(
        "import-history", help="Import OHLCV bars from CSV for backtesting"
    )
    import_p.add_argument("csv_path")
    import_p.add_argument("--symbol", required=True)
    import_p.add_argument("--out", default=None)

    evolve_p = sub.add_parser(
        "evolve", help="Evolve genomes, assess out-of-sample, record the verdict"
    )
    evolve_p.add_argument("--config", required=True)
    evolve_p.add_argument("--population", type=int, default=None)
    evolve_p.add_argument("--generations", type=int, default=None)
    evolve_p.add_argument("--seed", type=int, default=42)
    evolve_p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols to pool, e.g. SPY,QQQ,IWM "
        "(loads data/bars/<SYMBOL>_<interval>.jsonl)",
    )
    evolve_p.add_argument("--interval", default="5minute", help="Bar file interval tag")
    evolve_p.add_argument(
        "--families",
        default=None,
        help="Comma-separated families to evaluate: base,vol_regime,session_effects "
        "(default: all that suit the interval)",
    )

    promote_p = sub.add_parser(
        "promote", help="Operator override: set promotion stage"
    )
    promote_p.add_argument("stage", choices=["shadow", "probation", "live"])
    promote_p.add_argument("--config", required=True)

    dash_p = sub.add_parser(
        "dashboard", help="Serve the animated read-only operator console"
    )
    dash_p.add_argument("--config", required=True)
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8787)
    dash_p.add_argument("--open", action="store_true", dest="open_browser")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "run":
        return cmd_run(
            args.config,
            strategy_name=args.strategy,
            daemon=args.daemon,
            once=args.once,
            duration_seconds=args.duration_seconds,
            dry_run=args.dry_run,
            session_policy=args.session_policy,
        )
    if args.command == "accounts":
        return cmd_accounts(args.config)
    if args.command == "probe":
        return cmd_probe(args.config, out_path=args.out)
    if args.command == "fetch-history":
        return cmd_fetch_history(
            args.config,
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            interval=args.interval,
            out=args.out,
            print_shape=args.print_shape,
        )
    if args.command == "import-history":
        return cmd_import_history(args.csv_path, symbol=args.symbol, out=args.out)
    if args.command == "evolve":
        return cmd_evolve(
            args.config,
            population=args.population,
            generations=args.generations,
            seed=args.seed,
            symbols=args.symbols,
            interval=args.interval,
            families=args.families,
        )
    if args.command == "promote":
        return cmd_promote(args.config, args.stage)
    if args.command == "dashboard":
        return cmd_dashboard(
            args.config, host=args.host, port=args.port, open_browser=args.open_browser
        )
    if args.command == "status":
        return cmd_status(args.config)
    if args.command == "flip-mode":
        return cmd_flip_mode(args.config, args.mode)
    if args.command == "reset-kill-switch":
        return cmd_reset_kill_switch(args.config)
    if args.command == "selfcheck":
        return cmd_selfcheck(args.config, offline=args.offline)
    if args.command == "auth":
        return cmd_auth(args.config, profile=args.profile)
    if args.command == "snapshot-tools":
        return cmd_snapshot_tools(args.config)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
