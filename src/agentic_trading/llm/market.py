"""Ground the model in the tape instead of asking it to guess.

The advisor used to be shown a price and a quantity and nothing else, which
makes its veto a coin flip dressed as judgment. These features are computed
locally from the same bars the backtests use, so the model gets the context a
trader would actually look at: trend, volatility, where price sits in its
recent range, and how wide the spread is right now.

Everything here is descriptive. Nothing in this module decides anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

# Enough bars for a trend and a range without turning every prompt into a
# data dump.
LOOKBACK = 60
# Volume features need most bars to carry real volume; below this they are
# dropped for that symbol rather than reported as signal.
VOLUME_COVERAGE_FLOOR = 0.8

# Bars are written undashed (BTCUSD_day.jsonl) by the fetcher, while the
# whitelist and the crypto tools use pairs (BTC-USD). Try both.
_INTERVALS = ("day", "5minute")


@dataclass(frozen=True)
class MarketFeatures:
    symbol: str
    bars: int
    last_close: float
    ret_1_pct: float
    ret_5_pct: float
    ret_20_pct: float
    vol_pct: float
    trend_pct: float
    from_high_pct: float
    range_position: float  # 0 = at the low, 1 = at the high
    spread_bps: Optional[float]
    # Volume is only reported when the bar file actually carries it: some
    # crypto files record zero volume on 80%+ of days, and a z-score computed
    # on that is an artefact, not activity.
    volume_z: Optional[float] = None
    volume_trend: Optional[float] = None
    volume_coverage: float = 0.0

    def describe(self) -> list[str]:
        """One ``key=value`` line per feature, for the prompt."""
        lines = [
            f"bars_used={self.bars}",
            f"last_close={self.last_close:.6g}",
            f"return_1bar_pct={self.ret_1_pct:+.2f}",
            f"return_5bar_pct={self.ret_5_pct:+.2f}",
            f"return_20bar_pct={self.ret_20_pct:+.2f}",
            f"realized_vol_per_bar_pct={self.vol_pct:.2f}",
            f"trend_slope_20bar_pct={self.trend_pct:+.2f}",
            f"below_recent_high_pct={self.from_high_pct:.2f}",
            f"range_position_0low_1high={self.range_position:.2f}",
        ]
        if self.spread_bps is not None:
            lines.append(f"spread_bps={self.spread_bps:.1f}")
        if self.volume_z is not None:
            lines.append(f"volume_z_score={self.volume_z:+.2f}")
        if self.volume_trend is not None:
            lines.append(f"volume_5v20_ratio={self.volume_trend:.2f}")
        if self.volume_coverage and self.volume_z is None:
            lines.append(
                f"volume=unreliable (present on {self.volume_coverage:.0%} of bars)"
            )
        return lines

    def to_dict(self) -> dict[str, Any]:
        """The same numbers as a record, so a verdict can be audited later.

        Without this the journal shows what the model *said* but not what it was
        shown, which makes an AI decision impossible to check or reproduce.
        """
        return {
            "bars": self.bars,
            "last_close": round(self.last_close, 6),
            "ret_1_pct": round(self.ret_1_pct, 3),
            "ret_5_pct": round(self.ret_5_pct, 3),
            "ret_20_pct": round(self.ret_20_pct, 3),
            "vol_pct": round(self.vol_pct, 3),
            "trend_pct": round(self.trend_pct, 3),
            "from_high_pct": round(self.from_high_pct, 3),
            "range_position": round(self.range_position, 3),
            "volume_z": None if self.volume_z is None else round(self.volume_z, 3),
            "volume_coverage": round(self.volume_coverage, 3),
            "spread_bps": (
                None if self.spread_bps is None else round(self.spread_bps, 3)
            ),
        }


def _tone(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"


def _short_lines(features: MarketFeatures) -> list[str]:
    return features.describe()


def bar_path(history_path: Path | str, symbol: str, interval: str) -> Optional[Path]:
    directory = Path(history_path)
    candidates = [
        directory / f"{symbol.replace('-', '').upper()}_{interval}.jsonl",
        directory / f"{symbol.upper()}_{interval}.jsonl",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_closes(path: Path, limit: int) -> tuple[list[float], list[float]]:
    """Tail the file: the newest ``limit`` closes (oldest first) and their volumes."""
    rows: list[str] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    rows.append(line)
                    if len(rows) > limit * 2:
                        del rows[: limit]
    except OSError:
        return [], []

    import json

    closes: list[float] = []
    volumes: list[float] = []
    for line in rows[-limit:]:
        try:
            payload = json.loads(line)
            close = float(payload["close"])
        except (ValueError, KeyError, TypeError):
            continue
        if math.isfinite(close) and close > 0:
            closes.append(close)
            try:
                volumes.append(float(payload.get("volume") or 0.0))
            except (TypeError, ValueError):
                volumes.append(0.0)
    return closes, volumes


def _stdev(values: Iterable[float]) -> float:
    data = list(values)
    if len(data) < 2:
        return 0.0
    mean = sum(data) / len(data)
    variance = sum((value - mean) ** 2 for value in data) / (len(data) - 1)
    return math.sqrt(max(variance, 0.0))


def features_from_closes(
    symbol: str,
    closes: list[float],
    *,
    spread_bps: Optional[float] = None,
    volumes: Optional[list[float]] = None,
) -> Optional[MarketFeatures]:
    if len(closes) < 6:
        return None
    window = closes[-LOOKBACK:] if len(closes) > LOOKBACK else closes
    last = window[-1]
    returns = [
        (window[i] / window[i - 1] - 1.0) * 100.0
        for i in range(1, len(window))
        if window[i - 1] > 0
    ]

    def pct_change(bars: int) -> float:
        if len(window) <= bars or window[-bars - 1] <= 0:
            return 0.0
        return (last / window[-bars - 1] - 1.0) * 100.0

    # Slope of a least-squares line through the last 20 bars, expressed as the
    # total % move the line implies across that span (not the raw per-bar slope,
    # which is unreadable at these price levels).
    trend_window = window[-20:] if len(window) >= 20 else window
    count = len(trend_window)
    mean_x = (count - 1) / 2
    mean_y = sum(trend_window) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    slope = (
        sum(
            (index - mean_x) * (value - mean_y)
            for index, value in enumerate(trend_window)
        )
        / denominator
        if denominator
        else 0.0
    )
    trend_pct = ((slope * count) / last) * 100.0 if last else 0.0
    high, low = max(window), min(window)
    span = high - low
    volume_z, volume_trend, volume_coverage = _volume_features(volumes, len(window))
    return MarketFeatures(
        symbol=symbol,
        bars=len(window),
        last_close=last,
        ret_1_pct=pct_change(1),
        ret_5_pct=pct_change(5),
        ret_20_pct=pct_change(20),
        vol_pct=_stdev(returns),
        trend_pct=trend_pct,
        from_high_pct=((high - last) / high) * 100.0 if high else 0.0,
        range_position=((last - low) / span) if span else 0.5,
        spread_bps=spread_bps,
        volume_z=volume_z,
        volume_trend=volume_trend,
        volume_coverage=volume_coverage,
    )


def _volume_features(
    volumes: Optional[list[float]], window: int
) -> tuple[Optional[float], Optional[float], float]:
    """(z-score, 5-vs-20 ratio, coverage). All ``None`` when unusable."""
    if not volumes:
        return None, None, 0.0
    recent = volumes[-window:] if len(volumes) > window else list(volumes)
    positive = [value for value in recent if value > 0]
    coverage = len(positive) / len(recent) if recent else 0.0
    if coverage < VOLUME_COVERAGE_FLOOR or len(positive) < 21:
        return None, None, coverage
    mean = sum(positive) / len(positive)
    spread = _stdev(positive)
    last = recent[-1]
    z_score = (last - mean) / spread if spread > 0 else 0.0
    last_five = [value for value in recent[-5:] if value > 0]
    last_twenty = [value for value in recent[-20:] if value > 0]
    trend = (
        (sum(last_five) / len(last_five)) / (sum(last_twenty) / len(last_twenty))
        if last_five and last_twenty and sum(last_twenty) > 0
        else 1.0
    )
    return z_score, trend, coverage


def features_for(
    symbol: str,
    *,
    history_path: Path | str | None,
    quote: Optional[dict[str, Any]] = None,
) -> Optional[MarketFeatures]:
    """Features for ``symbol`` from local bars, plus the live spread if given."""
    spread_bps: Optional[float] = None
    if quote:
        bid, ask = quote.get("bid"), quote.get("ask")
        try:
            bid_f, ask_f = float(bid), float(ask)
            mid = (bid_f + ask_f) / 2
            if mid > 0 and ask_f >= bid_f:
                spread_bps = (ask_f - bid_f) / mid * 10_000
        except (TypeError, ValueError):
            spread_bps = None
    if not history_path:
        return None
    for interval in _INTERVALS:
        path = bar_path(history_path, symbol, interval)
        if path is None:
            continue
        closes, volumes = _load_closes(path, LOOKBACK)
        features = features_from_closes(
            symbol, closes, spread_bps=spread_bps, volumes=volumes
        )
        if features is not None:
            return features
    return None


def context_lines(features: Optional[MarketFeatures]) -> list[str]:
    """Prompt lines for the advisor; empty when no bars are available."""
    if features is None:
        return ["market_context=unavailable"]
    return ["market_context:"] + _short_lines(features)
