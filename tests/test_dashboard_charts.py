"""The cockpit's chart helpers are pure: data in, numbers and SVG paths out."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipUnless(NODE, "node is not installed")
class ChartHelperTests(unittest.TestCase):
    def js(self, expression: str):
        from agentic_trading.dashboard_charts_js import CHARTS

        script = CHARTS + "\nconsole.log(JSON.stringify(" + expression + "));\n"
        done = subprocess.run(
            [NODE, "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_scale_maps_the_ends_and_a_flat_domain_to_the_middle(self) -> None:
        self.assertEqual(
            self.js("[Charts.scale(0, 10, 0, 100)(0), Charts.scale(0, 10, 0, 100)(10),"
                    " Charts.scale(0, 10, 100, 0)(2.5), Charts.scale(3, 3, 0, 100)(3)]"),
            [0, 100, 75, 50],
        )

    def test_extent_always_includes_zero_and_pads(self) -> None:
        lo, hi = self.js("Charts.extent([1, 2, 3])")
        self.assertAlmostEqual(lo, -0.3)
        self.assertAlmostEqual(hi, 3.3)
        self.assertEqual(self.js("Charts.extent([])"), [-1.2, 1.2])
        lo, hi = self.js("Charts.extent([NaN, null, -2, Infinity])")
        self.assertAlmostEqual(lo, -2.2)
        self.assertAlmostEqual(hi, 0.2)

    def test_line_path_handles_none_one_many_and_gaps(self) -> None:
        ident = "(v) => v"
        self.assertEqual(self.js(f"Charts.linePath([], {ident}, {ident})"), "")
        self.assertEqual(self.js(f"Charts.linePath([[1, 2]], {ident}, {ident})"), "M 1.0 2.0")
        self.assertEqual(
            self.js(f"Charts.linePath([[0, 0], [1, 1], [2, NaN], [3, 3]], {ident}, {ident})"),
            "M 0.0 0.0 L 1.0 1.0 M 3.0 3.0",
        )

    def test_arcs_cover_the_circle_and_skip_empty_parts(self) -> None:
        arcs = self.js(
            "Charts.arcs([{name: 'QQQ', value: 0.6}, {name: 'none', value: 0},"
            " {name: 'bad', value: NaN}, {name: 'BTC', value: 0.4}])"
        )
        self.assertEqual([a["name"] for a in arcs], ["QQQ", "BTC"])
        self.assertAlmostEqual(arcs[0]["start"], 0)
        self.assertAlmostEqual(arcs[0]["end"], 216)
        self.assertAlmostEqual(arcs[-1]["end"], 360)
        self.assertEqual(self.js("Charts.arcs([{name: 'x', value: 0}])"), [])

    def test_a_full_circle_arc_does_not_collapse(self) -> None:
        path = self.js("Charts.arcPath(50, 50, 30, 40, 0, 360)")
        self.assertTrue(path.startswith("M ") and path.endswith(" Z"))
        self.assertEqual(path.count(" A "), 2)
        start = path.split(" A ")[0]
        end_of_outer = path.split(" A ")[1].split(" L ")[0].split()[-2:]
        self.assertNotEqual(start.split()[1:], end_of_outer)

    def test_tween_eases_and_clamps(self) -> None:
        self.assertEqual(
            self.js("[Charts.tween(0, 10, 0), Charts.tween(0, 10, 1), Charts.tween(0, 10, 7),"
                    " Charts.tween(0, 10, -1)]"),
            [0, 10, 10, 0],
        )
        self.assertGreater(self.js("Charts.tween(0, 10, 0.5)"), 5)  # ease-out runs ahead

    def test_day_index_counts_days(self) -> None:
        self.assertEqual(
            self.js("Charts.dayIndex('2026-09-27', '2026-09-25')"), 2)
        self.assertAlmostEqual(
            self.js("Charts.dayIndex('2026-09-25T12:00:00+00:00', '2026-09-25')"), 0.5)


class ChartModuleTests(unittest.TestCase):
    def test_the_module_is_a_raw_string_without_control_bytes(self) -> None:
        from agentic_trading.dashboard_charts_js import CHARTS

        self.assertIn("const Charts =", CHARTS)
        self.assertNotIn("\b", CHARTS)
        self.assertNotIn("document.", CHARTS)  # pure: no DOM
