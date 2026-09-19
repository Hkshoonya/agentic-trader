"""Back-check agent: verify the backend, the data and the analysis path.

The bot can fail in ways that look like success. A stalled feed looks like a
quiet market. A truncated bar file looks like a strategy with no signals. A
broken analysis path looks like evidence that never improves. Every one of those
has actually happened in this repo, and each was found by a human noticing
something odd rather than by the system noticing it could not see.

So this runs the things the daemon depends on, end to end, and reports:

- **state**: every state file the loop reads still parses and is internally sane
  (caps inside their ceiling, confidence a probability, stage a known value)
- **data**: every whitelisted symbol has bars, recent enough to trade on, that
  pass the quality gate
- **analysis**: bars load and a backtest actually runs — proving the code path
  the evidence depends on, not just that files exist
- **broker**: read-only round trips still work (optional; skipped offline)
- **plumbing**: journal writable, notifier channels resolvable, console
  answering, risk caps not silently loosened

Checks never place orders and never mutate state. A failure is reported, not
repaired: fixing is the operator's call, or the daemon's existing self-healing.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading import jsonio
from agentic_trading.history_sync import check_records, quality_for
from agentic_trading.orders import is_crypto_symbol

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Daily bars: a file whose newest bar is older than this cannot be traded on.
MAX_BAR_AGE_DAYS = 4
MIN_BARS = 200


@dataclass
class Check:
    name: str
    status: str
    detail: str
    ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "ms": round(self.ms, 1),
        }


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    # Which process produced this report. The live loop only trusts a back-check
    # its own daemon wrote: on 2026-09-18 a report left behind by another
    # process disarmed the armed book ten minutes before its daily rebalance,
    # and the order it had already cleared was blocked.
    pid: int = 0

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status == WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "pid": self.pid or os.getpid(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": sum(1 for check in self.checks if check.status == OK),
            "warnings": [check.to_dict() for check in self.warnings],
            "failures": [check.to_dict() for check in self.failures],
            "checks": [check.to_dict() for check in self.checks],
        }


def _timed(name: str, fn: Callable[[], tuple[str, str]]) -> Check:
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 — a failing check is the finding
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"[:200]
    return Check(
        name=name,
        status=status,
        detail=detail,
        ms=(time.perf_counter() - started) * 1000,
    )


def _read(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def check_state_files(config: Any) -> Check:
    """State the loop reads must parse; caps must sit inside their ceiling."""
    state_dir = Path(config.state_dir)
    expected = ("promotion.json", "effective_limits.json", "risk_guard.json")

    def run() -> tuple[str, str]:
        missing: list[str] = []
        corrupt: list[str] = []
        for name in expected:
            path = state_dir / name
            if not path.is_file():
                missing.append(name)
            elif _read(path) is None:
                corrupt.append(name)
        if corrupt:
            return FAIL, f"unparseable: {', '.join(corrupt)}"

        limits = _read(state_dir / "effective_limits.json")
        problems: list[str] = []
        # Only sanity-check a budget that exists: a fresh install has not
        # computed one yet, which is "not started", not "broken".
        if isinstance(limits, dict):
            try:
                order_cap = Decimal(str(limits.get("max_order_pct")))
                daily_cap = Decimal(str(limits.get("daily_notional_pct")))
                ceiling_order = Decimal(str(config.max_order_pct))
                ceiling_daily = Decimal(str(config.daily_notional_pct))
                if order_cap > ceiling_order or daily_cap > ceiling_daily:
                    problems.append("stored budget exceeds the operator ceiling")
                confidence = float(limits.get("confidence", 0.0) or 0.0)
                if not 0.0 <= confidence <= 1.0:
                    problems.append(f"confidence out of range: {confidence}")
            except (TypeError, ValueError, ArithmeticError) as exc:
                problems.append(f"limits unreadable: {type(exc).__name__}")

        promotion = _read(state_dir / "promotion.json")
        if isinstance(promotion, dict) and promotion.get("stage") not in (
            "shadow",
            "probation",
            "live",
        ):
            problems.append(f"unknown stage {promotion.get('stage')!r}")
        if problems:
            return FAIL, "; ".join(problems)
        if missing:
            return WARN, f"not created yet: {', '.join(missing)}"
        return OK, f"{len(expected)} state files parse; budget within ceiling"

    return _timed("state", run)


def check_data(config: Any, *, max_bar_age_days: int = MAX_BAR_AGE_DAYS) -> Check:
    """Every tradable symbol needs recent bars that pass the quality gate."""

    def run() -> tuple[str, str]:
        directory = Path(config.history_path) if config.history_path else None
        if directory is None or not directory.is_dir():
            return FAIL, "no history_path configured"
        now = datetime.now(timezone.utc)
        missing: list[str] = []
        stale: list[str] = []
        thin: list[str] = []
        flagged: list[str] = []
        universe = config.effective_whitelist
        for symbol in universe:
            stem = symbol.replace("-", "").upper()
            path = directory / f"{stem}_day.jsonl"
            if not path.is_file():
                missing.append(symbol)
                continue
            report = quality_for(path, symbol=symbol)
            if report.bars < MIN_BARS:
                thin.append(f"{symbol}({report.bars})")
            try:
                newest = datetime.fromisoformat(report.last_start)
            except ValueError:
                stale.append(symbol)
                continue
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            if (now - newest).days > max_bar_age_days:
                stale.append(f"{symbol}({(now - newest).days}d)")
            if report.issues:
                flagged.append(f"{symbol}: {report.issues[0]}")
        if missing:
            return FAIL, f"no bars for {', '.join(missing)}"
        if stale:
            return FAIL, f"stale bars: {', '.join(stale)}"
        if thin:
            return WARN, f"short history: {', '.join(thin)}"
        if flagged:
            return WARN, f"quality flags — {'; '.join(flagged[:3])}"
        return OK, f"{len(universe)} symbols current"

    return _timed("data", run)


def check_analysis(config: Any) -> Check:
    """Bars must load and a backtest must run: files existing is not proof."""

    def run() -> tuple[str, str]:
        from agentic_trading.backtest import CostModel, run_backtest
        from agentic_trading.evolution import random_genome
        from agentic_trading.history import load_bars

        directory = Path(config.history_path) if config.history_path else None
        if directory is None:
            return FAIL, "no history_path configured"
        # The universe is a frozenset; pick deterministically so the check is
        # reproducible run to run.
        symbol = sorted(config.effective_whitelist)[0]
        stem = symbol.replace("-", "").upper()
        path = directory / f"{stem}_day.jsonl"
        if not path.is_file():
            return FAIL, f"no bars to analyse for {symbol}"
        bars = load_bars(path)
        if len(bars) < 60:
            return FAIL, f"only {len(bars)} bars for {symbol}"
        genome = random_genome(__import__("random").Random(7))
        metrics = run_backtest(
            bars[-500:], genome, costs=CostModel(), starting_cash=Decimal("50")
        )
        return OK, (
            f"backtest ran on {min(len(bars), 500)} bars of {symbol} "
            f"({metrics.trades} trades, equity {metrics.final_equity:.2f})"
        )

    return _timed("analysis", run)


def check_evidence(config: Any, *, max_age_days: int = 30) -> Check:
    """The evidence report must exist, be fresh, and still cover the size traded.

    This is the check that answers "is the analysis behind the order size still
    real?": if the walk-forward report is older than a month, or the book is now
    larger than the largest size the report found inside the drawdown ceiling,
    the number on the console no longer justifies the risk being taken.
    """

    def run() -> tuple[str, str]:
        from agentic_trading.evidence import read_report
        from agentic_trading.limits import load_limits

        report = read_report(config)
        if not report:
            return WARN, "no strategy_evidence.json yet (run: agentic-trading walkforward)"
        generated = str(report.get("generated_at") or "")
        try:
            age_days = (
                datetime.now(timezone.utc) - datetime.fromisoformat(generated)
            ).total_seconds() / 86_400
        except ValueError:
            return WARN, f"evidence report has unreadable timestamp {generated!r}"
        if age_days > max_age_days:
            return WARN, (
                f"evidence report is {age_days:.0f} days old "
                f"(refresh: agentic-trading walkforward)"
            )
        gate = report.get("gate_size") or {}
        stored = load_limits(config.state_dir)
        live = (
            float(stored.max_order_pct)
            if stored is not None
            else float(config.max_order_pct)
        )
        ceiling = float(gate.get("per_order_pct") or 0.0)
        if ceiling and live > ceiling * 1.001:
            return FAIL, (
                f"trading {live:.4f} per order but the evidence only supports "
                f"{ceiling:.4f} inside the {report.get('drawdown_ceiling_pct')}% "
                "drawdown ceiling"
            )
        gate_dd = gate.get("max_drawdown_pct")
        detail = (
            f"evidence {age_days:.0f}d old; {live * 100:.2f}%/order vs gate "
            f"{ceiling * 100:.2f}%"
        )
        if gate_dd is not None:
            detail += f" (maxDD {float(gate_dd):.1f}%)"
        return OK, detail

    return _timed("evidence", run)


def check_broker(config: Any, broker: Any) -> Check:
    """Read-only round trips: can we still see the account and the market?"""

    def run() -> tuple[str, str]:
        crypto = [s for s in config.effective_whitelist if is_crypto_symbol(s)][:6]
        equity_symbols = [
            s for s in config.effective_whitelist if not is_crypto_symbol(s)
        ][:20]
        # One retry: the first back-check runs seconds after a boot, and a cold
        # resolver can fail once while the daemon reconnects fine moments later.
        # Marking the whole system unhealthy for that is a false alarm.
        last_error: Optional[Exception] = None
        for attempt in range(2):
            started = time.perf_counter()
            try:
                equity = broker.get_equity()
                quotes = 0
                if crypto:
                    quotes += len(broker.get_crypto_quotes(crypto) or {})
                if equity_symbols:
                    quotes += len(broker.get_quotes(equity_symbols) or {})
            except Exception as exc:  # noqa: BLE001 — reported, not raised
                last_error = exc
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                raise
            elapsed = time.perf_counter() - started
            note = "" if attempt == 0 else " (after one retry)"
            return OK, (
                f"account {equity} in {elapsed * 1000:.0f}ms; "
                f"quotes for {len(crypto) + len(equity_symbols)} symbols{note}"
            )
        raise last_error if last_error else RuntimeError("broker check failed")

    return _timed("broker", run)


def check_plumbing(config: Any, *, port: Optional[int] = None) -> Check:
    """Journal writable, notifier resolvable, console answering."""

    def run() -> tuple[str, str]:
        notes: list[str] = []
        problems: list[str] = []
        journal_dir = Path(config.journal_dir)
        try:
            journal_dir.mkdir(parents=True, exist_ok=True)
            probe = journal_dir / ".selfcheck"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            notes.append("journal writable")
        except OSError as exc:
            problems.append(f"journal not writable: {exc}")

        from agentic_trading.notify import build_notifier

        notifier = build_notifier()
        if notifier is None:
            notes.append("alerts disabled")
        else:
            notes.append(
                "alerts via " + "+".join(channel.name for channel in notifier.channels)
            )

        from agentic_trading.llm.advisor import advisor_enabled

        notes.append("LLM advisor on" if advisor_enabled() else "LLM advisor off")

        resolved_port = port or int(os.environ.get("AGENTIC_DASHBOARD_PORT", "8787"))
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{resolved_port}/api/health", timeout=3
            ) as response:
                if response.status == 200:
                    notes.append("console answering")
                else:
                    notes.append(f"console returned {response.status}")
        except (urllib.error.URLError, OSError, ValueError):
            # A stopped console is not a broken agent: the daemon trades, journals
            # and self-evaluates with no dashboard attached. Reporting it as a
            # failure made a headless run look unhealthy — and made the check
            # pass on any machine where *some* console happened to answer on the
            # port, including another workspace's.
            notes.append(
                f"console not running on port {resolved_port} "
                "(start it with: agentic-trading dashboard)"
            )

        if problems:
            return FAIL, "; ".join(problems)
        return OK, "; ".join(notes)

    return _timed("plumbing", run)


def run_checks(
    config: Any,
    broker: Any = None,
    *,
    port: Optional[int] = None,
    include_broker: bool = True,
) -> HealthReport:
    """Run every check. ``include_broker=False`` keeps it offline (tests)."""
    report = HealthReport(started_at=datetime.now(timezone.utc).isoformat())
    report.checks.append(check_state_files(config))
    report.checks.append(check_data(config))
    report.checks.append(check_analysis(config))
    report.checks.append(check_evidence(config))
    if include_broker and broker is not None:
        report.checks.append(check_broker(config, broker))
    report.checks.append(check_plumbing(config, port=port))
    report.finished_at = datetime.now(timezone.utc).isoformat()
    return report


def write_report(config: Any, report: HealthReport) -> Path:
    path = Path(config.state_dir) / "health.json"
    jsonio.write_text(path, jsonio.dumps(report.to_dict(), indent=2) + "\n")
    return path
