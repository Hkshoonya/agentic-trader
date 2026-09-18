"""End to end: does the scout change the book while the agent is running?

The unit tests cover the membership rules. This one runs the real daemon loop
with a stubbed broker whose "Trending stocks" list contains a name the operator
never configured, and checks the whole chain: the pass runs, the symbol is
adopted, the state file changes, and the runtime applies it to the guard,
strategy and quote feed without a restart.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading import discovery
from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.history import Bar, load_bars, save_bars
from agentic_trading.history_sync import bar_stem
from agentic_trading.runtime import run_daemon
from tests.fakes import Call, FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 9, 18, 17, 0, 0, tzinfo=timezone.utc)

# Tools the recorded snapshot predates. Discovery treats a missing mapping as
# "this account does not offer it", so the test has to offer them explicitly.
DISCOVERY_TOOLS = (
    "get_popular_watchlists",
    "get_watchlists",
    "get_watchlist_items",
    "get_currency_pairs",
    "get_equity_historicals",
)


def _walk(seed: int, *, drift: float, count: int = 300) -> list[float]:
    rng = random.Random(seed)
    price = 100.0
    closes: list[float] = []
    for _ in range(count):
        price *= 1.0 + drift + rng.gauss(0.0, 0.008)
        closes.append(round(price, 2))
    return closes


def _write_bars(directory: Path, symbol: str, closes: list[float]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    start = datetime(2019, 1, 1, tzinfo=timezone.utc)
    save_bars(
        directory / f"{bar_stem(symbol)}_day.jsonl",
        [
            Bar(
                symbol=bar_stem(symbol),
                start=start + timedelta(days=index),
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.01)),
                low=Decimal(str(close * 0.99)),
                close=Decimal(str(close)),
                volume=Decimal("1000000"),
            )
            for index, close in enumerate(closes)
        ],
    )


class _ScoutClient(FakeMcpClient):
    """The broker surfaces discovery reads, on top of the recorded fixtures."""

    def __init__(self, tools: list[dict], *, bar_dir: Path) -> None:
        super().__init__(tools)
        self.bar_dir = bar_dir

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append(Call(name=name, arguments=dict(arguments)))
        if name == "get_popular_watchlists":
            return {
                "results": [
                    {"id": "trending", "display_name": "Trending stocks"}
                ]
            }
        if name == "get_watchlists":
            return {"results": []}
        if name == "get_watchlist_items":
            if arguments.get("list_id") != "trending":
                return {"results": []}
            return {"results": [{"symbol": "NVDA", "object_type": "instrument"}]}
        if name == "get_currency_pairs":
            return {"results": []}
        if name == "get_equity_tradability":
            return {
                "results": [
                    {"symbol": symbol, "fractional": True}
                    for symbol in arguments.get("symbols", [])
                ]
            }
        if name == "get_equity_historicals":
            symbol = str(arguments["symbols"][0]).upper()
            path = self.bar_dir / f"{bar_stem(symbol)}_day.jsonl"
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"data": {"results": [{"symbol": symbol, "bars": records}]}}
        if name == "get_equity_quotes":
            return {
                "quotes": [
                    {
                        "symbol": symbol,
                        "bid_price": "99.99",
                        "ask_price": "100.01",
                    }
                    for symbol in arguments.get("symbols", [])
                ]
            }
        return super().call_tool(name, arguments)


class _QuietStrategy:
    """Decides nothing: this test is about the universe, not about orders."""

    def on_quote(self, quote: dict) -> list:
        return []


class _Feed:
    def poll(self) -> list[dict]:
        return [
            {
                "symbol": "SPY",
                "observed_at": FIXED_NOW.isoformat(),
                "quote_at": FIXED_NOW.isoformat(),
                "bid": Decimal("99.99"),
                "ask": Decimal("100.01"),
            }
        ]


def _config(tmp: Path) -> Path:
    path = tmp / "agentic.toml"
    path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                'strategy = "fixture"',
                'symbol_whitelist = ["SPY"]',
                'max_order_pct = "0.05"',
                'daily_notional_pct = "0.20"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 4",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 3600",
                'timezone = "local"',
                f'quotes_path = "{tmp / "quotes.jsonl"}"',
                f'journal_dir = "{tmp / "journal"}"',
                f'state_dir = "{tmp / "state"}"',
                f'history_path = "{tmp / "bars"}"',
                f'tools_snapshot_path = "{tmp / "tools.json"}"',
                f'token_path = "{tmp / "missing.json"}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                'autonomy = "auto"',
                "autonomy_enabled = true",
                "selfcheck_minutes = 0",
                "cycle_stats_seconds = 0",
                "discovery_enabled = true",
                "discovery_interval_hours = 0.001",
                "discovery_max_symbols = 2",
                "discovery_min_bars = 260",
                'discovery_min_dollar_volume = "1000000"',
                "discovery_min_hold_hours = 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _records(config) -> list[dict]:
    path = Path(config.journal_dir) / f"{FIXED_NOW.date().isoformat()}.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DiscoveryRuntimeTests(unittest.TestCase):
    def test_a_trending_name_is_adopted_and_applied_without_a_restart(self) -> None:
        tools = json.loads(
            (FIXTURES / "tools_snapshot.json").read_text(encoding="utf-8")
        )["tools"]
        tools = list(tools) + [{"name": name} for name in DISCOVERY_TOOLS]

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            bars = tmp / "bars"
            _write_bars(bars, "SPY", _walk(1, drift=0.004))
            _write_bars(bars, "NVDA", _walk(2, drift=0.006))
            config = load_config(_config(tmp))
            client = _ScoutClient(tools, bar_dir=bars)
            broker = Broker(client, tools)

            run_daemon(
                config,
                broker=broker,
                strategy=_QuietStrategy(),
                feed=_Feed(),
                duration_seconds=3.0,
                clock=lambda: FIXED_NOW,
                session_clock=lambda: "regular",
                sleep=lambda seconds: None,
            )

            adopted = discovery.load_universe(config.state_dir).adopted
            self.assertEqual(adopted, ("NVDA",))
            events = [record.get("event") for record in _records(config)]
            self.assertIn("discovery", events)
            self.assertIn("universe_changed", events)
            changed = [
                record
                for record in _records(config)
                if record.get("event") == "universe_changed"
            ][-1]
            self.assertIn("NVDA", changed["tradeable"])
            # A restart is not needed for the new universe to be the book the
            # rest of the system reads.
            self.assertIn("NVDA", load_config(_config(tmp)).effective_whitelist)

    def test_the_scout_does_not_run_when_the_operator_keeps_autonomy_manual(self) -> None:
        tools = json.loads(
            (FIXTURES / "tools_snapshot.json").read_text(encoding="utf-8")
        )["tools"] + [{"name": name} for name in DISCOVERY_TOOLS]

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            bars = tmp / "bars"
            _write_bars(bars, "SPY", _walk(1, drift=0.004))
            _write_bars(bars, "NVDA", _walk(2, drift=0.006))
            config_path = _config(tmp)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'autonomy = "auto"', 'autonomy = "manual"'
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            client = _ScoutClient(tools, bar_dir=bars)
            broker = Broker(client, tools)

            run_daemon(
                config,
                broker=broker,
                strategy=_QuietStrategy(),
                feed=_Feed(),
                duration_seconds=1.0,
                clock=lambda: FIXED_NOW,
                session_clock=lambda: "regular",
                sleep=lambda seconds: None,
            )

            self.assertEqual(discovery.load_universe(config.state_dir).adopted, ())
            self.assertEqual(client.calls_named("get_popular_watchlists"), [])


if __name__ == "__main__":
    unittest.main()
