"""The cockpit script: pure formatting helpers, a script that parses, ids that exist."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

NODE = shutil.which("node") or shutil.which("nodejs")


def _node(script: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, "-e", script], input=stdin, capture_output=True,
                          text=True, timeout=30)


@unittest.skipUnless(NODE, "node is not installed")
class CockpitFormatTests(unittest.TestCase):
    def js(self, expression: str):
        from agentic_trading.dashboard_charts_js import CHARTS
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        done = _node(CHARTS + COCKPIT + "\nconsole.log(JSON.stringify(" + expression + "));\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_percent_and_money_read_like_a_ticker(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.pct(1.234), CockpitFmt.pct(-0.8), CockpitFmt.pct(0),"
                    " CockpitFmt.pct(null), CockpitFmt.money(49.451), CockpitFmt.money(NaN)]"),
            ["+1.23%", "−0.80%", "+0.00%", "—", "$49.45", "—"],
        )

    def test_countdown_scales_its_units(self) -> None:
        day, hour, minute = 86400000, 3600000, 60000
        self.assertEqual(
            self.js(f"[CockpitFmt.countdown(4*{day} + {hour} + 46*{minute}),"
                    f" CockpitFmt.countdown(2*{hour} + {minute}),"
                    f" CockpitFmt.countdown(46*{minute} + 10000),"
                    " CockpitFmt.countdown(0), CockpitFmt.countdown(-5)]"),
            ["4d 1h 46m", "2h 1m", "46m 10s", "due now", "due now"],
        )

    def test_end_labels_are_spread_apart_in_order_and_on_screen(self) -> None:
        self.assertEqual(self.js("CockpitFmt.spread([100, 102, 50], 14, 0, 200)"), [100, 114, 50])
        self.assertEqual(self.js("CockpitFmt.spread([195, 196], 14, 0, 200)"), [186, 200])
        self.assertEqual(self.js("CockpitFmt.spread([2, 3], 14, 10, 200)"), [10, 24])
        self.assertEqual(self.js("CockpitFmt.spread([], 14, 0, 200)"), [])

    def test_the_money_ring_shows_legs_until_a_strategy_is_funded(self) -> None:
        legs = {"QQQ": 0.6, "BTC-USD": 0.4}
        all_bench = json.dumps({"weights": {"momentum_rotation": 0, "benchmark": 1}, "legs": legs})
        funded = json.dumps({"weights": {"momentum_rotation": 0.4, "benchmark": 0.6}, "legs": legs})
        label = "(n) => n === 'benchmark' ? 'Buy-and-hold' : 'Momentum rotation'"
        self.assertEqual(self.js(f"CockpitFmt.moneyParts({all_bench}, {label})"),
                         [{"name": "QQQ", "value": 0.6}, {"name": "BTC", "value": 0.4}])
        self.assertEqual(self.js(f"CockpitFmt.moneyParts({funded}, {label})"),
                         [{"name": "Momentum rotation", "value": 0.4},
                          {"name": "Buy-and-hold", "value": 0.6}])
        self.assertEqual(self.js(f"CockpitFmt.moneyParts(null, {label})"), [])

    def test_the_ticker_key_changes_only_with_the_items(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.tickerKey([{at: 'a', text: 'x'}]) === CockpitFmt.tickerKey([{at: 'a', text: 'x'}]),"
                    " CockpitFmt.tickerKey([{at: 'a', text: 'x'}]) === CockpitFmt.tickerKey([{at: 'b', text: 'x'}])]"),
            [True, False],
        )

    def test_the_whole_page_script_parses(self) -> None:
        from agentic_trading.dashboard_html import HTML

        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        done = _node("new Function(require('fs').readFileSync(0, 'utf8'));", stdin=script)
        self.assertEqual(done.returncode, 0, done.stderr)


class CockpitPageWiringTests(unittest.TestCase):
    def test_every_element_the_cockpit_draws_into_exists(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT
        from agentic_trading.dashboard_html import HTML

        wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", COCKPIT))
        self.assertGreater(len(wanted), 10)
        markup = HTML.split("<script>", 1)[0]
        self.assertEqual({w for w in wanted if f'id="{w}"' not in markup}, set())

    def test_the_cockpit_boots_once_after_the_console_script(self) -> None:
        from agentic_trading.dashboard_html import HTML

        script = HTML.split("<script>", 1)[1]
        self.assertEqual(script.count("Cockpit.start();"), 1)
        self.assertLess(script.index("const esc ="), script.index("const Cockpit ="))
        self.assertLess(script.index("const Cockpit ="), script.index("Cockpit.start();"))

    def test_the_module_is_raw_and_escapes_what_it_writes(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        self.assertNotIn("\b", COCKPIT)
        self.assertNotIn("${", COCKPIT)  # concatenation only; every value passes esc()
        self.assertGreater(COCKPIT.count("esc("), 15)
