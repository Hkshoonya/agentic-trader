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
    def test_the_declared_roles_are_exactly_these(self) -> None:
        names = {agent.name for agent in FLEET}
        self.assertEqual(
            names,
            {"data", "research", "strategy", "execution", "evolution", "backcheck"},
        )

    def test_the_evolution_agent_cannot_change_anything(self) -> None:
        """A model that may change the system is a model that may increase risk.

        The evolution agent reads telemetry and writes proposals. If it ever
        gains `may_reduce_risk` it could start refusing trades; if it gains
        `may_trade` the whole design is gone. Both are pinned here.
        """
        evolution = next(agent for agent in FLEET if agent.name == "evolution")
        self.assertEqual(evolution.authority, READ_ONLY)
        self.assertFalse(authority_allows(evolution.authority, "reduce_risk"))
        self.assertFalse(authority_allows(evolution.authority, "trade"))

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


class EvolutionAgentTests(unittest.TestCase):
    """The evolution agent proposes; it never applies."""

    def _config(self, tmp: Path):
        import json as _json

        from agentic_trading.config import load_config

        (tmp / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "journal").mkdir(parents=True, exist_ok=True)
        path = tmp / "agentic.toml"
        path.write_text(
            "\n".join(
                [
                    'mode = "shadow"',
                    'symbol_whitelist = ["SPY"]',
                    'max_order_pct = "0.01"',
                    'daily_notional_pct = "0.04"',
                    'daily_loss_pct = "0.03"',
                    "max_open_positions = 1",
                    "equity_refresh_ticks = 30",
                    "equity_refresh_seconds = 60",
                    'timezone = "local"',
                    f'quotes_path = "{tmp / "q.jsonl"}"',
                    f'journal_dir = "{tmp / "journal"}"',
                    f'state_dir = "{tmp / "state"}"',
                    f'tools_snapshot_path = "{tmp / "t.json"}"',
                    f'token_path = "{tmp / "k.json"}"',
                    'mcp_url = "https://x"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return load_config(path)

    def _answer(self, **overrides):
        import json as _json

        base = {
            "title": "Tighten the cost assumption",
            "category": "research",
            "evidence": "measured_per_side_bps is null; assumed 2.0",
            "change": "run a cost sensitivity sweep over 0-20 bps",
            "expected_effect": "a break-even cost number to compare against",
            "risk": "analysis only",
            "falsified_if": "break-even lands below 2 bps",
            "confidence": 0.8,
        }
        base.update(overrides)
        return _json.dumps({"proposals": [base]})

    def test_a_well_formed_proposal_is_kept(self) -> None:
        from agentic_trading.evolve_system import parse_proposals

        proposals = parse_proposals(self._answer())
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].category, "research")
        self.assertEqual(proposals[0].status, "proposed")

    def test_a_proposal_without_evidence_or_a_falsifier_is_dropped(self) -> None:
        """Those two fields are what make it reviewable rather than an opinion."""
        from agentic_trading.evolve_system import parse_proposals

        self.assertEqual(parse_proposals(self._answer(evidence="")), [])
        self.assertEqual(parse_proposals(self._answer(falsified_if="")), [])

    def test_an_unknown_category_is_refused(self) -> None:
        from agentic_trading.evolve_system import parse_proposals

        self.assertEqual(parse_proposals(self._answer(category="vibes")), [])

    def test_garbled_output_is_not_a_crash(self) -> None:
        from agentic_trading.evolve_system import parse_proposals

        for payload in (None, "", "not json", {"proposals": "no"}, {"proposals": [1, 2]}):
            self.assertEqual(parse_proposals(payload), [])

    def test_running_it_writes_proposals_and_journals_but_changes_no_state(self) -> None:
        from unittest import mock

        import json as _json

        from agentic_trading import evolve_system
        from agentic_trading.evolve_system import read, run
        from agentic_trading.llm.client import FakeLlmClient

        class _Stub:
            model = "stub"

            def complete(self, system: str, user: str) -> str:
                return self_answer

        self_answer = self._answer()
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            config = self._config(tmp)
            (tmp / "state" / "effective_limits.json").write_text(
                _json.dumps({"max_order_pct": "0.01"})
            )
            before = (tmp / "state" / "effective_limits.json").read_text()
            journal = mock.Mock()
            proposals = run(config, journal=journal, client=_Stub())
            stored = read(config)
            after = (tmp / "state" / "effective_limits.json").read_text()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(stored["proposals"]), 1)
        # The only things it may write are its own file and the journal.
        self.assertEqual(before, after, "a proposal must not change the limits")
        events = [call.args[0]["event"] for call in journal.append.call_args_list]
        self.assertEqual(events, ["system_proposals"])

    def test_the_same_proposal_is_not_stored_twice(self) -> None:
        from unittest import mock

        from agentic_trading.evolve_system import read, run

        answer = self._answer()

        class _Stub:
            model = "stub"

            def complete(self, system: str, user: str) -> str:
                return answer

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name))
            journal = mock.Mock()
            run(config, journal=journal, client=_Stub())
            run(config, journal=journal, client=_Stub())
            stored = read(config)
        self.assertEqual(len(stored["proposals"]), 1)

    def test_no_model_means_no_proposals_and_no_noise(self) -> None:
        from unittest import mock

        from agentic_trading.evolve_system import read, run
        from agentic_trading.llm.client import FakeLlmClient

        with tempfile.TemporaryDirectory() as name:
            config = self._config(Path(name))
            journal = mock.Mock()
            proposals = run(config, journal=journal, client=FakeLlmClient([]))
            stored = read(config)
        self.assertEqual(proposals, [])
        self.assertEqual(stored, {})
        journal.append.assert_not_called()


class ProposalQueueTests(unittest.TestCase):
    """The queue is what the operator reads, so it must render honestly."""

    def test_a_missing_file_is_an_empty_queue_not_an_error(self) -> None:
        from agentic_trading.dashboard import _proposals_view

        view = _proposals_view(None)
        self.assertEqual(view["count"], 0)
        self.assertEqual(view["proposals"], [])

    def test_entries_are_trimmed_for_the_console(self) -> None:
        from agentic_trading.dashboard import _proposals_view

        view = _proposals_view(
            {
                "model": "deepseek-flash",
                "updated_at": "2026-09-18T22:00:00+00:00",
                "proposals": [
                    {
                        "title": "Tighten costs",
                        "category": "research",
                        "confidence": 0.8,
                        "status": "proposed",
                        "evidence": "x" * 400,
                        "change": "y" * 400,
                    }
                ],
            }
        )
        self.assertEqual(view["count"], 1)
        self.assertEqual(view["model"], "deepseek-flash")
        entry = view["proposals"][0]
        self.assertLessEqual(len(entry["evidence"]), 200)
        # The change text is not needed on the console; the queue is a summary.
        self.assertNotIn("change", entry)

    def test_the_role_of_the_agent_is_stated(self) -> None:
        from agentic_trading.agents import FLEET

        evolution = next(a for a in FLEET if a.name == "evolution")
        self.assertIn("propose", evolution.role)


class CostMarginTests(unittest.TestCase):
    """Proposal #4 from the evolution agent: how much cost the edge survives.

    The gate grades with an assumed cost model. An edge whose break-even sits
    below realistic costs is not an edge, and that number was never computed —
    the agent noticed the gap from the telemetry.
    """

    def test_break_even_is_reported_against_the_assumed_cost(self) -> None:
        from decimal import Decimal as D

        from agentic_trading.backtest import CostModel
        from agentic_trading.history import Bar
        from agentic_trading.walkforward import build_evidence
        from datetime import datetime, timedelta, timezone

        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        series = {
            symbol: [
                Bar(
                    symbol=symbol,
                    start=start + timedelta(days=index),
                    open=D(str(100.0 * 1.004**index)),
                    high=D(str(100.0 * 1.004**index)),
                    low=D(str(100.0 * 1.004**index)),
                    close=D(str(100.0 * 1.004**index)),
                    volume=D("10"),
                )
                for index in range(400)
            ]
            for symbol in ("SPY", "QQQ")
        }
        report = build_evidence(
            series,
            per_order_pct=0.01,
            max_positions=2,
            folds=2,
            grid=(0.01,),
            costs=CostModel(),
        )
        costs = report["costs"]
        self.assertIn("break_even_per_side_bps", costs)
        # Gross must be >= net: costs can only ever subtract.
        self.assertGreaterEqual(costs["gross_expectancy_bps"], costs["net_expectancy_bps"])
        self.assertEqual(costs["assumed_per_side_bps"], 2.0)


class DegradedHealthTests(unittest.TestCase):
    """An agent that logged an error must not read as plain "ok".

    The data agent showed `ok` while `last_error` named an exception; only the
    model reading the telemetry noticed, which is a poor division of labour.
    """

    def _entry(self, **overrides):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "name": "data",
            "last_run_at": now,
            "last_ok_at": now,
            "consecutive_failures": 0,
            "last_error": "",
        }
        entry.update(overrides)
        return entry

    def test_a_standing_error_reads_as_degraded(self) -> None:
        from agentic_trading.agents import grade_health

        health = grade_health(
            self._entry(consecutive_failures=1, last_error="UnboundLocalError")
        )
        self.assertEqual(health.status, "degraded")
        self.assertIn("UnboundLocalError", health.detail["last_error"])

    def test_a_clean_agent_is_still_ok(self) -> None:
        from agentic_trading.agents import grade_health

        self.assertEqual(grade_health(self._entry()).status, "ok")

    def test_three_failures_outrank_degraded(self) -> None:
        from agentic_trading.agents import grade_health

        health = grade_health(
            self._entry(consecutive_failures=3, last_error="still broken")
        )
        self.assertEqual(health.status, "failing")
