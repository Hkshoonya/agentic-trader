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
