"""The scout proposes; the schema decides; the budget is a hard limit."""

from __future__ import annotations

import json
import unittest
from datetime import date

from agentic_trading.llm.client import FakeLlmClient
from agentic_trading.swarm.scout import Scout, week_key

GOOD = {"recipe": {"family": "trend", "params": {"horizons": [20, 50, 100, 200], "min_vote": 0.75,
                                                 "max_positions": 3},
                   "universe": "crypto", "per_order_pct": 0.2, "inverse_vol": True},
        "rationale": "Coins trend   harder than stocks;\nask for three of four horizons."}
MONDAY = date(2026, 10, 5)


class ScoutTests(unittest.TestCase):
    def test_a_valid_proposal_becomes_a_scout_recipe(self) -> None:
        memory: dict = {}
        client = FakeLlmClient(json.dumps(GOOD))
        recipe, note = Scout(client, 3).propose(memory, today=MONDAY, living=[], trials=12)
        self.assertEqual((recipe.family, recipe.origin), ("trend", "scout"))
        self.assertEqual(note, "Coins trend harder than stocks; ask for three of four horizons.")
        self.assertEqual((memory["week"], memory["spent"], memory["last_rationale"]), ("2026-W41", 1, note))
        system, user = client.calls[0]
        self.assertIn("max_positions", system)
        self.assertEqual(json.loads(user)["recipes_tried_so_far"], 12)

    def test_fenced_json_is_accepted(self) -> None:
        recipe, _ = Scout(FakeLlmClient("```json\n" + json.dumps(GOOD) + "\n```"), 3).propose(
            {}, today=MONDAY, living=[], trials=0)
        self.assertIsNotNone(recipe)

    def test_bad_answers_are_refused_and_still_counted(self) -> None:
        bad = {**GOOD, "recipe": {**GOOD["recipe"], "per_order_pct": 0.9}}
        for text, needle in (("not json", "refused"), (json.dumps(bad), "per_order_pct"),
                             (json.dumps({"rationale": "x"}), "refused")):
            with self.subTest(text=text[:20]):
                memory: dict = {}
                recipe, note = Scout(FakeLlmClient(text), 3).propose(memory, today=MONDAY, living=[], trials=0)
                self.assertIsNone(recipe)
                self.assertIn(needle, note)
                self.assertEqual(memory["spent"], 1)

    def test_the_weekly_budget_and_its_reset(self) -> None:
        memory = {"week": "2026-W41", "spent": 3}
        client = FakeLlmClient(json.dumps(GOOD))
        recipe, note = Scout(client, 3).propose(memory, today=MONDAY, living=[], trials=0)
        self.assertIsNone(recipe)
        self.assertIn("spent", note)
        self.assertEqual(client.calls, [])
        recipe, _ = Scout(client, 3).propose(memory, today=date(2026, 10, 12), living=[], trials=0)
        self.assertIsNotNone(recipe)
        self.assertEqual((memory["week"], memory["spent"]), ("2026-W42", 1))

    def test_an_unreachable_model_is_a_plain_note(self) -> None:
        def boom(system: str, user: str) -> str:
            raise TimeoutError("slow")
        recipe, note = Scout(FakeLlmClient(boom), 3).propose({}, today=MONDAY, living=[], trials=0)
        self.assertIsNone(recipe)
        self.assertIn("could not be reached (TimeoutError)", note)

    def test_the_client_gets_the_short_timeout(self) -> None:
        client = FakeLlmClient("{}")
        client.timeout = 30.0
        Scout(client, 3)
        self.assertEqual(client.timeout, 15.0)
        self.assertEqual(week_key(MONDAY), "2026-W41")
