"""Shadow runtime integration: SpyScalperStrategy never places orders."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import paper_scalper
from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.runtime import run_loop
from agentic_trading.strategies.spy_scalper import SpyScalperStrategy
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
TOOLS_SNAPSHOT = FIXTURES / "tools_snapshot.json"


def load_tools() -> list[dict]:
    payload = json.loads(TOOLS_SNAPSHOT.read_text(encoding="utf-8"))
    return payload["tools"]


def _write_config(tmp: Path, *, quotes_path: Path, mode: str = "shadow") -> Path:
    journal_dir = tmp / "journal"
    state_dir = tmp / "state"
    tools_path = tmp / "tools_snapshot.json"
    token_path = tmp / "missing-tokens.json"
    config_path = tmp / "agentic.toml"
    config_path.write_text(
        "\n".join(
            [
                f'mode = "{mode}"',
                'strategy = "spy_scalper"',
                'symbol_whitelist = ["SPY"]',
                'max_order_pct = "0.05"',
                'daily_notional_pct = "0.20"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 1",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'quotes_path = "{quotes_path}"',
                f'journal_dir = "{journal_dir}"',
                f'state_dir = "{state_dir}"',
                f'tools_snapshot_path = "{tools_path}"',
                f'token_path = "{token_path}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _synthetic_rising_quotes() -> str:
    """Three strictly rising mids to trigger a BUY, plus a take-profit quote."""
    rows = [
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:00Z",
            "quote_at": "2026-09-15T11:59:59Z",
            "bid": "100.00",
            "ask": "100.02",
        },
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:05Z",
            "quote_at": "2026-09-15T12:00:04Z",
            "bid": "100.05",
            "ask": "100.07",
        },
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:10Z",
            "quote_at": "2026-09-15T12:00:09Z",
            "bid": "100.10",
            "ask": "100.12",
        },
        {
            "symbol": "SPY",
            "observed_at": "2026-09-15T12:00:20Z",
            "quote_at": "2026-09-15T12:00:19Z",
            "bid": "100.30",
            "ask": "100.32",
        },
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


class SpyScalperShadowRuntimeTests(unittest.TestCase):
    def test_spy_scalper_shadow_never_places(self) -> None:
        tools = load_tools()
        client = FakeMcpClient(tools)
        broker = Broker(client, tools)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            quotes_path = tmp / "quotes.jsonl"
            quotes_path.write_text(_synthetic_rising_quotes(), encoding="utf-8")
            config_path = _write_config(tmp, quotes_path=quotes_path)
            config = load_config(config_path)

            # Wider spreads for synthetic quotes (mirrors unit-test knobs).
            scalper = paper_scalper.Config(
                symbol="SPY",
                initial_cash=paper_scalper.D("50"),
                max_spread_bps=paper_scalper.D("50"),
                slippage_bps=paper_scalper.D("1"),
                fee_per_order=paper_scalper.D("0"),
                take_profit_bps=paper_scalper.D("10"),
                stop_loss_bps=paper_scalper.D("10"),
                max_hold_seconds=60,
                cooldown_seconds=30,
                max_trades=3,
                daily_loss_limit=paper_scalper.D("1"),
                max_quote_age_seconds=10,
                max_signal_gap_seconds=30,
            )

            run_loop(
                config,
                broker=broker,
                strategy=SpyScalperStrategy(scalper),
                tools=tools,
            )

            place_calls = [c for c in client.calls if c.name == "place_equity_order"]
            self.assertEqual(len(place_calls), 0)

            journal_file = (
                Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
            )
            self.assertTrue(journal_file.is_file())
            records = [
                json.loads(line)
                for line in journal_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            activity = [
                r
                for r in records
                if r.get("would_place") is True
                or r.get("event") in ("accepted", "rejected")
            ]
            self.assertGreaterEqual(len(activity), 1)


if __name__ == "__main__":
    unittest.main()
