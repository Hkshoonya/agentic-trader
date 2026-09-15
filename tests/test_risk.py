from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from agentic_trading.journal import DecisionJournal
from agentic_trading.risk import PortfolioSnapshot, RiskGuard, ShadowBook
from agentic_trading.types import OrderIntent, Side


def intent(
    side=Side.BUY,
    symbol="SPY",
    notional="50",
    decision_id="d1",
    quantity=None,
    ref_price=None,
):
    kwargs = dict(
        decision_id=decision_id,
        symbol=symbol,
        side=side,
        reason="t",
        created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    if quantity is not None:
        kwargs["quantity"] = Decimal(str(quantity))
        kwargs["ref_price"] = Decimal(str(ref_price or "100"))
    else:
        kwargs["notional_usd"] = Decimal(notional)
    return OrderIntent(**kwargs)


class RiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = RiskGuard(
            mode="shadow",
            whitelist=frozenset({"SPY"}),
            max_order_pct=Decimal("0.05"),
            daily_notional_pct=Decimal("0.20"),
            daily_loss_pct=Decimal("0.03"),
            max_open_positions=1,
            baseline_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
        )

    def test_rejects_unknown_symbol(self):
        d = self.guard.evaluate(
            intent(symbol="AAPL"),
            PortfolioSnapshot(open_positions=0, held={"SPY": Decimal("0")}),
        )
        self.assertFalse(d.allowed)

    def test_rejects_over_max_order(self):
        d = self.guard.evaluate(intent(notional="51"), PortfolioSnapshot(0, {}))
        self.assertFalse(d.allowed)

    def test_blocks_entry_when_max_positions_allows_close(self):
        snap = PortfolioSnapshot(open_positions=1, held={"SPY": Decimal("1")})
        entry = self.guard.evaluate(intent(side=Side.BUY, decision_id="e"), snap)
        close = self.guard.evaluate(
            intent(side=Side.SELL, notional="40", decision_id="c"), snap
        )
        self.assertFalse(entry.allowed)
        self.assertTrue(close.allowed)

    def test_rejects_sell_that_would_short(self):
        snap = PortfolioSnapshot(open_positions=0, held={})
        d = self.guard.evaluate(intent(side=Side.SELL, notional="10"), snap)
        self.assertFalse(d.allowed)

    def test_kill_switch_blocks_all_writes(self):
        self.guard.trip_kill_switch("loss")
        d = self.guard.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertFalse(d.allowed)
        self.guard.reset_kill_switch()
        d2 = self.guard.evaluate(intent(decision_id="d2"), PortfolioSnapshot(0, {}))
        self.assertTrue(d2.allowed)

    def test_shadow_marks_would_place_not_place(self):
        d = self.guard.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertTrue(d.allowed)
        self.assertTrue(d.would_place)
        self.assertFalse(d.may_place)

    def test_live_may_place(self):
        g = RiskGuard(
            mode="live",
            whitelist=frozenset({"SPY"}),
            max_order_pct=Decimal("0.05"),
            daily_notional_pct=Decimal("0.20"),
            daily_loss_pct=Decimal("0.03"),
            max_open_positions=1,
            baseline_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
        )
        d = g.evaluate(intent(), PortfolioSnapshot(0, {}))
        self.assertTrue(d.may_place)

    def test_positions_read_failed_blocks_entry_not_verified_close(self):
        snap = PortfolioSnapshot(open_positions=0, held={}, positions_read_failed=True)
        entry = self.guard.evaluate(intent(decision_id="e"), snap)
        self.assertFalse(entry.allowed)
        unverified = self.guard.evaluate(
            intent(side=Side.SELL, notional="10", decision_id="c0"), snap
        )
        self.assertFalse(unverified.allowed)
        verified = self.guard.evaluate(
            intent(side=Side.SELL, notional="10", decision_id="c1"),
            PortfolioSnapshot(
                open_positions=0,
                held={"SPY": Decimal("1")},
                positions_read_failed=True,
            ),
        )
        self.assertTrue(verified.allowed)

    def test_daily_notional_accumulation_rejects_when_exceeded(self):
        snap = PortfolioSnapshot(0, {})
        first = self.guard.evaluate(intent(notional="50", decision_id="a"), snap)
        self.assertTrue(first.allowed)
        self.guard.record_accepted(intent(notional="50", decision_id="a"))
        # 50 + 50 = 100 OK (20% of 1000); 50 + 151 would exceed — after 150 used, 51 more fails
        self.guard.record_accepted(intent(notional="50", decision_id="b"))
        self.guard.record_accepted(intent(notional="50", decision_id="c"))
        # daily used = 150; remaining room = 50
        ok = self.guard.evaluate(intent(notional="50", decision_id="d"), snap)
        self.assertTrue(ok.allowed)
        self.guard.record_accepted(intent(notional="50", decision_id="d"))
        # daily used = 200; any further notional fails
        over = self.guard.evaluate(intent(notional="1", decision_id="e"), snap)
        self.assertFalse(over.allowed)

    def test_shadow_book_rebuild_blocks_second_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp))
            journal.append(
                {
                    "event": "accepted",
                    "decision_id": "d0",
                    "would_place": True,
                    "intent": {
                        "symbol": "SPY",
                        "side": "buy",
                        "notional_usd": "50",
                        "quantity": "0.5",
                        "ref_price": "100",
                    },
                }
            )
            book = ShadowBook.from_journal(journal)
            snap = book.as_snapshot()
            self.assertEqual(snap.open_positions, 1)
            self.assertGreater(snap.held.get("SPY", Decimal("0")), 0)
            second = self.guard.evaluate(intent(decision_id="d1"), snap)
            self.assertFalse(second.allowed)

    def test_shadow_realized_loss_trips_kill(self):
        # -3% of 1000 baseline = -30
        self.guard.note_shadow_realized(Decimal("-30"))
        blocked = self.guard.evaluate(
            intent(decision_id="after"), PortfolioSnapshot(0, {})
        )
        self.assertFalse(blocked.allowed)

        # Also via ShadowBook apply path
        guard2 = RiskGuard(
            mode="shadow",
            whitelist=frozenset({"SPY"}),
            max_order_pct=Decimal("0.05"),
            daily_notional_pct=Decimal("0.20"),
            daily_loss_pct=Decimal("0.03"),
            max_open_positions=1,
            baseline_equity=Decimal("1000"),
            current_equity=Decimal("1000"),
        )
        book = ShadowBook()
        book.apply_accepted(intent(quantity="1", ref_price="100", decision_id="b1"))
        # Sell at 70 → realized -30
        book.apply_accepted(
            intent(side=Side.SELL, quantity="1", ref_price="70", decision_id="s1")
        )
        self.assertEqual(book.realized_pnl, Decimal("-30"))
        guard2.note_shadow_realized(book.realized_pnl)
        self.assertFalse(
            guard2.evaluate(intent(decision_id="x"), PortfolioSnapshot(0, {})).allowed
        )

    def test_persist_and_load_kill_and_daily_notional(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.guard.trip_kill_switch("test")
            self.guard.record_accepted(intent(notional="40", decision_id="p1"))
            self.guard.persist(state)

            loaded = RiskGuard(
                mode="shadow",
                whitelist=frozenset({"SPY"}),
                max_order_pct=Decimal("0.05"),
                daily_notional_pct=Decimal("0.20"),
                daily_loss_pct=Decimal("0.03"),
                max_open_positions=1,
                baseline_equity=Decimal("1000"),
                current_equity=Decimal("1000"),
            )
            loaded.load(state)
            self.assertFalse(
                loaded.evaluate(
                    intent(decision_id="k"), PortfolioSnapshot(0, {})
                ).allowed
            )
            loaded.reset_kill_switch()
            # daily notional still 40; 50 more is fine (room 160)
            self.assertTrue(
                loaded.evaluate(
                    intent(notional="50", decision_id="ok"), PortfolioSnapshot(0, {})
                ).allowed
            )

    def test_load_rebuilds_daily_notional_from_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal_dir = root / "journal"
            state_dir = root / "state"
            journal = DecisionJournal(journal_dir)
            journal.append(
                {
                    "event": "accepted",
                    "decision_id": "j1",
                    "would_place": True,
                    "notional": "80",
                    "intent": {
                        "symbol": "SPY",
                        "side": "buy",
                        "notional_usd": "80",
                    },
                }
            )
            guard = RiskGuard(
                mode="shadow",
                whitelist=frozenset({"SPY"}),
                max_order_pct=Decimal("0.05"),
                daily_notional_pct=Decimal("0.20"),
                daily_loss_pct=Decimal("0.03"),
                max_open_positions=1,
                baseline_equity=Decimal("1000"),
                current_equity=Decimal("1000"),
            )
            guard.persist(state_dir)
            guard.load(state_dir, journal=journal)
            # 80 used; 50 ok; after recording to 130, another 80 would exceed 200
            self.assertTrue(
                guard.evaluate(
                    intent(notional="50", decision_id="n1"), PortfolioSnapshot(0, {})
                ).allowed
            )
            over = guard.evaluate(
                intent(notional="50", decision_id="n2"), PortfolioSnapshot(0, {})
            )
            # 80+50=130 <= 200, so allowed — push past
            guard.record_accepted(intent(notional="50", decision_id="n1"))
            guard.record_accepted(intent(notional="50", decision_id="n1b"))
            # used 180; 50 more => 230 > 200
            self.assertFalse(
                guard.evaluate(
                    intent(notional="50", decision_id="n3"), PortfolioSnapshot(0, {})
                ).allowed
            )


class ShadowBookTests(unittest.TestCase):
    def test_avg_cost_realized_pnl(self):
        book = ShadowBook()
        book.apply_accepted(intent(quantity="2", ref_price="100", decision_id="b"))
        book.apply_accepted(
            intent(side=Side.SELL, quantity="1", ref_price="110", decision_id="s")
        )
        self.assertEqual(book.held["SPY"], Decimal("1"))
        self.assertEqual(book.realized_pnl, Decimal("10"))
        snap = book.as_snapshot()
        self.assertEqual(snap.open_positions, 1)


class JournalEmptyIterTests(unittest.TestCase):
    def test_iter_today_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = DecisionJournal(Path(tmp))
            self.assertEqual(list(j.iter_today()), [])


if __name__ == "__main__":
    unittest.main()
