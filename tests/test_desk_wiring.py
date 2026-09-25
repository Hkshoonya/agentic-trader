"""The desk wired into config, the CLI factory, evidence and the trial."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from tests.test_runtime_daemon import _write_config


def _config(tmp: Path, *extra: str):
    return load_config(
        _write_config(
            tmp,
            extra=['strategy = "desk"', f'history_path = "{tmp / "bars"}"', *extra],
        )
    )


class DeskConfigTests(unittest.TestCase):
    def test_the_desk_loads_with_its_default_members(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
        self.assertEqual(config.strategy, "desk")
        self.assertEqual(
            config.desk_members, ("momentum_rotation", "trend_crypto", "benchmark")
        )
        self.assertEqual(config.desk_member_order_pct, Decimal("0.19"))

    def test_unknown_members_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ValueError, "desk_members"):
                _config(Path(name), 'desk_members = ["benchmark", "moonshot"]')

    def test_the_benchmark_member_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(ValueError, "benchmark"):
                _config(Path(name), 'desk_members = ["momentum_rotation"]')


class DeskFactoryTests(unittest.TestCase):
    def test_build_strategy_returns_a_desk_with_its_members(self) -> None:
        from agentic_trading.cli import build_strategy
        from agentic_trading.desk.desk import StrategyDesk

        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name))
            desk = build_strategy(config)
            self.assertIsInstance(desk, StrategyDesk)
            self.assertEqual(
                [member.name for member in desk.members],
                ["momentum_rotation", "trend_crypto", "benchmark"],
            )
            # Member strategies save their own state under the desk directory.
            rotation = desk.members[0].strategy
            self.assertEqual(
                rotation.state_path,
                Path(config.state_dir) / "desk" / "momentum_rotation_strategy.json",
            )

    def test_the_evidence_engine_grades_no_rule_for_the_desk(self) -> None:
        from agentic_trading.walkforward import rank_targets, rule_for_strategy

        self.assertEqual(rule_for_strategy("desk"), "none")
        self.assertEqual(rank_targets({}, datetime.now(timezone.utc), rule="none"), [])


class TrialHandoverTests(unittest.TestCase):
    def test_the_running_trial_becomes_the_rotation_members_book(self) -> None:
        from agentic_trading import trial
        from agentic_trading.cli import build_strategy

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = _config(tmp)
            started = datetime(2026, 9, 23, 23, 20, tzinfo=timezone.utc)
            Path(config.state_dir).mkdir(parents=True, exist_ok=True)
            trial.start_trial(config, starting_equity=Decimal("49.9"), now=started)
            # In production the trial was started under the rotation, before
            # the desk existed; start_trial records the config's strategy.
            trial_path = Path(config.state_dir) / "trial.json"
            payload = json.loads(trial_path.read_text(encoding="utf-8"))
            payload["strategy"] = "momentum_rotation"
            trial_path.write_text(json.dumps(payload), encoding="utf-8")
            journal = Path(config.journal_dir)
            journal.mkdir(parents=True)
            record = {
                "event": "accepted",
                "mode": "shadow",
                "at": (started + timedelta(minutes=3)).isoformat(),
                "intent": {
                    "symbol": "SOL-USD",
                    "side": "buy",
                    "quantity": "0.05",
                    "ref_price": "190",
                },
            }
            (journal / "2026-09-23.jsonl").write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )

            desk = build_strategy(config)
            book = desk.members[0].book
            self.assertEqual(book.positions, {"SOL-USD": Decimal("0.05")})
            self.assertEqual(book.starting_equity, Decimal("49.9"))
            self.assertEqual(book.entries, 1)

            scored = trial.score_trial(config, now=started + timedelta(days=1))
        self.assertEqual(scored["entries"], 1)
        self.assertEqual([p["symbol"] for p in scored["positions"]], ["SOL-USD"])
