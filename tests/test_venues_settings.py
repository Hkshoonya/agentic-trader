"""The [venues] table: off by default, strict about typos and formats."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.venues.settings import VenuesConfig, load_venues_config


def _load(body: str) -> VenuesConfig:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "agentic.toml"
        path.write_text('mode = "shadow"\n' + body, encoding="utf-8")
        return load_venues_config(path)


class VenuesConfigTests(unittest.TestCase):
    def test_no_table_means_everything_is_off(self) -> None:
        config = _load("")
        self.assertFalse(config.enabled)
        self.assertEqual(config.alpaca_feed, "iex")
        self.assertTrue(config.use_alpaca_paper)
        self.assertFalse(config.use_alpaca_live)

    def test_values_are_read_and_typed(self) -> None:
        config = _load(
            '[venues]\nenabled = true\nalpaca_stock_symbols = ["spy", "QQQ"]\n'
            'alpaca_live_max_order_usd = "12.5"\nmax_open_orders = 3\nuse_coinbase = true\n'
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.alpaca_stock_symbols, ("SPY", "QQQ"))
        self.assertEqual(config.max_order_usd("alpaca_live"), Decimal("12.5"))
        self.assertEqual(config.max_order_usd("alpaca_paper"), Decimal("500"))
        self.assertEqual(config.max_open_orders, 3)

    def test_a_typo_is_an_error_not_a_silent_default(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _load("[venues]\nenabeld = true\n")
        self.assertIn("enabeld", str(caught.exception))

    def test_symbol_formats_are_checked_per_venue(self) -> None:
        for bad in ('alpaca_crypto_symbols = ["BTC-USD"]', 'coinbase_products = ["BTC/USD"]',
                    'alpaca_stock_symbols = ["BTC/USD"]'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _load(f"[venues]\n{bad}\n")

    def test_feed_caps_and_stale_are_checked(self) -> None:
        for bad in ('alpaca_feed = "opra"', 'max_daily_loss_usd = "0"', "max_open_orders = 0",
                    "stale_after_seconds = 0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _load(f"[venues]\n{bad}\n")

    def test_unknown_venue_has_no_cap(self) -> None:
        with self.assertRaises(KeyError):
            VenuesConfig().max_order_usd("robinhood")
