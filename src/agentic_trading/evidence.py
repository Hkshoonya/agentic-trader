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
    wanted = [s.upper() for s in (symbols or config.symbol_whitelist)]
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
            f"{len(list(config.symbol_whitelist))} whitelisted symbols"
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
