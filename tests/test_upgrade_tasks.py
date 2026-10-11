"""Earner work first; a task that fails twice rests 30 days; out-of-scope work is never retried."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.upgrade.tasks import (SEED_BACKLOG, candidates, extend_backlog, known_titles, note_health,
                                           open_count, pick, record)

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)


def _proposals(state: Path, rows: list[dict]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "proposals.json").write_text(json.dumps({"proposals": rows}))


class TaskTests(unittest.TestCase):
    def test_strategy_proposals_come_first_then_the_backlog_then_health(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [
                {"title": "Tighten swarm caps", "category": "risk", "confidence": 0.95, "status": "proposed",
                 "change": "c1 in the swarm book"},
                {"title": "Better momentum", "category": "strategy", "confidence": 0.6, "status": "proposed",
                 "change": "c2 in strategies/momentum_rotation.py"},
                {"title": "Done already", "category": "strategy", "confidence": 0.9, "status": "applied",
                 "change": "c3 in swarm/blend.py"},
            ])
            note_health(state, "agentic-trading is not active")
            found = candidates(state)
        titles = [t.title for t in found]
        self.assertEqual(titles[0], "Better momentum")
        self.assertEqual(titles[1], "Tighten swarm caps")
        self.assertNotIn("Done already", titles)
        self.assertEqual(found[2].source, "backlog")
        self.assertEqual(found[-1].source, "health")
        self.assertEqual(len([t for t in found if t.source == "backlog"]), len(SEED_BACKLOG))

    def test_only_proposals_naming_the_allow_list_are_worked_on(self) -> None:
        def row(title: str, change: str, category: str = "strategy") -> dict:
            return {"title": title, "category": category, "status": "proposed", "change": change}

        keep = [row("Blend by evidence", "edit src/agentic_trading/swarm/blend.py"),
                row("Rotation lookback", "momentum_rotation should use 90 days", "research"),
                row("Swarm cull", "cull the swarm sooner", "reliability"),
                row("Recipe test", "add tests/test_swarm_recipe.py cases")]
        drop = [row("Chase ceiling", "add a chase ceiling to trend-entry gating"),  # a category, not a place
                row("Min notional", "log it in src/agentic_trading/runtime.py", "execution"),
                row("Clamp sizing", "sizer.py and desk/allocator.py", "risk"),
                row("Funding guard", "edit tests/test_swarm_funding.py"),  # a protected test
                row("Docs only", "explain it in docs/notes.md")]
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, keep + drop)
            titles = {t.title for t in candidates(state) if t.source == "proposal"}
        self.assertEqual(titles, {r["title"] for r in keep})

    def test_failures_rest_and_out_of_scope_is_final(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [{"title": "A", "category": "strategy", "confidence": 0.9, "status": "proposed",
                                "change": "a in swarm/blend.py"}])
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

    def test_open_count_counts_backlog_and_proposals_by_picks_rules(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _proposals(state, [{"title": "Swarm blend", "category": "strategy", "status": "proposed",
                                "change": "swarm/blend.py"}])
            note_health(state, "a service is down")
            self.assertEqual(open_count(state, NOW), len(SEED_BACKLOG) + 1)  # health never counts
            first = pick(state, NOW)
            record(state, first, "out_of_scope", NOW)
            self.assertEqual(open_count(state, NOW), len(SEED_BACKLOG))
            self.assertEqual(open_count(state, NOW, sources=("backlog",)), len(SEED_BACKLOG))

    def test_open_count_without_seed_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertEqual(open_count(state, NOW, seed=False), 0)
            self.assertFalse((state / "upgrade" / "backlog.json").exists())

    def test_extend_backlog_appends_and_known_titles_sees_ledger_and_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            first = pick(state, NOW)
            record(state, first, "failed", NOW)
            extend_backlog(state, [{"key": "plan-1", "title": "New swarm idea", "detail": "d", "origin": "planner"}])
            titles = known_titles(state)
            self.assertIn("New swarm idea", titles)
            self.assertIn(first.title, titles)
            self.assertEqual(candidates(state)[-1].title, "New swarm idea")
