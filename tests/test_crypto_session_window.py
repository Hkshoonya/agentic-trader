"""Crypto keeps its exit when the stock market is closed.

``session_policy`` describes the *equity* calendar. Applying it to the whole
loop meant a crypto position could not be sold between 16:00 and 09:30 ET, on
weekends, or on holidays — the instrument trades around the clock, and the
trend that justified the position does not wait for the opening bell. The
daemon now keeps polling outside the equity window and acts on the pairs alone.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.journal import DecisionJournal
from agentic_trading.runtime import run_daemon
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient

FIXTURES = Path(__file__).parent / "fixtures"
# Saturday 03:00 ET — the equity market is closed and crypto is not.
FIXED_NOW = datetime(2026, 9, 19, 7, 0, 30, tzinfo=timezone.utc)


def _tools() -> list[dict]:
    return json.loads((FIXTURES / "tools_snapshot.json").read_text())["tools"]


def _quote(symbol: str, *, bid: str = "100.00", ask: str = "100.05") -> dict:
    return {
        "symbol": symbol,
        "observed_at": "2026-09-19T07:00:00Z",
        "quote_at": "2026-09-19T06:59:59Z",
        "bid": Decimal(bid),
        "ask": Decimal(ask),
    }


class _StubFeed:
    def __init__(self, quotes: list[dict]) -> None:
        self.quotes = list(quotes)

    def poll(self) -> list[dict]:
        pending, self.quotes = self.quotes, []
        return pending


class _ExitEverything:
    """A strategy that only ever asks to close the position it is shown."""

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        return [
            OrderIntent(
                decision_id=f"exit-{quote['symbol']}",
                symbol=quote["symbol"],
                side=Side.SELL,
                quantity=Decimal("0.5"),
                ref_price=quote["bid"],
                reason="trend_exit",
                created_at=FIXED_NOW,
            )
        ]


def _write_config(tmp: Path, *, whitelist: str) -> Path:
    config_path = tmp / "agentic.toml"
    config_path.write_text(
        "\n".join(
            [
                'mode = "shadow"',
                f"symbol_whitelist = [{whitelist}]",
                'max_order_pct = "0.50"',
                'daily_notional_pct = "0.90"',
                'daily_loss_pct = "0.03"',
                "max_open_positions = 4",
                "equity_refresh_ticks = 30",
                "equity_refresh_seconds = 60",
                'timezone = "local"',
                f'quotes_path = "{tmp / "quotes.jsonl"}"',
                f'journal_dir = "{tmp / "journal"}"',
                f'state_dir = "{tmp / "state"}"',
                f'tools_snapshot_path = "{tmp / "tools.json"}"',
                f'token_path = "{tmp / "missing.json"}"',
                'mcp_url = "https://agent.robinhood.com/mcp/trading"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _seed_position(config: Any, symbol: str, quantity: str = "1") -> None:
    """Write the buy into the journal so the shadow book holds the position."""
    journal = DecisionJournal(Path(config.journal_dir))
    journal.append(
        {
            "decision_id": f"seed-{symbol}",
            "event": "accepted",
            "would_place": True,
            "symbol": symbol,
            "side": "buy",
            "quantity": quantity,
            "notional": "100",
            "intent": {
                "symbol": symbol,
                "side": "buy",
                "quantity": quantity,
                "ref_price": "100",
                "created_at": FIXED_NOW.isoformat(),
            },
        }
    )


def _records(config: Any) -> list[dict]:
    path = Path(config.journal_dir) / f"{date.today().isoformat()}.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(config: Any, broker: Broker, feed: Any) -> None:
    run_daemon(
        config,
        broker=broker,
        strategy=_ExitEverything(),
        feed=feed,
        once=True,
        clock=lambda: FIXED_NOW,
        session_clock=lambda: "weekend",
    )


class CryptoSessionWindowTests(unittest.TestCase):
    def test_a_crypto_exit_is_accepted_while_the_stock_market_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, whitelist='"BTC-USD"'))
            _seed_position(config, "BTC-USD")
            broker = Broker(FakeMcpClient(_tools()), _tools())
            _run(config, broker, _StubFeed([_quote("BTC-USD")]))

            events = [record["event"] for record in _records(config)]
            self.assertIn("crypto_session", events)
            self.assertNotIn("session_closed", events)
            accepted = [
                record
                for record in _records(config)
                if record.get("event") == "accepted"
                and record.get("symbol") == "BTC-USD"
                and record.get("side") == "sell"
            ]
            self.assertEqual(len(accepted), 1, events)
            self.assertTrue(accepted[0]["would_place"])

    def test_stock_quotes_are_left_alone_in_the_same_cycle(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(
                _write_config(tmp, whitelist='"SPY", "BTC-USD"')
            )
            _seed_position(config, "BTC-USD")
            broker = Broker(FakeMcpClient(_tools()), _tools())
            _run(
                config,
                broker,
                _StubFeed([_quote("SPY"), _quote("BTC-USD")]),
            )

            accepted = [
                record
                for record in _records(config)
                if record.get("event") == "accepted"
                and record.get("side") == "sell"
            ]
            self.assertEqual([row["symbol"] for row in accepted], ["BTC-USD"])

    def test_a_stock_only_book_still_stops_for_the_closed_session(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            config = load_config(_write_config(tmp, whitelist='"SPY"'))
            broker = Broker(FakeMcpClient(_tools()), _tools())
            _run(config, broker, _StubFeed([_quote("SPY")]))

            events = [
                record["event"]
                for record in _records(config)
                if record.get("event") != "live_gate"
            ]
            self.assertEqual(events, ["session_closed"])


if __name__ == "__main__":
    unittest.main()
