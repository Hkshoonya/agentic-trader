"""The ``[fast]`` table of ``agentic.toml``: the switchboard's settings.

Off unless ``enabled = true``. As in ``[venues]``, a misspelled key is an
error. Booleans must be real TOML booleans, because ``enabled = "false"`` (a
string) would otherwise read as true.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_CRYPTO = re.compile(r"^[A-Z]{2,10}/USD$")
MAX_FEE = Decimal("0.02")


@dataclass(frozen=True)
class FastConfig:
    enabled: bool = False
    symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")
    alpaca_fee: Decimal = Decimal("0.0025")
    coinbase_fee: Decimal = Decimal("0.006")
    fill_delay_ms: int = 250
    cost_gate_multiple: Decimal = Decimal("3")
    max_positions: int = 3
    cooldown_minutes: float = 5.0
    daily_loss_stop: Decimal = Decimal("0.03")


def _decimal(name: str, value: Any, low: Decimal, high: Decimal) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"fast.{name} must be a number") from None
    if not number.is_finite() or not low <= number <= high:
        raise ValueError(f"fast.{name} must be between {low} and {high}")
    return number


def _whole(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"fast.{name} must be a whole number from {low} to {high}")
    return value


def load_fast_config(path: Path | str) -> FastConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("fast") or {}
    if not isinstance(raw, dict):
        raise ValueError("[fast] must be a table")
    known = {f.name for f in fields(FastConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[fast] has unknown keys {unknown}; known: {sorted(known)}")
    values: dict[str, Any] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("fast.enabled must be true or false, without quotes")
        values["enabled"] = raw["enabled"]
    if "symbols" in raw:
        symbols = raw["symbols"]
        if not isinstance(symbols, list) or not symbols:
            raise ValueError("fast.symbols must be a non-empty list")
        cleaned = tuple(str(item).strip().upper() for item in symbols)
        bad = [s for s in cleaned if not _CRYPTO.match(s)]
        if bad:
            raise ValueError(f"fast.symbols must look like BTC/USD: {bad}")
        values["symbols"] = cleaned
    for key in ("alpaca_fee", "coinbase_fee"):
        if key in raw:
            values[key] = _decimal(key, raw[key], Decimal("0"), MAX_FEE)
    if "cost_gate_multiple" in raw:
        values["cost_gate_multiple"] = _decimal(
            "cost_gate_multiple", raw["cost_gate_multiple"], Decimal("1"), Decimal("20"))
    if "daily_loss_stop" in raw:
        values["daily_loss_stop"] = _decimal(
            "daily_loss_stop", raw["daily_loss_stop"], Decimal("0.001"), Decimal("0.5"))
    if "fill_delay_ms" in raw:
        values["fill_delay_ms"] = _whole("fill_delay_ms", raw["fill_delay_ms"], 0, 5000)
    if "max_positions" in raw:
        values["max_positions"] = _whole("max_positions", raw["max_positions"], 1, 10)
    if "cooldown_minutes" in raw:
        minutes = raw["cooldown_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 <= minutes <= 1440:
            raise ValueError("fast.cooldown_minutes must be from 0 to 1440")
        values["cooldown_minutes"] = float(minutes)
    return FastConfig(**values)
