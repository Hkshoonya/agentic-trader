"""What the upgrader works on, in order (spec U9, plan P1/P2/P5)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio

CATEGORY_ORDER = ("strategy", "research", "reliability", "data", "execution", "risk")
REST = timedelta(days=30)
FINAL = ("shipped", "out_of_scope")
SEED_BACKLOG: list[dict[str, str]] = [
    {"key": "seed-swarm-shares", "title": "Weight the swarm's contributors by forward evidence, not volatility alone",
     "detail": "In swarm/blend.py, shares are inverse-volatility. Make each contributor's share also grow with the "
               "t-statistic of its trailing forward excess (shrunk toward zero for short records), so proven "
               "agents earn more of the book. Test the ordering and that shares still sum to 1."},
    {"key": "seed-swarm-sizing", "title": "Add a volatility-targeted sizing choice to swarm recipes",
     "detail": "Extend swarm/recipe.py SIZING with a sizing mode that targets a fixed annualised volatility per "
               "position, keeping old recipes' ids unchanged. Test validation, ids and agent_weights."},
    {"key": "seed-swarm-rejections", "title": "Let refused recipes be screened again after 90 days",
     "detail": "swarm/step.py remembers refused recipe ids forever. History moves; let a refusal expire after "
               "90 days (store the refusal date in the ledger) so a recipe can be retried on new data."},
    {"key": "seed-swarm-lock", "title": "Write the swarm step lock atomically",
     "detail": "swarm/store.py creates the lock file, then writes its time; an empty file reads as stale. "
               "Write the timestamp so a just-created lock is never taken over. Test the race."},
    {"key": "seed-swarm-first-day", "title": "Start a newborn's forward record on its first full day",
     "detail": "swarm/life.py: a newborn's first forward day shows minus that day's benchmark move because it "
               "enters at the close. Exclude the entry day from excess. Test with a known first day."},
]


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    detail: str
    source: str  # proposal, backlog or health


def _upgrade_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "upgrade"


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")


def _key(title: str) -> str:
    return "proposal-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def candidates(state_dir: Path | str) -> list[Task]:
    raw = _read(Path(state_dir) / "proposals.json", {})
    rows = [p for p in (raw.get("proposals") if isinstance(raw, dict) else []) or []
            if isinstance(p, dict) and p.get("status") == "proposed" and p.get("title")]

    def rank(p: dict) -> tuple:
        category = str(p.get("category") or "")
        order = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return order, -float(p.get("confidence") or 0)

    out = [Task(_key(str(p["title"])), str(p["title"]),
                f"{p.get('change') or ''}\nExpected effect: {p.get('expected_effect') or ''}\n"
                f"Falsified if: {p.get('falsified_if') or ''}", "proposal") for p in sorted(rows, key=rank)]
    backlog_path = _upgrade_dir(state_dir) / "backlog.json"
    if not backlog_path.is_file():
        _write(backlog_path, SEED_BACKLOG)
    out += [Task(str(b["key"]), str(b["title"]), str(b.get("detail") or ""), "backlog")
            for b in _read(backlog_path, []) if isinstance(b, dict) and b.get("key")]
    out += [Task(str(h["key"]), str(h["title"]), str(h.get("detail") or ""), "health")
            for h in _read(_upgrade_dir(state_dir) / "health.json", []) if isinstance(h, dict) and h.get("key")]
    return out


def pick(state_dir: Path | str, now: datetime) -> Optional[Task]:
    ledger = _read(_upgrade_dir(state_dir) / "attempts.json", {})
    for task in candidates(state_dir):
        entry = ledger.get(task.key) or {}
        if entry.get("outcome") in FINAL:
            continue
        recent = [f for f in entry.get("failures", []) if now - datetime.fromisoformat(f) < REST]
        if len(recent) >= 2:
            continue
        return task
    return None


def record(state_dir: Path | str, task: Task, outcome: str, now: datetime, note: str = "") -> None:
    path = _upgrade_dir(state_dir) / "attempts.json"
    ledger = _read(path, {})
    entry = ledger.setdefault(task.key, {"title": task.title, "failures": []})
    entry.update(outcome=outcome, note=note[:300], last=now.isoformat())
    if outcome in ("failed", "rolled_back"):
        entry["failures"].append(now.isoformat())
    _write(path, ledger)


def note_health(state_dir: Path | str, problem: str) -> None:
    path = _upgrade_dir(state_dir) / "health.json"
    rows = _read(path, [])
    key = "health-" + hashlib.sha1(problem.encode("utf-8")).hexdigest()[:10]
    if not any(r.get("key") == key for r in rows if isinstance(r, dict)):
        rows.append({"key": key, "title": f"Investigate: {problem}", "detail": problem})
        _write(path, rows[-50:])
