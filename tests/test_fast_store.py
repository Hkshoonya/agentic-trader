"""The switchboard survives restarts; broken files are moved aside, never fatal."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.regime import TRENDING
from agentic_trading.fast.store import FastStore
from agentic_trading.fast.switchboard import Switchboard
from tests.fast_support import T0
from tests.test_fast_switchboard import CFG, OPEN, _enter, _seed

D = Decimal


def _board_from(store: FastStore, now=T0):
    book, mirror_book, state, notes = store.load(now)
    board = Switchboard(CFG, book, mirror=CoinbaseMirror(mirror_book, D("0.006")),
                        regime_reader=lambda bars: TRENDING)
    return board, state, notes


class StoreTests(unittest.TestCase):
    def test_an_open_trade_and_its_cooldowns_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            board, _, _ = _board_from(store)
            _seed(board)
            _enter(board)
            board.cooldown_until["ETH/USD"] = OPEN + timedelta(minutes=4)
            store.save(board)
            self.assertTrue(store.book_path.is_file())
            again, state, notes = _board_from(store, OPEN + timedelta(minutes=1))
            self.assertEqual(again.restore(state, OPEN + timedelta(minutes=1)), [])
        self.assertEqual(notes, [])
        trade = again.trades["BTC/USD"]
        self.assertEqual((trade.playbook, trade.entry), ("breakout", 105.2))
        self.assertEqual(trade.quantity, again.book.positions["BTC/USD"])
        self.assertEqual(again.cooldown_until["ETH/USD"], OPEN + timedelta(minutes=4))
        self.assertEqual(again.day, board.day)

    def test_the_book_starts_at_the_desk_members_equity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            self.assertEqual(store.starting_equity(), D("50"))
            (Path(name) / "desk").mkdir()
            (Path(name) / "desk" / "account.json").write_text(json.dumps({"starting_equity": "49.9"}))
            self.assertEqual(store.starting_equity(), D("49.9"))
            book, _, _, _ = store.load(T0)
        self.assertEqual(book.starting_equity, D("49.9"))

    def test_unreadable_files_are_moved_aside_and_a_fresh_start_is_noted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = FastStore(name)
            store.book_path.parent.mkdir(parents=True)
            store.book_path.write_text("not json")
            store.engine_path.parent.mkdir(parents=True)
            store.engine_path.write_text("[1, 2]")
            book, _, state, notes = store.load(T0)
            moved = sorted(p.name for p in Path(name).rglob("*.corrupt-*"))
        self.assertEqual(state, {})
        self.assertTrue(book.is_new)
        self.assertEqual(len(notes), 2)
        self.assertEqual(moved, ["engine.json.corrupt-20261005T120000Z", "switchboard.json.corrupt-20261005T120000Z"])

    def test_a_holding_without_a_trade_is_recovered_and_a_phantom_trade_dropped(self) -> None:
        book = MemberBook("switchboard", starting_equity=D("300"))
        book.positions = {"BTC/USD": D("0.5")}
        book.prices = {"BTC/USD": D("100")}
        board = Switchboard(CFG, book)
        phantom = {"symbol": "ETH/USD", "playbook": "pullback", "entry": 10.0, "stop": 9.0, "target": 11.0,
                   "quantity": "1", "opened_at": T0.isoformat(), "high": 10.0}
        events = board.restore({"trades": [phantom], "skipped_total": 4}, T0)
        self.assertEqual(sorted(board.trades), ["BTC/USD"])
        recovered = board.trades["BTC/USD"]
        self.assertEqual((recovered.playbook, recovered.stop, recovered.target), ("recovered", 98.0, 102.0))
        self.assertEqual([e.kind for e in events], ["fast_recovered"])
        self.assertEqual(board.skipped_total, 4)
