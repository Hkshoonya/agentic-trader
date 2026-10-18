"""The Windows host's install and migrate scripts: they parse, and migrate.ps1 reads migrate.sh's archive."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
POWERSHELL = shutil.which("powershell")
TAR = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32" / "tar.exe"


def _ps(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows hosts only")
class ScriptSyntaxTests(unittest.TestCase):
    def test_every_script_parses(self) -> None:
        for script in sorted(DEPLOY.glob("*.ps1")):
            with self.subTest(script=script.name):
                check = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                         f"'{script}', [ref]$null, [ref]$e) | Out-Null; $e | ForEach-Object {{ $_.Message }}")
                done = _ps("-Command", check, cwd=ROOT)
                self.assertEqual(done.stdout.strip(), "", done.stdout)


@unittest.skipUnless(os.name == "nt" and POWERSHELL and TAR.is_file(), "Windows hosts only")
class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "deploy").mkdir(parents=True)
        (self.repo / "config").mkdir()
        shutil.copy2(DEPLOY / "migrate.ps1", self.repo / "deploy" / "migrate.ps1")
        self.home = self.tmp / "home"
        self.home.mkdir()

    def _linux_archive(self) -> Path:
        """What `deploy/migrate.sh export` writes on the Linux host."""
        path = self.tmp / "agentic-move.tgz"
        files = {
            "config/agentic.toml": 'state_dir = "data/state"\njournal_dir = "/home/doczeus/x/journal"\n',
            "config/secrets.toml": "[alpaca]\nkey = 'k'\n",
            ".env": "DEEPSEEK_API_KEY=k\n",
            "data/state/promotion.json": "{}\n",
            "data/journal/2026-10-17.jsonl": "{}\n",
            "_home/.config/agentic-trading/tokens.json": "{}\n",
            "_home/.config/agentic-trading/upgrade.env": "DEEPSEEK_API_KEY=k\n",
        }
        with tarfile.open(path, "w:gz") as archive:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_import_lands_the_record_and_the_home_files_and_flags_linux_paths(self) -> None:
        archive = self._linux_archive()
        done = _ps("-File", str(self.repo / "deploy" / "migrate.ps1"), "import", str(archive),
                   "-HomeDir", str(self.home), cwd=self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((self.repo / "config" / "secrets.toml").is_file())
        self.assertTrue((self.repo / "data" / "journal" / "2026-10-17.jsonl").is_file())
        self.assertTrue((self.home / ".config" / "agentic-trading" / "tokens.json").is_file())
        self.assertFalse((self.repo / "_home").exists())
        # Owner-only, and still readable by the owner: icacls once left a file with an empty ACL.
        self.assertIn("alpaca", (self.repo / "config" / "secrets.toml").read_text())
        self.assertEqual((self.home / ".config" / "agentic-trading" / "tokens.json").read_text(), "{}\n")
        acl = subprocess.run(["icacls", str(self.repo / "config" / "secrets.toml")], capture_output=True,
                             text=True).stdout
        self.assertNotIn("Users:", acl)
        self.assertNotIn("Everyone", acl)
        self.assertIn("names Linux paths", done.stdout + done.stderr)

    def test_export_leaves_the_arm_latches_behind_and_round_trips(self) -> None:
        (self.repo / "config" / "agentic.toml").write_text('state_dir = "data/state"\n')
        state = self.repo / "data" / "state"
        state.mkdir(parents=True)
        (state / "promotion.json").write_text("{}")
        (state / "arm.json").write_text("{}")
        (state / "venues_arm.json").write_text("{}")
        (self.home / ".config" / "agentic-trading").mkdir(parents=True)
        (self.home / ".config" / "agentic-trading" / "tokens.json").write_text("{}")
        out = self.tmp / "out.tgz"
        done = _ps("-File", str(self.repo / "deploy" / "migrate.ps1"), "export", str(out),
                   "-HomeDir", str(self.home), cwd=self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with tarfile.open(out) as archive:
            names = set(archive.getnames())
        self.assertIn("data/state/promotion.json", names)
        self.assertIn("_home/.config/agentic-trading/tokens.json", names)
        self.assertNotIn("data/state/arm.json", names)
        self.assertNotIn("data/state/venues_arm.json", names)
        again = _ps("-File", str(self.repo / "deploy" / "migrate.ps1"), "export", str(out), cwd=self.repo)
        self.assertNotEqual(again.returncode, 0)  # never overwrite an archive


if __name__ == "__main__":
    unittest.main()
