"""The agent fleet: separate jobs, declared authority, observed health.

The bot is one process but not one agent. Data can fail without stopping
research; research can be stale without blocking execution; and only execution
may reach the broker. Those are the properties worth testing — a roster that
merely lists names would make the system look modular without being safer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.agents import (
    FLEET,
    MAY_REDUCE_RISK,
    MAY_TRADE,
    READ_ONLY,
    authority_allows,
    grade_health,
    load_roster,
)


class FleetTests(unittest.TestCase):
    def test_the_four_named_roles_exist_plus_the_back_check(self) -> None:
        names = {agent.name for agent in FLEET}
        self.assertEqual(
            names, {"data", "research", "strategy", "execution", "backcheck"}
        )

    def test_only_execution_may_trade(self) -> None:
        traders = [
            agent.name for agent in FLEET if authority_allows(agent.authority, "trade")
        ]
        self.assertEqual(traders, ["execution"])

    def test_data_and_research_cannot_even_reduce_risk(self) -> None:
        """They inform; they do not decide. A data bug must not veto a trade."""
        for agent in FLEET:
            if agent.name in ("data", "research", "backcheck"):
                self.assertEqual(agent.authority, READ_ONLY)
                self.assertFalse(authority_allows(agent.authority, "reduce_risk"))

    def test_strategy_may_reduce_risk_but_not_trade(self) -> None:
        strategy = next(agent for agent in FLEET if agent.name == "strategy")
        self.assertEqual(strategy.authority, MAY_REDUCE_RISK)
        self.assertTrue(authority_allows(strategy.authority, "reduce_risk"))
        self.assertFalse(authority_allows(strategy.authority, "trade"))

    def test_every_agent_declares_a_cadence_and_a_purpose(self) -> None:
        for agent in FLEET:
            self.assertGreater(agent.cadence_seconds, 0, agent.name)
            self.assertGreater(len(agent.description), 40, agent.name)

    def test_an_unknown_action_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ValueError):
            authority_allows(MAY_TRADE, "launch_missiles")


class HealthTests(unittest.TestCase):
    def _entry(self, **overrides):
        now = datetime.now(timezone.utc)
        entry = {
            "name": "data",
            "last_run_at": now.isoformat(),
            "last_ok_at": now.isoformat(),
            "consecutive_failures": 0,
        }
        entry.update(overrides)
        return entry

    def test_a_recent_success_is_ok(self) -> None:
        self.assertEqual(grade_health(self._entry()).status, "ok")

    def test_silence_beyond_a_few_cadences_is_stale(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        health = grade_health(self._entry(last_ok_at=old))
        self.assertEqual(health.status, "stale")
        self.assertGreater(health.age_seconds or 0, 86_400)

    def test_repeated_failures_are_failing_even_if_it_ran(self) -> None:
        health = grade_health(
            self._entry(consecutive_failures=3, last_error="broker unavailable")
        )
        self.assertEqual(health.status, "failing")
        self.assertIn("broker", health.detail["last_error"])

    def test_a_single_failure_is_not_a_failure_state(self) -> None:
        """Transient errors are normal; three in a row is a problem."""
        self.assertEqual(grade_health(self._entry(consecutive_failures=1)).status, "ok")

    def test_an_agent_that_never_ran_is_unknown_not_ok(self) -> None:
        health = grade_health({"name": "research"})
        self.assertEqual(health.status, "unknown")
        self.assertIsNone(health.age_seconds)

    def test_a_disabled_agent_is_reported_as_disabled(self) -> None:
        self.assertEqual(
            grade_health(self._entry(status="disabled")).status, "disabled"
        )

    def test_an_unreadable_timestamp_does_not_crash_the_roster(self) -> None:
        self.assertEqual(grade_health(self._entry(last_ok_at="nonsense")).status, "unknown")


class RosterTests(unittest.TestCase):
    def test_every_declared_agent_appears_even_if_it_never_ran(self) -> None:
        """An agent missing from the roster would look like an agent that is fine."""
        with tempfile.TemporaryDirectory() as name:
            roster = load_roster(Path(name))
        self.assertEqual(
            [agent["name"] for agent in roster["agents"]],
            [agent.name for agent in FLEET],
        )
        self.assertTrue(all(a["health"]["status"] == "unknown" for a in roster["agents"]))

    def test_published_facts_are_merged_with_the_declared_role(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "agents.json").write_text(
                json.dumps(
                    {
                        "pid": 1,
                        "mode": "live",
                        "stage": "live",
                        "agents": [
                            {
                                "name": "execution",
                                "last_ok_at": datetime.now(timezone.utc).isoformat(),
                                "detail": {"max_order_pct": "0.0204", "armed": False},
                            }
                        ],
                    }
                )
            )
            roster = load_roster(state)
        execution = next(a for a in roster["agents"] if a["name"] == "execution")
        self.assertEqual(execution["authority"], MAY_TRADE)
        self.assertEqual(execution["detail"]["max_order_pct"], "0.0204")
        self.assertEqual(execution["health"]["status"], "ok")
        # Roles come from the declaration, never from the state file.
        self.assertIn("order placement", execution["role"])

    def test_a_corrupt_state_file_still_yields_the_full_fleet(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            (state / "agents.json").write_text("{not json")
            roster = load_roster(state)
        self.assertEqual(len(roster["agents"]), len(FLEET))
