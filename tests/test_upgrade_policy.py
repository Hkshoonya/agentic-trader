"""The upgrader's walls: what it may touch, what it may write, how big a change may be."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from agentic_trading.upgrade.policy import (
    FileChange, allowed_path, check, control_closure, module_of, parse_diff, scan,
)

OK = dict(live=False, closure=set(), tests_before=10, tests_after=11, max_files=12, max_lines=400)
STRAT = "src/agentic_trading/strategies/trend_crypto.py"


def _check(changes, before=None, after=None, **over):
    return check(changes, before=before or {}, after=after or {}, **{**OK, **over})


class PathTests(unittest.TestCase):
    def test_the_allow_list_and_live_mode(self) -> None:
        for path in (STRAT, "src/agentic_trading/swarm/blend.py", "tests/test_swarm_blend.py",
                     "tests/test_strategies_x.py", "docs/notes.md"):
            self.assertTrue(allowed_path(path, live=False), path)
        for path in ("src/agentic_trading/risk.py", "src/agentic_trading/desk/desk.py", "tests/test_swarm_funding.py",
                     "tests/test_swarm_desk.py", "tests/conftest.py", "src/agentic_trading/upgrade/policy.py",
                     "deploy/x.service", "pyproject.toml", "config/agentic.toml", "README.md"):
            self.assertFalse(allowed_path(path, live=False), path)
        self.assertFalse(allowed_path(STRAT, live=True))
        self.assertTrue(allowed_path("docs/notes.md", live=True))

    def test_module_names(self) -> None:
        self.assertEqual(module_of("src/agentic_trading/swarm/blend.py"), "agentic_trading.swarm.blend")
        self.assertEqual(module_of("src/agentic_trading/swarm/__init__.py"), "agentic_trading.swarm")
        self.assertIsNone(module_of("docs/x.md"))

    def test_the_control_closure_follows_imports_even_inside_functions(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "src" / "agentic_trading"
            (root / "swarm").mkdir(parents=True)
            (root / "__init__.py").write_text("")
            (root / "swarm" / "__init__.py").write_text("")
            (root / "dashboard.py").write_text("def view():\n    from agentic_trading.helper import x\n")
            (root / "helper.py").write_text("from agentic_trading.swarm import deep\n")
            (root / "swarm" / "deep.py").write_text("X = 1\n")
            (root / "swarm" / "free.py").write_text("Y = 2\n")
            closure = control_closure(Path(name) / "src")
        self.assertIn("agentic_trading.helper", closure)
        self.assertIn("agentic_trading.swarm.deep", closure)
        self.assertNotIn("agentic_trading.swarm.free", closure)


class DiffTests(unittest.TestCase):
    def test_parse_name_status_and_lines(self) -> None:
        name_status = "M\tsrc/agentic_trading/swarm/blend.py\nA\tdocs/n.md\nR100\told.py\tnew.py\nD\tgone.py\n"
        unified = textwrap.dedent("""\
            diff --git a/src/agentic_trading/swarm/blend.py b/src/agentic_trading/swarm/blend.py
            --- a/src/agentic_trading/swarm/blend.py
            +++ b/src/agentic_trading/swarm/blend.py
            @@ -1 +1 @@
            -X = 1
            +X = 2
            diff --git a/docs/n.md b/docs/n.md
            +++ b/docs/n.md
            +hello
            """)
        changes = {c.path: c for c in parse_diff(name_status, unified)}
        self.assertEqual(changes["src/agentic_trading/swarm/blend.py"].added, ("X = 2",))
        self.assertEqual(changes["src/agentic_trading/swarm/blend.py"].removed, ("X = 1",))
        self.assertEqual((changes["new.py"].status, changes["new.py"].old_path), ("R", "old.py"))
        self.assertEqual(changes["gone.py"].status, "D")


class CheckTests(unittest.TestCase):
    def test_a_small_allowed_change_passes(self) -> None:
        change = FileChange(STRAT, "M", added=("x = 2",), removed=("x = 1",))
        self.assertEqual(_check([change], {STRAT: "x = 1\n"}, {STRAT: "x = 2\n"}), [])

    def test_each_wall(self) -> None:
        cases = {
            "outside": [FileChange("src/agentic_trading/risk.py", "M", added=("a",))],
            "deletes": [FileChange(STRAT, "D")],
            "renames": [FileChange("src/agentic_trading/risk2.py", "R", old_path=STRAT)],
        }
        for needle, changes in cases.items():
            with self.subTest(needle=needle):
                self.assertTrue(any(needle in r for r in _check(changes)), _check(changes))
        imported = [FileChange("src/agentic_trading/swarm/deep.py", "M", added=("a",))]
        self.assertTrue(any("control module" in r for r in
                            _check(imported, closure={"agentic_trading.swarm.deep"})))
        self.assertTrue(any("test count" in r for r in _check([], tests_after=9)))
        big = [FileChange(f"docs/{i}.md", "A", added=("x",)) for i in range(13)]
        self.assertTrue(any("files" in r for r in _check(big)))
        long = [FileChange("docs/a.md", "A", added=tuple("x" for _ in range(401)))]
        self.assertTrue(any("lines" in r for r in _check(long)))

    def test_forbidden_code_is_refused_only_when_new(self) -> None:
        before = {STRAT: "import os\n"}
        for code, needle in (("import subprocess\n", "subprocess"), ("import socket\n", "socket"),
                             ("x = os.environ['K']\n", "os.environ"), ("eval('1')\n", "eval"),
                             ("open('/etc/passwd')\n", "open"), ("import httpx\n", "httpx")):
            with self.subTest(code=code):
                after = {STRAT: "import os\n" + code}
                reasons = _check([FileChange(STRAT, "M", added=(code.strip(),))], before, after)
                self.assertTrue(any(needle in r for r in reasons), reasons)
        same = {STRAT: "import subprocess\n"}
        self.assertEqual(_check([FileChange(STRAT, "M", added=("y = 1",))], same,
                                {STRAT: "import subprocess\ny = 1\n"}), [])

    def test_a_deleted_test_and_a_secret_are_refused(self) -> None:
        path = "tests/test_swarm_blend.py"
        before = {path: "def test_a():\n    pass\ndef test_b():\n    pass\n"}
        after = {path: "def test_a():\n    pass\n"}
        self.assertTrue(any("deletes test" in r for r in _check([FileChange(path, "M", removed=("x",))],
                                                                before, after)))
        leak = [FileChange("docs/a.md", "A", added=('api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"',))]
        self.assertTrue(any("secret" in r for r in _check(leak)))

    def test_readme_may_change_only_its_test_counts(self) -> None:
        fine = [FileChange("README.md", "M", added=("[![tests](https://img.shields.io/badge/tests-1300%20passing-35d07f)](#verify)",),
                           removed=("[![tests](https://img.shields.io/badge/tests-1279%20passing-35d07f)](#verify)",))]
        self.assertEqual(_check(fine), [])
        prose = [FileChange("README.md", "M", added=("The system is now allowed to trade live.",))]
        self.assertTrue(any("README.md" in r for r in _check(prose)))

    def test_forbidden_constructs_are_counted(self) -> None:
        self.assertEqual(scan("import subprocess\nimport subprocess as s\n")["subprocess"], 2)
        self.assertEqual(sum(scan("x = 1\n").values()), 0)
