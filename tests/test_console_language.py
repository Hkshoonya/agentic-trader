"""The console speaks to the person deciding whether to arm the agent.

The journal keeps codes, bps and p-values because they can be counted and
replayed. The screen keeps sentences. These tests pin the screen: no statistical
jargon in the labels, and no layout that lets a value run out of its table.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agentic_trading.dashboard_html import HTML
from agentic_trading.dashboard_js import SCRIPT


class ConsoleLanguageTests(unittest.TestCase):
    def test_the_column_headers_are_sentences(self) -> None:
        # The script is embedded in the page; its regexes still name the old
        # journal vocabulary, so only the markup before it is the screen.
        markup = HTML.split("<script>", 1)[0].lower()
        for wanted in (
            "what the checks said",
            "how the price is moving",
            "how wildly it moves",
            "held back by",
            "cost to trade",
        ):
            self.assertIn(wanted, markup)
        for retired in ("trend vote", "vol (annualised)", "blocked by"):
            self.assertNotIn(retired, markup)

    def test_no_statistical_jargon_reaches_the_screen(self) -> None:
        # Phrases that would put a statistic on the screen rather than a
        # sentence. The regexes that translate the journal's own vocabulary are
        # exempt: they quote codes, they do not display them.
        for banned in (
            "+ ' bps'",
            "' bps/side",
            "'p-value'",
            "'bootstrap p'",
            "+ ' bps/side'",
        ):
            self.assertNotIn(banned, SCRIPT)
        # The two numbers an operator acts on are shown in dollars per $100.
        self.assertIn("perHundred(ev.oos_expectancy_bps)", SCRIPT)
        self.assertIn("perHundred(s.expectancy_bps)", SCRIPT)
        self.assertIn("perHundred(r.spread_bps)", SCRIPT)

    def test_the_plain_language_helpers_exist(self) -> None:
        for helper in ("const esc =", "const perHundred =", "const plainLuck =",
                       "const plainScore ="):
            self.assertIn(helper, SCRIPT)
        # Every dynamic value written into innerHTML goes through esc().
        self.assertIn("esc(", SCRIPT)

    def test_a_long_value_cannot_run_out_of_its_card(self) -> None:
        # Values wrap rather than widen their box, and the tables keep a scroll
        # container of their own.
        self.assertIn("overflow-wrap:anywhere", HTML)
        self.assertIn(".tablewrap{overflow:auto", HTML)


class LauncherLanguageTests(unittest.TestCase):
    """The Windows window is the client's whole view; it reads plainly too."""

    def setUp(self) -> None:
        source = Path(__file__).resolve().parent.parent / "windows" / "launcher.py"
        self.source = source.read_text(encoding="utf-8")

    def test_the_status_lines_do_not_print_raw_rates(self) -> None:
        render = self.source.split("def _render", 1)[1]
        code = "\n".join(
            line for line in render.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("%/order", code)
        self.assertNotIn("%/day", code)
        self.assertNotIn(" bps", code)
        self.assertIn("most it may spend", render)
        self.assertIn("per $100 traded", render)

    def test_the_autonomy_switch_says_what_it_now_allows(self) -> None:
        self.assertIn("promote itself, pick symbols", self.source)


if __name__ == "__main__":
    unittest.main()
