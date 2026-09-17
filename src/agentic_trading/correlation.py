"""How many separate bets is the book actually taking?

Six crypto majors and ten liquid equities are not sixteen independent
opportunities. BTC/ETH/SOL/LINK/LTC/BCH move together, and so do broad equity
indices and megacaps on a risk-off day. A position limit counted in *positions*
therefore understates concentration: four correlated symbols is one bet taken
four times, which is exactly how a small account takes a large loss.

This measures pairwise correlation on the same bars the evidence uses, so the
guard can limit *clusters* rather than names.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading import jsonio
from agentic_trading.history_sync import bar_stem

FILE_NAME = "correlations.json"
DEFAULT_LOOKBACK = 120


@dataclass
class CorrelationState:
    threshold: float
    lookback: int
    updated_at: str
    pairs: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def key(left: str, right: str) -> str:
        first, second = sorted((bar_stem(left), bar_stem(right)))
        return f"{first}|{second}"

    def get(self, left: str, right: str) -> Optional[float]:
        if bar_stem(left) == bar_stem(right):
            return 1.0
        return self.pairs.get(self.key(left, right))

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "lookback": self.lookback,
            "updated_at": self.updated_at,
            "pairs": dict(self.pairs),
        }


def correlation(left: list[float], right: list[float]) -> Optional[float]:
    """Pearson correlation of two return series, or ``None`` if undefined."""
    size = min(len(left), len(right))
    if size < 10:
        return None
    left, right = left[-size:], right[-size:]
    mean_left = sum(left) / size
    mean_right = sum(right) / size
    covariance = sum(
        (left[index] - mean_left) * (right[index] - mean_right)
        for index in range(size)
    )
    variance_left = sum((value - mean_left) ** 2 for value in left)
    variance_right = sum((value - mean_right) ** 2 for value in right)
    denominator = math.sqrt(variance_left * variance_right)
    if denominator <= 0:
        return None
    return max(-1.0, min(1.0, covariance / denominator))


def returns_from_closes(closes: list[float]) -> list[float]:
    return [
        (closes[index] / closes[index - 1] - 1.0)
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]


def load_closes(path: Path, lookback: int) -> list[float]:
    closes: list[float] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-(lookback + 1) :]:
        if not line.strip():
            continue
        try:
            close = float(json.loads(line)["close"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if close > 0:
            closes.append(close)
    return closes


def compute_state(
    history_path: Path | str,
    symbols: Iterable[str],
    *,
    lookback: int = DEFAULT_LOOKBACK,
    threshold: float = 0.7,
    interval: str = "day",
) -> CorrelationState:
    """Pairwise correlations between the symbols that have bars on disk."""
    directory = Path(history_path)
    series: dict[str, list[float]] = {}
    for symbol in symbols:
        stem = bar_stem(symbol)
        closes = load_closes(directory / f"{stem}_{interval}.jsonl", lookback)
        if len(closes) >= 11:
            series[stem] = returns_from_closes(closes)

    pairs: dict[str, float] = {}
    names = sorted(series)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            value = correlation(series[left], series[right])
            if value is not None:
                pairs[f"{left}|{right}"] = round(value, 4)
    return CorrelationState(
        threshold=threshold,
        lookback=lookback,
        updated_at=datetime.now(timezone.utc).isoformat(),
        pairs=pairs,
    )


def save_state(state_dir: Path | str, state: CorrelationState) -> Path:
    path = Path(state_dir) / FILE_NAME
    jsonio.write_text(path, jsonio.dumps(state.to_dict(), indent=2) + "\n")
    return path


def load_state(state_dir: Path | str) -> Optional[CorrelationState]:
    path = Path(state_dir) / FILE_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    pairs = raw.get("pairs")
    if not isinstance(pairs, dict):
        return None
    try:
        return CorrelationState(
            threshold=float(raw.get("threshold", 0.7)),
            lookback=int(raw.get("lookback", DEFAULT_LOOKBACK)),
            updated_at=str(raw.get("updated_at", "")),
            pairs={str(k): float(v) for k, v in pairs.items()},
        )
    except (TypeError, ValueError):
        return None


def correlated_with(
    candidate: str,
    held: Iterable[str],
    state: Optional[CorrelationState],
    *,
    threshold: Optional[float] = None,
) -> tuple[list[str], list[str]]:
    """Split held symbols into ``(correlated, unknown)`` against the candidate.

    An unknown pair counts as correlated: the guard must not hand out extra
    room on the strength of a correlation nobody measured.
    """
    limit = threshold if threshold is not None else (
        state.threshold if state is not None else 0.7
    )
    correlated: list[str] = []
    unknown: list[str] = []
    for symbol in held:
        if bar_stem(symbol) == bar_stem(candidate):
            correlated.append(symbol)
            continue
        value = state.get(candidate, symbol) if state is not None else None
        if value is None:
            unknown.append(symbol)
        elif abs(value) >= limit:
            correlated.append(symbol)
    return correlated, unknown
