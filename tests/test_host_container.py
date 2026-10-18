"""The self-upgrader's container on a Windows host: what it mounts, what it passes, when it refuses."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_trading.host import container
from agentic_trading.host.units import TIMERS
from agentic_trading.upgrade.run import RunResult

ROOT = Path(__file__).resolve().parent.parent


class _Runner:
    def __init__(self, **codes: int) -> None:
        self.codes, self.calls = codes, []

    def __call__(self, argv, cwd=None, timeout=600.0) -> RunResult:
        self.calls.append(list(argv))
        line = " ".join(argv)
        for key, code in self.codes.items():
            if key.replace("_", " ") in line:
                return RunResult(code, "true" if key == "autocrlf" else "")
        return RunResult(0, "false" if "core.autocrlf" in line else "")


class ImageTests(unittest.TestCase):
    def test_the_image_holds_every_dependency_the_suite_needs(self) -> None:
        wanted = container.requirements(ROOT / "pyproject.toml")
        self.assertTrue(any(r.startswith("httpx") for r in wanted))
        self.assertTrue(any(r.startswith("pytest") for r in wanted))
        self.assertTrue(any(r.startswith("alpaca-py") for r in wanted))

    def test_the_tag_follows_the_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            repo = Path(name)
            (repo / "deploy" / "upgrader").mkdir(parents=True)
            shutil.copy2(ROOT / container.DOCKERFILE, repo / container.DOCKERFILE)
            shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
            before = container.image_tag(repo)
            text = (repo / "pyproject.toml").read_text(encoding="utf-8")
            (repo / "pyproject.toml").write_text(text.replace('"httpx>=0.27"', '"httpx>=0.28"'), encoding="utf-8")
            self.assertNotEqual(container.image_tag(repo), before)


class RunArgvTests(unittest.TestCase):
    def test_it_mounts_only_the_checkout_and_the_logins(self) -> None:
        argv = container.run_argv(Path("C:/live"), "img:1", ["upgrade", "run", "--config", "c"], environ={})
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertEqual(mounts, [f"{Path('C:/live')}:/work/live", "agentic-upgrader-home:/home/node"])
        self.assertNotIn("--privileged", argv)
        self.assertFalse(any(a.startswith("--network") or a == "--net" for a in argv))
        self.assertNotIn("--cap-add", argv)
        self.assertIn("seccomp=unconfined", argv)  # bwrap's user namespaces; the probe proves the walls
        self.assertIn("AGENTIC_SERVICE_HOST=windows", argv)
        self.assertEqual(argv[argv.index("img:1") + 1:], ["python", "-m", "agentic_trading", "upgrade", "run",
                                                          "--config", "c"])

    def test_the_writer_key_passes_by_name_never_by_value(self) -> None:
        argv = container.run_argv(Path("/r"), "img", ["x"], environ={"DEEPSEEK_API_KEY": "sk-secret-value"})
        self.assertIn("DEEPSEEK_API_KEY", argv)
        self.assertFalse(any("sk-secret-value" in a for a in argv))
        self.assertNotIn("DEEPSEEK_API_KEY", container.run_argv(Path("/r"), "img", ["x"], environ={}))


class ProblemTests(unittest.TestCase):
    def test_each_reason_to_refuse(self) -> None:
        tag = container.image_tag(ROOT)
        self.assertEqual(container.problems(ROOT, tag, _Runner(docker_info=1)),
                         ["Docker is not running (start Docker Desktop)"])
        self.assertIn("not built", container.problems(ROOT, None, _Runner())[0])
        self.assertIn("is gone", container.problems(ROOT, tag, _Runner(image_inspect=1))[0])
        self.assertIn("changed since", container.problems(ROOT, "agentic-upgrader:old", _Runner())[0])
        self.assertIn("line endings", " ".join(container.problems(ROOT, tag, _Runner(autocrlf=0))))
        self.assertIn("bwrap cannot start", container.problems(ROOT, tag, _Runner(bwrap=1))[0])
        self.assertEqual(container.problems(ROOT, tag, _Runner()), [])


class HostScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _host(self):
        from agentic_trading.host.supervisor import Supervisor

        spawned = []

        class _P:
            pid = 1

            def poll(self):
                return None

            def stop(self, timeout):
                return 0

        sup = Supervisor(self.root, self.root / "state", services=(), timers=TIMERS, sandbox=False,
                         windows_container=True, alert=lambda *a: None,
                         spawn=lambda argv, **k: spawned.append(argv) or _P())
        return sup, spawned

    def test_the_upgrade_stays_off_until_the_image_is_built(self) -> None:
        sup, _ = self._host()
        sup.start()
        self.assertFalse(sup.handle("start", ["agentic-trading-upgrade"])[0])

    def test_once_built_the_upgrade_runs_in_the_container(self) -> None:
        marker = self.root / "state" / "host" / "upgrader.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"image": "agentic-upgrader:abc"}))
        sup, spawned = self._host()
        sup.start()
        self.assertTrue(sup.handle("start", ["agentic-trading-upgrade"])[0])
        self.assertEqual(spawned[-1][3:], ["host", "container", "exec", "--config", "config/agentic.toml", "--",
                                          "upgrade", "run", "--config", "config/agentic.toml"])
        sup.handle("start", ["agentic-trading-upgrade-watch"])  # the canary stays native
        self.assertEqual(spawned[-1][3:5], ["upgrade", "watch"])


class InsideTheContainerTests(unittest.TestCase):
    def test_the_upgrader_inside_asks_the_windows_host(self) -> None:
        from agentic_trading.upgrade import services

        with patch.dict("os.environ", {"AGENTIC_SERVICE_HOST": "windows"}), patch("os.name", "posix"):
            self.assertTrue(services.windows())
            self.assertEqual(services.restart(["a"])[3:5], ["host", "restart"])

    def test_the_cycle_uses_the_images_venv(self) -> None:
        from agentic_trading.upgrade.cli import _venv

        with patch.dict("os.environ", {"AGENTIC_UPGRADE_VENV": "/venv"}):
            self.assertEqual(_venv(Path("/work/live")), Path("/venv"))
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("AGENTIC_UPGRADE_VENV", None)
            self.assertEqual(_venv(Path("/work/live")), Path("/work/live") / ".venv")


if __name__ == "__main__":
    unittest.main()
