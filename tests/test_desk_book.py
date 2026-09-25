"""A member's paper book: fills, costs, marks, samples and persistence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.backtest import CostModel
from agentic_trading.desk.book import MemberBook

D = Decimal
FREE = CostModel(D("0"), D("0"), D("0"))
# Equities 2 bps/side; crypto a fixed $0.05 per order.
COSTS = CostModel(D("2"), D("1"), D("0"), crypto=CostModel(D("0"), D("0"), D("0.05")))
DAY1 = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
DAY2 = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


def _book(**kwargs) -> MemberBook:
    return MemberBook("m", starting_equity=D("50"), **kwargs)


class FillTests(unittest.TestCase):
    def test_a_buy_spends_notional_including_costs(self) -> None:
        book = _book()
        qty = book.buy("MSFT", D("10"), D("100"), COSTS)
        self.assertEqual(qty, D("0.099980"))  # 10 / (100 * 1.0002), rounded down
        self.assertEqual(book.positions["MSFT"], qty)
        self.assertAlmostEqual(float(book.cash), 40.0, places=3)
        self.assertEqual(book.entries, 1)

    def test_crypto_pays_the_fixed_fee(self) -> None:
        book = _book()
        qty = book.buy("BTC-USD", D("10"), D("100"), COSTS)
        self.assertEqual(qty, D("0.099500"))  # (10 - 0.05) / 100
        proceeds_before = book.cash
        book.sell("BTC-USD", qty, D("100"), COSTS)
        self.assertEqual(book.cash - proceeds_before, qty * 100 - D("0.05"))

    def test_a_member_never_borrows(self) -> None:
        book = _book()
        book.buy("MSFT", D("45"), D("10"), FREE)
        self.assertEqual(book.buy("AAPL", D("10"), D("10"), FREE), D("0.500000"))
        self.assertEqual(book.cash, D("0"))
        self.assertEqual(book.buy("NVDA", D("10"), D("10"), FREE), D("0"))

    def test_an_order_under_the_minimum_is_skipped(self) -> None:
        self.assertEqual(_book().buy("MSFT", D("0.99"), D("10"), FREE), D("0"))

    def test_a_sell_never_exceeds_holdings(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        self.assertEqual(book.sell("MSFT", D("5"), D("10"), FREE), D("1.000000"))
        self.assertNotIn("MSFT", book.positions)
        self.assertEqual(book.exits, 1)
        self.assertEqual(book.sell("MSFT", D("1"), D("10"), FREE), D("0"))


class MarkTests(unittest.TestCase):
    def test_marks_value_positions_and_sample_once_per_day(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        self.assertFalse(book.mark({"MSFT": D("12")}, DAY1))
        self.assertEqual(book.equity, D("52"))
        self.assertTrue(book.mark({"MSFT": D("11")}, DAY2))
        # The closing mark of day 1 is the last price seen that day.
        self.assertEqual(
            [(day, Decimal(value)) for day, value in book.samples],
            [("2026-09-24", D("52"))],
        )

    def test_weights_are_position_value_over_equity(self) -> None:
        book = _book()
        book.buy("MSFT", D("25"), D("10"), FREE)
        book.mark({"MSFT": D("10")}, DAY1)
        self.assertEqual(book.weights(), {"MSFT": 0.5})


class SeedingTests(unittest.TestCase):
    def test_holdings_seeded_before_any_price_derive_cash_once_priced(self) -> None:
        book = _book()
        book.set_holdings({"SOL-USD": D("0.05")})
        self.assertTrue(book.cash_pending)
        self.assertEqual(book.equity, D("50"))  # no invented value while unpriced
        book.mark({"SOL-USD": D("200")}, DAY1)
        self.assertFalse(book.cash_pending)
        self.assertEqual(book.cash, D("40"))
        self.assertEqual(book.equity, D("50"))

    def test_seeding_a_traded_book_reconciles_quantities_only(self) -> None:
        book = _book()
        book.buy("MSFT", D("10"), D("10"), FREE)
        cash = book.cash
        book.set_holdings({"MSFT": D("0.5")})
        self.assertEqual(book.positions, {"MSFT": D("0.5")})
        self.assertEqual(book.cash, cash)
        self.assertFalse(book.cash_pending)

    def test_the_account_ledger_follows_fills_by_quantity(self) -> None:
        book = _book()
        book.apply_fill("QQQ", D("0.1"), D("100"), FREE)
        book.apply_fill("QQQ", D("-0.04"), D("110"), FREE)
        self.assertEqual(book.positions["QQQ"], D("0.06"))
        self.assertEqual(book.cash, D("50") - D("10") + D("4.4"))


class PersistenceTests(unittest.TestCase):
    def test_a_saved_book_loads_back_identically(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "desk" / "m.json"
            book = MemberBook("m", starting_equity=D("50"), path=path)
            book.buy("MSFT", D("10"), D("10"), FREE)
            book.mark({"MSFT": D("11")}, DAY1)
            book.mark({"MSFT": D("11")}, DAY2)
            book.save()
            loaded, reset = MemberBook.load(path, name="m", starting_equity=D("99"))
        self.assertFalse(reset)
        self.assertFalse(loaded.is_new)
        self.assertEqual(loaded.cash, book.cash)
        self.assertEqual(loaded.positions, book.positions)
        self.assertEqual(loaded.samples, book.samples)
        self.assertEqual(loaded.starting_equity, D("50"))
        self.assertEqual((loaded.entries, loaded.exits), (1, 0))

    def test_a_missing_file_is_a_new_book(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            book, reset = MemberBook.load(
                Path(name) / "x.json", name="x", starting_equity=D("50")
            )
        self.assertTrue(book.is_new)
        self.assertFalse(reset)
        self.assertEqual(book.cash, D("50"))

    def test_a_corrupt_file_resets_the_book_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "x.json"
            path.write_text("{not json", encoding="utf-8")
            book, reset = MemberBook.load(path, name="x", starting_equity=D("50"))
        self.assertTrue(reset)
        self.assertEqual(book.cash, D("50"))
