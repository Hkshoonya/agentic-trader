"""One daily upgrade: refuse unless every guard holds, then write, check, test, review, ship, watch."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from agentic_trading import jsonio
from agentic_trading.fast.service import FastJournal
from agentic_trading.upgrade import engines as engine_module
from agentic_trading.upgrade import planner as planner_module
from agentic_trading.upgrade.control import load_control, save_control
from agentic_trading.upgrade.policy import COUNT_FILES, check, control_closure, parse_diff, with_test_counts
from agentic_trading.upgrade.run import Runner
from agentic_trading.upgrade.sandbox import count_tests, probe, run_doc_counts, run_suite
from agentic_trading.upgrade.settings import UpgradeConfig
from agentic_trading.upgrade.tasks import Task, pick, record


@dataclass(frozen=True)
class Outcome:
    code: int
    message: str


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"


def run_cycle(*, state_dir: Path, journal_dir: Path, repo: Path, venv: Path, settings: UpgradeConfig, mode: str,
              now: datetime, runner: Runner, shipyard: Any,
              writers: Sequence[tuple[str, Callable[..., Any]]] = engine_module.WRITERS,
              review: Callable[..., Any] = engine_module.review, plan: Callable[..., dict] = planner_module.run,
              probe_paths: tuple[Path, Path]) -> Outcome:
    journal = FastJournal(journal_dir, prefix="upgrade")
    task_holder: dict[str, Task] = {}

    def finish(code: int, outcome: str, message: str) -> Outcome:
        task = task_holder.get("task")
        journal.append({"event": f"upgrade_{outcome}", "at": now.isoformat(), "text": message,
                        "task": task.title if task else ""})
        path = Path(state_dir) / "upgrade.json"
        jsonio.write_text(path, jsonio.dumps({"enabled": settings.enabled, "updated_at": now.isoformat(), "last": {
            "at": now.isoformat(), "outcome": outcome, "message": message,
            "task": task.title if task else ""}}, indent=2) + "\n")
        return Outcome(code, message)

    if not settings.enabled:
        return finish(0, "off", "the upgrader is off ([upgrade] enabled = false)")
    control = load_control(state_dir)
    if control.paused:
        return finish(0, "paused", f"paused: {control.reason}")
    if control.canary:
        return finish(0, "waiting", f"a canary is open until {control.canary.get('until')}")
    try:
        guard = json.loads((Path(state_dir) / "risk_guard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        guard = {}
    if guard.get("kill_switch"):
        return finish(0, "refused", "the kill switch is engaged")
    branch_now = runner(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=30.0).out.strip()
    if branch_now != "live":
        return finish(1, "refused", f"the live checkout is on {branch_now or '?'}, not on live")
    dirty = [line for line in runner(["git", "-C", str(repo), "status", "--porcelain"], timeout=60.0).out.splitlines()
             if line.strip() and not line[3:].startswith("data/")]
    if dirty:
        return finish(1, "refused", f"the live checkout has uncommitted changes outside data/: {dirty[0][3:]}")
    # An empty stand-in for the worktree: the probe asks what a sandboxed worktree can see, and mounting
    # the live checkout itself would hand it config/secrets.toml.
    probe_root = Path(state_dir) / "upgrade" / "probe-root"
    probe_root.mkdir(parents=True, exist_ok=True)
    problems = probe(runner, worktree=probe_root, venv=venv, secrets=probe_paths[0], home_file=probe_paths[1])
    if problems:
        return finish(1, "refused", "the sandbox is not safe: " + "; ".join(problems))
    if planner_module.due(state_dir, mode=mode, now=now):  # the queue is low: plan first (planner spec P1)
        planned = plan(repo=Path(repo), state_dir=state_dir, runner=runner, cwd=probe_root, now=now)
        journal.append({"event": "upgrade_plan", "at": now.isoformat(),
                        "added": [row["title"] for row in planned.get("added", [])],
                        "skipped": planned.get("skipped", []), "error": planned.get("error", "")})

    task = pick(state_dir, now)
    if task is None:
        return finish(0, "idle", "nothing to work on")
    task_holder["task"] = task
    branch = f"auto/{now:%Y-%m-%d}-{_slug(task.title)}"
    wt = shipyard.prepare(branch)

    def fail(outcome: str, message: str, failure: bool = True) -> Outcome:
        record(state_dir, task, "failed" if failure else outcome, now, message)
        shipyard.cleanup(wt)
        return finish(0, outcome, message)

    def unavailable(engine: str, why: str) -> Outcome:
        # A spent quota says nothing about the task: no failure is charged, so it keeps its place.
        shipyard.cleanup(wt)
        return finish(0, "unavailable", f"{task.title}: the {engine} is unavailable ({why}); the task keeps its place")

    tests_before = count_tests(runner, worktree=wt, venv=venv)
    scratch = Path(state_dir) / "upgrade"
    scratch.mkdir(parents=True, exist_ok=True)
    written, tried, engine = None, [], ""
    for index, (engine, writer) in enumerate(writers):
        if written is not None:  # the one before may have left edits: the next starts on a fresh worktree
            shipyard.cleanup(wt)
            wt = shipyard.prepare(branch)
        written = writer(task, worktree=wt, scratch=scratch, runner=runner, gitdir=shipyard.gitdir(wt), venv=venv)
        tried.append(f"{engine}: {written.status}")
        journal.append({"event": "upgrade_writer", "at": now.isoformat(), "engine": engine,
                        "status": written.status, "text": written.summary, "task": task.title})
        # The next engine gets the task when this one could not run, or failed in a way that says nothing
        # about the task. A timeout ends the chain, so a run stays inside the unit's time limit.
        if written.status not in ("unavailable", "failed") or index == len(writers) - 1:
            break
    tried_text = "; ".join(tried)
    if written.status == "not_applicable":
        record(state_dir, task, "out_of_scope", now, written.summary)
        shipyard.cleanup(wt)
        return finish(0, "out_of_scope", f"{task.title}: out of scope ({written.summary})")
    if written.status == "unavailable":
        return unavailable("writer", f"{written.summary}; tried {tried_text}")
    if written.status != "changed":
        return fail("failed", f"{task.title}: the writer failed ({written.summary}); tried {tried_text}")
    if not shipyard.intact(wt):
        return fail("refused", f"{task.title}: the writer replaced the worktree's git link")
    tests_after = count_tests(runner, worktree=wt, venv=venv)
    # README and CONTRIBUTING are the system's account of itself, not the writer's: whatever it wrote there
    # is dropped, and trusted code writes the count the suite now has. A file swapped for a symlink or a
    # directory is left for the walls to refuse, never written through.
    for name in COUNT_FILES:
        doc = wt / name
        if not doc.is_symlink() and (doc.is_file() or not doc.exists()):
            text = shipyard.file_at(name)
            doc.write_text(with_test_counts(text, tests_after) if tests_after else text, encoding="utf-8")
    names, unified = shipyard.diff(wt)
    changes = parse_diff(names, unified)
    if not changes:
        return fail("failed", f"{task.title}: the writer changed nothing")
    before = {c.path: shipyard.file_at(c.old_path or c.path) for c in changes if c.path.endswith(".py")}
    after = {c.path: (wt / c.path).read_text(encoding="utf-8") if (wt / c.path).is_file() else ""
             for c in changes if c.path.endswith(".py")}
    reasons = check(changes, live=(mode == "live"), closure=control_closure(Path(repo) / "src"), before=before,
                    after=after, tests_before=tests_before or 0, tests_after=tests_after or 0,
                    max_files=settings.max_files, max_lines=settings.max_lines)
    if tests_before is None or tests_after is None:
        reasons.append("the tests could not be counted")
    odd = [c.path for c in changes if (wt / c.path).is_symlink() or (wt / c.path).is_dir()]
    if odd:
        reasons.append(f"a change may not add a symlink or a submodule: {odd[0]}")
    if reasons:
        return fail("refused", f"{task.title}: refused by the walls: " + "; ".join(reasons))
    suite = run_suite(runner, worktree=wt, venv=venv)
    if suite.code != 0:
        return fail("failed", f"{task.title}: the tests failed: {suite.out.strip()[-300:]}")
    docs = run_doc_counts(runner, worktree=wt, venv=venv)
    if docs.code != 0:
        return fail("failed", f"{task.title}: the doc counts disagree: {docs.out.strip()[-200:]}")
    if not shipyard.settle(wt):
        return fail("refused", f"{task.title}: the tests changed the worktree, so the review would not see "
                               "what the walls checked")
    verdict = review(task, worktree=wt, base="live", runner=runner, gitdir=shipyard.gitdir(wt))
    if not verdict.available:
        return unavailable("reviewer", verdict.reason)
    if not verdict.approved:
        return fail("rejected", f"{task.title}: the reviewer rejected it: {verdict.reason}")
    if load_control(state_dir).paused:
        shipyard.cleanup(wt)
        return finish(0, "paused", f"paused before deploying {task.title}")
    if not shipyard.commit(wt, task.title):
        return fail("failed", f"{task.title}: the commit failed")
    body = (f"Automatic upgrade.\n\nTask: {task.title}\n\n{task.detail}\n\nWriter ({engine}): {written.summary}\n"
            f"Reviewer: {verdict.reason}\nTests: {tests_before} → {tests_after}, all passing in the sandbox.")
    pr = shipyard.publish(wt, branch, task.title, body)
    commit = shipyard.head(wt)
    if not shipyard.deploy(branch, tag=commit):
        return fail("failed", f"{task.title}: the deploy failed; live is unchanged")
    control = load_control(state_dir)
    control.canary = {"commit": commit, "branch": branch, "task": task.key, "title": task.title, "pr": pr,
                      "started_at": now.isoformat(),
                      "until": (now + timedelta(hours=settings.canary_hours)).isoformat()}
    control.last_shipped = dict(control.canary)
    save_control(state_dir, control)
    record(state_dir, task, "shipped", now, written.summary)
    shipyard.cleanup(wt)
    return finish(0, "shipped", f"shipped {task.title} ({pr or 'no PR'}); canary open for "
                                f"{settings.canary_hours} h")
