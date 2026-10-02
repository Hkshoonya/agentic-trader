"""[fast] settings are strict, and the fee-only cost model charges only the fee."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.costs import FeeOnlyCosts
from agentic_trading.fast.settings import FastConfig, load_fast_config


def _load(text: str) -> FastConfig:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "agentic.toml"
        path.write_text(text, encoding="utf-8")
        return load_fast_config(path)


class SettingsTests(unittest.TestCase):
    def test_off_by_default_with_the_spec_defaults(self) -> None:
        config = _load("")
        self.assertFalse(config.enabled)
        self.assertEqual(config.symbols, ("BTC/USD", "ETH/USD", "SOL/USD"))
        self.assertEqual((config.alpaca_fee, config.coinbase_fee), (Decimal("0.0025"), Decimal("0.006")))
        self.assertEqual((config.fill_delay_ms, config.max_positions), (250, 3))
        self.assertEqual((config.cost_gate_multiple, config.daily_loss_stop), (Decimal("3"), Decimal("0.03")))
        self.assertEqual(config.cooldown_minutes, 5.0)

    def test_a_full_table_parses(self) -> None:
        config = _load('[fast]\nenabled = true\nsymbols = ["btc/usd"]\nalpaca_fee = "0.0015"\n'
                       'fill_delay_ms = 500\nmax_positions = 1\ncooldown_minutes = 2\n')
        self.assertTrue(config.enabled)
        self.assertEqual(config.symbols, ("BTC/USD",))
        self.assertEqual(config.alpaca_fee, Decimal("0.0015"))
        self.assertEqual((config.fill_delay_ms, config.max_positions, config.cooldown_minutes), (500, 1, 2.0))

    def test_mistakes_are_refused_with_the_key_named(self) -> None:
        cases = {
            '[fast]\nenabeld = true\n': "unknown keys",
            '[fast]\nenabled = "false"\n': "fast.enabled",
            '[fast]\nalpaca_fee = "0.05"\n': "fast.alpaca_fee",
            '[fast]\ncoinbase_fee = "-0.001"\n': "fast.coinbase_fee",
            '[fast]\nsymbols = ["BTC-USD"]\n': "fast.symbols",
            '[fast]\nsymbols = []\n': "fast.symbols",
            '[fast]\nmax_positions = 0\n': "fast.max_positions",
            '[fast]\nfill_delay_ms = "250"\n': "fast.fill_delay_ms",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, needle):
                    _load(text)


class FeeOnlyCostsTests(unittest.TestCase):
    def test_a_book_pays_the_fee_and_nothing_else(self) -> None:
        book = MemberBook("switchboard", starting_equity=Decimal("50"))
        costs = FeeOnlyCosts(Decimal("0.0025"))
        quantity = book.buy("BTC/USD", Decimal("10"), Decimal("100"), costs)
        self.assertAlmostEqual(float(quantity), 10 / (100 * 1.0025), places=5)  # rounded down to 6 dp
        self.assertEqual(book.cash, Decimal("50") - quantity * Decimal("100") * Decimal("1.0025"))
        self.assertEqual(costs.for_symbol("ETH/USD").fee_per_order, Decimal("0"))
