"""The planner: tool-less, stdin-fed, and every task it offers is checked by trusted code before it is queued."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentic_trading.upgrade import planner
from agentic_trading.upgrade.run import RunResult
from agentic_trading.upgrade.tasks import candidates

NOW = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
CLOSURE = {"agentic_trading.swarm.store"}


def _task(
    title: str = "Weight swarm shares by forward t-statistic",
    files=None,
    evidence="trend median +2.0%",
    **extra,
) -> dict:
    return {
        "title": title,
        "detail": "do it",
        "files": ["src/agentic_trading/swarm/blend.py"] if files is None else files,
        "evidence": evidence,
        "falsified_if": "shares stop summing to 1",
        **extra,
    }


def _reply(*tasks: dict) -> str:
    return json.dumps({"tasks": list(tasks)})


class AcceptTests(unittest.TestCase):
    def accept(self, reply: str, known=(), room: int = 8):
        return planner.accept(
            reply, closure=CLOSURE, known=list(known), room=room, now=NOW
        )

    def test_a_good_task_becomes_a_backlog_row(self) -> None:
        added, skipped = self.accept(_reply(_task()))
        self.assertEqual(skipped, [])
        row = added[0]
        self.assertTrue(row["key"].startswith("plan-"))
        self.assertEqual(row["origin"], "planner")
        self.assertIn("Files: src/agentic_trading/swarm/blend.py", row["detail"])
        self.assertIn("Evidence: trend median +2.0%", row["detail"])
        self.assertIn("Falsified if: shares stop summing to 1", row["detail"])

    def test_each_refusal_names_its_reason(self) -> None:
        cases = {
            "title": _task(title="short"),
            "1-6 files": _task(files=[]),
            "1-6 files ": _task(
                files=[f"src/agentic_trading/swarm/f{i}.py" for i in range(7)]
            ),
            "outside the allow-list": _task(files=["src/agentic_trading/risk.py"]),
            "outside the allow-list ": _task(files=["tests/test_swarm_funding.py"]),
            "control module": _task(files=["src/agentic_trading/swarm/store.py"]),
            "swarm/ or strategies/": _task(files=["docs/notes.md"]),
            "evidence": _task(evidence=" "),
        }
        for reason, task in cases.items():
            with self.subTest(reason=reason):
                added, skipped = self.accept(_reply(task))
                self.assertEqual(added, [])
                self.assertIn(reason.strip(), skipped[0])

    def test_near_duplicates_and_a_full_backlog_are_skipped(self) -> None:
        added, skipped = self.accept(
            _reply(_task()), known=["Weight the swarm shares by forward t statistic"]
        )
        self.assertEqual(added, [])
        self.assertIn("near-duplicate", skipped[0])
        added, skipped = self.accept(
            _reply(
                _task(), _task(title="Weight swarm shares by forward t-statistic too")
            )
        )
        self.assertEqual(len(added), 1)  # the second repeats the first
        added, skipped = self.accept(_reply(_task()), room=0)
        self.assertIn("backlog is full", skipped[0])

    def test_at_most_three_tasks(self) -> None:
        titles = ("Cull trend agents on drawdown", "Screen reversal recipes longer", "Blend rotation by evidence",
                  "Retry refused momentum recipes")
        tasks = [_task(title=title) for title in titles]
        added, skipped = self.accept(_reply(*tasks))
        self.assertEqual(len(added), 3)
        self.assertIn("more than 3", skipped[0])

    def test_json_inside_prose_or_fences_is_read(self) -> None:
        for wrapped in (
            f"Here you go:\n```json\n{_reply(_task())}\n```\nThanks",
            f"Plan: {_reply(_task())}",
        ):
            with self.subTest(wrapped=wrapped[:12]):
                added, _ = self.accept(wrapped)
                self.assertEqual(len(added), 1)

    def test_an_unreadable_reply_raises(self) -> None:
        for bad in ("no json here", '{"ideas": []}', "{not json}"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.accept(bad)


def _swarm(state: Path) -> None:
    (state / "swarm").mkdir(parents=True)
    agents = [
        {
            "id": "a",
            "family": "trend",
            "excess_pct": 1.0,
            "drawdown_pct": 1.0,
            "state": "nursery",
            "share": 0.0,
        },
        {
            "id": "b",
            "family": "trend",
            "excess_pct": 3.0,
            "drawdown_pct": 2.0,
            "state": "funded",
            "share": 0.5,
        },
        {
            "id": "c",
            "family": "reversal",
            "excess_pct": 2.0,
            "drawdown_pct": 0.5,
            "state": "nursery",
            "share": 0.0,
        },
    ]
    (state / "swarm.json").write_text(
        json.dumps(
            {
                "as_of": "2026-10-08",
                "book": {"equity": "48.40"},
                "agents": agents,
                "trials": 41,
                "alive": 3,
            }
        )
    )
    (state / "swarm" / "lineage.json").write_text(
        json.dumps(
            {
                "agents": {
                    "d": {
                        "recipe": {"family": "trend"},
                        "died": "2026-10-09",
                        "cause": "culled",
                    },
                    "a": {"recipe": {"family": "trend"}, "died": None, "cause": ""},
                }
            }
        )
    )
    (state / "swarm" / "ledger.json").write_text(json.dumps({"rejected": ["x", "y"]}))
    (state / "swarm.log").write_text("".join(f"line {i}\n" for i in range(100)))


class DigestTests(unittest.TestCase):
    def test_family_medians_deaths_and_tails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            _swarm(state)
            found = planner.digest(state)
        self.assertEqual(found["families"]["trend"]["median_excess_pct"], 2.0)
        self.assertEqual(found["families"]["reversal"]["median_excess_pct"], 2.0)
        self.assertEqual(found["families"]["trend"]["deaths"], 1)
        self.assertEqual(found["families"]["trend"]["funded"], 1)
        self.assertEqual(found["death_causes"], {"culled": 1})
        self.assertEqual(found["refused_recipes"], 2)
        self.assertEqual(len(found["swarm_log"]), 40)
        self.assertEqual(found["swarm_log"][-1], "line 99")

    def test_missing_or_corrupt_files_give_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "swarm.json").write_text("{broken")
            found = planner.digest(state)
        self.assertEqual(found["families"], {})
        self.assertEqual(found["agents"], [])
        self.assertEqual(found["swarm_log"], [])


def _repo(root: Path, size: int = 50) -> Path:
    for folder in ("swarm", "strategies"):
        (root / "src" / "agentic_trading" / folder).mkdir(parents=True)
        (root / "src" / "agentic_trading" / folder / "a.py").write_text(
            "X = 1\n" + "#" * size + "\n"
        )
    return root


class PromptTests(unittest.TestCase):
    def test_rules_source_and_the_untrusted_marker(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            repo = _repo(Path(name) / "repo")
            text = planner.prompt(repo, Path(name) / "state")
        self.assertIn(planner.PLAN_RULES, text)
        self.assertIn("### src/agentic_trading/swarm/a.py", text)
        self.assertIn("### src/agentic_trading/strategies/a.py", text)
        self.assertIn("untrusted", text)

    def test_source_past_the_cap_is_cut(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            repo = _repo(Path(name) / "repo", size=planner.SOURCE_CAP)
            text = planner.prompt(repo, Path(name) / "state")
        self.assertIn("cut at the source cap", text)
        self.assertIn("left out", text)
        self.assertLess(len(text), planner.SOURCE_CAP + 40_000)


class DueTests(unittest.TestCase):
    def test_due_only_outside_live_when_low_and_once_a_day(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertFalse(
                planner.due(state, mode="shadow", now=NOW)
            )  # the seed backlog is full of work
            (state / "upgrade" / "backlog.json").write_text("[]")
            self.assertTrue(planner.due(state, mode="shadow", now=NOW))
            self.assertFalse(planner.due(state, mode="live", now=NOW))
            (state / "upgrade" / "planner.json").write_text(
                json.dumps({"at": NOW.isoformat()})
            )
            self.assertFalse(planner.due(state, mode="shadow", now=NOW))


class _Runner:
    def __init__(self, result: RunResult) -> None:
        self.result, self.calls = result, []

    def __call__(self, argv, cwd=None, timeout=600.0, input=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "input": input})
        return self.result


class RunTests(unittest.TestCase):
    def _go(self, result: RunResult, save: bool = True):
        name = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, name, ignore_errors=True)
        root = Path(name)
        repo, state = _repo(root / "repo", size=200_000), root / "state"
        (state / "upgrade").mkdir(parents=True)
        (state / "upgrade" / "backlog.json").write_text("[]")
        runner = _Runner(result)
        record = planner.run(
            repo=repo, state_dir=state, runner=runner, cwd=root, now=NOW, save=save
        )
        return record, runner, state

    def test_the_prompt_goes_on_stdin_and_rows_reach_the_backlog(self) -> None:
        record, runner, state = self._go(
            RunResult(0, _reply(_task(files=["src/agentic_trading/swarm/a.py"])))
        )
        call = runner.calls[0]
        self.assertEqual(call["argv"], planner.COMMAND)
        self.assertTrue(all(len(part) < 1024 for part in call["argv"]))
        self.assertGreater(len(call["input"]), 128 * 1024)
        self.assertEqual(record["error"], "")
        self.assertEqual([t.title for t in candidates(state)], [_task()["title"]])
        saved = json.loads((state / "upgrade" / "planner.json").read_text())
        self.assertEqual(saved["added"], [_task()["title"]])

    def test_a_dry_run_writes_nothing(self) -> None:
        record, _, state = self._go(
            RunResult(0, _reply(_task(files=["src/agentic_trading/swarm/a.py"]))),
            save=False,
        )
        self.assertEqual(len(record["added"]), 1)
        self.assertEqual(
            json.loads((state / "upgrade" / "backlog.json").read_text()), []
        )
        self.assertFalse((state / "upgrade" / "planner.json").exists())

    def test_failures_set_error_and_add_nothing(self) -> None:
        for result in (RunResult(1, "Not logged in"), RunResult(0, "I have no ideas")):
            with self.subTest(out=result.out):
                record, _, state = self._go(result)
                self.assertNotEqual(record["error"], "")
                self.assertEqual(record["added"], [])
                self.assertEqual(
                    json.loads((state / "upgrade" / "backlog.json").read_text()), []
                )
                self.assertTrue(
                    (state / "upgrade" / "planner.json").exists()
                )  # it ran today; no retry


if __name__ == "__main__":
    unittest.main()
