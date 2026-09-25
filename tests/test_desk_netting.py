"""Netting: one order per symbol for the whole desk, only when the gap matters."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agentic_trading.desk.netting import gap_intent, target_quantities
from agentic_trading.types import Side

D = Decimal
NOW = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)


def quote(symbol: str, bid: str, ask: str, session: str = "regular") -> dict:
    return {"symbol": symbol, "bid": D(bid), "ask": D(ask), "market_session": session}


class TargetTests(unittest.TestCase):
    def test_two_members_wanting_the_same_symbol_make_one_target(self) -> None:
        targets, unpriced = target_quantities(
            {"a": 0.5, "b": 0.5, "benchmark": 0.0},
            {"a": {"MSFT": 0.4}, "b": {"MSFT": 0.2, "BTC-USD": 0.5}},
            D("100"),
            {"MSFT": D("10"), "BTC-USD": D("50")},
        )
        # MSFT: (0.5*0.4 + 0.5*0.2) * 100 = $30 -> 3 shares; BTC: 0.5*0.5*100 = $25 -> 0.5
        self.assertEqual(targets, {"MSFT": D("3.000000"), "BTC-USD": D("0.500000")})
        self.assertEqual(unpriced, set())

    def test_a_symbol_without_a_price_is_reported_not_zeroed(self) -> None:
        targets, unpriced = target_quantities(
            {"a": 1.0}, {"a": {"MSFT": 0.5}}, D("100"), {}
        )
        self.assertEqual(targets, {})
        self.assertEqual(unpriced, {"MSFT"})

    def test_bar_keyed_member_symbols_are_netted_in_broker_form(self) -> None:
        targets, _ = target_quantities(
            {"a": 1.0}, {"a": {"BTCUSD": 0.5}}, D("100"), {"BTC-USD": D("50")}
        )
        self.assertEqual(targets, {"BTC-USD": D("1.000000")})


class GapTests(unittest.TestCase):
    def test_a_material_shortfall_buys_the_gap_at_the_ask(self) -> None:
        intent = gap_intent(
            "MSFT", target=D("3"), held=D("1"), quote=quote("MSFT", "9.9", "10"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity, intent.ref_price), (Side.BUY, D("2.000000"), D("10")))
        self.assertEqual(intent.reason, "desk_rebalance")

    def test_a_gap_under_one_dollar_or_five_percent_is_left_alone(self) -> None:
        # $0.90 gap on a $30 target: under both $1 and 5% ($1.50).
        self.assertIsNone(
            gap_intent("MSFT", target=D("3"), held=D("2.91"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )
        # $1.20 gap on a $100 target: over $1 but under 5% ($5).
        self.assertIsNone(
            gap_intent("MSFT", target=D("10"), held=D("9.88"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )

    def test_a_zero_target_sells_everything_even_dust(self) -> None:
        intent = gap_intent(
            "SOL-USD", target=D("0"), held=D("0.001"), quote=quote("SOL-USD", "200", "201"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity, intent.ref_price), (Side.SELL, D("0.001"), D("200")))

    def test_an_excess_sells_only_the_gap(self) -> None:
        intent = gap_intent(
            "MSFT", target=D("1"), held=D("3"), quote=quote("MSFT", "10", "10.1"), created_at=NOW
        )
        assert intent is not None
        self.assertEqual((intent.side, intent.quantity), (Side.SELL, D("2.000000")))

    def test_equities_act_only_in_the_regular_session(self) -> None:
        self.assertIsNone(
            gap_intent("MSFT", target=D("3"), held=D("0"), quote=quote("MSFT", "10", "10", "extended"), created_at=NOW)
        )
        self.assertIsNotNone(
            gap_intent("BTC-USD", target=D("1"), held=D("0"), quote=quote("BTC-USD", "10", "10", "extended"), created_at=NOW)
        )

    def test_no_gap_no_intent(self) -> None:
        self.assertIsNone(
            gap_intent("MSFT", target=D("2"), held=D("2"), quote=quote("MSFT", "10", "10"), created_at=NOW)
        )
