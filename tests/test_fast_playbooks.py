"""Each playbook's entry, stop and target, from scripted bars."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.fast.playbooks import (
    BREAKOUT, ENTRIES, PULLBACK, RECOVERED, SQUEEZE_BREAK, Trade, atr, breakout_entry, ema,
    exit_reason, pullback_entry, squeeze_entry,
)
from agentic_trading.fast.regime import SQUEEZE, TRENDING
from tests.fast_support import T0, bars_from


def _trade(playbook, entry, stop, target=None):
    return Trade("BTC/USD", playbook, entry, stop, target, Decimal("0.1"), T0, entry)


class IndicatorTests(unittest.TestCase):
    def test_atr_and_ema(self) -> None:
        self.assertEqual(atr(bars_from([100, 104] * 20)), 4.0)
        self.assertEqual(atr(bars_from([100])), 0.0)
        self.assertEqual(ema([5.0] * 10, 3), 5.0)
        self.assertGreater(ema([float(i) for i in range(50)], 5), ema([float(i) for i in range(50)], 20))


class EntryTests(unittest.TestCase):
    def test_breakout_needs_a_new_30_bar_high(self) -> None:
        bars = bars_from([100, 104] * 20)
        self.assertIsNone(breakout_entry(bars, 103.9))
        plan = breakout_entry(bars, 105.0)
        self.assertEqual(plan.playbook, BREAKOUT)
        self.assertAlmostEqual(plan.stop, 105.0 - 1.5 * 4.0)
        self.assertIsNone(plan.target)
        self.assertAlmostEqual(plan.expected_move, 8.0)
        self.assertIsNone(breakout_entry(bars[:20], 200.0))  # too little history

    def test_pullback_buys_the_turn_after_a_dip_to_the_fast_average(self) -> None:
        closes = [100.0 + i for i in range(79)] + [174.0]
        fast = ema(closes, 20)
        bars = bars_from(closes, wick=0.5)
        bars[-1].low, bars[-1].high = fast - 0.1, 175.0
        self.assertIsNone(pullback_entry(bars, 174.5))  # not yet above the dip bar's high
        plan = pullback_entry(bars, 175.5)
        self.assertEqual(plan.playbook, PULLBACK)
        self.assertAlmostEqual(plan.target, 178.5)  # the 60-bar high (close 178 + wick)
        self.assertLess(plan.stop, bars[-1].low)
        self.assertAlmostEqual(plan.expected_move, 178.5 - 175.5)

    def test_squeeze_break_targets_the_range_height(self) -> None:
        bars = bars_from([100.0 + 0.1 * (i % 2) for i in range(60)])
        self.assertIsNone(squeeze_entry(bars, 100.05))
        plan = squeeze_entry(bars, 100.2)
        self.assertEqual(plan.playbook, SQUEEZE_BREAK)
        self.assertAlmostEqual(plan.stop, 100.05)
        self.assertAlmostEqual(plan.expected_move, 0.1)
        self.assertAlmostEqual(plan.target, 100.3)

    def test_the_playbooks_per_regime(self) -> None:
        self.assertEqual([name for name, _ in ENTRIES[TRENDING]], [BREAKOUT, PULLBACK])
        self.assertEqual([name for name, _ in ENTRIES[SQUEEZE]], [SQUEEZE_BREAK])


class ExitTests(unittest.TestCase):
    def test_the_breakout_stop_trails_up_and_never_down(self) -> None:
        bars = bars_from([100, 104] * 20)  # ATR 4 -> trail 6
        trade = _trade(BREAKOUT, 100.0, 94.0)
        self.assertIsNone(exit_reason(trade, bars, 110.0))
        self.assertAlmostEqual(trade.stop, 104.0)
        self.assertIsNone(exit_reason(trade, bars, 105.0))
        self.assertAlmostEqual(trade.stop, 104.0)  # unchanged on the way down
        self.assertEqual(exit_reason(trade, bars, 103.9), "trailing stop")

    def test_fixed_stop_and_target_playbooks(self) -> None:
        for playbook in (PULLBACK, SQUEEZE_BREAK, RECOVERED):
            with self.subTest(playbook=playbook):
                trade = _trade(playbook, 100.0, 98.0, 103.0)
                self.assertIsNone(exit_reason(trade, [], 101.0))
                self.assertEqual(exit_reason(trade, [], 103.0), "target")
                self.assertEqual(exit_reason(_trade(playbook, 100.0, 98.0, 103.0), [], 97.5), "stop")

    def test_a_trade_round_trips_through_a_dict(self) -> None:
        trade = _trade(PULLBACK, 100.0, 98.0, 103.0)
        self.assertEqual(Trade.from_dict(trade.to_dict()), trade)
