"""The venue-neutral shapes every broker's data becomes."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.venues.model import Tick, VenueOrder, new_client_order_id

T0 = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)


class TickTests(unittest.TestCase):
    def test_a_tick_serialises_to_strings_and_iso_times(self) -> None:
        tick = Tick("alpaca_paper", "SPY", Decimal("500.01"), Decimal("500.03"), None, None, T0, T0)
        row = tick.to_row()
        self.assertEqual(row["bid"], "500.01")
        self.assertIsNone(row["last"])
        self.assertEqual(row["exchange_at"], "2026-09-30T14:00:00+00:00")
        self.assertEqual(row["venue"], "alpaca_paper")


class VenueOrderTests(unittest.TestCase):
    def test_exactly_one_of_notional_or_qty(self) -> None:
        with self.assertRaises(ValueError):
            VenueOrder("id", "SPY", "buy")
        with self.assertRaises(ValueError):
            VenueOrder("id", "SPY", "buy", notional=Decimal("1"), qty=Decimal("1"))

    def test_a_limit_order_needs_a_positive_limit_price(self) -> None:
        with self.assertRaises(ValueError):
            VenueOrder("id", "SPY", "buy", type="limit", qty=Decimal("1"))
        VenueOrder("id", "SPY", "buy", type="limit", qty=Decimal("1"), limit_price=Decimal("250"))

    def test_sizes_and_side_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            VenueOrder("id", "SPY", "buy", notional=Decimal("0"))
        with self.assertRaises(ValueError):
            VenueOrder("id", "SPY", "hold", notional=Decimal("1"))  # type: ignore[arg-type]

    def test_estimated_notional(self) -> None:
        self.assertEqual(
            VenueOrder("id", "SPY", "buy", notional=Decimal("5")).estimated_notional(Decimal("500")),
            Decimal("5"),
        )
        self.assertEqual(
            VenueOrder("id", "SPY", "buy", qty=Decimal("2")).estimated_notional(Decimal("500")),
            Decimal("1000"),
        )

    def test_client_order_ids_are_unique_and_prefixed(self) -> None:
        ids = {new_client_order_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(i.startswith("at-") and len(i) <= 48 for i in ids))
