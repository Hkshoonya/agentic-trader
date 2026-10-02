"""Venue order events, one dated file per UTC day, secrets scrubbed.

``venues-<date>.jsonl`` sits beside the Robinhood journals. Readers that
expect dated Robinhood files skip it by name.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading import jsonio
from agentic_trading.venues.secrets import Credentials, redact


class VenueJournal:
    def __init__(self, journal_dir: Path | str, credentials: Iterable[Credentials] = ()) -> None:
        self.journal_dir = Path(journal_dir)
        self._credentials = list(credentials)

    def append(self, record: dict[str, Any], *, now: Optional[datetime] = None) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        text = redact(jsonio.dumps({"at": current.isoformat(), **record}), self._credentials)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        path = self.journal_dir / f"venues-{current.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(json.loads(text), separators=(",", ":")) + "\n")
