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
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

_T = TypeVar("_T")

_SHARING_ATTEMPTS = 50


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


def _retry_sharing_lock(action: Callable[[], _T]) -> _T:
    """Run ``action``, riding out a Windows sharing violation.

    Windows refuses to replace a file another process has open, and refuses to
    open one while it is being replaced; both surface as ``PermissionError``
    for a few milliseconds. Left alone, a reader would treat that as "no state
    stored" (the agent's reduced budget reverting to the operator's ceiling)
    and a writer would drop the update. On POSIX a ``PermissionError`` is a
    real permission problem, so it is raised at once.
    """
    for attempt in range(_SHARING_ATTEMPTS):
        try:
            return action()
        except PermissionError:
            if os.name != "nt" or attempt == _SHARING_ATTEMPTS - 1:
                raise
            time.sleep(0.002 * (attempt + 1))
    raise AssertionError("unreachable")


def read_text(path: Path | str) -> str:
    """Read a file written by :func:`write_text`, never failing mid-swap."""
    source = Path(path)
    return _retry_sharing_lock(lambda: source.read_text(encoding="utf-8"))


def write_text(path: Path | str, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` so a concurrent reader never sees a half-written file.

    The daemon and the self-evaluation worker both read these state files while
    the other may be writing them. A torn read of ``effective_limits.json``
    fails to parse, which used to mean "no limits stored" — i.e. the agent's
    reduced budget silently reverted to the operator's ceiling. Rename is
    atomic on POSIX, so a reader sees either the old file or the new one. On
    Windows the rename waits out a reader holding the destination open.
    """
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(destination.parent), prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _retry_sharing_lock(lambda: os.replace(temporary, destination))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
