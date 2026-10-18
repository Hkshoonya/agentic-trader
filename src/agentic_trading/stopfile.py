"""Stop on request from the Windows host: when ``AGENTIC_STOP_FILE`` appears, raise SIGINT here.

Windows has no SIGTERM to send, and a console control event never reaches a process on another
console, so the host asks by creating a file instead. Raising SIGINT in this process runs the same
handlers Ctrl+C and systemd's stop do: the daemon finishes its tick, the venues loop cancels cleanly.
Without the variable (every Linux unit, every manual run) nothing is started.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

ENV = "AGENTIC_STOP_FILE"


def watch(environ: Mapping[str, str] = os.environ, interval: float = 0.5) -> Optional[threading.Thread]:
    named = environ.get(ENV)
    if not named:
        return None
    path = Path(named)

    def loop() -> None:
        while not path.exists():
            time.sleep(interval)
        signal.raise_signal(signal.SIGINT)

    thread = threading.Thread(target=loop, daemon=True, name="stop-file")
    thread.start()
    return thread
