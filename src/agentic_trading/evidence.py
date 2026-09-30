"""Turn the walk-forward rig into a state artifact the console can show.

``walkforward.py`` answers the question; this module decides which bars it is
allowed to answer it on, stamps the answer so a stale report is obvious, and
writes it where the dashboard, the self-check, and the operator can all read
the same numbers.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading.config import Config
from agentic_trading.history import Bar, load_bars
from agentic_trading.history_sync import bar_stem
from agentic_trading.walkforward import (
    GATE_SIZE_GRID,
    SmallAccountFloor,
    build_evidence,
)

EVIDENCE_FILE = "strategy_evidence.json"
EVIDENCE_SCHEMA_VERSION = 4
FALLBACK_STARTING_EQUITY = Decimal("50")


def shadow_forward_stats(
    config: Config, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Prospective shadow performance reconstructed from simulated fills.

    Only shadow acceptances count. A live acceptance is an approval written
    before broker placement and may never fill, so treating it as forward
    performance would manufacture evidence. Prices are charged through the same
    current cost model as the historical walk-forward.
    """
    from agentic_trading.execution import cost_model_for
    from agentic_trading.journal import DecisionJournal

    costs = cost_model_for(config.state_dir)

    def charges(symbol: str) -> tuple[Decimal, Decimal]:
        model = costs.for_symbol(symbol)
        return (
            Decimal(str(model.per_side_bps)) / Decimal("10000"),
            max(Decimal("0"), Decimal(str(model.fee_per_order))),
        )

    held: dict[str, Decimal] = {}
    average_cost: dict[str, Decimal] = {}
    completed = wins = entries = invalid = 0
    realized = gross_profit = gross_loss = Decimal("0")
    first: Optional[datetime] = None
    last: Optional[datetime] = None

    for record in DecisionJournal(Path(config.journal_dir)).iter_all():
        if (
            record.get("event") != "accepted"
            or str(record.get("mode") or "shadow") == "live"
        ):
            continue
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        symbol = str(record.get("symbol") or intent.get("symbol") or "").upper()
        side = str(record.get("side") or intent.get("side") or "").lower()
        try:
            quantity = Decimal(str(record.get("quantity") or intent.get("quantity")))
            price = Decimal(str(record.get("ref_price") or intent.get("ref_price")))
        except (ArithmeticError, TypeError, ValueError):
            invalid += 1
            continue
        if not symbol or side not in ("buy", "sell") or quantity <= 0 or price <= 0:
            invalid += 1
            continue
        stamp = _record_time(record, intent)
        if stamp is not None:
            first = stamp if first is None else min(first, stamp)
            last = stamp if last is None else max(last, stamp)

        per_side, fee = charges(symbol)
        if side == "buy":
            previous = held.get(symbol, Decimal("0"))
            previous_cost = average_cost.get(symbol, Decimal("0"))
            cash_cost = quantity * price * (Decimal("1") + per_side) + fee
            new_quantity = previous + quantity
            average_cost[symbol] = (previous_cost * previous + cash_cost) / new_quantity
            held[symbol] = new_quantity
            entries += 1
            continue

        previous = held.get(symbol, Decimal("0"))
        if previous <= 0 or quantity > previous:
            invalid += 1
            continue
        proceeds = quantity * price * (Decimal("1") - per_side) - fee
        pnl = proceeds - average_cost.get(symbol, Decimal("0")) * quantity
        realized += pnl
        completed += 1
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)
        remaining = previous - quantity
        if remaining <= 0:
            held.pop(symbol, None)
            average_cost.pop(symbol, None)
        else:
            held[symbol] = remaining

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    observed_days = (
        max(0.0, (current.astimezone(timezone.utc) - first).total_seconds() / 86_400)
        if first is not None
        else 0.0
    )
    profit_factor = (
        float(gross_profit / gross_loss)
        if gross_loss > 0
        else (None if gross_profit <= 0 else float("inf"))
    )
    return {
        "source": "shadow_journal",
        "entries": entries,
        "completed_trades": completed,
        "wins": wins,
        "win_rate": round(wins / completed, 4) if completed else 0.0,
        "realized_pnl": str(realized.quantize(Decimal("0.0001"))),
        "profit_factor": profit_factor,
        "open_positions": len(held),
        "observed_days": round(observed_days, 2),
        "first_at": first.isoformat() if first is not None else "",
        "last_at": last.isoformat() if last is not None else "",
        "invalid_records": invalid,
        "cost_per_side_bps": float(costs.per_side_bps),
        "fee_per_order_usd": float(costs.fee_per_order),
        "crypto_cost_per_side_bps": float(costs.for_symbol("BTC-USD").per_side_bps),
        "crypto_fee_per_order_usd": float(costs.for_symbol("BTC-USD").fee_per_order),
    }


def _record_time(record: dict[str, Any], intent: dict[str, Any]) -> Optional[datetime]:
    raw = record.get("at") or intent.get("created_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def with_current_forward(config: Config, report: dict[str, Any]) -> dict[str, Any]:
    """Attach current prospective/cost evidence without rewriting the file."""
    return {
        **report,
        "forward": shadow_forward_stats(config),
        **report_runtime_checks(config, report),
    }


def report_runtime_checks(
    config: Config, report: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Cheap capital, cost, sizing and strategy-book checks for a report.

    Unlike :func:`with_current_forward`, this does not replay the journal. It is
    safe to call on the sizing path every cycle, where an old assessment must
    never authorize a newly enlarged floor or a changed trading book, even
    briefly during startup.
    """
    from agentic_trading.execution import cost_model_for

    current = cost_model_for(config.state_dir)
    reported = report.get("costs") if isinstance(report.get("costs"), dict) else {}
    reported_bps = float(reported.get("assumed_per_side_bps") or 0.0)
    reported_fee = float(reported.get("assumed_fee_per_order_usd") or 0.0)
    current_bps = float(current.per_side_bps)
    current_fee = float(current.fee_per_order)
    # A report written before costs were split by asset class charged its one
    # model to crypto too, so its top-level numbers stand in for the crypto ones.
    reported_crypto_bps = float(
        reported.get("assumed_crypto_per_side_bps", reported_bps) or 0.0
    )
    reported_crypto_fee = float(
        reported.get("assumed_crypto_fee_per_order_usd", reported_fee) or 0.0
    )
    current_crypto = current.for_symbol("BTC-USD")
    current_crypto_bps = float(current_crypto.per_side_bps)
    current_crypto_fee = float(current_crypto.fee_per_order)
    current_equity = float(_current_equity(config.state_dir))
    try:
        reported_equity = float(report.get("starting_equity") or 0.0)
    except (TypeError, ValueError):
        reported_equity = 0.0
    capital_current = (
        reported_equity > 0 and current_equity + 1e-9 >= reported_equity * 0.95
    )
    production = (
        (report.get("configs") or {}).get("production")
        if isinstance(report.get("configs"), dict)
        else {}
    ) or {}
    try:
        reported_size = float(production.get("per_order_pct") or 0.0)
    except (TypeError, ValueError):
        reported_size = 0.0
    current_size = effective_per_order_pct(config)
    # A report run at a larger size is conservative for the current book. A
    # report run at a smaller size is not evidence for the risk now requested.
    current_floor = small_account_floor_spec(config)
    reported_floor = (
        report.get("small_account_floor")
        if isinstance(report.get("small_account_floor"), dict)
        else None
    )
    floor_model_current = _floor_model_matches(reported_floor, current_floor)
    sizing_current = bool(
        reported_size > 0
        and current_size <= reported_size * 1.001 + 1e-12
        and floor_model_current
    )
    cost_current = (
        reported_bps + 1e-9 >= current_bps
        and reported_fee + 1e-9 >= current_fee
        and reported_crypto_bps + 1e-9 >= current_crypto_bps
        and reported_crypto_fee + 1e-9 >= current_crypto_fee
    )
    try:
        reported_positions = int(report.get("max_positions") or 0)
    except (TypeError, ValueError):
        reported_positions = 0
    series = report.get("series") if isinstance(report.get("series"), dict) else {}
    reported_symbols = {str(symbol).upper() for symbol in (series.get("symbols") or [])}
    current_symbols = {str(symbol).upper() for symbol in config.effective_whitelist}
    reported_base_sizing = str(report.get("base_sizing") or report.get("sizing") or "")
    current_base_sizing = str(getattr(config, "sizing", "flat"))
    model_current = bool(
        str(report.get("strategy") or "") == str(config.strategy)
        and reported_positions == int(config.max_open_positions)
        and reported_symbols == current_symbols
        and reported_base_sizing == current_base_sizing
    )
    return {
        "cost_check": {
            "current": cost_current,
            "reported_per_side_bps": reported_bps,
            "current_per_side_bps": current_bps,
            "reported_fee_per_order_usd": reported_fee,
            "current_fee_per_order_usd": current_fee,
            "reported_crypto_fee_per_order_usd": reported_crypto_fee,
            "current_crypto_fee_per_order_usd": current_crypto_fee,
        },
        "capital_check": {
            "current": capital_current,
            "reported_equity": reported_equity,
            "current_equity": current_equity,
            "minimum_current_equity": reported_equity * 0.95,
        },
        "sizing_check": {
            "current": sizing_current,
            "reported_per_order_pct": reported_size,
            "current_per_order_pct": current_size,
            "floor_model_current": floor_model_current,
            "reported_floor": reported_floor,
            "current_floor": (
                current_floor.to_dict() if current_floor is not None else None
            ),
            "small_account_target_notional": str(
                effective_small_account_target(config)
            ),
        },
        "model_check": {
            "current": model_current,
            "reported_strategy": report.get("strategy"),
            "current_strategy": config.strategy,
            "reported_max_positions": reported_positions,
            "current_max_positions": int(config.max_open_positions),
            "reported_sizing": reported_base_sizing,
            "current_sizing": current_base_sizing,
            "reported_symbols": sorted(reported_symbols),
            "current_symbols": sorted(current_symbols),
        },
    }


def retrospective_floor_ready(
    config: Config, assessment: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Whether a stored retrospective pass is still valid for today's $5 size.

    The assessment and report must be the same artifact, and that artifact must
    still cover current capital, costs and potential floor sizing. This closes
    the restart/config-change gap where a former 1% pass could otherwise be
    reused for a newly configured 10.2% order before the next refresh cycle.
    """
    evidence = (
        assessment.get("evidence")
        if isinstance(assessment.get("evidence"), dict)
        else {}
    )
    if not bool(evidence.get("retrospective_eligible")):
        reasons = list(evidence.get("retrospective_reasons") or [])
        return False, reasons or ["retrospective evidence has not passed"]
    report = read_report(config)
    if not report:
        return False, ["no walk-forward report is available"]
    if is_stale(report, max_age_days=30.0):
        return False, ["walk-forward report is stale or uses an obsolete schema"]
    if str(report.get("strategy") or config.strategy) != config.strategy:
        return False, ["walk-forward report was built for a different strategy"]
    assessed_at = str(evidence.get("report_generated_at") or "")
    reported_at = str(report.get("generated_at") or "")
    if not assessed_at or assessed_at != reported_at:
        return False, ["the current walk-forward report has not been assessed"]
    checks = report_runtime_checks(config, report)
    failures: list[str] = []
    if not checks["cost_check"]["current"]:
        failures.append("walk-forward execution-cost assumptions are stale")
    if not checks["capital_check"]["current"]:
        failures.append("walk-forward capital assumption is stale")
    if not checks["sizing_check"]["current"]:
        failures.append("walk-forward report did not test the current floor size")
    if not checks["model_check"]["current"]:
        failures.append("walk-forward report does not match the current strategy book")
    return not failures, failures


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
    explicit_size = per_order_pct is not None
    if per_order_pct is None:
        per_order_pct = effective_per_order_pct(config)
    from agentic_trading.execution import cost_model_for, load_report
    from agentic_trading.walkforward import rule_for_strategy

    costs = cost_model_for(config.state_dir)
    starting_equity = _current_equity(config.state_dir)
    # ``--per-order`` is a hypothetical percentage experiment. It must not
    # masquerade as evidence for the live floor, whose fixed-dollar and daily
    # cap behaviour is different; report_runtime_checks marks that deliberate
    # model mismatch stale for promotion. The normal autonomous build has no
    # override and reproduces the runtime floor exactly.
    floor_model = None if explicit_size else small_account_floor_spec(config)
    report = build_evidence(
        series,
        per_order_pct=per_order_pct,
        max_positions=max_positions or config.max_open_positions,
        folds=folds,
        grid=grid,
        starting_cash=float(starting_equity),
        costs=costs,
        # Grade the sizing the account actually trades. Quoting a flat-sizing
        # frontier next to a proportional book would misstate the drawdown.
        proportional=getattr(config, "sizing", "flat") == "proportional",
        small_account_floor=floor_model,
        rule=rule_for_strategy(config.strategy),
    )
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["history_path"] = str(config.history_path or "data/bars")
    report["strategy"] = config.strategy
    report["forward"] = shadow_forward_stats(config)
    measured = load_report(config.state_dir)
    cost_detail = report.setdefault("costs", {})
    if (
        measured is not None
        and measured.round_trips
        and (
            float(costs.fee_per_order) > 0
            or float(costs.for_symbol("BTC-USD").fee_per_order) > 0
        )
    ):
        cost_detail["source"] = "measured_round_trip_fixed_fee"
        cost_detail["measured_round_trips"] = len(measured.round_trips)
        cost_detail["measured_round_trip_bps"] = measured.measured_round_trip_bps
    elif measured is not None and measured.usable:
        cost_detail["source"] = "measured_fill_slippage"
        cost_detail["measured_fills"] = measured.fills
    else:
        cost_detail["source"] = "default_assumption"
    return report


def _current_equity(state_dir: Path | str) -> Decimal:
    """Capital the evidence must actually survive, with a safe fresh-install fallback.

    Percentage costs are scale-free; a measured fixed fee is not. Backtesting a
    permanent hard-coded $50 account would stay pessimistic after the account
    grows and could become optimistic after a loss. The durable account snapshot
    is refreshed by the daemon from the broker, so it is the right test capital.
    """
    from agentic_trading import account

    raw = account.load(state_dir).get("last_equity")
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, TypeError, ValueError):
        return FALLBACK_STARTING_EQUITY
    if not value.is_finite() or value <= 0:
        return FALLBACK_STARTING_EQUITY
    return value


def _policy_per_order_pct(config: Config) -> Decimal:
    """The evidence/confidence ladder's cap before any small-account floor."""
    path = Path(config.state_dir) / "effective_limits.json"
    try:
        payload = json.loads(path.read_text())
        value = Decimal(str(payload.get("max_order_pct", "")))
        if value.is_finite() and value > 0:
            return value
    except (ArithmeticError, OSError, ValueError, TypeError):
        pass
    return Decimal(str(config.max_order_pct))


def _policy_daily_notional_pct(config: Config) -> Decimal:
    """The confidence ladder's daily cap before any small-account floor."""
    path = Path(config.state_dir) / "effective_limits.json"
    try:
        payload = json.loads(path.read_text())
        value = Decimal(str(payload.get("daily_notional_pct", "")))
        if value.is_finite() and value > 0:
            return value
    except (ArithmeticError, OSError, ValueError, TypeError):
        pass
    return Decimal(str(config.daily_notional_pct))


def effective_small_account_target(config: Config) -> Decimal:
    """Minimum useful entry after broker rules and measured execution costs.

    The configured $5 research unit is a floor, not a promise to spend exactly
    $5 when the venue has proved more expensive. If a measured fixed round-trip
    cost needs a larger order to stay within ``max_cost_share_of_order``, that
    larger amount becomes the target. ``sizer.floor_cap`` adds its 2% rounding
    and price-drift margin when converting this dollar value to an equity share.
    """
    from agentic_trading.execution import required_notional_for_cost

    target = max(
        Decimal(str(config.min_order_notional)),
        Decimal(str(config.small_account_target_notional)),
    )
    required = required_notional_for_cost(
        config.state_dir,
        max_share=float(config.max_cost_share_of_order),
    )
    if required is not None:
        measured = Decimal(str(required))
        if measured.is_finite():
            target = max(target, measured)
    return target


def small_account_floor_spec(config: Config) -> Optional[SmallAccountFloor]:
    """Exact fixed-dollar floor policy the live sizer would apply today."""
    from agentic_trading.sizer import FLOOR_MARGIN

    order_ceiling = Decimal(str(config.small_account_max_order_pct))
    daily_ceiling = Decimal(str(config.small_account_max_daily_pct))
    if daily_ceiling <= 0:
        daily_ceiling = order_ceiling
    if order_ceiling <= 0 or daily_ceiling <= 0:
        return None
    return SmallAccountFloor(
        target_notional=float(effective_small_account_target(config)),
        policy_per_order_pct=float(_policy_per_order_pct(config)),
        policy_daily_notional_pct=float(_policy_daily_notional_pct(config)),
        max_order_pct=float(order_ceiling),
        max_daily_notional_pct=float(daily_ceiling),
        margin=float(FLOOR_MARGIN),
        proportional=getattr(config, "sizing", "flat") == "proportional",
    )


def _floor_model_matches(
    reported: Optional[dict[str, Any]], current: Optional[SmallAccountFloor]
) -> bool:
    """Strictly compare the sizing path, allowing only JSON float noise."""
    if current is None:
        return reported is None
    if reported is None:
        return False
    expected = current.to_dict()
    for key, wanted in expected.items():
        try:
            seen = float(reported.get(key))
        except (TypeError, ValueError):
            return False
        if not math.isclose(seen, float(wanted), rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True


def effective_per_order_pct(
    config: Config,
    *,
    policy_pct: Optional[Decimal] = None,
    equity: Optional[Decimal] = None,
) -> float:
    """Largest per-order share this account may use after the $5 floor.

    This is the *potential* production size, whether or not the strategy has
    qualified to activate it yet. Evidence must test that potential size before
    the runtime is allowed to use it; grading only the smaller policy cap would
    let the small-account rule bypass the drawdown gate.
    """
    from agentic_trading.sizer import floor_cap

    policy = (
        _policy_per_order_pct(config)
        if policy_pct is None
        else Decimal(str(policy_pct))
    )
    capital = (
        _current_equity(config.state_dir) if equity is None else Decimal(str(equity))
    )
    effective = floor_cap(
        equity=capital,
        policy_cap=policy,
        min_notional=effective_small_account_target(config),
        max_cap=Decimal(str(config.small_account_max_order_pct)),
    )
    return float(effective)


def report_path(config: Config) -> Path:
    return Path(config.state_dir) / EVIDENCE_FILE


def write_report(
    config: Config, report: dict[str, Any], *, out: Optional[str] = None
) -> Path:
    from agentic_trading import jsonio

    path = Path(out) if out else report_path(config)
    jsonio.write_text(path, jsonio.dumps(report, indent=2) + "\n")
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
    try:
        schema = int(report.get("schema_version") or 0)
    except (TypeError, ValueError):
        return True
    if schema < EVIDENCE_SCHEMA_VERSION:
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
    capital_stale = _capital_stale(config, existing)
    sizing_stale = _sizing_stale(config, existing)
    model_stale = _model_stale(config, existing)
    if (
        not is_stale(existing, max_age_days=max_age_days)
        and not capital_stale
        and not sizing_stale
        and not model_stale
    ):
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
    report["capital_refresh"] = capital_stale
    report["sizing_refresh"] = sizing_stale
    report["model_refresh"] = model_stale
    write_report(config, report)
    return report


def _capital_stale(config: Config, report: Optional[dict[str, Any]]) -> bool:
    """A lower account makes fixed costs larger, so old evidence is optimistic."""
    if not report or "starting_equity" not in report:
        return False
    try:
        reported = Decimal(str(report.get("starting_equity")))
    except (ArithmeticError, TypeError, ValueError):
        return True
    current = _current_equity(config.state_dir)
    if not reported.is_finite() or reported <= 0:
        return True
    return current < reported * Decimal("0.95")


def _sizing_stale(config: Config, report: Optional[dict[str, Any]]) -> bool:
    """A report for another size *or sizing path* cannot authorize this one."""
    if not report:
        return False
    return not bool(report_runtime_checks(config, report)["sizing_check"]["current"])


def _model_stale(config: Config, report: Optional[dict[str, Any]]) -> bool:
    """The strategy, slots, sizing mode and universe are evidence inputs."""
    if not report:
        return False
    return not bool(report_runtime_checks(config, report)["model_check"]["current"])
