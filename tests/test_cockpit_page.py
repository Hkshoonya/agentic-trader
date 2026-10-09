"""The cockpit page: four tabs, nothing loaded from outside, no card lost."""

from __future__ import annotations

import re
import unittest

from agentic_trading.dashboard_html import HTML
from agentic_trading.dashboard_js import SCRIPT

COCKPIT_IDS = (
    "say-now", "say-money", "say-just", "race", "race-trial", "race-tip",
    "t-account", "t-spark", "t-trial", "t-trial-ring", "t-money",
    "t-money-legend", "t-next", "ticker", "tape",
)


def _ids(markup: str) -> set[str]:
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', markup))


def _markup() -> str:
    return HTML.split("<script>", 1)[0]


def _section(name: str) -> str:
    return HTML.split(f'id="tab-{name}"', 1)[1].split("</section>", 1)[0]


class CockpitPageTests(unittest.TestCase):
    def test_the_page_loads_nothing_from_outside(self) -> None:
        self.assertNotRegex(HTML, r"<script[^>]+src=")
        self.assertNotRegex(HTML, r'(src|href)="(https?:)?//')
        self.assertNotRegex(HTML, r"url\([\"']?(https?:)?//")
        self.assertNotIn("@import", HTML)

    def test_four_tabs_open_on_the_overview(self) -> None:
        for tab in ("overview", "strategies", "orders", "health"):
            self.assertIn(f'id="tab-{tab}"', HTML)
            self.assertIn(f'data-tab="{tab}"', HTML)
        self.assertIn('<section class="tab on" id="tab-overview">', HTML)

    def test_every_element_the_existing_script_needs_is_still_on_the_page(self) -> None:
        # A whole literal id only: getElementById('tab-' + name) builds an id at
        # run time and is not a fixed element the markup must carry.
        wanted = set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", SCRIPT))
        wanted |= set(re.findall(r"setBadge\('([A-Za-z0-9_-]+)',", SCRIPT))
        wanted |= set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)", SCRIPT))
        self.assertGreater(len(wanted), 30)
        self.assertEqual(wanted - _ids(SCRIPT) - _ids(_markup()), set())

    def test_the_overview_holds_the_cockpit(self) -> None:
        self.assertIn(".cockpit{display:grid;grid-template-columns:1fr 2.2fr 1fr", HTML)
        overview = _section("overview")
        for element in COCKPIT_IDS:
            self.assertIn(f'id="{element}"', overview, element)

    def test_cards_sit_in_their_tabs(self) -> None:
        placed = {
            "strategies": ("members", "candidates", "universe", "evidence", "gate",
                           "frontier", "evolution", "proposals"),
            "orders": ("orders", "chart", "stream", "notional", "trades", "equity"),
            "health": ("agents", "health", "alerts", "account", "streak", "regimes"),
        }
        for tab, elements in placed.items():
            for element in elements:
                self.assertIn(f'id="{element}"', _section(tab), f"{element} in {tab}")

    def test_the_header_shows_armed_state_and_the_arm_card_keeps_its_checklist(self) -> None:
        # The arm control renders its whole pre-flight checklist; in the top bar
        # it pushed the cockpit off one screen. The header keeps the ARMED badge,
        # and the button sits on the Health tab beside the checks behind it.
        header = HTML.split("<header>", 1)[1].split("</header>", 1)[0]
        for element in ("armed", "netdot", "tabs"):
            self.assertIn(f'id="{element}"', header)
        self.assertNotIn('id="arm"', header)
        health = _section("health")
        self.assertIn("<h2>Order submission</h2>", health)
        for element in ("arm", "arm-status"):
            self.assertIn(f'id="{element}"', health)

    def test_timers_skip_work_while_the_page_is_hidden(self) -> None:
        self.assertIn("if (!document.hidden) refresh();", SCRIPT)
        self.assertIn("if (!document.hidden) refreshCandidates();", SCRIPT)
        self.assertNotIn("setInterval(refresh, 2000)", SCRIPT)

    def test_the_chart_helpers_ride_in_the_page(self) -> None:
        self.assertIn("const Charts =", HTML)
        self.assertNotIn("__COCKPIT_CSS__", HTML)
        self.assertNotIn("__SCRIPT__", HTML)

    def test_the_venues_card_is_on_the_health_tab(self) -> None:
        self.assertIn('id="venues"', _section("health"))

    def test_the_switchboard_card_is_on_the_strategies_tab(self) -> None:
        self.assertIn('id="fast"', _section("strategies"))

    def test_the_swarm_card_is_on_the_strategies_tab(self) -> None:
        self.assertIn('id="swarm"', _section("strategies"))

    def test_the_evolution_card_is_on_the_strategies_tab(self) -> None:
        self.assertIn('id="upgrade"', _section("strategies"))


class CockpitSizingTests(unittest.TestCase):
    """The cockpit adapts to the screen instead of assuming 1440x900.

    It first shipped with fixed pixel type and ``height: calc(100vh - 160px)``:
    tiny text and an empty band on a 2560 monitor, and a 150px race strip on a
    laptop narrower than 1100px.
    """

    def test_the_overview_height_comes_from_the_measured_header(self) -> None:
        self.assertNotIn("100vh - 160px", HTML)
        self.assertIn("var(--head", HTML)
        self.assertIn("ResizeObserver", HTML)

    def test_type_scales_with_the_viewport(self) -> None:
        for rule in (".say{font-size:clamp(", ".num{font-size:clamp(", ".item{", "clamp("):
            self.assertIn(rule, HTML)

    def test_a_stacked_layout_gives_the_race_a_real_height(self) -> None:
        narrow = HTML.split("@media(max-width:1100px){", 1)[1].split("}}", 1)[0]
        self.assertIn("#race{", narrow)
        self.assertIn("height:clamp(", narrow)
