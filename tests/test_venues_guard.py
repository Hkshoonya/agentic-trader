"""Every venue order passes the guard; live venues refuse all until armed."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.venues import arming
from agentic_trading.venues.guard import Limits, VenueGuard
from agentic_trading.venues.model import AccountView, VenueOrder
from agentic_trading.venues.settings import VenuesConfig

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)  # a Wednesday
SMALL = AccountView("alpaca_live", "live", Decimal("1000"), Decimal("1000"), Decimal("1000"))
RICH = AccountView("alpaca_live", "live", Decimal("30000"), Decimal("30000"), Decimal("30000"))


def _order(side="buy", symbol="SPY", notional="5"):
    return VenueOrder(f"id-{side}-{symbol}", symbol, side, notional=Decimal(notional))


def _guard(tmp: Path, venue: str = "alpaca_paper") -> VenueGuard:
    return VenueGuard(venue, Limits.for_venue(VenuesConfig(), venue), tmp)


def _check(guard, order=None, account=SMALL, open_orders=0, now=NOW, price="500"):
    return guard.check(order or _order(), price=Decimal(price), account=account,
                       open_orders=open_orders, now=now)


class ArmingTests(unittest.TestCase):
    def test_arming_is_for_live_venues_and_at_most_a_day(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ValueError):
                arming.arm(name, "alpaca_paper", hours=1, now=NOW)
            with self.assertRaises(ValueError):
                arming.arm(name, "alpaca_live", hours=25, now=NOW)
            arming.arm(name, "alpaca_live", hours=2, now=NOW)
            self.assertTrue(arming.is_armed(name, "alpaca_live", now=NOW + timedelta(hours=1)))
            self.assertFalse(arming.is_armed(name, "alpaca_live", now=NOW + timedelta(hours=3)))
            self.assertFalse(arming.is_armed(name, "coinbase", now=NOW))
            arming.disarm(name, "alpaca_live")
            self.assertFalse(arming.is_armed(name, "alpaca_live", now=NOW))

    def test_a_corrupt_or_hand_widened_arm_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "venues_arm.json"
            path.write_text("{not json")
            self.assertFalse(arming.is_armed(name, "alpaca_live", now=NOW))
            path.write_text(json.dumps({"alpaca_live": {"armed_at": NOW.isoformat(),
                                                        "until": (NOW + timedelta(hours=48)).isoformat()}}))
            self.assertFalse(arming.is_armed(name, "alpaca_live", now=NOW))
            path.write_text(json.dumps({"alpaca_live": {"armed_at": "2026-09-30T15:00:00",
                                                        "until": "2026-09-30T16:00:00"}}))  # no zone
            self.assertFalse(arming.is_armed(name, "alpaca_live", now=NOW))


class GuardTests(unittest.TestCase):
    def test_a_paper_order_under_every_cap_is_allowed_without_arming(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            verdict = _check(_guard(Path(name)))
        self.assertTrue(verdict.allowed, verdict.reason)

    def test_a_live_venue_refuses_until_armed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            guard = _guard(Path(name), "alpaca_live")
            self.assertIn("not armed", _check(guard).reason)
            arming.arm(name, "alpaca_live", hours=1, now=NOW)
            self.assertTrue(_check(guard).allowed)
            self.assertIn("not armed", _check(guard, now=NOW + timedelta(hours=2)).reason)

    def test_the_order_cap(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            verdict = _check(_guard(Path(name)), _order(notional="501"))
        self.assertFalse(verdict.allowed)
        self.assertIn("$500", verdict.reason)

    def test_the_daily_notional_cap_resets_the_next_day(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            guard = _guard(Path(name))
            guard.record_fill(_order(notional="190"), notional=Decimal("190"), now=NOW)
            self.assertIn("daily", _check(guard, _order(notional="20")).reason)
            self.assertTrue(_check(guard, _order(notional="20"), now=NOW + timedelta(days=1)).allowed)

    def test_the_daily_loss_cap(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            guard = _guard(Path(name))
            guard.record_pnl(Decimal("-10"), now=NOW)
            self.assertIn("loss", _check(guard).reason)

    def test_the_open_order_cap(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertIn("open", _check(_guard(Path(name)), open_orders=5).reason)

    def test_a_quantity_order_needs_a_price(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            order = VenueOrder("id", "SPY", "buy", qty=Decimal("1"))
            self.assertIn("price", _check(_guard(Path(name)), order, price="0").reason)

    def test_the_kill_switch_and_its_reset(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            guard = _guard(Path(name))
            guard.trip("operator stop")
            self.assertIn("operator stop", _check(guard).reason)
            guard.reset()
            self.assertTrue(_check(guard).allowed)

    def test_state_survives_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _guard(Path(name)).record_fill(_order(notional="190"), notional=Decimal("190"), now=NOW)
            self.assertIn("daily", _check(_guard(Path(name)), _order(notional="20")).reason)

    def test_unreadable_guard_state_trips_the_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "venue_guard_alpaca_paper.json").write_text("\x00\x00")
            verdict = _check(_guard(Path(name)))
        self.assertFalse(verdict.allowed)
        self.assertIn("unreadable", verdict.reason)


class PatternDayTraderTests(unittest.TestCase):
    def _three_day_trades(self, guard: VenueGuard) -> None:
        for back in (0, 1, 2):
            day = NOW - timedelta(days=back)
            guard.record_fill(_order("buy", "QQQ"), notional=Decimal("5"), now=day)
            guard.record_fill(_order("sell", "QQQ"), notional=Decimal("5"), now=day)

    def test_a_fourth_day_trade_under_25k_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            arming.arm(name, "alpaca_live", hours=1, now=NOW)
            guard = _guard(Path(name), "alpaca_live")
            self._three_day_trades(guard)
            self.assertEqual(guard.day_trades_in_window(NOW), 3)
            guard.record_fill(_order("buy", "SPY"), notional=Decimal("5"), now=NOW)
            self.assertIn("day trade", _check(guard, _order("sell", "SPY")).reason)
            self.assertTrue(_check(guard, _order("sell", "SPY"), account=RICH).allowed)

    def test_crypto_and_paper_are_not_pattern_day_trading(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            arming.arm(name, "alpaca_live", hours=1, now=NOW)
            live = _guard(Path(name), "alpaca_live")
            self._three_day_trades(live)
            live.record_fill(_order("buy", "BTC/USD"), notional=Decimal("5"), now=NOW)
            self.assertTrue(_check(live, _order("sell", "BTC/USD")).allowed)
            paper = _guard(Path(name), "alpaca_paper")
            self._three_day_trades(paper)
            paper.record_fill(_order("buy", "SPY"), notional=Decimal("5"), now=NOW)
            self.assertTrue(_check(paper, _order("sell", "SPY")).allowed)

    def test_the_window_is_five_weekdays(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            guard = _guard(Path(name), "alpaca_live")
            old = NOW - timedelta(days=7)  # last Wednesday: outside Mon-Fri x5
            guard.record_fill(_order("buy", "QQQ"), notional=Decimal("5"), now=old)
            guard.record_fill(_order("sell", "QQQ"), notional=Decimal("5"), now=old)
            self.assertEqual(guard.day_trades_in_window(NOW), 0)
