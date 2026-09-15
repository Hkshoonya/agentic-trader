from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterator


class DecisionJournal:
    def __init__(self, journal_dir: Path) -> None:
        self._journal_dir = journal_dir

    def _today_path(self) -> Path:
        return self._journal_dir / f"{date.today().isoformat()}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        with self._today_path().open("a", encoding="utf-8") as f:
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
