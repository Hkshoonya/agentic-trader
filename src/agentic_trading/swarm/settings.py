"""The ``[swarm]`` table of ``agentic.toml``: the swarm's population rules.

Off unless ``enabled = true``. A misspelled key is an error, and booleans must
be real TOML booleans, as in ``[fast]``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentic_trading.fast.settings import _decimal, _whole


@dataclass(frozen=True)
class SwarmConfig:
    enabled: bool = False
    max_agents: int = 24
    screens_per_day: int = 12
    nursery_days: int = 20
    cull_after_days: int = 60
    max_drawdown: Decimal = Decimal("0.25")
    llm_scout: bool = False
    llm_proposals_per_week: int = 3
    seed: int = 7


WHOLE = {"max_agents": (1, 100), "screens_per_day": (1, 100), "nursery_days": (1, 365),
         "cull_after_days": (1, 3650), "llm_proposals_per_week": (0, 50), "seed": (0, 2**31 - 1)}


def _swarm(call: Any, *args: Any) -> Any:
    """The fast table's checks, with their messages naming ``swarm.``."""
    try:
        return call(*args)
    except ValueError as exc:
        raise ValueError(str(exc).replace("fast.", "swarm.", 1)) from None


def load_swarm_config(path: Path | str) -> SwarmConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("swarm") or {}
    if not isinstance(raw, dict):
        raise ValueError("[swarm] must be a table")
    known = {f.name for f in fields(SwarmConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[swarm] has unknown keys {unknown}; known: {sorted(known)}")
    values: dict[str, Any] = {}
    for key in ("enabled", "llm_scout"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"swarm.{key} must be true or false, without quotes")
            values[key] = raw[key]
    for key, (low, high) in WHOLE.items():
        if key in raw:
            values[key] = _swarm(_whole, key, raw[key], low, high)
    if "max_drawdown" in raw:
        values["max_drawdown"] = _swarm(_decimal, "max_drawdown", raw["max_drawdown"], Decimal("0.05"), Decimal("0.8"))
    return SwarmConfig(**values)
