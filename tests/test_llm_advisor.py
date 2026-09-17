"""LLM advisor: may veto an entry, may not extend risk, never blocks the loop."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from agentic_trading.llm.advisor import (
    AdvisorDecision,
    LlmAdvisor,
    advisor_enabled,
    build_advisor,
    parse_decision,
)
from agentic_trading.llm.client import FakeLlmClient, load_dotenv
from agentic_trading.llm.client import running_under_tests
from agentic_trading.llm.market import features_from_closes


class _CountingClient:
    model = "counting"

    def __init__(self, reply: str = '{"action":"veto","confidence":0.8,"reason":"trap"}') -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


class BurstControlTests(unittest.TestCase):
    """A signal burst must not queue a model call per symbol inside the loop."""

    def _advisor(self, client, clock=None, **kwargs) -> LlmAdvisor:
        return LlmAdvisor(client, model="counting", clock=clock, **kwargs)

    def test_the_same_situation_reuses_one_verdict(self) -> None:
        client = _CountingClient()
        advisor = self._advisor(client)
        features = features_from_closes("BTC-USD", [100.0 + i for i in range(40)])
        for _ in range(5):
            advisor.review_entry(
                symbol="BTC-USD",
                side="buy",
                ref_price="139",
                quantity="0.01",
                reason="trend",
                context={"market": features},
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual(advisor.cache_hits, 4)
        self.assertTrue(advisor.last_reused)

    def test_a_different_situation_is_asked_again(self) -> None:
        client = _CountingClient()
        advisor = self._advisor(client)
        rising = features_from_closes("BTC-USD", [100.0 + i for i in range(40)])
        falling = features_from_closes("BTC-USD", [140.0 - i for i in range(40)])
        advisor.review_entry(
            symbol="BTC-USD", side="buy", ref_price="139", quantity="0.01",
            reason="trend", context={"market": rising},
        )
        advisor.review_entry(
            symbol="BTC-USD", side="buy", ref_price="101", quantity="0.01",
            reason="trend", context={"market": falling},
        )
        self.assertEqual(client.calls, 2)

    def test_the_other_side_is_a_different_question(self) -> None:
        client = _CountingClient()
        advisor = self._advisor(client)
        features = features_from_closes("BTC-USD", [100.0 + i for i in range(40)])
        for side in ("buy", "sell"):
            advisor.review_entry(
                symbol="BTC-USD", side=side, ref_price="139", quantity="0.01",
                reason="exit", context={"market": features},
            )
        self.assertEqual(client.calls, 2)

    def test_a_verdict_expires(self) -> None:
        now = [0.0]
        client = _CountingClient()
        advisor = self._advisor(client, clock=lambda: now[0], cache_seconds=60.0)
        features = features_from_closes("BTC-USD", [100.0 + i for i in range(40)])
        advisor.review_entry(
            symbol="BTC-USD", side="buy", ref_price="139", quantity="0.01",
            reason="trend", context={"market": features},
        )
        now[0] = 61.0
        advisor.review_entry(
            symbol="BTC-USD", side="buy", ref_price="139", quantity="0.01",
            reason="trend", context={"market": features},
        )
        self.assertEqual(client.calls, 2)

    def test_the_per_minute_budget_degrades_to_no_opinion(self) -> None:
        now = [0.0]
        client = _CountingClient()
        advisor = self._advisor(
            client, clock=lambda: now[0], max_calls_per_minute=2, cache_seconds=0.0
        )
        features = [
            features_from_closes("BTC-USD", [100.0 + i + j * 40 for i in range(40)])
            for j in range(5)
        ]
        results = [
            advisor.review_entry(
                symbol="BTC-USD", side="buy", ref_price="139", quantity="0.01",
                reason="trend", context={"market": feature},
            )
            for feature in features
        ]
        self.assertEqual(client.calls, 2)
        self.assertEqual(advisor.budget_skips, 3)
        self.assertIn("budget", advisor.last_error)
        self.assertIsNone(results[-1])

    def test_the_budget_refills_after_a_minute(self) -> None:
        now = [0.0]
        client = _CountingClient()
        advisor = self._advisor(
            client, clock=lambda: now[0], max_calls_per_minute=1, cache_seconds=0.0
        )
        first = features_from_closes("BTC-USD", [100.0 + i for i in range(40)])
        second = features_from_closes("BTC-USD", [140.0 - i for i in range(40)])
        advisor.review_entry(
            symbol="BTC-USD", side="buy", ref_price="139", quantity="0.01",
            reason="trend", context={"market": first},
        )
        self.assertIsNone(
            advisor.review_entry(
                symbol="BTC-USD", side="buy", ref_price="101", quantity="0.01",
                reason="trend", context={"market": second},
            )
        )
        now[0] = 61.0
        self.assertIsNotNone(
            advisor.review_entry(
                symbol="BTC-USD", side="buy", ref_price="101", quantity="0.01",
                reason="trend", context={"market": second},
            )
        )


class ParseTests(unittest.TestCase):
    def test_parses_veto_and_allow(self) -> None:
        veto = parse_decision('{"action":"veto","confidence":0.8,"reason":"blow-off"}')
        self.assertTrue(veto.vetoes)
        self.assertEqual(veto.confidence, 0.8)
        allow = parse_decision('{"action":"allow","confidence":2.0,"hold":true}')
        self.assertFalse(allow.vetoes)
        self.assertEqual(allow.confidence, 1.0)  # clamped
        self.assertTrue(allow.hold)

    def test_tolerates_markdown_fences(self) -> None:
        decision = parse_decision('```json\n{"action":"allow","confidence":0.5}\n```')
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "allow")

    def test_rejects_malformed_output(self) -> None:
        for raw in ("", "not json", '{"action":"maybe"}', "[]", '{"confidence":1}'):
            self.assertIsNone(parse_decision(raw), raw)


class AdvisorTests(unittest.TestCase):
    def test_veto_is_returned_and_recorded(self) -> None:
        client = FakeLlmClient('{"action":"veto","confidence":0.9,"reason":"trap"}')
        advisor = LlmAdvisor(client)
        decision = advisor.review_entry(
            symbol="SPY", side="buy", ref_price="100", quantity="1", reason="test"
        )
        self.assertTrue(decision.vetoes)
        self.assertEqual(len(advisor.decisions), 1)
        self.assertEqual(advisor.errors, 0)

    def test_client_failure_is_not_fatal_and_counts_as_error(self) -> None:
        class Broken:
            def complete(self, system: str, user: str) -> str:
                raise RuntimeError("model down")

        advisor = LlmAdvisor(Broken())
        self.assertIsNone(
            advisor.review_entry(
                symbol="SPY", side="buy", ref_price="1", quantity="1", reason="t"
            )
        )
        self.assertEqual(advisor.errors, 1)
        self.assertEqual(advisor.decisions, [])

    def test_bad_json_is_no_opinion_not_a_veto(self) -> None:
        advisor = LlmAdvisor(FakeLlmClient("garbage"))
        self.assertIsNone(
            advisor.review_entry(
                symbol="SPY", side="buy", ref_price="1", quantity="1", reason="t"
            )
        )
        self.assertEqual(advisor.errors, 1)

    def test_hold_opinion_is_recorded(self) -> None:
        advisor = LlmAdvisor(
            FakeLlmClient('{"action":"allow","confidence":0.7,"hold":true,"reason":"run"}')
        )
        decision = advisor.review_entry(
            symbol="SPY", side="sell", ref_price="1", quantity="1", reason="timeout"
        )
        self.assertTrue(decision.hold)
        self.assertFalse(decision.vetoes)


class EnablementTests(unittest.TestCase):
    def test_disabled_without_the_env_flag(self) -> None:
        with mock.patch.dict(os.environ, {"AGENTIC_LLM_ADVISOR": "0"}, clear=False):
            self.assertFalse(advisor_enabled())
            self.assertIsNone(build_advisor())

    def test_enabled_requires_a_real_client(self) -> None:
        env = {"AGENTIC_LLM_ADVISOR": "1"}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "agentic_trading.llm.advisor.build_llm_client",
            return_value=FakeLlmClient(),
        ):
            # Fake client means no real model configured -> no advisor.
            self.assertIsNone(build_advisor())


class DotenvTests(unittest.TestCase):
    def test_the_suite_never_loads_a_developers_dotenv(self) -> None:
        # The runner itself must be detected, whatever command is used: the
        # README documents `python -m unittest`, which exports no env marker.
        self.assertTrue(running_under_tests())
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / ".env"
            path.write_text("AGENTIC_DOTENV_LEAK=1\n", encoding="utf-8")
            os.environ.pop("AGENTIC_DOTENV_LEAK", None)
            self.assertEqual(load_dotenv(path), 0)
            self.assertNotIn("AGENTIC_DOTENV_LEAK", os.environ)

    def test_loads_export_style_lines_without_overriding_real_env(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / ".env"
            path.write_text(
                "export AGENTIC_TEST_ONE=from-file\n"
                "# comment\n"
                "AGENTIC_TEST_THREE=val  # trailing note\n"
                "AGENTIC_TEST_TWO='quoted value'\n"
                "AGENTIC_TEST_ONE=ignored-because-set\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"AGENTIC_TEST_ONE": "from-env"}, clear=False
            ):
                loaded = load_dotenv(path, force=True)
                self.assertGreaterEqual(loaded, 1)
                self.assertEqual(os.environ["AGENTIC_TEST_ONE"], "from-env")
                self.assertEqual(os.environ["AGENTIC_TEST_TWO"], "quoted value")
                self.assertEqual(os.environ["AGENTIC_TEST_THREE"], "val")
            os.environ.pop("AGENTIC_TEST_TWO", None)
            os.environ.pop("AGENTIC_TEST_THREE", None)


if __name__ == "__main__":
    unittest.main()
