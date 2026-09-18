"""Turn the walk-forward rig into a state artifact the console can show.

``walkforward.py`` answers the question; this module decides which bars it is
allowed to answer it on, stamps the answer so a stale report is obvious, and
writes it where the dashboard, the self-check, and the operator can all read
the same numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading.config import Config
from agentic_trading.history import Bar, load_bars
from agentic_trading.history_sync import bar_stem
from agentic_trading.walkforward import GATE_SIZE_GRID, build_evidence

EVIDENCE_FILE = "strategy_evidence.json"


def load_series(
    config: Config,
    *,
    symbols: Optional[Iterable[str]] = None,
    interval: str = "day",
) -> dict[str, list[Bar]]:
    """Every whitelisted symbol that has a usable bar file, in config order."""
    directory = Path(config.history_path or "data/bars")
    # The effective universe: the evidence gate must price the book that
    # actually trades, including whatever the scout has adopted.
    wanted = [s.upper() for s in (symbols or config.effective_whitelist)]
    series: dict[str, list[Bar]] = {}
    for symbol in wanted:
        path = directory / f"{bar_stem(symbol)}_{interval}.jsonl"
        if not path.is_file():
            continue
        try:
            bars = load_bars(path)
        except Exception:  # noqa: BLE001 — one bad file must not hide the rest
            continue
        if bars:
            series[symbol] = bars
    return series


def build_report(
    config: Config,
    *,
    per_order_pct: Optional[float] = None,
    max_positions: Optional[int] = None,
    folds: int = 6,
    grid: tuple[float, ...] = GATE_SIZE_GRID,
    symbols: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """The full evidence report, priced on the configured universe.

    ``per_order_pct`` defaults to what the live ladder would size *today* (the
    effective per-order ceiling), not the configured ceiling — the report must
    describe the book that is actually running.
    """
    series = load_series(config, symbols=symbols)
    if not series:
        raise RuntimeError(
            f"no bar files under {config.history_path or 'data/bars'} for "
            f"{len(list(config.effective_whitelist))} whitelisted symbols"
        )
    if per_order_pct is None:
        per_order_pct = _effective_per_order_pct(config)
    report = build_evidence(
        series,
        per_order_pct=per_order_pct,
        max_positions=max_positions or config.max_open_positions,
        folds=folds,
        grid=grid,
        starting_cash=50.0,
        # Grade the sizing the account actually trades. Quoting a flat-sizing
        # frontier next to a proportional book would misstate the drawdown.
        proportional=getattr(config, "sizing", "flat") == "proportional",
    )
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["history_path"] = str(config.history_path or "data/bars")
    return report


def _effective_per_order_pct(config: Config) -> float:
    """What the risk ladder is sizing today, falling back to the config ceiling."""
    path = Path(config.state_dir) / "effective_limits.json"
    try:
        payload = json.loads(path.read_text())
        value = float(payload.get("max_order_pct", ""))
        if value > 0:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return float(config.max_order_pct)


def report_path(config: Config) -> Path:
    return Path(config.state_dir) / EVIDENCE_FILE


def write_report(config: Config, report: dict[str, Any], *, out: Optional[str] = None) -> Path:
    path = Path(out) if out else report_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str) + "\n")
    tmp.replace(path)
    return path


def read_report(config: Config) -> Optional[dict[str, Any]]:
    path = report_path(config)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def report_age_days(
    report: Optional[dict[str, Any]], *, now: Optional[datetime] = None
) -> Optional[float]:
    """How old the report is, or ``None`` when its timestamp is unusable."""
    stamp = str((report or {}).get("generated_at") or "")
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - seen).total_seconds() / 86_400


def is_stale(
    report: Optional[dict[str, Any]],
    *,
    max_age_days: float,
    now: Optional[datetime] = None,
) -> bool:
    """Stale covers missing and unreadable, so a bad report self-heals.

    The promotion gate refuses to act on a stale report, which is the right
    behaviour and, without this, also a deadline: the agent would strand itself
    the first time nobody regenerated the numbers.
    """
    if not report:
        return True
    age = report_age_days(report, now=now)
    return age is None or age > max_age_days


def refresh_if_stale(
    config: Config,
    *,
    max_age_days: float,
    max_positions: Optional[int] = None,
    folds: int = 6,
    grid: tuple[float, ...] = GATE_SIZE_GRID,
    symbols: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    """Rebuild the evidence report when it is missing or too old.

    Returns the new report when one was built, ``None`` when the existing one is
    still current. Any failure propagates to the caller: the daemon journals it
    rather than trading on a report it could not produce.
    """
    existing = read_report(config)
    if not is_stale(existing, max_age_days=max_age_days):
        return None
    report = build_report(
        config,
        max_positions=max_positions,
        folds=folds,
        grid=grid,
        symbols=symbols,
    )
    report["age_at_build_days"] = report_age_days(existing)
    report["refresh_max_age_days"] = max_age_days
    write_report(config, report)
    return report
