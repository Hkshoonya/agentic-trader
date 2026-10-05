"""A whole daily step: seeding, catching up, refusing stale or repeated work, ageing."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic_trading.backtest import CostModel
from agentic_trading.config import load_config
from agentic_trading.desk.book import MemberBook
from agentic_trading.swarm.screen import Screen
from agentic_trading.swarm.settings import SwarmConfig
from agentic_trading.swarm.step import finished_day, run_step
from agentic_trading.swarm.store import SwarmStore
from tests.swarm_support import D0, universe
from tests.test_runtime_daemon import _write_config

SERIES = universe(500)
LAST = SERIES["BTCUSD"][-1].start.date()
SWARM = SwarmConfig(enabled=True, max_agents=6, screens_per_day=3)
PASS = Screen(True, "passed", 25, 5.0, 10.0, {})


def _now(day):  # half past midnight on ``day``
    return datetime.combine(day, time(0, 30), tzinfo=timezone.utc)


class StepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(_write_config(Path(self.tmp.name)))
        self.store = SwarmStore(self.config.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _step(self, day, series=SERIES, swarm=SWARM, **kw):
        return run_step(self.config, swarm, now=_now(day), series=series, costs=CostModel(), cash=50, **kw)

    def _events(self) -> list[dict]:
        out = []
        for path in sorted(Path(self.config.journal_dir).glob("swarm-*.jsonl")):
            out += [json.loads(line) for line in path.read_text().splitlines()]
        return out

    def test_a_day_counts_only_once_a_later_bar_exists(self) -> None:
        self.assertEqual(finished_day(SERIES, LAST), LAST - timedelta(days=1))  # today's bar is forming
        self.assertEqual(finished_day(SERIES, LAST + timedelta(days=1)), LAST - timedelta(days=1))  # no sync since
        self.assertEqual(finished_day(SERIES, LAST + timedelta(days=2)), LAST - timedelta(days=1))

    def test_stale_bars_do_nothing_but_say_so(self) -> None:
        result = self._step(LAST + timedelta(days=9))
        self.assertEqual(result.code, 0)
        self.assertEqual([e["event"] for e in self._events()], ["swarm_stale_bars"])
        self.assertTrue(self.store.read_status()["stale"])
        self.assertEqual(self.store.load(_now(LAST))[0].trials, 0)

    def test_the_first_step_seeds_a_population_and_a_second_run_changes_nothing(self) -> None:
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self.assertEqual(self._step(LAST + timedelta(days=1)).code, 0)
            state, _ = self.store.load(_now(LAST))
            births = [e for e in self._events() if e["event"] == "swarm_birth"]
            self.assertEqual((len(state.living), state.trials, len(births)), (3, 3, 3))
            as_of = LAST - timedelta(days=1)
            self.assertEqual(state.last_step, as_of.isoformat())
            self.assertTrue(all(a.born == LAST.isoformat() for a in state.living))
            status = self.store.read_status()
            self.assertEqual((status["as_of"], status["alive"], status["trials"]), (as_of.isoformat(), 3, 3))
            self.assertEqual({a["state"] for a in status["agents"]}, {"nursery"})
            book_before = self.store.book_path.read_text()
            again = self._step(LAST + timedelta(days=1))
        self.assertIn("already", again.message)
        self.assertEqual(self.store.load(_now(LAST))[0].trials, 3)
        self.assertEqual(self.store.book_path.read_text(), book_before)

    def test_a_missed_stretch_is_replayed_one_day_at_a_time(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=3), series=early)
            self._step(LAST + timedelta(days=1))
        book, _ = MemberBook.load(self.store.book_path, name="swarm", starting_equity=50)
        days = [d for d, _ in book.samples]  # first step: as_of LAST-5; second: LAST-4 .. LAST-1
        self.assertEqual(days, [(LAST - timedelta(days=5 - i)).isoformat() for i in range(4)])

    def test_a_replay_culls_each_missed_day_with_that_days_weekday(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=9)] for s, bars in SERIES.items()}
        seen = []

        def spy(agents, *, today, **kw):
            seen.append(today)
            return []

        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=8), series=early)
            with mock.patch("agentic_trading.swarm.step.cull", side_effect=spy):
                self._step(LAST + timedelta(days=1))
        expected = [LAST - timedelta(days=9 - i) for i in range(9)]  # LAST-9 .. LAST-1, a Monday among them
        self.assertEqual(seen, expected)
        self.assertIn(0, {d.weekday() for d in seen})

    def test_an_erroring_agent_sits_out_and_the_rest_carry_on(self) -> None:
        from agentic_trading.swarm import life
        real = life.forward_record
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=1))
            victim = self.store.load(_now(LAST))[0].living[0].recipe.id

            def flaky(recipe, *args, **kw):
                if recipe.id == victim:
                    raise ArithmeticError("bad bar")
                return real(recipe, *args, **kw)

            with mock.patch("agentic_trading.swarm.step.forward_record", side_effect=flaky):
                self._step(LAST + timedelta(days=1))
        errors = [e for e in self._events() if e["event"] == "swarm_agent_error"]
        self.assertEqual(len(errors), 1)
        states = {a["id"]: a["state"] for a in self.store.read_status()["agents"]}
        self.assertEqual(states[victim], "errored")

    def test_a_held_lock_means_another_step_is_running(self) -> None:
        self.assertTrue(self.store.lock(_now(LAST + timedelta(days=1))))  # held by a step running right now
        self.assertIn("another swarm step", self._step(LAST + timedelta(days=1)).message)

    def test_agents_age_out_of_the_nursery_over_weeks(self) -> None:
        start = LAST - timedelta(days=25)
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            for offset in range(26):
                day = start + timedelta(days=offset)
                window = {s: [b for b in bars if b.start.date() <= day] for s, bars in SERIES.items()}
                self._step(day + timedelta(days=1), series=window)
        status = self.store.read_status()
        oldest = max(a["forward_days"] for a in status["agents"])
        self.assertGreaterEqual(oldest, 20)
        self.assertTrue({a["state"] for a in status["agents"]} & {"contributing", "waiting"})
        state, _ = self.store.load(_now(LAST))
        self.assertGreaterEqual(state.trials, len(state.living))  # the ledger never undercounts

    def test_an_unpatched_step_runs_the_real_screen(self) -> None:
        result = self._step(LAST + timedelta(days=1), swarm=SwarmConfig(enabled=True, max_agents=2,
                                                                          screens_per_day=2))
        self.assertEqual(result.code, 0)
        state, _ = self.store.load(_now(LAST))
        self.assertEqual(state.trials, 2)  # two screens, whatever they decided


class CliTests(unittest.TestCase):
    def test_a_disabled_swarm_does_nothing_and_status_reads_plainly(self) -> None:
        import contextlib, io
        from agentic_trading.swarm.cli import dispatch_swarm, status_lines
        from types import SimpleNamespace as NS

        with tempfile.TemporaryDirectory() as name:
            path = _write_config(Path(name))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = dispatch_swarm(NS(config=str(path), swarm_action="step"))
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out.getvalue())
        lines = status_lines({"as_of": "2026-10-04", "alive": 1, "trials": 4,
                              "book": {"equity": "50.10", "return_pct": 0.2},
                              "agents": [{"name": "trend-ab12", "state": "nursery", "forward_days": 3,
                                          "excess_pct": 0.5, "share": 0.0}]})
        self.assertIn("4 recipes tried", lines[0])
        self.assertIn("trend-ab12: nursery", lines[1])


def _samples(store) -> list[str]:
    book, _ = MemberBook.load(store.book_path, name="swarm", starting_equity=50)
    return [d for d, _ in book.samples]


class ReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(_write_config(Path(self.tmp.name)))
        self.store = SwarmStore(self.config.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _step(self, day, series=SERIES, **kw):
        return run_step(self.config, SWARM, now=_now(day), series=series, costs=CostModel(), cash=50, **kw)

    def test_the_live_dashed_crypto_keys_work(self) -> None:
        dashed = {("BTC-USD" if s == "BTCUSD" else "ETH-USD" if s == "ETHUSD" else "SOL-USD" if s == "SOLUSD"
                   else s): bars for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            result = self._step(LAST + timedelta(days=1), series=dashed)
        self.assertNotIn("waits", result.message)
        status = self.store.read_status()
        self.assertEqual((status["as_of"], status["alive"]), ((LAST - timedelta(days=1)).isoformat(), 3))

    def test_a_crash_while_breeding_never_replays_days_into_the_book(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=3), series=early)
            with mock.patch("agentic_trading.swarm.step.immigrant", side_effect=RuntimeError("killed")):
                with self.assertRaises(RuntimeError):
                    self._step(LAST + timedelta(days=1))
            self._step(LAST + timedelta(days=1))
        days = _samples(self.store)
        self.assertEqual(days, sorted(set(days)))

    def test_a_book_ahead_of_the_ledger_is_not_replayed(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=3), series=early)
            self._step(LAST + timedelta(days=1))
            ledger = json.loads(self.store.ledger_path.read_text())
            ledger["last_step"] = (LAST - timedelta(days=4)).isoformat()  # as if the save was lost
            self.store.ledger_path.write_text(json.dumps(ledger))
            self._step(LAST + timedelta(days=1))
        days = _samples(self.store)
        self.assertEqual(days, sorted(set(days)))

    def test_a_screen_that_raises_is_a_counted_rejection_not_a_crash(self) -> None:
        calls = []

        def flaky(recipe, *args, **kw):
            calls.append(recipe.id)
            if len(calls) == 1:
                raise TypeError("bad parameter")
            return PASS

        with mock.patch("agentic_trading.swarm.step.screen", side_effect=flaky):
            result = self._step(LAST + timedelta(days=1))
        self.assertEqual(result.code, 0)
        state, _ = self.store.load(_now(LAST))
        self.assertEqual((state.trials, len(state.living)), (3, 2))
        rejected = [e for e in self._events() if e["event"] == "swarm_rejected"]
        self.assertIn("errored", rejected[0]["text"])

    def test_an_agent_that_errored_once_recovers(self) -> None:
        from agentic_trading.swarm import life
        real = life.forward_record
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        mid = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=2)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            self._step(LAST - timedelta(days=3), series=early)
            victim = self.store.load(_now(LAST))[0].living[0].recipe.id

            def flaky(recipe, *args, **kw):
                if recipe.id == victim:
                    raise ArithmeticError("bad bar")
                return real(recipe, *args, **kw)

            with mock.patch("agentic_trading.swarm.step.forward_record", side_effect=flaky):
                self._step(LAST - timedelta(days=1), series=mid)
            self._step(LAST + timedelta(days=1))
        agent = next(a for a in self.store.load(_now(LAST))[0].living if a.recipe.id == victim)
        self.assertEqual(agent.errored, "")

    def _events(self) -> list[dict]:
        out = []
        for path in sorted(Path(self.config.journal_dir).glob("swarm-*.jsonl")):
            out += [json.loads(line) for line in path.read_text().splitlines()]
        return out


class DeferredFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(_write_config(Path(self.tmp.name)))
        self.store = SwarmStore(self.config.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _events(self) -> list[dict]:
        out = []
        for path in sorted(Path(self.config.journal_dir).glob("swarm-*.jsonl")):
            out += [json.loads(line) for line in path.read_text().splitlines()]
        return out

    def test_a_spent_scout_stays_quiet(self) -> None:
        from agentic_trading.llm.client import FakeLlmClient
        from agentic_trading.swarm.scout import week_key
        from agentic_trading.swarm.store import SwarmState

        as_of = LAST - timedelta(days=1)
        self.store.save(SwarmState(scout={"week": week_key(as_of), "spent": 3}))
        client = FakeLlmClient("{}")
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            run_step(self.config, SwarmConfig(enabled=True, max_agents=6, screens_per_day=3, llm_scout=True),
                     now=_now(LAST + timedelta(days=1)), series=SERIES, costs=CostModel(), cash=50,
                     scout_client=client)
        self.assertEqual([e for e in self._events() if e["event"] == "swarm_scout"], [])
        self.assertEqual(client.calls, [])

    def test_a_rejected_recipe_is_never_screened_again(self) -> None:
        from agentic_trading.swarm.recipe import validate

        same = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5,
                                                       "max_positions": 4},
                         "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})
        fail = Screen(False, "lost -1.0% after costs")
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.immigrant", return_value=same), \
                mock.patch("agentic_trading.swarm.step.screen", return_value=fail) as screened:
            run_step(self.config, SWARM, now=_now(LAST - timedelta(days=3)), series=early, costs=CostModel(), cash=50)
            run_step(self.config, SWARM, now=_now(LAST + timedelta(days=1)), series=SERIES, costs=CostModel(),
                     cash=50)
        self.assertEqual(screened.call_count, 1)
        self.assertEqual(self.store.load(_now(LAST))[0].trials, 1)


class BookSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(_write_config(Path(self.tmp.name)))
        self.store = SwarmStore(self.config.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_corrupt_book_stops_the_step_and_is_never_overwritten(self) -> None:
        early = {s: [b for b in bars if b.start.date() <= LAST - timedelta(days=4)] for s, bars in SERIES.items()}
        with mock.patch("agentic_trading.swarm.step.screen", return_value=PASS):
            run_step(self.config, SWARM, now=_now(LAST - timedelta(days=3)), series=early, costs=CostModel(), cash=50)
            self.store.book_path.write_text("{garbage")
            result = run_step(self.config, SWARM, now=_now(LAST + timedelta(days=1)), series=SERIES,
                              costs=CostModel(), cash=50)
        self.assertEqual(result.code, 1)
        self.assertIn("unreadable", result.message)
        self.assertEqual(self.store.book_path.read_text(), "{garbage")
        self.assertEqual(self.store.load(_now(LAST))[0].last_step, (LAST - timedelta(days=5)).isoformat())

    def test_a_long_market_weekend_does_not_stop_the_swarm(self) -> None:
        from agentic_trading.swarm.step import stale_note
        from tests.swarm_support import daily
        start = LAST - timedelta(days=30)
        series = {"QQQ": daily("QQQ", [400.0] * 20, start=start),  # its last close: start + 19
                  "BTCUSD": daily("BTCUSD", [60000.0] * 25, start=start)}
        today = start + timedelta(days=19 + 5)  # a Thursday holiday plus a weekend, and a day of sync lag
        self.assertEqual(stale_note(series, today), "")
        self.assertIn("QQQ", stale_note(series, today + timedelta(days=2)))
