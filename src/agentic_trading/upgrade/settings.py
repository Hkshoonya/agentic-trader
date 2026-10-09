"""The ``[upgrade]`` table: off unless ``enabled = true``; misspelled keys are errors."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UpgradeConfig:
    enabled: bool = False
    canary_hours: int = 24
    max_files: int = 12
    max_lines: int = 400


LIMITS = {"canary_hours": (1, 168), "max_files": (1, 12), "max_lines": (1, 400)}  # the walls only tighten


def load_upgrade_config(path: Path | str) -> UpgradeConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("upgrade") or {}
    if not isinstance(raw, dict):
        raise ValueError("[upgrade] must be a table")
    unknown = sorted(set(raw) - {f.name for f in fields(UpgradeConfig)})
    if unknown:
        raise ValueError(f"[upgrade] has unknown keys {unknown}")
    values: dict[str, Any] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("upgrade.enabled must be true or false, without quotes")
        values["enabled"] = raw["enabled"]
    for key, (low, high) in LIMITS.items():
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"upgrade.{key} must be a whole number from {low} to {high}")
            values[key] = value
    return UpgradeConfig(**values)
