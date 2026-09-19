from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tomllib
from typing import Optional


@dataclass(frozen=True)
class Config:
    mode: str
    symbol_whitelist: frozenset[str]
    max_order_pct: Decimal
    daily_notional_pct: Decimal
    daily_loss_pct: Decimal
    max_open_positions: int
    equity_refresh_ticks: int
    equity_refresh_seconds: int
    timezone: str
    quotes_path: Path
    journal_dir: Path
    state_dir: Path
    tools_snapshot_path: Path
    token_path: Path
    mcp_url: str
    strategy: str = "fixture"
    scalper_config: Path | None = None
    # Phase 3 — autonomous loop
    quote_source: str = "file"  # file | mcp
    poll_seconds: float = 5.0
    session_policy: str = "regular"  # regular | extended | all | any
    # Widest session policy the agent may move to on its own. Empty means "no
    # wider than session_policy", so widening trading hours is opt-in.
    max_session_policy: str = ""
    account_number: str | None = None
    order_type: str = "market"  # market (regular hours) | limit (marketable)
    max_orders_per_day: int = 10
    max_consecutive_errors: int = 3
    max_quote_age_seconds: float = 60.0
    # Every broker round trip costs ~1.3s over the remote MCP gateway, so the
    # open-order read (2 calls once crypto is in play) is throttled instead of
    # running on every poll.
    open_order_refresh_seconds: float = 15.0
    # Cadence heartbeat: how often the daemon journals measured cycle timing.
    cycle_stats_seconds: float = 60.0
    # How often the off-path worker refreshes one batch of LLM regime views.
    # Each refresh is a model call, so this trades freshness against API traffic.
    regime_refresh_seconds: float = 180.0
    # How often the daemon re-fetches daily bars so the evidence can change.
    # Without this the evaluation re-runs the same search over frozen files and
    # confidence can never move.
    history_refresh_hours: float = 24.0
    # How often the back-check agent verifies data, state and analysis paths.
    selfcheck_minutes: float = 30.0
    # Concentration limit: how many *correlated* positions the book may hold.
    # None leaves max_open_positions as the only limit.
    max_correlated_positions: Optional[int] = None
    correlation_threshold: float = 0.7
    correlation_lookback_days: int = 120
    # A once-a-day strategy gets one shot at the day. When every order in that
    # shot is refused for a *technical* reason — the account value was not read
    # yet, the broker's position book failed, the session was not armed — the
    # day's decision is handed back to the strategy instead of being spent, and
    # retried after this many seconds, at most this many times.
    rebalance_retry_seconds: float = 900.0
    rebalance_max_retries: int = 6
    # An order that passed every judgement but could not be submitted (the book
    # was not armed, or the broker review call failed) is held and resubmitted
    # when the obstacle clears — the models are never re-asked, so this cannot
    # become a way to shop for a different answer. Dropped after this age, or
    # after this many attempts.
    deferred_max_age_seconds: float = 3600.0
    deferred_max_attempts: int = 3
    # The most of an order a measured round trip may cost. A real $5 XLM round
    # trip cost $0.10 — 200 bps — while the quoted spread was 8 bps, so the
    # venue's part is much closer to a fixed charge than a percentage: at $1 the
    # same trip is 10% of the order and no edge survives it. An entry whose
    # notional cannot keep the measured cost inside this share is refused rather
    # than scaled up (see ``execution.required_notional_for_cost``). 0 disables
    # the rule; with no measured round trip it can never fire.
    max_cost_share_of_order: Decimal = Decimal("0.02")
    # The daily-budget schedule deliberately trades a size the drawdown evidence
    # does not support: at $50 the schedule is 22% per order where the evidence
    # supports ~3% inside its 15% drawdown ceiling. That is the operator's
    # decision to make — it is their account — but it must be *stated*, not
    # inferred, or the promotion gate would have to be quietly weakened. With
    # this off, a schedule above the evidence frontier fails the gate and the
    # book does not arm itself; with it on, the mismatch is reported as a note
    # on every assessment and shown on the console.
    accept_evidence_override: bool = False
    # Phase 4 — self-evaluation, promotion, autonomy
    autonomy: str = "manual"  # manual | assisted | auto
    history_path: Path | None = None
    evolution_interval_minutes: int = 60
    # Rebuild the walk-forward evidence report when it is older than this. The
    # promotion gate refuses a stale report, so something has to refresh it, and
    # "the operator will remember" is not a control. 0 disables.
    evidence_refresh_days: float = 7.0
    evolution_population: int = 24
    evolution_generations: int = 6
    promotion_cycles_required: int = 3
    min_oos_trades: int = 30
    equity_sizing: bool = True
    min_order_notional: Decimal = Decimal("1.00")
    # Small-account mode. Below the equity where the per-order cap can clear
    # ``min_order_notional``, the agent refuses every entry — correct, and
    # useless on a $50 account. When this ceiling is positive the per-order cap
    # is raised to just enough to place one order, never above this value, and it
    # falls back the moment the account is big enough. 0 disables the rule.
    small_account_max_order_pct: Decimal = Decimal("0")
    # "flat" gives every entry the same per-order budget; "proportional" scales
    # that budget by the strategy's inverse-volatility weight, so wilder symbols
    # take smaller positions. Measured on the current universe, proportional
    # sizing earns the same bps per trade with a third of the drawdown
    # (2.04%/order: 23.9% -> 9.0% max drawdown).
    sizing: str = "flat"
    # The operator's daily-budget schedule, as
    # ``[[up_to_equity, share_at_full_confidence, share_at_zero_confidence], ...]``
    # (the third number is optional and defaults to ``daily_budget_confidence_floor``
    # of the share). A small account has to be concentrated to do anything at
    # all — $50 spread over four positions is $12 a position, and at a 1%
    # ceiling it would take fifty days to become fully invested. Above a few
    # thousand the same concentration stops being a necessity and becomes a
    # liability, so the share falls with account size. The last row covers
    # everything above its threshold. Empty means "use daily_notional_pct flat",
    # which is what every config written before this one does.
    daily_budget_schedule: tuple[tuple[float, Decimal, Decimal], ...] = ()
    # Where a row sits at zero confidence when it does not say so itself: the
    # budget is scaled between this fraction of the row and the row itself as
    # confidence moves from 0 to 1, so a tier is a ceiling the system has to
    # earn rather than a number it holds by default.
    daily_budget_confidence_floor: Decimal = Decimal("0.70")
    # Hard ceiling on a single order, whatever the schedule and the confidence
    # say. The scheduled per-order ceiling is at most this, and at most
    # ``daily share / max_open_positions`` so one order cannot eat the day.
    max_order_hard_pct: Decimal = Decimal("0.25")
    # How often the evolution agent may propose changes to the system. It writes
    # proposals for review and can apply nothing; 0 disables it.
    evolution_agent_interval_hours: float = 24.0
    # Jev's per-entry judgment ("is this entry chasing?"). The probability is
    # recorded on every order either way; it only *vetoes* when this is on, and
    # then only at or above the threshold. Advisory by default: the model adds
    # information, the operator decides how much authority it gets.
    # Autonomous execution: arm this workspace without a human once every
    # pre-flight check is green, and disarm it the moment one fails. Requires
    # AGENTIC_ALLOW_AUTONOMY=1 as well, so a config file alone cannot arm an
    # account anywhere. Off by default.
    auto_arm: bool = False
    auto_arm_min_interval_hours: float = 6.0
    jev_veto_chase: bool = False
    jev_chase_threshold: Decimal = Decimal("0.75")
    # Symbol scout: the agent may add and drop its own symbols as the tape
    # changes, so a trend it was not configured for is not a trend it misses.
    # The operator's ``symbol_whitelist`` is the core: the scout can only add
    # or remove *its own* picks, never the operator's, and it never drops a
    # symbol the book still holds. Off by default — the whitelist an operator
    # wrote is the set of instruments the account is authorised to trade until
    # they say otherwise.
    discovery_enabled: bool = False
    # How often the scout re-reads the market and the candidate pool.
    discovery_interval_hours: float = 6.0
    # Ceiling on scout-added symbols, on top of the operator's whitelist.
    discovery_max_symbols: int = 6
    # Minimum daily bars a candidate must have before it can be traded: the
    # trend vote reads a 252-session horizon, and a short file cannot answer.
    discovery_min_bars: int = 260
    # Trend-vote thresholds (the vote is the share of the 50/100/200/252-bar
    # horizons that are rising, 0..1). Enter on 3 of 4, leave on 1 of 4, so a
    # symbol is not added and dropped on the same wobble.
    discovery_enter_vote: Decimal = Decimal("0.75")
    discovery_exit_vote: Decimal = Decimal("0.25")
    # Liquidity floor in dollars of daily volume (median of the last 60 bars).
    # A trend in something nobody trades is a trend that cannot be exited.
    discovery_min_dollar_volume: Decimal = Decimal("5000000")
    # Equity candidates the scout may consider. Stocks have no public
    # screenshot of "trading right now" from the broker read tools, so the
    # operator supplies the pool and the scout applies the same data tests to
    # it. Crypto candidates come from the exchange's live pair list instead.
    discovery_candidates: tuple[str, ...] = ()
    # The broker's own discovery lists, by display name. These are Robinhood's
    # answer to "what is moving today", which is a better pool than any list
    # written by hand; the scout still applies the full data test to each name.
    discovery_lists: tuple[str, ...] = ()
    # How many candidates one pass may fetch history for. Each one costs a
    # broker call, so this trades coverage against gateway traffic.
    discovery_max_candidates: int = 40
    # Widest spread the book will pay to cross, in basis points of the mid. A
    # rule that is right and pays more in spread than it earns is still wrong.
    discovery_max_spread_bps: Decimal = Decimal("25")
    # A candidate that moves this closely with something already held is the
    # same bet, not a new one.
    discovery_max_correlation: Decimal = Decimal("0.85")
    # A newly adopted symbol is held at least this long before a cooling trend
    # can drop it, so a single quiet week does not churn the book.
    discovery_min_hold_hours: float = 48.0
    # What the scout has adopted, read from ``state_dir/universe.json`` at load
    # time. Kept separate from ``symbol_whitelist`` so the operator's core book
    # stays exactly what they wrote, and so the scout can tell its own picks
    # from theirs.
    discovered_symbols: frozenset[str] = frozenset()

    @property
    def effective_whitelist(self) -> frozenset[str]:
        """The core book plus the scout's picks — what may actually be traded.

        One definition, so the risk guard, the quote feed, the strategy, the
        evidence gate and the self-check cannot disagree about the universe.
        """
        return self.symbol_whitelist | self.discovered_symbols

    def __post_init__(self) -> None:
        if self.mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        if self.strategy not in ("fixture", "spy_scalper", "llm", "trend_crypto"):
            raise ValueError(
                "strategy must be fixture|spy_scalper|llm|trend_crypto"
            )
        if self.quote_source not in ("file", "mcp"):
            raise ValueError("quote_source must be file|mcp")
        if self.session_policy not in ("regular", "extended", "all", "any"):
            raise ValueError("session_policy must be regular|extended|all|any")
        if self.max_session_policy:
            from agentic_trading.limits import SESSION_POLICIES

            if self.max_session_policy not in SESSION_POLICIES:
                raise ValueError("max_session_policy must be regular|extended|all|any")
            if SESSION_POLICIES.index(self.max_session_policy) < SESSION_POLICIES.index(
                self.session_policy
            ):
                raise ValueError(
                    "max_session_policy must be at least as wide as session_policy "
                    "(the agent may widen hours, never narrow below what you set)"
                )
        if self.order_type not in ("market", "limit"):
            raise ValueError("order_type must be market|limit")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.max_orders_per_day < 1:
            raise ValueError("max_orders_per_day must be >= 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if (
            self.max_correlated_positions is not None
            and self.max_correlated_positions < 1
        ):
            raise ValueError("max_correlated_positions must be >= 1 when set")
        if not 0.0 < self.correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be in (0, 1]")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.open_order_refresh_seconds < 0:
            raise ValueError("open_order_refresh_seconds must be >= 0")
        if self.cycle_stats_seconds < 0:
            raise ValueError("cycle_stats_seconds must be >= 0")
        if self.regime_refresh_seconds < 0:
            raise ValueError("regime_refresh_seconds must be >= 0")
        if self.history_refresh_hours < 0:
            raise ValueError("history_refresh_hours must be >= 0")
        if self.selfcheck_minutes < 0:
            raise ValueError("selfcheck_minutes must be >= 0")
        if self.autonomy not in ("manual", "assisted", "auto"):
            raise ValueError("autonomy must be manual|assisted|auto")
        if self.evolution_interval_minutes < 0:
            raise ValueError("evolution_interval_minutes must be >= 0")
        if self.evidence_refresh_days < 0:
            raise ValueError("evidence_refresh_days must be >= 0")
        if self.evolution_population < 2:
            raise ValueError("evolution_population must be >= 2")
        if self.evolution_generations < 1:
            raise ValueError("evolution_generations must be >= 1")
        if self.promotion_cycles_required < 1:
            raise ValueError("promotion_cycles_required must be >= 1")
        if self.min_oos_trades < 1:
            raise ValueError("min_oos_trades must be >= 1")
        if self.min_order_notional <= 0:
            raise ValueError("min_order_notional must be positive")
        if self.small_account_max_order_pct < 0:
            raise ValueError("small_account_max_order_pct must be >= 0")
        if self.sizing not in ("flat", "proportional"):
            raise ValueError("sizing must be flat|proportional")
        if not 0 <= float(self.jev_chase_threshold) <= 1:
            raise ValueError("jev_chase_threshold must be between 0 and 1")
        if self.discovery_interval_hours <= 0:
            raise ValueError("discovery_interval_hours must be positive")
        if self.discovery_max_symbols < 0:
            raise ValueError("discovery_max_symbols must be >= 0")
        if self.discovery_min_bars < 60:
            raise ValueError("discovery_min_bars must be >= 60")
        for name, value in (
            ("discovery_enter_vote", self.discovery_enter_vote),
            ("discovery_exit_vote", self.discovery_exit_vote),
        ):
            if not 0 <= float(value) <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if float(self.discovery_exit_vote) >= float(self.discovery_enter_vote):
            raise ValueError(
                "discovery_exit_vote must be below discovery_enter_vote "
                "(otherwise a symbol is added and dropped on the same reading)"
            )
        if self.discovery_min_dollar_volume < 0:
            raise ValueError("discovery_min_dollar_volume must be >= 0")
        if self.rebalance_retry_seconds <= 0:
            raise ValueError("rebalance_retry_seconds must be positive")
        if self.rebalance_max_retries < 0:
            raise ValueError("rebalance_max_retries must be >= 0")
        if self.deferred_max_age_seconds < 0:
            raise ValueError("deferred_max_age_seconds must be >= 0")
        if self.deferred_max_attempts < 0:
            raise ValueError("deferred_max_attempts must be >= 0")
        if self.max_cost_share_of_order < 0:
            raise ValueError("max_cost_share_of_order must be >= 0")
        if self.discovery_max_candidates < 0:
            raise ValueError("discovery_max_candidates must be >= 0")
        if float(self.discovery_max_spread_bps) < 0:
            raise ValueError("discovery_max_spread_bps must be >= 0")
        if not 0 < float(self.discovery_max_correlation) <= 1:
            raise ValueError("discovery_max_correlation must be in (0, 1]")
        if self.discovery_min_hold_hours < 0:
            raise ValueError("discovery_min_hold_hours must be >= 0")


def _schedule(raw: Any) -> tuple[tuple[float, Decimal, Decimal], ...]:
    """Parse ``[[up_to_equity, share, floor?], ...]`` into ordered thresholds.

    An unreadable row is dropped rather than guessed at: a malformed risk
    schedule must not become a different risk schedule.
    """
    if not isinstance(raw, list):
        return ()
    rows: list[tuple[float, Decimal, Decimal]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) not in (2, 3):
            continue
        try:
            threshold = float(row[0])
            share = Decimal(str(row[1]))
            floor = (
                Decimal(str(row[2])) if len(row) == 3 else Decimal("0")
            )
        except (ArithmeticError, TypeError, ValueError):
            continue
        if threshold < 0 or not 0 < share <= 1:
            continue
        if floor < 0 or floor > share:
            floor = Decimal("0")
        rows.append((threshold, share, floor))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def _adopted_symbols(state_dir: Any) -> frozenset[str]:
    """What the scout has adopted, read straight from its own state file.

    Deliberately a plain JSON read rather than an import of ``discovery``:
    configuration is the bottom of the dependency graph, and a missing or
    corrupt file must mean "nothing adopted", never a failed start-up.
    """
    if not state_dir:
        return frozenset()
    try:
        payload = json.loads(
            (Path(state_dir) / "universe.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    adopted = payload.get("adopted")
    if not isinstance(adopted, list):
        return frozenset()
    return frozenset(
        str(symbol).strip().upper() for symbol in adopted if str(symbol).strip()
    )


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    scalper_raw = raw.get("scalper_config")
    return Config(
        mode=raw["mode"],
        symbol_whitelist=frozenset(s.upper() for s in raw["symbol_whitelist"]),
        max_order_pct=Decimal(str(raw["max_order_pct"])),
        daily_notional_pct=Decimal(str(raw["daily_notional_pct"])),
        daily_loss_pct=Decimal(str(raw["daily_loss_pct"])),
        max_open_positions=int(raw["max_open_positions"]),
        max_correlated_positions=(
            int(raw["max_correlated_positions"])
            if raw.get("max_correlated_positions") is not None
            else None
        ),
        correlation_threshold=float(raw.get("correlation_threshold", 0.7)),
        correlation_lookback_days=int(raw.get("correlation_lookback_days", 120)),
        equity_refresh_ticks=int(raw["equity_refresh_ticks"]),
        equity_refresh_seconds=int(raw["equity_refresh_seconds"]),
        timezone=str(raw.get("timezone", "local")),
        quotes_path=Path(raw["quotes_path"]),
        journal_dir=Path(raw["journal_dir"]),
        state_dir=Path(raw["state_dir"]),
        tools_snapshot_path=Path(raw["tools_snapshot_path"]),
        token_path=Path(raw["token_path"]).expanduser(),
        mcp_url=str(raw["mcp_url"]),
        strategy=str(raw.get("strategy", "fixture")),
        scalper_config=Path(scalper_raw) if scalper_raw else None,
        quote_source=str(raw.get("quote_source", "file")),
        poll_seconds=float(raw.get("poll_seconds", 5.0)),
        session_policy=str(raw.get("session_policy", "regular")),
        max_session_policy=str(raw.get("max_session_policy", "")),
        account_number=(
            str(raw["account_number"]) if raw.get("account_number") else None
        ),
        order_type=str(raw.get("order_type", "market")),
        max_orders_per_day=int(raw.get("max_orders_per_day", 10)),
        max_consecutive_errors=int(raw.get("max_consecutive_errors", 3)),
        max_quote_age_seconds=float(raw.get("max_quote_age_seconds", 60.0)),
        open_order_refresh_seconds=float(raw.get("open_order_refresh_seconds", 15.0)),
        cycle_stats_seconds=float(raw.get("cycle_stats_seconds", 60.0)),
        regime_refresh_seconds=float(raw.get("regime_refresh_seconds", 180.0)),
        history_refresh_hours=float(raw.get("history_refresh_hours", 24.0)),
        selfcheck_minutes=float(raw.get("selfcheck_minutes", 30.0)),
        autonomy=str(raw.get("autonomy", "manual")),
        history_path=Path(raw["history_path"]) if raw.get("history_path") else None,
        evolution_interval_minutes=int(raw.get("evolution_interval_minutes", 60)),
        evidence_refresh_days=float(raw.get("evidence_refresh_days", 7.0)),
        evolution_population=int(raw.get("evolution_population", 24)),
        evolution_generations=int(raw.get("evolution_generations", 6)),
        promotion_cycles_required=int(raw.get("promotion_cycles_required", 3)),
        min_oos_trades=int(raw.get("min_oos_trades", 30)),
        equity_sizing=bool(raw.get("equity_sizing", True)),
        min_order_notional=Decimal(str(raw.get("min_order_notional", "1.00"))),
        small_account_max_order_pct=Decimal(
            str(raw.get("small_account_max_order_pct", "0"))
        ),
        sizing=str(raw.get("sizing", "flat")),
        daily_budget_schedule=_schedule(raw.get("daily_budget_schedule")),
        daily_budget_confidence_floor=Decimal(
            str(raw.get("daily_budget_confidence_floor", "0.70"))
        ),
        max_order_hard_pct=Decimal(str(raw.get("max_order_hard_pct", "0.25"))),
        evolution_agent_interval_hours=float(
            raw.get("evolution_agent_interval_hours", 24.0)
        ),
        auto_arm=bool(raw.get("auto_arm", False)),
        auto_arm_min_interval_hours=float(
            raw.get("auto_arm_min_interval_hours", 6.0)
        ),
        jev_veto_chase=bool(raw.get("jev_veto_chase", False)),
        jev_chase_threshold=Decimal(str(raw.get("jev_chase_threshold", "0.75"))),
        discovery_enabled=bool(raw.get("discovery_enabled", False)),
        discovery_interval_hours=float(raw.get("discovery_interval_hours", 6.0)),
        discovery_max_symbols=int(raw.get("discovery_max_symbols", 6)),
        discovery_min_bars=int(raw.get("discovery_min_bars", 260)),
        discovery_enter_vote=Decimal(
            str(raw.get("discovery_enter_vote", "0.75"))
        ),
        discovery_exit_vote=Decimal(str(raw.get("discovery_exit_vote", "0.25"))),
        discovery_min_dollar_volume=Decimal(
            str(raw.get("discovery_min_dollar_volume", "5000000"))
        ),
        discovery_candidates=tuple(
            str(symbol).upper()
            for symbol in (raw.get("discovery_candidates") or [])
        ),
        discovery_lists=tuple(
            str(name) for name in (raw.get("discovery_lists") or [])
        ),
        discovery_max_candidates=int(raw.get("discovery_max_candidates", 40)),
        discovery_max_spread_bps=Decimal(
            str(raw.get("discovery_max_spread_bps", "25"))
        ),
        discovery_max_correlation=Decimal(
            str(raw.get("discovery_max_correlation", "0.85"))
        ),
        discovery_min_hold_hours=float(
            raw.get("discovery_min_hold_hours", 48.0)
        ),
        discovered_symbols=_adopted_symbols(raw.get("state_dir")),
        rebalance_retry_seconds=float(raw.get("rebalance_retry_seconds", 900.0)),
        rebalance_max_retries=int(raw.get("rebalance_max_retries", 6)),
        deferred_max_age_seconds=float(raw.get("deferred_max_age_seconds", 3600.0)),
        deferred_max_attempts=int(raw.get("deferred_max_attempts", 3)),
        max_cost_share_of_order=Decimal(
            str(raw.get("max_cost_share_of_order", "0.02"))
        ),
        accept_evidence_override=bool(raw.get("accept_evidence_override", False)),
    )
