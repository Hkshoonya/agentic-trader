"""24/7 crypto shadow loop: quotes flow, execution stays impossible."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.marketdata import (
    CompositeQuoteFeed,
    CryptoQuoteFeed,
    build_quote_feed,
    is_crypto_pair,
)
from agentic_trading.orders import OrderValidationError
from agentic_trading.runtime import build_order_request
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient

TOOLS = [
    {"name": "get_equity_quotes", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_crypto_quotes", "inputSchema": {"type": "object", "properties": {}}},
]

OBSERVED = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
CRYPTO_PAYLOAD = {
    "data": {
        "results": [
            {
                "symbol": "BTCUSD",
                "id": "abc",
                "bid_price": "118250.00",
                "ask_price": "118280.00",
                "bid_time": "2026-09-16T02:59:59Z",
                "ask_time": "2026-09-16T02:59:59Z",
            }
        ]
    }
}


class SymbolConventionTests(unittest.TestCase):
    def test_crypto_pairs_are_recognised(self) -> None:
        for symbol in ("BTC-USD", "btc-usd", "BTCUSD", "ETH-USD"):
            self.assertTrue(is_crypto_pair(symbol), symbol)

    def test_equities_are_not_crypto(self) -> None:
        for symbol in ("SPY", "QQQ", "AAPL", "NVDA"):
            self.assertFalse(is_crypto_pair(symbol), symbol)


class CryptoFeedTests(unittest.TestCase):
    def test_crypto_quotes_normalize_to_the_shared_contract(self) -> None:
        class _StubBroker:
            def get_crypto_quotes(self, symbols: list[str]) -> dict:
                assert symbols == ["BTC-USD"]
                return CRYPTO_PAYLOAD

        feed = CryptoQuoteFeed(_StubBroker(), ["BTC-USD"], clock=lambda: OBSERVED)  # type: ignore[arg-type]
        quotes = feed.poll()
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        # The crypto namespace returns pairs undashed (BTCUSD), which is the
        # shape the live feed actually produces.
        self.assertEqual(quote["symbol"], "BTCUSD")
        self.assertEqual(quote["bid"], Decimal("118250.00"))
        self.assertEqual(quote["ask"], Decimal("118280.00"))
        self.assertEqual(quote["observed_at"], OBSERVED.isoformat())

    def test_mixed_whitelist_builds_a_composite_feed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg_path = tmp / "agentic.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        'mode = "shadow"',
                        'symbol_whitelist = ["SPY", "BTC-USD"]',
                        'max_order_pct = "0.05"',
                        'daily_notional_pct = "0.20"',
                        'daily_loss_pct = "0.03"',
                        "max_open_positions = 1",
                        "equity_refresh_ticks = 30",
                        "equity_refresh_seconds = 60",
                        'timezone = "local"',
                        f'quotes_path = "{tmp / "q.jsonl"}"',
                        f'journal_dir = "{tmp / "journal"}"',
                        f'state_dir = "{tmp / "state"}"',
                        f'tools_snapshot_path = "{tmp / "tools.json"}"',
                        f'token_path = "{tmp / "tok.json"}"',
                        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                        'quote_source = "mcp"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            feed = build_quote_feed(config, Broker(FakeMcpClient(TOOLS), TOOLS))
            self.assertIsInstance(feed, CompositeQuoteFeed)
            self.assertEqual(len(feed.feeds), 2)

    def test_equity_only_whitelist_stays_a_single_feed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg_path = tmp / "agentic.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        'mode = "shadow"',
                        'symbol_whitelist = ["SPY"]',
                        'max_order_pct = "0.05"',
                        'daily_notional_pct = "0.20"',
                        'daily_loss_pct = "0.03"',
                        "max_open_positions = 1",
                        "equity_refresh_ticks = 30",
                        "equity_refresh_seconds = 60",
                        'timezone = "local"',
                        f'quotes_path = "{tmp / "q.jsonl"}"',
                        f'journal_dir = "{tmp / "journal"}"',
                        f'state_dir = "{tmp / "state"}"',
                        f'tools_snapshot_path = "{tmp / "tools.json"}"',
                        f'token_path = "{tmp / "tok.json"}"',
                        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                        'quote_source = "mcp"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            feed = build_quote_feed(config, Broker(FakeMcpClient(TOOLS), TOOLS))
            self.assertNotIsInstance(feed, CompositeQuoteFeed)


class CryptoExecutionIsRefusedTests(unittest.TestCase):
    def _intent(self, symbol: str) -> OrderIntent:
        return OrderIntent(
            decision_id="d-crypto",
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.01"),
            ref_price=Decimal("118280"),
            reason="test",
            created_at=OBSERVED,
        )

    def test_crypto_order_request_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg_path = tmp / "agentic.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        'mode = "live"',
                        'symbol_whitelist = ["BTC-USD"]',
                        'max_order_pct = "0.05"',
                        'daily_notional_pct = "0.20"',
                        'daily_loss_pct = "0.03"',
                        "max_open_positions = 1",
                        "equity_refresh_ticks = 30",
                        "equity_refresh_seconds = 60",
                        'timezone = "local"',
                        f'quotes_path = "{tmp / "q.jsonl"}"',
                        f'journal_dir = "{tmp / "journal"}"',
                        f'state_dir = "{tmp / "state"}"',
                        f'tools_snapshot_path = "{tmp / "tools.json"}"',
                        f'token_path = "{tmp / "tok.json"}"',
                        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            with self.assertRaises(OrderValidationError) as ctx:
                build_order_request(
                    self._intent("BTC-USD"),
                    account_number="A1",
                    config=config,
                    session="regular",
                )
            self.assertIn("crypto execution is not enabled", str(ctx.exception))

    def test_equity_still_builds_normally(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            cfg_path = tmp / "agentic.toml"
            cfg_path.write_text(
                "\n".join(
                    [
                        'mode = "live"',
                        'symbol_whitelist = ["SPY"]',
                        'max_order_pct = "0.05"',
                        'daily_notional_pct = "0.20"',
                        'daily_loss_pct = "0.03"',
                        "max_open_positions = 1",
                        "equity_refresh_ticks = 30",
                        "equity_refresh_seconds = 60",
                        'timezone = "local"',
                        f'quotes_path = "{tmp / "q.jsonl"}"',
                        f'journal_dir = "{tmp / "journal"}"',
                        f'state_dir = "{tmp / "state"}"',
                        f'tools_snapshot_path = "{tmp / "tools.json"}"',
                        f'token_path = "{tmp / "tok.json"}"',
                        'mcp_url = "https://agent.robinhood.com/mcp/trading"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_config(cfg_path)
            request = build_order_request(
                self._intent("SPY"),
                account_number="A1",
                config=config,
                session="regular",
            )
            self.assertEqual(request.symbol, "SPY")


if __name__ == "__main__":
    unittest.main()
