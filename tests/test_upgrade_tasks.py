"""Earner work first; a task that fails twice rests 30 days; out-of-scope work is never retried."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.upgrade.tasks import SEED_BACKLOG, candidates, note_health, pick, record

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def _proposals(state: Path, rows: list[dict]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "proposals.json").write_text(json.dumps({"proposals": rows}))


class TaskTests(unittest.TestCase):
    def test_strategy_proposals_come_first_then_the_backlog_then_health(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [
                {"title": "Tighten risk caps", "category": "risk", "confidence": 0.95, "status": "proposed",
                 "change": "c1"},
                {"title": "Better momentum", "category": "strategy", "confidence": 0.6, "status": "proposed",
                 "change": "c2"},
                {"title": "Done already", "category": "strategy", "confidence": 0.9, "status": "applied",
                 "change": "c3"},
            ])
            note_health(state, "agentic-trading is not active")
            found = candidates(state)
        titles = [t.title for t in found]
        self.assertEqual(titles[0], "Better momentum")
        self.assertEqual(titles[1], "Tighten risk caps")
        self.assertNotIn("Done already", titles)
        self.assertEqual(found[2].source, "backlog")
        self.assertEqual(found[-1].source, "health")
        self.assertEqual(len([t for t in found if t.source == "backlog"]), len(SEED_BACKLOG))

    def test_failures_rest_and_out_of_scope_is_final(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [{"title": "A", "category": "strategy", "confidence": 0.9, "status": "proposed",
                                "change": "a"}])
            first = pick(state, NOW)
            record(state, first, "failed", NOW)
            self.assertEqual(pick(state, NOW).key, first.key)  # one failure: still eligible
            record(state, first, "failed", NOW)
            self.assertNotEqual(pick(state, NOW).key, first.key)  # two: rests
            self.assertEqual(pick(state, NOW + timedelta(days=31)).key, first.key)  # back after 30 days
            record(state, first, "out_of_scope", NOW, "lives in runtime.py")
            self.assertNotEqual(pick(state, NOW + timedelta(days=90)).key, first.key)
            ledger = json.loads((state / "upgrade" / "attempts.json").read_text())
        self.assertEqual(ledger[first.key]["outcome"], "out_of_scope")
