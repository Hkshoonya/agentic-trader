"""Shadow P&L and positions accumulate; only the risk counters are day-scoped."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from agentic_trading.journal import DecisionJournal
from agentic_trading.risk import ShadowBook
from agentic_trading.types import OrderIntent, Side


def accept(symbol: str, side: Side, quantity: str, price: str, day: date) -> dict:
    return {
        "event": "accepted",
        "symbol": symbol,
        "side": side.value,
        "quantity": quantity,
        "ref_price": price,
        "intent": {
            "symbol": symbol,
            "side": side.value,
            "quantity": quantity,
            "ref_price": price,
            "created_at": f"{day.isoformat()}T12:00:00+00:00",
        },
    }


def intent(symbol: str, side: Side, quantity: str, price: str) -> OrderIntent:
    return OrderIntent(
        decision_id=f"d-{symbol}-{side.value}",
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        ref_price=Decimal(price),
        reason="test",
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )


class AccumulationTests(unittest.TestCase):
    def test_realized_pnl_survives_a_day_roll(self) -> None:
        book = ShadowBook()
        book.apply_accepted(intent("BTC-USD", Side.BUY, "1", "100"))
        book.apply_accepted(intent("BTC-USD", Side.SELL, "1", "110"))
        self.assertEqual(book.realized_pnl, Decimal("10"))
        self.assertEqual(book.realized_total, Decimal("10"))

        book.roll_day()

        # Today's number resets for the kill switch; the record does not.
        self.assertEqual(book.realized_pnl, Decimal("0"))
        self.assertEqual(book.realized_total, Decimal("10"))

    def test_a_second_day_adds_to_the_total(self) -> None:
        book = ShadowBook()
        book.apply_accepted(intent("BTC-USD", Side.BUY, "1", "100"))
        book.apply_accepted(intent("BTC-USD", Side.SELL, "1", "105"))
        book.roll_day()
        book.apply_accepted(intent("ETH-USD", Side.BUY, "2", "50"))
        book.apply_accepted(intent("ETH-USD", Side.SELL, "2", "45"))
        self.assertEqual(book.realized_pnl, Decimal("-10"))
        self.assertEqual(book.realized_total, Decimal("-5"))

    def test_positions_survive_a_multi_day_rebuild(self) -> None:
        """This is what makes an exit possible the morning after an entry."""
        with tempfile.TemporaryDirectory() as name:
            journal = DecisionJournal(Path(name))
            yesterday = date.today() - timedelta(days=1)
            path = Path(name) / f"{yesterday.isoformat()}.jsonl"
            path.write_text(
                json.dumps(accept("BTC-USD", Side.BUY, "0.01", "100", yesterday)) + "\n"
            )

            today_only = ShadowBook.from_journal(journal)
            window = ShadowBook.from_journal(journal, days=7)

        self.assertEqual(today_only.held, {})
        self.assertEqual(window.held["BTC-USD"], Decimal("0.01"))

    def test_a_multi_day_rebuild_keeps_the_running_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            journal = DecisionJournal(Path(name))
            old = date.today() - timedelta(days=2)
            (Path(name) / f"{old.isoformat()}.jsonl").write_text(
                json.dumps(accept("BTC-USD", Side.BUY, "1", "100", old)) + "\n"
                + json.dumps(accept("BTC-USD", Side.SELL, "1", "120", old)) + "\n"
            )
            book = ShadowBook.from_journal(journal, days=7)

        self.assertEqual(book.realized_total, Decimal("20"))
        self.assertEqual(book.held, {})

    def test_iter_recent_is_oldest_first_and_tolerates_missing_days(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            journal = DecisionJournal(Path(name))
            old = date.today() - timedelta(days=3)
            (Path(name) / f"{old.isoformat()}.jsonl").write_text(
                json.dumps({"event": "accepted", "marker": "old"}) + "\n"
            )
            journal.append({"event": "accepted", "marker": "today"})

            markers = [
                record.get("marker")
                for record in journal.iter_recent(days=5)
                if record.get("marker")
            ]

        self.assertEqual(markers, ["old", "today"])


if __name__ == "__main__":
    unittest.main()
