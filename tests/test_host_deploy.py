"""Moving to another host: install.sh renders every unit for its own paths; migrate.sh carries the
record but leaves the arm latches behind and stops the old host first."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
ORIGIN = ("/home/doczeus", "Agnetic TraDING")
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUBLOG"
case "$*" in *show-user*) echo yes;; *is-active*) echo inactive; exit 3;; esac
exit 0
"""


@unittest.skipUnless(
    os.name == "posix" and shutil.which("sh") and shutil.which("tar"),
    "POSIX hosts only",
)
class HostDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        for name in ("systemctl", "loginctl"):
            (stubs / name).write_text(STUB)
            (stubs / name).chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.env = dict(
            os.environ, PATH=f"{stubs}:{os.environ['PATH']}", STUBLOG=str(self.log)
        )
        # The upgrader's sandbox clears the environment: the scripts must not need $USER.
        self.env.pop("USER", None)

    def _host(self, name: str) -> tuple[Path, Path]:
        repo, home = self.tmp / name / "my repo & co", self.tmp / name / "home"
        shutil.copytree(DEPLOY, repo / "deploy")
        (repo / ".venv" / "bin").mkdir(parents=True)
        (repo / ".venv" / "bin" / "agentic-trading").touch(mode=0o755)
        (repo / "config").mkdir()
        home.mkdir()
        return repo, home

    def _sh(self, home: Path, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", *argv],
            env=dict(self.env, HOME=str(home)),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_install_renders_every_unit_and_launcher_for_this_checkout(self) -> None:
        repo, home = self._host("new")
        (repo / "config" / "agentic.toml").write_text("")
        self._sh(home, str(repo / "deploy" / "install.sh"), "--enable")
        units, bins = home / ".config" / "systemd" / "user", home / ".local" / "bin"
        shipped = sorted(p.name for p in DEPLOY.glob("agentic-trading*"))
        installed = sorted(p.name for p in [*units.iterdir(), *bins.iterdir()])
        self.assertEqual(shipped, installed)
        self.assertIn("agentic-trading.service", installed)
        for path in [*units.iterdir(), *bins.iterdir()]:
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertFalse(any(o in text for o in ORIGIN))
        daemon = (units / "agentic-trading.service").read_text()
        self.assertIn(f"ExecStart={bins}/agentic-trading-daemon", daemon)
        self.assertIn(f"append:{repo}/data/state/daemon.log", daemon)
        self.assertIn(f'cd "{repo}"', (bins / "agentic-trading-daemon").read_text())
        self.assertTrue(os.access(bins / "agentic-trading-upgrade", os.X_OK))
        self.assertIn("enable --now agentic-trading ", self.log.read_text())

    def test_install_refuses_without_a_venv_or_config(self) -> None:
        repo, home = self._host("bare")
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self._sh(home, str(repo / "deploy" / "install.sh"))
        self.assertIn("config/agentic.toml", caught.exception.stderr)
        self.assertFalse((home / ".local").exists())

    def test_migrate_stops_the_old_host_and_leaves_the_arm_latches_behind(self) -> None:
        old, old_home = self._host("old")
        (old / "config" / "agentic.toml").write_text("mode = 'shadow'\n")
        state = old / "data" / "state"
        state.mkdir(parents=True)
        for name in ("arm.json", "venues_arm.json", "daemon.pid", "trial.json"):
            (state / name).write_text("{}")
        (old_home / ".config" / "agentic-trading").mkdir(parents=True)
        (old_home / ".config" / "agentic-trading" / "tokens.json").write_text("{}")
        bundle = self.tmp / "move.tgz"
        self._sh(old_home, str(old / "deploy" / "migrate.sh"), "export", str(bundle))
        self.assertIn("disable --now agentic-trading ", self.log.read_text())
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        with tarfile.open(bundle) as tar:
            names = set(tar.getnames())
        self.assertIn("data/state/trial.json", names)
        self.assertTrue(
            {
                "data/state/arm.json",
                "data/state/venues_arm.json",
                "data/state/daemon.pid",
            }.isdisjoint(names)
        )

        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self._sh(old_home, str(old / "deploy" / "migrate.sh"), "export", str(bundle))
        self.assertIn("exists", caught.exception.stderr)

        new, new_home = self._host("new")
        self._sh(new_home, str(new / "deploy" / "migrate.sh"), "import", str(bundle))
        self.assertTrue((new / "data" / "state" / "trial.json").exists())
        self.assertEqual((new / "config" / "agentic.toml").stat().st_mode & 0o077, 0)
        self.assertEqual((new_home / ".config" / "agentic-trading" / "tokens.json").stat().st_mode & 0o077, 0)
        self.assertFalse((new / "data" / "state" / "arm.json").exists())
        self.assertTrue(
            (new_home / ".config" / "agentic-trading" / "tokens.json").exists()
        )
        self.assertFalse((new / "_home").exists())

    def test_the_upgrade_launcher_loads_the_writer_key_file_when_present(self) -> None:
        launcher = (DEPLOY / "agentic-trading-upgrade").read_text()
        self.assertIn('env_file="$HOME/.config/agentic-trading/upgrade.env"', launcher)
        self.assertIn(
            'if [ -r "$env_file" ]; then set -a; . "$env_file"; set +a; fi', launcher
        )
        self.assertLess(launcher.index("env_file="), launcher.index("upgrade run"))

    def test_the_upgrade_launcher_shows_the_outcome_and_keeps_the_runs_exit_code(self) -> None:
        launcher = (DEPLOY / "agentic-trading-upgrade").read_text()
        self.assertIn("|| status=$?", launcher)
        self.assertIn('notify-send -u "$urgency"', launcher)
        self.assertTrue(launcher.rstrip().endswith('exit "$status"'))


if __name__ == "__main__":
    unittest.main()
