"""Where the switchboard's paper life survives restarts.

* ``data/state/desk/switchboard.json``: the member book the desk judges, in
  the ordinary MemberBook format.
* ``data/state/fast/mirror.json``: the Coinbase comparison book.
* ``data/state/fast/engine.json``: open trades, cooldowns, the day's start
  equity and counters.

Writes are atomic (``jsonio.write_text``). An unreadable file is moved aside as
``<name>.corrupt-<UTC stamp>`` and a fresh one starts. The caller journals
that; it is never a crash.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentic_trading import jsonio
from agentic_trading.desk.book import MemberBook

DEFAULT_EQUITY = Decimal("50")
BOOK_NAME, MIRROR_NAME = "switchboard", "switchboard@coinbase"


class FastStore:
    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.book_path = self.state_dir / "desk" / "switchboard.json"
        self.mirror_path = self.state_dir / "fast" / "mirror.json"
        self.engine_path = self.state_dir / "fast" / "engine.json"

    def starting_equity(self) -> Decimal:
        """The desk members' starting equity, so the race is fair; else $50."""
        try:
            raw = json.loads((self.state_dir / "desk" / "account.json").read_text(encoding="utf-8"))
            value = Decimal(str(raw["starting_equity"]))
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            return DEFAULT_EQUITY
        return value if value.is_finite() and value > 0 else DEFAULT_EQUITY

    def load(self, now: datetime) -> tuple[MemberBook, MemberBook, dict[str, Any], list[str]]:
        notes: list[str] = []
        book = self._book(self.book_path, BOOK_NAME, self.starting_equity(), now, notes)
        mirror = self._book(self.mirror_path, MIRROR_NAME, book.starting_equity, now, notes)
        state: dict[str, Any] = {}
        if self.engine_path.is_file():
            try:
                raw = json.loads(self.engine_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                state = raw
            except (OSError, ValueError):
                self.move_aside(self.engine_path, now, notes)
        return book, mirror, state, notes

    def save(self, board: Any) -> None:
        board.book.save()
        if board.mirror is not None:
            board.mirror.book.save()
        jsonio.write_text(self.engine_path, jsonio.dumps(board.to_state(), indent=2) + "\n")

    def _book(self, path: Path, name: str, equity: Decimal, now: datetime, notes: list[str]) -> MemberBook:
        book, broken = MemberBook.load(path, name=name, starting_equity=equity)
        if broken:
            self.move_aside(path, now, notes)
        return book

    @staticmethod
    def move_aside(path: Path, now: datetime, notes: list[str]) -> None:
        target = path.with_name(f"{path.name}.corrupt-{now:%Y%m%dT%H%M%SZ}")
        try:
            path.rename(target)
            notes.append(f"{path.name} was unreadable: moved to {target.name}, starting fresh")
        except OSError as exc:
            notes.append(f"{path.name} was unreadable and could not be moved aside ({exc.strerror})")
