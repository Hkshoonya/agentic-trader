"""Where the swarm survives restarts.

``data/state/swarm/`` holds:
- ``population.json``: the living agents;
- ``lineage.json``: every agent ever born, with its parents and how it died, and the trial count;
- ``ledger.json``: the trial count and the last finished step;
- ``scout.json``: the LLM scout's spending this week.

``data/state/desk/swarm.json`` is the member book the desk judges, and
``data/state/swarm.json`` is the dashboard's picture.

Writes are atomic. An unreadable file is moved aside, as the fast engine does, and
the trial count is the larger of the ledger's and the lineage's, so it never falls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio
from agentic_trading.fast.store import FastStore
from agentic_trading.swarm.life import Agent

STALE_LOCK_SECONDS = 3 * 3600
REJECTED_KEEP = 20_000  # ~12 refusals a day for years; enough never to re-screen an idea


@dataclass
class SwarmState:
    living: list[Agent] = field(default_factory=list)
    lineage: dict[str, dict[str, Any]] = field(default_factory=dict)
    trials: int = 0
    last_step: str = ""
    scout: dict[str, Any] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)  # recipe ids screened and refused


class SwarmStore:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.dir = self.state_dir / "swarm"
        self.population_path = self.dir / "population.json"
        self.lineage_path = self.dir / "lineage.json"
        self.ledger_path = self.dir / "ledger.json"
        self.scout_path = self.dir / "scout.json"
        self.lock_path = self.dir / "step.lock"
        self.book_path = self.state_dir / "desk" / "swarm.json"
        self.status_path = self.state_dir / "swarm.json"

    def _read(self, path: Path, now: datetime, notes: list[str]) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return raw
        except (OSError, ValueError):
            FastStore.move_aside(path, now, notes)
            return None

    def load(self, now: datetime) -> tuple[SwarmState, list[str]]:
        notes: list[str] = []
        population = self._read(self.population_path, now, notes) or {}
        lineage = self._read(self.lineage_path, now, notes) or {}
        ledger = self._read(self.ledger_path, now, notes) or {}
        scout = self._read(self.scout_path, now, notes) or {}
        living = []
        for row in population.get("agents") or []:
            try:
                living.append(Agent.from_row(row))
            except (KeyError, TypeError, ValueError):
                notes.append("an unreadable agent was dropped from population.json")
        trials = max(_count(ledger.get("trials")), _count(lineage.get("trials")))
        agents = lineage.get("agents") if isinstance(lineage.get("agents"), dict) else {}
        rejected = [str(r) for r in ledger.get("rejected") or [] if isinstance(r, str)]
        return SwarmState(living, agents, trials, str(ledger.get("last_step") or ""), scout, rejected), notes

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")

    def save(self, state: SwarmState) -> None:
        self._write(self.population_path, {"agents": [a.to_row() for a in state.living]})
        self._write(self.lineage_path, {"trials": state.trials, "agents": state.lineage})
        self._write(self.ledger_path, {"trials": state.trials, "last_step": state.last_step,
                                       "rejected": state.rejected[-REJECTED_KEEP:]})
        self._write(self.scout_path, state.scout)

    def write_status(self, status: dict[str, Any]) -> None:
        self._write(self.status_path, status)

    def read_status(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def lock(self, now: datetime) -> bool:
        """One step at a time. A lock older than three hours belonged to a step that died."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    taken = datetime.fromisoformat(self.lock_path.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    taken = None
                if taken is not None and (now - taken).total_seconds() < STALE_LOCK_SECONDS:
                    return False
                self.lock_path.unlink(missing_ok=True)
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(now.isoformat())
            return True
        return False

    def unlock(self) -> None:
        self.lock_path.unlink(missing_ok=True)


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
