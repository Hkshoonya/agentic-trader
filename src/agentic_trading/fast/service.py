"""The switchboard inside the venues service: ticks in; book, status and journal out.

``run_venues`` hands every bus tick to ``FastEngine.on_tick``. An exception
from the switchboard marks the engine failed (health shows it, the book is
saved) and the engine stops consuming. The price feeds and the recorder keep
running; they never share a call stack with strategy code.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agentic_trading import jsonio
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.store import FastStore
from agentic_trading.fast.switchboard import Switchboard
from agentic_trading.venues.model import Tick

RECENT = 20


class FastJournal:
    """``fast-<date>.jsonl`` beside the trader's journals; dated readers skip it by name."""

    def __init__(self, journal_dir: Path | str) -> None:
        self.journal_dir = Path(journal_dir)
        self.errors = 0

    def append(self, record: dict[str, Any]) -> None:
        day = str(record.get("at") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        try:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            with (self.journal_dir / f"fast-{day}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except OSError:
            self.errors += 1  # a full disk must not stop the switchboard


def fast_problem(config: FastConfig, venues_config: Any, sources: list[Any]) -> str:
    """Why the switchboard cannot run with this configuration, or "" when it can."""
    missing = [s for s in config.symbols if s not in venues_config.alpaca_crypto_symbols]
    if missing:
        return f"symbols {missing} are not in [venues] alpaca_crypto_symbols, so no prices would arrive for them"
    if not any(getattr(source, "key", "") == "alpaca_crypto" for source in sources):
        return ("the switchboard needs the Alpaca crypto stream: Alpaca keys and "
                "use_alpaca_live (or use_alpaca_paper) in [venues]")
    return ""


class FastEngine:
    def __init__(self, config: FastConfig, *, state_dir: Path | str, journal_dir: Path | str,
                 now: Optional[datetime] = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.config = config
        self.state_dir = Path(state_dir)
        self.store = FastStore(state_dir)
        book, mirror_book, state, notes = self.store.load(current)
        self.board = Switchboard(config, book, mirror=CoinbaseMirror(mirror_book, config.coinbase_fee))
        self.journal = FastJournal(journal_dir)
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self.failed = ""
        self.save_error = ""
        for note in notes:
            self._record({"at": current.isoformat(), "event": "fast_reset", "symbol": "*", "text": note})
        for event in self.board.restore(state, current):
            self._record(event.to_record())

    def on_tick(self, tick: Tick) -> None:
        if self.failed:
            return
        try:
            events = self.board.on_tick(tick)
        except Exception as exc:  # noqa: BLE001 - a strategy bug must not touch the feeds
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            self._record({"at": tick.received_at.isoformat(), "event": "fast_failed",
                          "symbol": tick.symbol, "text": f"The switchboard stopped: {self.failed}"})
            self.save()
            return
        traded = False
        for event in events:
            self._record(event.to_record())
            traded = traded or event.kind in ("fast_entry", "fast_exit")
        if traded:
            self.save()

    def save(self) -> None:
        try:
            self.store.save(self.board)
            self.save_error = ""
        except OSError as exc:
            self.save_error = f"could not save: {exc.strerror}"

    def status(self, now: datetime) -> dict[str, Any]:
        return {
            "enabled": True,
            "as_of": now.isoformat(),
            "failed": self.failed,
            "save_error": self.save_error,
            **self.board.status(),
            "recent": list(reversed(self.recent)),
        }

    def write_status(self, now: datetime) -> None:
        try:
            jsonio.write_text(self.state_dir / "fast.json", jsonio.dumps(self.status(now), indent=2) + "\n")
        except OSError:
            pass  # the console shows the file as stale; the switchboard keeps trading

    def _record(self, record: dict[str, Any]) -> None:
        self.recent.append(record)
        self.journal.append(record)
