"""The switchboard's rules, driven tick by tick."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.regime import CHOPPY, SQUEEZE, TRENDING, UNCLEAR, WARMING
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.switchboard import Switchboard
from tests.fast_support import T0, quote

D = Decimal
SEC, MS = timedelta(seconds=1), timedelta(milliseconds=1)
CFG = FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"))
WIDE = [100, 104] * 16          # 31 closed bars: ATR 4, 30-bar high 104
OPEN = T0 + timedelta(minutes=31)  # the minute still open after seeding


def _board(config=CFG, equity="300", mirror=None):
    book = MemberBook("switchboard", starting_equity=D(equity))
    return Switchboard(config, book, mirror=mirror, regime_reader=lambda bars: TRENDING)


def _seed(board, symbol="BTC/USD", closes=WIDE):
    for i, close in enumerate(closes):
        board.bars.add(symbol, float(close), T0 + timedelta(minutes=i))
    board.regimes[symbol] = TRENDING


def _enter(board, symbol="BTC/USD", at=OPEN):
    """Warm the feed, break out above 104, and fill 300 ms later."""
    board.on_tick(quote(symbol, 102.9, 103.1, at + SEC))   # first price: never an entry
    board.on_tick(quote(symbol, 104.9, 105.1, at + 2 * SEC))
    return board.on_tick(quote(symbol, 105.0, 105.2, at + 2 * SEC + 300 * MS))


def _exit_through_a_gap(board):
    board.on_tick(quote("BTC/USD", 90.0, 90.2, OPEN + 3 * SEC))
    return board.on_tick(quote("BTC/USD", 89.5, 89.7, OPEN + 3 * SEC + 300 * MS))


class EntryTests(unittest.TestCase):
    def test_a_breakout_fills_at_the_ask_with_a_third_of_the_book(self) -> None:
        board = _board()
        _seed(board)
        [entry] = [e for e in _enter(board) if e.kind == "fast_entry"]
        self.assertEqual((entry.data["playbook"], entry.data["price"]), ("breakout", "105.2"))
        self.assertIn("expecting", entry.text)
        trade = board.trades["BTC/USD"]
        self.assertEqual((trade.playbook, trade.entry), ("breakout", 105.2))
        self.assertAlmostEqual(float(trade.quantity * D("105.2") * D("1.0025")), 100.0, places=3)

    def test_choppy_unclear_and_warming_stand_aside(self) -> None:
        for regime in (CHOPPY, UNCLEAR, WARMING):
            with self.subTest(regime=regime):
                board = _board()
                _seed(board)
                board.regimes["BTC/USD"] = regime
                _enter(board)
                self.assertEqual((board.trades, board.filler.pending), ({}, []))

    def test_the_cost_gate_skips_small_moves_and_reports_once_a_minute(self) -> None:
        board = _board()
        _seed(board, closes=[100, 100.4] * 16)  # ATR 0.4: expects 0.8, under 3 x ~0.52
        board.on_tick(quote("BTC/USD", 100.3, 100.31, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + 2 * SEC))
        board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + 3 * SEC))
        self.assertEqual((board.trades, board.filler.pending, board.skipped_total), ({}, [], 1))
        events = board.on_tick(quote("BTC/USD", 100.49, 100.51, OPEN + timedelta(minutes=1, seconds=1)))
        [skip] = [e for e in events if e.kind == "fast_skipped"]
        self.assertIn("cost gate", skip.text)

    def test_on_fifteen_minute_bars_a_skip_counts_once_per_bar(self) -> None:
        board = _board(FastConfig(enabled=True, symbols=("BTC/USD",), bar_minutes=15))
        self.assertEqual((board.bars.minutes, board.bars.keep), (15, 240))
        step = timedelta(minutes=15)
        for i, close in enumerate([100, 100.4] * 16):
            board.bars.add("BTC/USD", float(close), T0 + i * step)
        board.regimes["BTC/USD"] = TRENDING
        start = T0 + 32 * step  # the bar still open after seeding
        for seconds in range(1, 15 * 60 + 20, 20):  # a price every 20 s, past the bar's end
            board.on_tick(quote("BTC/USD", 100.49, 100.51, start + seconds * SEC))
            if 1 < seconds < 15 * 60:  # the first price never enters
                self.assertEqual((board.trades, board.skipped_total), ({}, 1))
        board.on_tick(quote("BTC/USD", 100.59, 100.61, start + step + 21 * SEC))  # a new high in the next bar
        self.assertEqual((board.trades, board.skipped_total), ({}, 2))

    def test_the_position_cap(self) -> None:
        board = _board(FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"), max_positions=1))
        _seed(board)
        _seed(board, "ETH/USD")
        _enter(board)
        board.on_tick(quote("ETH/USD", 102.9, 103.1, OPEN + 5 * SEC))
        board.on_tick(quote("ETH/USD", 104.9, 105.1, OPEN + 6 * SEC))
        self.assertEqual(board.filler.pending, [])
        self.assertNotIn("ETH/USD", board.trades)

    def test_the_first_price_after_a_quiet_feed_never_enters(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 32 * SEC))  # 31 s of silence
        self.assertEqual(board.filler.pending, [])
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 33 * SEC))
        self.assertEqual(len(board.filler.pending), 1)

    def test_a_crossed_quote_is_never_used(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))
        board.on_tick(quote("BTC/USD", 105.2, 105.0, OPEN + 2 * SEC))
        self.assertEqual(board.filler.pending, [])

    def test_an_entry_too_small_to_fill_is_reported_not_traded(self) -> None:
        board = _board(equity="2")  # a third of $2 is under the $1 minimum
        _seed(board)
        events = _enter(board)
        self.assertIn("fast_unfilled", [e.kind for e in events])
        self.assertEqual(board.trades, {})


class ExitTests(unittest.TestCase):
    def test_an_open_trade_keeps_its_playbook_when_the_reading_changes(self) -> None:
        board = _board()
        _seed(board)
        _enter(board)
        board.regimes["BTC/USD"] = SQUEEZE
        board.on_tick(quote("BTC/USD", 98.0, 98.2, OPEN + 3 * SEC))  # under the 99.2 trail
        self.assertEqual(board.exiting["BTC/USD"], "trailing stop")
        self.assertEqual(board.trades["BTC/USD"].playbook, "breakout")

    def test_a_gap_through_the_stop_fills_at_the_real_bid(self) -> None:
        board = _board()
        _seed(board)
        _enter(board)
        [done] = [e for e in _exit_through_a_gap(board) if e.kind == "fast_exit"]
        self.assertEqual(done.data["price"], "89.5")
        self.assertLess(done.data["net_pct"], -14)
        self.assertEqual(board.trades, {})

    def test_a_cooldown_follows_every_exit(self) -> None:
        # the gap loss is ~5% of the book: loosen the daily stop so only the cooldown is tested
        board = _board(FastConfig(enabled=True, symbols=("BTC/USD", "ETH/USD"), daily_loss_stop=D("0.5")))
        _seed(board)
        _enter(board)
        _exit_through_a_gap(board)
        self.assertEqual(board.cooldown_until["BTC/USD"], OPEN + 3 * SEC + 300 * MS + timedelta(minutes=5))
        soon = OPEN + timedelta(minutes=2)
        board.on_tick(quote("BTC/USD", 105.9, 106.1, soon))
        board.on_tick(quote("BTC/USD", 105.9, 106.1, soon + SEC))
        self.assertEqual(board.filler.pending, [])
        later = OPEN + timedelta(minutes=6)
        board.on_tick(quote("BTC/USD", 106.9, 107.1, later))
        board.on_tick(quote("BTC/USD", 106.9, 107.1, later + SEC))
        self.assertEqual(len(board.filler.pending), 1)

    def test_the_daily_loss_stop_lasts_until_the_next_utc_day(self) -> None:
        board = _board()
        _seed(board)
        board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + SEC))  # the day starts at $300
        board.book.cash -= D("12")  # down 4%
        events = board.on_tick(quote("BTC/USD", 102.9, 103.1, OPEN + 2 * SEC))
        self.assertIn("fast_halted", [e.kind for e in events])
        board.on_tick(quote("BTC/USD", 104.9, 105.1, OPEN + 3 * SEC))
        self.assertEqual(board.filler.pending, [])
        self.assertTrue(board.status()["halted"])
        tomorrow = OPEN + timedelta(days=1)
        board.on_tick(quote("BTC/USD", 105.9, 106.1, tomorrow))
        board.on_tick(quote("BTC/USD", 105.9, 106.1, tomorrow + SEC))
        self.assertFalse(board.status()["halted"])
        self.assertEqual(len(board.filler.pending), 1)


class MirrorAndStatusTests(unittest.TestCase):
    def test_coinbase_ticks_feed_the_mirror_and_status_shows_both_books(self) -> None:
        mirror = CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=D("300")), D("0.006"))
        board = _board(mirror=mirror)
        _seed(board)
        board.on_tick(quote("BTC-USD", 105.0, 105.3, OPEN + 2 * SEC, venue="coinbase"))
        _enter(board)
        self.assertIn("BTC/USD", mirror.book.positions)
        status = board.status()
        coin = status["coins"][0]
        self.assertEqual((coin["symbol"], coin["regime"], coin["trade"]["playbook"]),
                         ("BTC/USD", "trending", "breakout"))
        self.assertEqual(status["mirror"]["unpriced"], 0)
        self.assertIsNotNone(status["book"]["return_pct"])
        self.assertTrue(status["coins"][1]["standing_aside"])  # ETH is still warming


class GateEachPlanTests(unittest.TestCase):
    def _entries(self, breakout_move, pullback_move):
        from agentic_trading.fast.playbooks import Plan
        return {TRENDING: (
            ("breakout", lambda bars, price: Plan("breakout", price - 1, None, breakout_move)),
            ("pullback", lambda bars, price: Plan("pullback", price - 1, price + 10, pullback_move)),
        )}

    def test_a_passing_plan_is_taken_when_an_earlier_one_fails_the_gate(self) -> None:
        from unittest.mock import patch
        board = _board()
        _seed(board)
        with patch.dict("agentic_trading.fast.switchboard.ENTRIES", self._entries(0.01, 10.0), clear=True):
            _enter(board)
        self.assertEqual(board.trades["BTC/USD"].playbook, "pullback")
        self.assertEqual(board.skipped_total, 0)

    def test_a_skip_is_counted_only_when_every_plan_fails(self) -> None:
        from unittest.mock import patch
        board = _board()
        _seed(board)
        with patch.dict("agentic_trading.fast.switchboard.ENTRIES", self._entries(0.01, 0.02), clear=True):
            _enter(board)
        self.assertEqual((board.trades, board.skipped_total), ({}, 1))
