"""The two engines: Codex writes the change in its sandbox, Claude reviews it (spec U4, U10)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.tasks import Task

ALLOWED_TEXT = (
    "src/agentic_trading/strategies/, src/agentic_trading/swarm/, tests/test_swarm_*.py (except "
    "tests/test_swarm_funding.py and tests/test_swarm_desk.py), tests/test_strategies_*.py, docs/, and only the "
    "test-count numbers in README.md and CONTRIBUTING.md"
)
WRITE_RULES = f"""You are improving one part of an automated trading system that must earn more over time.
Make ONE small, well-tested improvement for the task below.
Rules you must follow:
- You may ONLY create or modify files under: {ALLOWED_TEXT}. Never delete or rename files.
- Never use subprocess, sockets, HTTP, os.environ/os.getenv, eval/exec/__import__, or open files outside the repo.
- Never delete or weaken a test. Write a failing test first, then the code. Keep the change under 12 files and
  400 changed lines. Keep on-disk JSON formats readable by the current code.
- Run: python -m pytest tests -q   (there is no network; the suite must pass). If you add tests, update the test
  count in README.md (3 places) and CONTRIBUTING.md, then run: python tools/check_doc_counts.py
- Do not commit. Do not touch git.
- If the task cannot be done inside the allowed files, change nothing and end with one line:
  NOT_APPLICABLE: <reason>
- Otherwise end with one line: DONE: <one sentence on what changed and why it should earn more>
"""
REVIEW_RULES = f"""You are the second reviewer of an automatic change to a trading system. Be strict.
Approve only if ALL hold: it does the task; it only touches {ALLOWED_TEXT}; tests were added or strengthened and
none weakened; it adds no network, subprocess, environment or out-of-repo file access; it does not change on-disk
JSON formats in a way the previous code could not read; it cannot make the system take more risk than before.
End with exactly one line: VERDICT: APPROVE — <reason>   or   VERDICT: REJECT — <reason>
"""
REVIEW_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"


@dataclass(frozen=True)
class EngineResult:
    status: str
    summary: str


@dataclass(frozen=True)
class Verdict:
    approved: bool
    reason: str


def write(task: Task, *, worktree: Path, scratch: Path, runner: Runner, timeout: float = 2700.0) -> EngineResult:
    last = scratch / "codex-last.txt"
    last.unlink(missing_ok=True)
    prompt = f"{WRITE_RULES}\nTASK: {task.title}\n{task.detail}\n"
    done = runner(["codex", "exec", "-C", str(worktree), "-s", "workspace-write", "--skip-git-repo-check",
                   "--ephemeral", "-o", str(last), prompt], cwd=worktree, timeout=timeout)
    if done.code != 0:
        return EngineResult("failed", f"codex exited {done.code}: {done.out.strip()[-200:]}")
    text = last.read_text(encoding="utf-8") if last.is_file() else done.out
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("NOT_APPLICABLE:"):
            return EngineResult("not_applicable", line[len("NOT_APPLICABLE:"):].strip()[:300])
        if line.startswith("DONE:"):
            return EngineResult("changed", line[len("DONE:"):].strip()[:300])
    return EngineResult("changed", (text.strip().splitlines() or ["no summary"])[-1][:300])


def review(task: Task, *, worktree: Path, base: str, runner: Runner, timeout: float = 1200.0) -> Verdict:
    prompt = (f"{REVIEW_RULES}\nTASK: {task.title}\n{task.detail}\n"
              f"See the change with: git diff --cached {base}\n")
    done = runner(["claude", "-p", prompt, "--bare", "--allowedTools", REVIEW_TOOLS, "--output-format", "text"],
                  cwd=worktree, timeout=timeout)
    if done.code != 0:
        return Verdict(False, f"the reviewer failed (exit {done.code}): {done.out.strip()[-200:]}")
    for line in reversed(done.out.strip().splitlines()):
        if "VERDICT:" in line:
            rest = line.split("VERDICT:", 1)[1].strip()
            return Verdict(rest.upper().startswith("APPROVE"), rest[:300])
    return Verdict(False, "the reviewer gave no verdict")
