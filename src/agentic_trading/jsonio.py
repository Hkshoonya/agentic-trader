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
