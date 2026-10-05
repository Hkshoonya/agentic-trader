"""The swarm's state survives restarts, corruption and a second step at once."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.fast.service import FastJournal
from agentic_trading.swarm.life import Agent
from agentic_trading.swarm.recipe import validate
from agentic_trading.swarm.store import SwarmState, SwarmStore

NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)
TREND = validate({"family": "trend", "params": {"horizons": [10, 20, 50, 100], "min_vote": 0.5, "max_positions": 4},
                  "universe": "all", "per_order_pct": 0.2, "inverse_vol": False})


class StoreTests(unittest.TestCase):
    def test_a_round_trip_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            state = SwarmState([Agent(TREND, "2026-10-05", {"2026-10-01": 0.01})],
                               {TREND.id: {"recipe": TREND.to_dict(), "born": "2026-10-05"}}, 7, "2026-10-04",
                               {"week": "2026-W41", "spent": 1, "last_rationale": "why"})
            store.save(state)
            again, notes = store.load(NOW)
        self.assertEqual(notes, [])
        self.assertEqual([a.recipe.id for a in again.living], [TREND.id])
        self.assertEqual((again.trials, again.last_step, again.scout["spent"]), (7, "2026-10-04", 1))

    def test_a_corrupt_file_moves_aside_and_the_trial_count_never_falls(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            store.save(SwarmState([Agent(TREND, "2026-10-05")], {TREND.id: {}}, 9, "2026-10-04", {}))
            (Path(name) / "swarm" / "population.json").write_text("{not json")
            (Path(name) / "swarm" / "ledger.json").write_text("[]")
            state, notes = store.load(NOW)
            moved = sorted(p.name for p in (Path(name) / "swarm").glob("*.corrupt-*"))
        self.assertEqual(state.living, [])
        self.assertEqual(state.trials, 9)  # from lineage.json
        self.assertEqual(len(moved), 2)
        self.assertEqual(len(notes), 2)

    def test_the_lock_admits_one_step_and_a_stale_lock_is_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            self.assertTrue(store.lock(NOW))
            self.assertFalse(SwarmStore(Path(name)).lock(NOW + timedelta(minutes=5)))
            self.assertTrue(SwarmStore(Path(name)).lock(NOW + timedelta(hours=4)))  # the first one died
            store.unlock()
            self.assertTrue(store.lock(NOW))

    def test_status_reads_back_and_a_missing_one_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = SwarmStore(Path(name))
            self.assertEqual(store.read_status(), {})
            store.write_status({"as_of": "2026-10-04"})
            self.assertEqual(store.read_status(), {"as_of": "2026-10-04"})


class JournalTests(unittest.TestCase):
    def test_the_fast_journal_takes_a_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            FastJournal(Path(name), prefix="swarm").append({"event": "swarm_birth", "at": "2026-10-05T00:30:00+00:00"})
            FastJournal(Path(name)).append({"event": "fast_entry", "at": "2026-10-05T00:30:00+00:00"})
            names = sorted(p.name for p in Path(name).iterdir())
        self.assertEqual(names, ["fast-2026-10-05.jsonl", "swarm-2026-10-05.jsonl"])
