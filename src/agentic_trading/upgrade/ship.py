"""Ship a change forward with a record, and take it back by revert (spec U5, U7).

The live checkout's tracked ``data/bars`` files change locally all day, so nothing here ever
resets the tree. Forward is a fast-forward of ``live``; backward is a ``git revert`` commit,
plus a restore of the swarm's stores from the snapshot taken just before the deploy.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from agentic_trading.upgrade.run import Runner

IDENTITY = ["-c", "user.name=Hkshoonya", "-c", "user.email=154622641+Hkshoonya@users.noreply.github.com"]
SERVICES = ("agentic-trading", "agentic-trading-dashboard", "agentic-trading-venues")
STORES = ("swarm", "desk/swarm.json")  # the editable modules' data, relative to state_dir


class Shipyard:
    def __init__(self, repo: Path, state_dir: Path, runner: Runner, live: str = "live") -> None:
        self.repo, self.state_dir, self.runner, self.live = Path(repo), Path(state_dir), runner, live

    def _git(self, *args: str, where: Optional[Path] = None, timeout: float = 300.0):
        return self.runner(["git", "-C", str(where or self.repo), *args], timeout=timeout)

    def prepare(self, branch: str) -> Path:
        wt = self.repo.parent / "agentic-trading-auto"
        self._git("worktree", "remove", "--force", str(wt))
        self._git("worktree", "prune")
        self._git("branch", "-D", branch)
        self._git("worktree", "add", "-b", branch, str(wt), self.live)
        return wt

    def diff(self, wt: Path) -> tuple[str, str]:
        self._git("add", "-A", where=wt)
        names = self._git("diff", "--cached", "--name-status", "-M", self.live, where=wt).out
        unified = self._git("diff", "--cached", "-U0", self.live, where=wt).out
        return names, unified

    def file_at(self, path: str) -> str:
        shown = self._git("show", f"{self.live}:{path}")
        return shown.out if shown.code == 0 else ""

    def head(self, path: Optional[Path] = None) -> str:
        return self._git("rev-parse", "HEAD", where=path).out.strip()

    def commit(self, wt: Path, title: str) -> bool:
        return self.runner(["git", *IDENTITY, "-C", str(wt), "commit", "-q", "-m", f"auto-upgrade: {title}"],
                           timeout=120.0).code == 0

    def publish(self, wt: Path, branch: str, title: str, body: str) -> str:
        if self._git("push", "-q", "-u", "origin", branch, where=wt, timeout=300.0).code != 0:
            return ""
        made = self.runner(["gh", "pr", "create", "--base", self.live, "--head", branch,
                            "--title", f"[auto-upgrade] {title}", "--body", body], cwd=wt, timeout=120.0)
        return made.out.strip().splitlines()[-1] if made.code == 0 and made.out.strip() else ""

    def snapshot(self, tag: str) -> None:
        target = self.state_dir / "upgrade" / "snapshots" / tag
        for store in STORES:
            source = self.state_dir / store
            if source.is_dir():
                shutil.copytree(source, target / store, dirs_exist_ok=True)
            elif source.is_file():
                (target / store).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target / store)

    def restore(self, tag: str) -> bool:
        source = self.state_dir / "upgrade" / "snapshots" / tag
        if not source.is_dir():
            return False
        for store in STORES:
            saved = source / store
            if saved.is_dir():
                shutil.copytree(saved, self.state_dir / store, dirs_exist_ok=True)
            elif saved.is_file():
                shutil.copy2(saved, self.state_dir / store)
        return True

    def restart(self) -> bool:
        return self.runner(["systemctl", "--user", "restart", *SERVICES], timeout=180.0).code == 0

    def deploy(self, branch: str, tag: str) -> bool:
        self.snapshot(tag)
        if self._git("merge", "--ff-only", branch).code != 0:
            return False
        self._git("push", "-q", "origin", self.live, timeout=300.0)
        return self.restart()

    def rollback(self, commit: str) -> tuple[bool, str]:
        reverted = self.runner(["git", *IDENTITY, "-C", str(self.repo), "revert", "--no-edit", commit], timeout=300.0)
        if reverted.code != 0:
            return False, f"git revert failed: {reverted.out.strip()[-200:]}"
        self._git("push", "-q", "origin", self.live, timeout=300.0)
        restored = self.restore(commit)
        if not self.restart():
            return False, "reverted, but the services did not restart"
        return True, "reverted and restarted" + ("" if restored else " (no data snapshot to restore)")

    def cleanup(self, wt: Path) -> None:
        self._git("worktree", "remove", "--force", str(wt))
