from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from agentic_trading.quotes import iter_quotes
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.types import OrderIntent, Side

DATA = Path(__file__).resolve().parent.parent / "data" / "spy_quotes.jsonl"


class QuotesReaderTests(unittest.TestCase):
    def test_reads_first_valid_line_from_spy_quotes(self) -> None:
        quote = next(iter_quotes(DATA))
        self.assertEqual(quote["symbol"], "SPY")
        self.assertIsInstance(quote["bid"], Decimal)
        self.assertIsInstance(quote["ask"], Decimal)
        self.assertGreater(quote["ask"], quote["bid"])
        self.assertIn("observed_at", quote)
        self.assertIn("quote_at", quote)

    def test_skips_malformed_lines(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write(
                '{"symbol":"SPY","observed_at":"2026-09-15T07:58:49Z",'
                '"quote_at":"2026-09-15T07:58:42Z","bid":"100","ask":"101"}\n'
            )
            handle.write("not json\n")
            handle.write('{"symbol":"SPY"}\n')
            path = Path(handle.name)
        try:
            quotes = list(iter_quotes(path))
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["symbol"], "SPY")
        finally:
            path.unlink()


class FixtureStrategyTests(unittest.TestCase):
    def test_emits_buy_then_sell_with_resolved_notional(self) -> None:
        quote = {
            "symbol": "SPY",
            "observed_at": "2026-09-15T07:58:49Z",
            "quote_at": "2026-09-15T07:58:42Z",
            "bid": Decimal("757.37"),
            "ask": Decimal("757.45"),
        }
        strategy = FixtureStrategy()

        buy_intents = strategy.on_quote(quote)
        self.assertEqual(len(buy_intents), 1)
        buy = buy_intents[0]
        self.assertEqual(buy.side, Side.BUY)
        self.assertEqual(buy.quantity, Decimal("0.01"))
        self.assertEqual(buy.ref_price, Decimal("757.45"))
        self.assertGreater(buy.resolved_notional(), Decimal("0"))

        sell_intents = strategy.on_quote(quote)
        self.assertEqual(len(sell_intents), 1)
        sell = sell_intents[0]
        self.assertEqual(sell.side, Side.SELL)
        self.assertEqual(sell.ref_price, Decimal("757.37"))
        self.assertGreater(sell.resolved_notional(), Decimal("0"))

        self.assertEqual(strategy.on_quote(quote), [])

    def test_fixture_on_real_quote_file(self) -> None:
        quote = next(iter_quotes(DATA))
        strategy = FixtureStrategy()
        intents = strategy.on_quote(quote)
        self.assertEqual(len(intents), 1)
        self.assertGreater(intents[0].resolved_notional(), Decimal("0"))

    def test_preloaded_intents(self) -> None:
        created_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
        preloaded = [
            OrderIntent(
                decision_id="fixed-1",
                symbol="SPY",
                side=Side.BUY,
                quantity=Decimal("1"),
                ref_price=Decimal("100"),
                reason="preload",
                created_at=created_at,
            )
        ]
        strategy = FixtureStrategy(preloaded)
        quote = {
            "symbol": "SPY",
            "observed_at": "2026-09-15T07:58:49Z",
            "bid": Decimal("99"),
            "ask": Decimal("101"),
        }

        intents = strategy.on_quote(quote)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].decision_id, "fixed-1")
        self.assertEqual(strategy.on_quote(quote), [])


if __name__ == "__main__":
    unittest.main()
