"""Strict JSON encoding for state files and the dashboard wire.

Python's ``json`` emits ``Infinity``, ``-Infinity`` and ``NaN`` by default. None
of those are valid JSON, so a single non-finite metric (a backtest with no
losing trades reports an infinite profit factor) produces a file that browsers,
``jq`` and other strict readers reject outright. One such field blanked every
panel of the operator console, so both the writers and the wire go through here.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def finite(value: Any) -> Any:
    """Return ``value`` with every non-finite float replaced by ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(item) for item in value]
    return value


def dumps(payload: Any, *, indent: Optional[int] = None) -> str:
    """``json.dumps`` that can never emit a non-standard number."""
    return json.dumps(finite(payload), indent=indent, default=str, allow_nan=False)


def write_text(path: Path | str, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` so a concurrent reader never sees a half-written file.

    The daemon and the self-evaluation worker both read these state files while
    the other may be writing them. A torn read of ``effective_limits.json``
    fails to parse, which used to mean "no limits stored" — i.e. the agent's
    reduced budget silently reverted to the operator's ceiling. Rename is
    atomic on POSIX, so a reader sees either the old file or the new one.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
