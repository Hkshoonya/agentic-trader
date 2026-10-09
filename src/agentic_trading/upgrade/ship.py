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
# Git in the worktree never runs a hook or an fsmonitor: whatever config git ends up reading, nothing runs.
INERT = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
SERVICES = ("agentic-trading", "agentic-trading-dashboard", "agentic-trading-venues")
STORES = ("swarm", "desk/swarm.json")  # the editable modules' data, relative to state_dir


class Shipyard:
    def __init__(self, repo: Path, state_dir: Path, runner: Runner, live: str = "live") -> None:
        self.repo, self.state_dir, self.runner, self.live = Path(repo), Path(state_dir), runner, live
        self._links: dict[Path, str] = {}  # each worktree's .git link as it was made, before any engine ran

    def _git(self, *args: str, where: Optional[Path] = None, timeout: float = 300.0):
        return self.runner(["git", "-C", str(where or self.repo), *args], timeout=timeout)

    def prepare(self, branch: str) -> Path:
        wt = self.repo.parent / "agentic-trading-auto"
        self._git("worktree", "remove", "--force", str(wt))
        self._git("worktree", "prune")
        self._git("branch", "-D", branch)
        self._git("worktree", "add", "-b", branch, str(wt), self.live)
        link = wt / ".git"
        if link.is_file() and not link.is_symlink():
            self._links[wt] = link.read_text(encoding="utf-8")
        return wt

    def gitdir(self, wt: Path) -> Path:
        """The worktree's git directory, read from its link as ``prepare`` found it (git numbers the name
        on a collision), and only if it lies under this repo's worktrees."""
        default = self.repo / ".git" / "worktrees" / wt.name
        made = self._links.get(wt, "")
        if made.startswith("gitdir:"):
            named = Path(made[len("gitdir:"):].strip())
            if named.parent == default.parent:
                return named
        return default

    def _wt(self, wt: Path, *args: str, timeout: float = 300.0, identity: bool = False):
        """Git in the worktree with its git directory named outright, so the worktree's own ``.git``
        link — a file the writer and the tests could replace — is never read."""
        return self.runner(["git", "-C", str(wt), f"--git-dir={self.gitdir(wt)}", f"--work-tree={wt}", *INERT,
                            *(IDENTITY if identity else []), *args], timeout=timeout)

    def intact(self, wt: Path) -> bool:
        """The worktree's .git link is still the plain file ``prepare`` made."""
        link = wt / ".git"
        made = self._links.get(wt)
        if made is None:
            return not link.exists() or (link.is_file() and not link.is_symlink())
        return link.is_file() and not link.is_symlink() and link.read_text(encoding="utf-8") == made

    def diff(self, wt: Path) -> tuple[str, str]:
        self._wt(wt, "add", "-A")
        # Whatever is still untracked is ignored, so the walls would never see it — yet the suite and the
        # reviewer would (a planted CLAUDE.md under an ignored path, say). Delete it, nested repos too.
        self._wt(wt, "clean", "-ffdxq")
        names = self._wt(wt, "diff", "--cached", "--name-status", "-M", self.live).out
        unified = self._wt(wt, "diff", "--cached", "-U0", self.live).out
        return names, unified

    def settle(self, wt: Path) -> bool:
        """Just before review: drop everything the test run created, and say whether the tracked tree
        and the .git link still match what the walls checked (a test that rewrites files fails here)."""
        self._wt(wt, "clean", "-ffdxq")
        return self.intact(wt) and self._wt(wt, "diff", "--quiet").code == 0

    def file_at(self, path: str) -> str:
        shown = self._git("show", f"{self.live}:{path}")
        return shown.out if shown.code == 0 else ""

    def head(self, path: Optional[Path] = None) -> str:
        found = self._wt(path, "rev-parse", "HEAD") if path is not None else self._git("rev-parse", "HEAD")
        return found.out.strip()

    def commit(self, wt: Path, title: str) -> bool:
        return self._wt(wt, "commit", "-q", "-m", f"auto-upgrade: {title}", timeout=120.0, identity=True).code == 0

    def publish(self, wt: Path, branch: str, title: str, body: str) -> str:
        if self._wt(wt, "push", "-q", "-u", "origin", branch).code != 0:
            return ""
        # gh runs git where it stands; it stands in the main checkout, never in the worktree.
        made = self.runner(["gh", "pr", "create", "--base", self.live, "--head", branch,
                            "--title", f"[auto-upgrade] {title}", "--body", body], cwd=self.repo, timeout=120.0)
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

    def rollback(self, commit: str, restore: bool = True) -> tuple[bool, str]:
        """Revert ``commit``; restore the swarm's stores only while its canary is open — once it has
        passed, the days of state written since are healthy and kept."""
        reverted = self.runner(["git", *IDENTITY, "-C", str(self.repo), "revert", "--no-edit", commit], timeout=300.0)
        if reverted.code != 0:
            self._git("revert", "--abort")  # never leave the live checkout mid-revert, with conflict markers
            lines = [l for l in reverted.out.splitlines() if l.startswith(("CONFLICT", "error", "fatal"))]
            return False, f"git revert failed: {(lines or reverted.out.strip().splitlines() or ['?'])[0][:200]}"
        self._git("push", "-q", "origin", self.live, timeout=300.0)
        note = ("" if self.restore(commit) else " (no data snapshot to restore)") if restore else " (data kept)"
        if not self.restart():
            return False, "reverted, but the services did not restart"
        return True, "reverted and restarted" + note

    def cleanup(self, wt: Path) -> None:
        self._git("worktree", "remove", "--force", str(wt))
