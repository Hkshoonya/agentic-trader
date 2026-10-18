"""A non-blocking exclusive lock on a file, the same on Linux and Windows.

``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. Both are released by the OS when the holder
exits, so a crashed job never leaves a stale lock behind.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive(path: Path | str) -> Iterator[bool]:
    """Yield True while this process holds ``path``'s lock, False if another process holds it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a+b") as handle:
        if not _acquire(handle):
            yield False
            return
        try:
            yield True
        finally:
            _release(handle)


if os.name == "nt":
    import msvcrt

    def _acquire(handle) -> bool:
        handle.seek(0)  # msvcrt locks a byte range from the current position: always byte 0
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _release(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _acquire(handle) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _release(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)
