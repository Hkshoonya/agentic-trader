from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class DecisionJournal:
    def __init__(self, journal_dir: Path) -> None:
        self._journal_dir = journal_dir

    def _today_path(self) -> Path:
        return self._journal_dir / f"{date.today().isoformat()}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        path = self._today_path()
        # Every record carries the time it happened. Without this, an advisor
        # call or a regime refresh is undated: you cannot measure how often the
        # model was consulted, and the console has to fall back to "now".
        if "at" not in record:
            record = {
                **record,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        # Journals carry account identifiers and order details: owner-only.
        handle = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(handle, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def has_decision(self, decision_id: str) -> bool:
        path = self._today_path()
        if not path.exists():
            return False
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("decision_id") == decision_id:
                    return True
        return False

    def iter_today(self) -> Iterator[dict[str, Any]]:
        path = self._today_path()
        if not path.exists():
            yield from ()
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
