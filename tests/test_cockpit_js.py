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

    def test_a_view_that_failed_on_the_server_does_not_replace_the_picture(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.accept({enabled: true}), CockpitFmt.accept({enabled: false}),"
                    " CockpitFmt.accept({enabled: false, error: 'boom'}), CockpitFmt.accept(null)]"),
            [True, True, False, False],
        )

    def test_the_health_dot_reads_kill_switch_failures_and_warnings(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.healthLevel({kill_switch: true, health: {healthy: true, failures: [], warnings: []}}),"
                    " CockpitFmt.healthLevel({health: {healthy: false, failures: [{name: 'data'}], warnings: []}}),"
                    " CockpitFmt.healthLevel({health: {healthy: true, failures: [], warnings: [{name: 'evidence'}]}}),"
                    " CockpitFmt.healthLevel({health: {healthy: true, failures: [], warnings: []}}),"
                    " CockpitFmt.healthLevel({})]"),
            ["bad", "bad", "warn", "ok", "unknown"],
        )

    def test_a_daily_sample_is_placed_at_the_end_of_its_day(self) -> None:
        # MemberBook.mark labels a sample with the UTC day that just closed.
        self.assertEqual(
            self.js("CockpitFmt.sampleTime('2026-09-29') === Date.parse('2026-09-30T00:00:00Z')"), True)

    def test_the_whole_page_script_parses(self) -> None:
        from agentic_trading.dashboard_html import HTML

        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        done = _node("new Function(require('fs').readFileSync(0, 'utf8'));", stdin=script)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_venue_health_levels_and_the_worst_of_two(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.venueLevel(null), CockpitFmt.venueLevel({enabled: false}),"
                    " CockpitFmt.venueLevel({enabled: true, stale: false, streams: [{status: 'live'}, {status: 'closed'}], venues: [{status: 'ok'}]}),"
                    " CockpitFmt.venueLevel({enabled: true, stale: false, streams: [{status: 'stale'}], venues: []}),"
                    " CockpitFmt.venueLevel({enabled: true, stale: true, streams: [], venues: []}),"
                    " CockpitFmt.venueLevel({enabled: true, stale: false, streams: [], venues: [{status: 'auth_failed'}]})]"),
            ["unknown", "unknown", "ok", "warn", "warn", "bad"],
        )
        self.assertEqual(
            self.js("[CockpitFmt.worst('ok', 'unknown'), CockpitFmt.worst('warn', 'ok'), CockpitFmt.worst('ok', 'bad')]"),
            ["ok", "warn", "bad"],
        )

    def test_switchboard_words(self) -> None:
        self.assertEqual(
            self.js("[CockpitFmt.regimeWord('choppy'), CockpitFmt.regimeWord('trending'), CockpitFmt.regimeWord('odd')]"),
            ["choppy · standing aside", "trending", "unknown"],
        )
        self.assertEqual(
            self.js("CockpitFmt.fastCompare({book: {return_pct: 0.42}, mirror: {return_pct: -0.31, unpriced: 2}})"),
            "the same trades: +0.42% at Alpaca, −0.31% at Coinbase (2 not copied)",
        )
        self.assertEqual(self.js("CockpitFmt.fastCompare({book: {return_pct: null}})"), "no trades yet")
        self.assertEqual(
            self.js("[CockpitFmt.fundedBadge(null), CockpitFmt.fundedBadge(0.42)]"),
            ["judged only — not yet funded", "judged only — not yet funded · would earn 42%"],
        )
        self.assertEqual(
            self.js("[CockpitFmt.tradeLine({standing_aside: true, trade: null}),"
                    " CockpitFmt.tradeLine({standing_aside: false, trade: null}),"
                    " CockpitFmt.tradeLine({trade: {playbook: 'breakout', entry: 105.2, stop: 99.2, pnl_pct: 0.5}})]"),
            ["standing aside", "watching for a setup", "breakout open at 105.2 · stop 99.2 · +0.50%"],
        )


    def test_swarm_words(self) -> None:
        self.assertEqual(
            self.js("CockpitFmt.swarmHead({alive: 5, trials: 31, book: {return_pct: 0.8}})"),
            "5 alive · 31 recipes tried · book +0.80%",
        )
        self.assertEqual(
            self.js("[CockpitFmt.swarmLine({forward_days: 33, state: 'contributing', excess_pct: 1.2, share: 0.5}),"
                    " CockpitFmt.swarmLine({forward_days: 3, state: 'nursery', excess_pct: -0.4, share: 0})]"),
            ["33 days · contributing 50% · +1.20% vs 60/40", "3 days · nursery · −0.40% vs 60/40"],
        )


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

    def test_the_console_refresh_feeds_the_health_dot(self) -> None:
        from agentic_trading.dashboard_js import SCRIPT

        self.assertIn("Cockpit.setHealth(", SCRIPT)

    def test_the_trial_ring_only_rebuilds_when_the_trial_changes(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        guard = COCKPIT.index("ring.dataset.key !== ringKey")
        build = COCKPIT.index("ring.innerHTML = '<path")
        self.assertLess(guard, build)  # the arcs are drawn only behind the key check

    def test_the_module_is_raw_and_escapes_what_it_writes(self) -> None:
        from agentic_trading.dashboard_cockpit_js import COCKPIT

        self.assertNotIn("\b", COCKPIT)
        self.assertNotIn("${", COCKPIT)  # concatenation only; every value passes esc()
        self.assertGreater(COCKPIT.count("esc("), 15)
