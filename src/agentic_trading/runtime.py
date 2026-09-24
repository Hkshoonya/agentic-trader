"""Shadow/live runtime: quotes → strategy → RiskGuard → journal / broker.

Two entry points:

- :func:`run_loop` — replay a local JSONL quote file once (soak / tests / replay)
- :func:`run_daemon` — continuous autonomous loop over a live quote feed with
  session gating, equity refresh, fill reconciliation and error kill-switch

Live placement requires **all** of: mode ``live``, a machine capability switch
(``AGENTIC_ALLOW_LIVE=1`` for manual operation or
``AGENTIC_ALLOW_AUTONOMY=1`` for autonomous operation), a currently eligible
workspace arm file, a session permitted by ``session_policy``, and a RiskGuard
allow decision. An environment variable alone is never an evidence bypass.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

from agentic_trading.broker import Broker, BrokerPayloadError
from agentic_trading.config import Config
from agentic_trading.journal import DecisionJournal
from agentic_trading.marketdata import QuoteFeed, build_quote_feed
from agentic_trading.notify import build_notifier
from agentic_trading.orders import (
    EquityOrderRequest,
    OrderValidationError,
    is_crypto_symbol,
)
from agentic_trading.quotes import iter_quotes
from agentic_trading.rh_mcp.snapshot import write_tools_snapshot
from agentic_trading.risk import PortfolioSnapshot, RiskGuard, ShadowBook
from agentic_trading.session import (
    market_hours_argument,
    next_session_open,
    session_allows,
    session_for,
)
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.types import OrderIntent, Side, new_decision_id


class Strategy(Protocol):
    def on_quote(self, quote: dict) -> list[OrderIntent]: ...


_MODE_FILE = "mode"
_HEARTBEAT_SECONDS = 900.0

# What ``process_intent`` returns when it decided *nothing* for a technical
# reason: the account value was not known yet, the runtime's own position book
# could not be read, the operator had not armed the session, or the broker's
# review call failed. On a strategy that rebalances once a day, treating those
# as "the day is spent" is how a whole day's signal disappears with no trade.
RETRY = "retry"


def effective_mode(config: Config) -> str:
    """Config mode, overridden by ``state_dir/mode`` when present."""
    path = Path(config.state_dir) / _MODE_FILE
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip().lower()
        if value in ("shadow", "live"):
            return value
    return config.mode


def write_mode(state_dir: Path | str, mode: str) -> None:
    if mode not in ("shadow", "live"):
        raise ValueError("mode must be shadow|live")
    from agentic_trading import jsonio

    jsonio.write_text(Path(state_dir) / _MODE_FILE, mode + "\n")


def build_guard(config: Config, mode: str) -> RiskGuard:
    return RiskGuard(
        mode=mode,
        # The scout's adopted symbols are tradeable exactly like the operator's:
        # one universe, or the guard would refuse the very symbols the strategy
        # was allowed to pick.
        whitelist=config.effective_whitelist,
        max_order_pct=config.max_order_pct,
        daily_notional_pct=config.daily_notional_pct,
        daily_loss_pct=config.daily_loss_pct,
        max_open_positions=config.max_open_positions,
        baseline_equity=Decimal("0"),
        current_equity=Decimal("0"),
        timezone=config.timezone,
    )


def _with_pair_symbol(order: dict[str, Any]) -> dict[str, Any]:
    """Give a crypto order the pair symbol the rest of the loop expects.

    Crypto orders carry ``currency_code`` (``BTC``) rather than a symbol, while
    every downstream check compares against whitelist pairs (``BTC-USD``).
    """
    if order.get("symbol"):
        return order
    code = str(order.get("currency_code") or "").strip().upper()
    if not code:
        return order
    return {**order, "symbol": f"{code}-USD"}


def _intent_payload(intent: OrderIntent) -> dict[str, Any]:
    side = intent.side.value if isinstance(intent.side, Side) else str(intent.side)
    payload: dict[str, Any] = {
        "decision_id": intent.decision_id,
        "symbol": intent.symbol,
        "side": side,
        "reason": intent.reason,
        "created_at": intent.created_at.isoformat(),
    }
    if intent.quantity is not None:
        payload["quantity"] = str(intent.quantity)
    if intent.ref_price is not None:
        payload["ref_price"] = str(intent.ref_price)
    if intent.notional_usd is not None:
        payload["notional_usd"] = str(intent.notional_usd)
    if intent.metadata is not None:
        payload["metadata"] = intent.metadata
    return payload


_FILE_SESSIONS = {
    "premarket": "premarket",
    "regular": "regular",
    "afterhours": "afterhours",
    "overnight": "overnight",
}


def modeled_session(quote: dict) -> str:
    """Session used to model order construction when replaying recorded quotes.

    Replay has no live session, so a quote's own ``session`` label is used and
    an unlabelled quote is modelled as regular hours.
    """
    value = str(quote.get("session") or "").strip().lower()
    return _FILE_SESSIONS.get(value, "regular")


def quote_is_fresh(quote: dict, *, now: datetime, max_age_seconds: float) -> bool:
    """True only when both transport and market timestamps are fresh.

    ``observed_at`` catches a replayed file or a stalled collector. ``quote_at``
    catches a broker that answered now with an old market snapshot (notably an
    equity quote outside its session). Checking only the former makes a stale
    price look fresh merely because the network request just completed.
    Unparseable, missing and future timestamps fail closed.
    """
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    for field in ("observed_at", "quote_at"):
        raw = quote.get(field)
        if not isinstance(raw, str) or not raw.strip():
            return False
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (current - stamp.astimezone(timezone.utc)).total_seconds()
        if not 0 <= age <= max_age_seconds:
            return False
    return True


def build_order_request(
    intent: OrderIntent,
    *,
    account_number: str,
    config: Config,
    session: str,
    ref_id: str | None = None,
) -> EquityOrderRequest:
    """Translate a strategy intent into a schema-valid broker order.

    Buys during regular hours default to dollar-based market orders so a small
    account can trade before it can afford a whole share. Sells always use the
    exact held quantity. Outside regular hours only limit orders execute, which
    also means whole shares (fractional limit orders are rejected upstream).
    """
    if intent.quantity is None or intent.ref_price is None:
        raise OrderValidationError(
            "live orders need quantity and ref_price; notional-only intents "
            "cannot be converted safely"
        )

    side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
    # Crypto trades 24/7 and is fractional by nature, so the equity rule that
    # fractional orders need regular_hours does not apply; forcing regular_hours
    # keeps the shared validator happy and the crypto args omit it anyway.
    is_crypto = is_crypto_symbol(intent.symbol)
    market_hours = "regular_hours" if is_crypto else market_hours_argument(session)
    fractional = intent.quantity != intent.quantity.to_integral_value()

    if fractional and market_hours != "regular_hours":
        # A fractional position could be opened but never exited outside
        # regular hours, so refuse the entry rather than strand the position.
        raise OrderValidationError(
            "fractional quantity requires regular_hours; refusing to trade a "
            "position that could not be exited"
        )

    # Outside regular hours only limit orders execute, so the session wins over
    # the configured preference instead of producing a guaranteed rejection.
    if config.order_type == "limit" or market_hours != "regular_hours":
        return EquityOrderRequest(
            account_number=account_number,
            symbol=intent.symbol,
            side=side,
            order_type="limit",
            quantity=intent.quantity,
            limit_price=intent.ref_price,
            market_hours=market_hours,
            ref_id=ref_id or intent.decision_id,
        )

    if side is Side.BUY and market_hours == "regular_hours":
        dollars = intent.resolved_notional().quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        if dollars <= 0:
            raise OrderValidationError("rounded dollar_amount is not positive")
        return EquityOrderRequest(
            account_number=account_number,
            symbol=intent.symbol,
            side=side,
            order_type="market",
            dollar_amount=dollars,
            market_hours=market_hours,
            ref_id=ref_id or intent.decision_id,
        )

    return EquityOrderRequest(
        account_number=account_number,
        symbol=intent.symbol,
        side=side,
        order_type="market",
        quantity=intent.quantity,
        market_hours=market_hours,
        ref_id=ref_id or intent.decision_id,
    )


class NotifyingJournal:
    """A journal that also fires operator alerts, and can never break on one.

    Delegates everything else to the real journal, so callers (and RiskGuard)
    keep using it exactly as before.
    """

    def __init__(self, inner: Any, notifier: Any) -> None:
        self._inner = inner
        self._notifier = notifier

    def append(self, record: dict[str, Any]) -> None:
        self._inner.append(record)
        try:
            alert = self._notifier.dispatch(record)
        except Exception:  # noqa: BLE001 — an alert must never cost a decision
            return
        if alert is not None:
            # Recorded through the inner journal so this cannot recurse.
            self._inner.append({"event": "notify", **alert.to_dict()})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Loop:
    """Shared quote → intent → guard → journal/broker pipeline."""

    def __init__(
        self,
        config: Config,
        broker: Broker,
        strategy: Optional[Strategy],
        stop_event: Optional[threading.Event] = None,
        *,
        force_shadow: bool = False,
        strategy_factory: Optional[Callable[[Config], Any]] = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.strategy = strategy or FixtureStrategy()
        # Used to rebuild the strategy when the scout changes the universe: a
        # strategy that was constructed for one symbol list cannot price another,
        # and re-seeding its book is not enough.
        self.strategy_factory = strategy_factory
        # Set by a worker, read by the daemon loop, which owns the quote feed.
        self.universe_dirty = False
        # A scout report the loop thread has not applied yet. Adoption swaps the
        # strategy and the guard's whitelist, so it belongs to the thread that
        # processes quotes — never to the worker that produced the report.
        self.pending_universe: Optional[list[str]] = None
        self.stop_event = stop_event
        self.force_shadow = force_shadow
        self.mode = "shadow" if force_shadow else effective_mode(config)
        self.journal = DecisionJournal(Path(config.journal_dir))
        notifier = build_notifier()
        self.notifier = notifier
        if notifier is not None:
            # Alerts ride along with the journal: every decision already lands
            # here, and an alert must never be able to block one.
            self.journal = NotifyingJournal(self.journal, notifier)
        self.guard = build_guard(config, self.mode)
        # Positions and cumulative P&L have no safe expiry window. Replay every
        # date-named shadow journal; only daily risk counters are day-scoped.
        self.shadow_book = ShadowBook.from_journal(self.journal, days=None)
        self.guard.attach_shadow_book(self.shadow_book)
        self.guard.load(config.state_dir, self.journal)
        if self.mode == "shadow":
            self.guard.note_shadow_realized(self.shadow_book.realized_pnl)
        self.account_number: Optional[str] = config.account_number
        self.orders_today = self._count_orders_today()
        self.consecutive_errors = 0
        self.open_order_symbols: set[str] = set()
        # Which side is working on each symbol. An entry must not stack on top
        # of a pending order of any kind, but an exit must be allowed to run
        # while an entry is still filling — and must not be repeated while its
        # own sell is still working.
        self.open_order_sides: dict[str, set[str]] = {}
        self._exit_pending_noted: set[str] = set()
        self._open_orders_read_at = 0.0
        # False until an actual read fails. Direct/test callers that have not
        # polled yet retain the old behaviour; the daemon polls before it lets a
        # quote reach the strategy. Once a live read fails, writes fail closed
        # until a later successful read proves no duplicate order is working.
        self.open_orders_read_failed = False
        self._stopping = False
        self.stage = "shadow"
        self.session_policy = config.session_policy
        # A rebalance that decided nothing may be tried again, on a leash:
        # without the wait, every quote for the next hour would re-run it.
        self._rebalance_retry_at = 0.0
        self._rebalance_retries = 0
        self._rebalance_retry_day = ""
        # Orders that passed every judgement but could not be submitted (the
        # arming switch, or a failed review call). See defer_intent.
        self.deferred_intents: dict[str, dict[str, Any]] = {}
        # What the runtime last told the strategy it holds, so a reconcile only
        # speaks when the book actually changed.
        self._strategy_book: dict[str, str] = {}
        self.evidence_confidence: float = 0.0
        self.last_evolution_at = 0.0
        # What each agent in the fleet last did; published by write_agent_state().
        self.agent_runs: dict[str, dict[str, Any]] = {}
        self.advisor = None
        self.regime_gate = None
        self.entry_advisor = None
        try:
            from agentic_trading.llm.advisor import build_advisor, build_regime_gate

            self.advisor = build_advisor()
            # Same opt-in as the advisor: the console of LLM features is one
            # switch, and every part of it is bounded to reducing risk.
            self.regime_gate = build_regime_gate(
                state_path=Path(config.state_dir) / "regimes.json"
            )
            # Jev's per-entry judgments (chasing, participation), read from a
            # cache the worker fills. None without a TypeSafe key.
            from agentic_trading.llm.jev import build_entry_advisor

            self.entry_advisor = build_entry_advisor(
                state_path=Path(config.state_dir) / "entry_context.json"
            )
        except Exception:  # noqa: BLE001 — advisory layer must never block startup
            self.advisor = None
            self.regime_gate = None
            self.entry_advisor = None
        self._feature_cache: dict[str, tuple[float, Any]] = {}
        self.apply_stage_caps()

    def apply_stage_caps(self) -> None:
        """Probation trades live but with a reduced per-order cap."""
        from agentic_trading.promotion import load_state, policy_from_config

        # Every ceiling below comes from the *risk view*: the flat config pair,
        # or the pair the size-and-confidence schedule derives for this account.
        view = self.risk_view()
        state = load_state(self.config.state_dir)
        self.stage = state.stage
        if state.stage == "probation":
            probation_cap = policy_from_config(self.config).probation_max_order_pct
            self.guard.max_order_pct = min(view.max_order_pct, probation_cap)
        else:
            self.guard.max_order_pct = view.max_order_pct
        # The agent's stored limits may only ever tighten what the operator set.
        from agentic_trading.limits import apply_to_guard, load_limits, reconcile

        self.guard.daily_notional_pct = view.daily_notional_pct
        # A ceiling lowered in the config must show up on the console now, not
        # at the next evaluation — evaluations are skipped while the bars are
        # unchanged, which is most days.
        reconcile(view)
        apply_to_guard(self.guard, view)
        if self.shadow_full_size_active():
            # A dry-run trial is paper: the confidence ladder protects money,
            # and there is none at stake, so paper orders use the ceilings the
            # operator set. Live orders never reach this branch.
            self.guard.max_order_pct = view.max_order_pct
            self.guard.daily_notional_pct = view.daily_notional_pct
        # Caps the evidence/confidence ladder currently justifies, before the
        # small-account floor is considered. Keep both separately so the floor
        # is re-derived rather than ratcheting its own previous result. These
        # assignments must follow ``apply_to_guard``: stored confidence limits
        # can be tighter than the stage/config view.
        self.policy_max_order_pct = Decimal(str(self.guard.max_order_pct))
        self.policy_daily_notional_pct = Decimal(str(self.guard.daily_notional_pct))
        stored = load_limits(view.state_dir)
        # Confidence in force right now, so every decision can record the
        # evidence level it was taken under.
        self.evidence_confidence = float((stored.confidence if stored else "") or 0.0)
        # The agent may widen trading hours only up to the operator's bound, and
        # only when its own confidence ladder says the evidence earned it.
        self.session_policy = str(
            (stored.session_policy if stored else "") or self.config.session_policy
        )
        self.apply_correlation_policy()
        self.apply_size_floor()
        self.reconcile_stage_mode()

    def shadow_full_size_active(self) -> bool:
        return bool(
            getattr(self.config, "shadow_full_size", False)
            and self.guard.mode == "shadow"
            and getattr(self, "stage", "shadow") == "shadow"
        )

    def risk_view(self) -> Config:
        """The operator's ceilings for *this* account size and confidence.

        ``config/agentic.toml`` can state the daily budget as a schedule keyed
        on account size (``daily_budget_schedule``) instead of one flat share.
        When it does, the ceilings are re-derived here every cycle from the
        equity the broker reports and the confidence the evidence earned: a $50
        account and a $5,000 account should not be trading the same fraction of
        themselves, and neither should trade the same fraction at confidence
        0.2 and 0.95.

        Without a schedule this returns the config unchanged, so every existing
        deployment keeps the flat ceilings it was written with.
        """
        from agentic_trading.limits import budget_ceilings

        if not getattr(self.config, "daily_budget_schedule", ()):
            return self.config
        equity = Decimal(str(self.guard.current_equity or 0))
        if equity <= 0:
            return self.config  # nothing measured yet; do not guess a budget
        order, daily = budget_ceilings(
            self.config, equity=equity, confidence=float(self.evidence_confidence)
        )
        return replace(self.config, max_order_pct=order, daily_notional_pct=daily)

    def apply_risk_budget(self) -> Optional[dict[str, Any]]:
        """Re-price the day's budget, and say so when it moves.

        Called every cycle: equity moves with the market, the confidence ladder
        moves with the evidence, and the schedule turns both into a ceiling. A
        change is journaled with the numbers, because a budget that moves
        silently is a budget nobody can audit.
        """
        if not getattr(self.config, "daily_budget_schedule", ()):
            return None
        before = (self.guard.max_order_pct, self.guard.daily_notional_pct)
        self.apply_stage_caps()
        after = (self.guard.max_order_pct, self.guard.daily_notional_pct)
        view = self.risk_view()
        if before == after and Decimal(str(before[1])) == view.daily_notional_pct:
            return None
        event = {
            "event": "budget_recomputed",
            "equity": str(self.guard.current_equity),
            "confidence": round(float(self.evidence_confidence), 4),
            "max_order_pct": str(after[0]),
            "daily_notional_pct": str(after[1]),
            "ceiling_max_order_pct": str(view.max_order_pct),
            "ceiling_daily_notional_pct": str(view.daily_notional_pct),
            "from_max_order_pct": str(before[0]),
            "from_daily_notional_pct": str(before[1]),
        }
        self.journal.append(event)
        return event

    def refresh_assessment(self) -> Optional[dict[str, Any]]:
        """Re-read the evidence verdict against the *current* policy.

        The full evaluation is skipped while the bars are unchanged, which is
        most days — but the verdict is a function of the policy as well as the
        bars, and the policy is a config file the operator can edit. Changing
        the size schedule without changing the history used to leave the console
        arguing with the new budget for a day, because the assessment that
        judged it was yesterday's. Re-reading it is a JSON parse and some
        arithmetic, so it costs nothing to do whenever the daemon looks.
        """
        from agentic_trading.evidence import (
            effective_per_order_pct,
            read_report,
            with_current_forward,
        )
        from agentic_trading.limits import load_limits
        from agentic_trading.promotion import (
            apply_assessment,
            assess_walkforward,
            load_state,
            policy_from_config,
            save_state,
        )
        from agentic_trading.selfimprove import update_limits

        report = read_report(self.config)
        if not report:
            return None
        report = with_current_forward(self.config, report)
        stored = load_limits(self.config.state_dir)
        policy_size = Decimal(
            str(
                stored.max_order_pct
                if stored is not None
                else self.config.max_order_pct
            )
        )
        live = effective_per_order_pct(
            self.config,
            policy_pct=policy_size,
            equity=Decimal(str(self.guard.current_equity or 0)),
        )
        try:
            verdict = assess_walkforward(
                report,
                policy_from_config(self.config),
                live_per_order_pct=live,
            )
        except Exception as exc:  # noqa: BLE001 — never kill the loop on this
            self.journal.append(
                {"event": "assessment_refresh_failed", "error": str(exc)[:200]}
            )
            return None
        state = load_state(self.config.state_dir)
        previous = {}
        if isinstance(state.last_assessment, dict):
            previous = state.last_assessment
        previous_evidence = (
            previous.get("evidence")
            if isinstance(previous.get("evidence"), dict)
            else {}
        )
        same_evidence = bool(
            previous_evidence.get("report_key")
            and previous_evidence.get("report_key")
            == verdict.evidence.get("report_key")
        )
        changed = (
            bool(previous.get("eligible")) != bool(verdict.eligible)
            or list(previous.get("reasons") or []) != list(verdict.reasons)
            or not same_evidence
        )
        if not changed:
            # A periodic rebuild may reproduce exactly the same measurements
            # under a new file timestamp. Relink the assessment without calling
            # apply_assessment: the same evidence must not advance a promotion
            # streak, but the floor must still know this exact report was read.
            if previous_evidence.get("report_generated_at") != verdict.evidence.get(
                "report_generated_at"
            ):
                state.last_assessment = verdict.to_dict()
                save_state(self.config.state_dir, state)
                event = {
                    "event": "assessment_relinked",
                    "report_key": verdict.evidence.get("report_key"),
                    "report_generated_at": verdict.evidence.get("report_generated_at"),
                }
                self.journal.append(event)
                return event
            return None
        events = apply_assessment(
            state,
            verdict,
            policy_from_config(self.config),
            equity=self.guard.current_equity,
        )
        save_state(self.config.state_dir, state)
        events.extend(update_limits(self.config, assessment=verdict))
        for event in events:
            self.journal.append(event)
        if any(
            transition.get("event") in ("promotion", "demotion")
            for transition in events
        ):
            _apply_promotion(self.config, self, self.journal, state)
        event = {
            "event": "assessment_refreshed",
            "eligible": verdict.eligible,
            "reasons": verdict.reasons,
            "notes": verdict.notes,
            "live_per_order_pct": round(live, 6),
            "gate_size_pct": verdict.evidence.get("gate_size_pct"),
        }
        self.journal.append(event)
        return event

    def apply_size_floor(self) -> Optional[dict[str, Any]]:
        """Enable one useful $5 forward entry only after retrospective proof.

        The floor solves a circular problem: a sub-$100 account cannot gather a
        forward record when its confidence cap produces orders below the useful
        minimum, but forward evidence is required before live promotion. The
        escape hatch is deliberately narrow: the exact floor size must first
        pass every historical, cost, recency, stress and drawdown check; the
        daily cap then permits at most one such opening entry. It does not skip
        the separate forward-evidence or live-arming gates.
        """
        from agentic_trading import sizer
        from agentic_trading.evidence import (
            effective_small_account_target,
            retrospective_floor_ready,
        )
        from agentic_trading.promotion import load_state

        policy_order = Decimal(
            str(getattr(self, "policy_max_order_pct", self.guard.max_order_pct))
        )
        policy_daily = Decimal(
            str(
                getattr(
                    self,
                    "policy_daily_notional_pct",
                    self.guard.daily_notional_pct,
                )
            )
        )
        order_ceiling = Decimal(str(self.config.small_account_max_order_pct))
        daily_ceiling = Decimal(str(self.config.small_account_max_daily_pct))
        if daily_ceiling <= 0:
            daily_ceiling = order_ceiling
        equity = Decimal(str(self.guard.current_equity or 0))
        broker_minimum = Decimal(str(self.config.min_order_notional))
        self._effective_min_order_notional = broker_minimum
        if equity <= 0:
            # Before the first equity refresh there is nothing to divide by, and
            # guessing a size is worse than waiting one cycle.
            return None

        target = effective_small_account_target(self.config)
        margin = Decimal(str(getattr(sizer, "FLOOR_MARGIN", Decimal("0.02"))))
        needed = (target * (Decimal(1) + margin)) / equity
        state = load_state(self.config.state_dir)
        assessment = (
            state.last_assessment if isinstance(state.last_assessment, dict) else {}
        )
        ready, readiness_blockers = retrospective_floor_ready(self.config, assessment)
        enabled = order_ceiling > 0 and daily_ceiling > 0

        if not enabled or not ready:
            self.guard.max_order_pct = policy_order
            self.guard.daily_notional_pct = policy_daily
            was_active = bool(getattr(self, "_size_floor_active", False))
            previous_ready = getattr(self, "_size_floor_ready", None)
            self._size_floor_active = False
            self._size_floor_sufficient = True
            self._size_floor_ready = ready
            self._size_floor_signature = None
            # A disabled default stays silent. An enabled but unqualified floor
            # is reported once (and whenever readiness changes), so the operator
            # can distinguish "waiting for evidence" from a broken order path.
            if not enabled and not was_active:
                return None
            if enabled and previous_ready is False and not was_active:
                return None
            event = {
                "event": "small_account_mode",
                "engaged": False,
                "sufficient": True,
                "forward_floor_ready": ready,
                "policy_max_order_pct": str(policy_order),
                "effective_max_order_pct": str(policy_order),
                "policy_daily_notional_pct": str(policy_daily),
                "effective_daily_notional_pct": str(policy_daily),
                "equity": str(equity),
                "min_order_notional": str(broker_minimum),
                "target_notional": str(target),
                "small_account_max_order_pct": str(order_ceiling),
                "small_account_max_daily_pct": str(daily_ceiling),
                "readiness_blockers": readiness_blockers,
                "reason": (
                    "small-account mode is disabled; policy caps are in force"
                    if not enabled
                    else "the $5 forward floor is held until the exact size "
                    "passes retrospective, cost, stress and drawdown gates"
                ),
            }
            self.journal.append(event)
            return event

        effective_order = sizer.floor_cap(
            equity=equity,
            policy_cap=policy_order,
            min_notional=target,
            max_cap=order_ceiling,
        )
        needs_floor = needed > policy_order
        effective_daily = policy_daily
        if needs_floor:
            # The daily ceiling is an actual ceiling while floor mode is active,
            # even when the ordinary policy allows more. With equal order/daily
            # ceilings, one floor-sized entry exhausts the opening budget.
            effective_daily = min(max(policy_daily, needed), daily_ceiling)
        sufficient = bool(effective_order >= needed and effective_daily >= needed)
        if not sufficient:
            # A ceiling that almost fits the target is not permission for an
            # almost-target order. Keep the ordinary caps so the effective
            # minimum below refuses the entry; this is also what the simulator
            # models when capital falls under the authorized threshold.
            effective_order = policy_order
            effective_daily = policy_daily
        self.guard.max_order_pct = effective_order
        self.guard.daily_notional_pct = effective_daily
        self._effective_min_order_notional = target
        engaged = bool(
            effective_order != policy_order or effective_daily != policy_daily
        )
        signature = (engaged, sufficient, ready, effective_order, effective_daily)
        previous_signature = getattr(self, "_size_floor_signature", None)
        self._size_floor_signature = signature
        self._size_floor_active = engaged
        self._size_floor_sufficient = sufficient
        self._size_floor_ready = ready
        if signature == previous_signature:
            return None

        event = {
            "event": "small_account_mode",
            "engaged": engaged,
            "sufficient": sufficient,
            "forward_floor_ready": ready,
            "needed_max_order_pct": str(needed.quantize(Decimal("0.000001"))),
            "policy_max_order_pct": str(policy_order),
            "effective_max_order_pct": str(effective_order),
            "policy_daily_notional_pct": str(policy_daily),
            "effective_daily_notional_pct": str(effective_daily),
            "equity": str(equity),
            "min_order_notional": str(broker_minimum),
            "target_notional": str(target),
            "small_account_max_order_pct": str(order_ceiling),
            "small_account_max_daily_pct": str(daily_ceiling),
            "reason": (
                "the exact $5 floor passed retrospective gates and is enabled "
                "for forward collection; live trading still requires the full "
                "forward gate and arming"
                if sufficient
                else "the qualified target cannot fit inside the authorized "
                "small-account order and daily ceilings; raise "
                "small_account_max_order_pct and small_account_max_daily_pct "
                f"to at least {needed.quantize(Decimal('0.0001'))}"
            ),
        }
        measured = self._evidence_drawdown(effective_order)
        event["drawdown_at_effective_pct"] = measured
        if measured is not None:
            event["reason"] = (
                f"{event['reason']}; walk-forward max drawdown at this size is "
                f"{measured:.2f}% (15% ceiling)"
            )
        self.journal.append(event)
        return event

    def _evidence_drawdown(self, per_order_pct: Decimal) -> Optional[float]:
        """Measured max drawdown at (or nearest to) this per-order size."""
        from agentic_trading.evidence import read_report

        try:
            report = read_report(self.config) or {}
        except Exception:  # noqa: BLE001 — a missing report must not block sizing
            return None
        rows = [
            row
            for row in (report.get("size_frontier") or [])
            if isinstance(row, dict) and row.get("per_order_pct")
        ]
        if not rows:
            return None
        wanted = float(per_order_pct)
        nearest = min(rows, key=lambda row: abs(float(row["per_order_pct"]) - wanted))
        if abs(float(nearest["per_order_pct"]) - wanted) > max(0.005, wanted * 0.5):
            return None
        value = nearest.get("max_drawdown_pct")
        return None if value is None else float(value)

    def reconcile_stage_mode(self) -> Optional[dict[str, Any]]:
        """Make the run mode agree with the stage the evidence earned.

        A stage can be reached on a path that never applied it (a promotion
        recorded from the walk-forward regrade while the search path — which
        owns the mode flip — did not run), which leaves the agent promoted on
        paper and shadow in practice. Reconciling at startup and after every
        stage change is cheaper than trusting two code paths to stay identical.
        """
        from agentic_trading import selfimprove
        from agentic_trading.promotion import load_state

        state = load_state(self.config.state_dir)
        if state.stage == "shadow":
            # The stage can raise the mode; only a demotion or the operator may
            # lower it. Forcing shadow here would override an operator who
            # configured mode = "live" on purpose.
            return None
        target = "live"
        current = effective_mode(self.config)
        if target == current:
            return None
        if not (self.config.autonomy == "auto" and selfimprove.autonomy_enabled()):
            # Raising risk needs the operator's consent switch.
            event = {
                "event": "stage_mode_blocked",
                "stage": state.stage,
                "mode": current,
                "hint": 'set autonomy = "auto" and AGENTIC_ALLOW_AUTONOMY=1',
            }
            self.journal.append(event)
            return event
        event = {
            "event": "stage_mode_reconciled",
            "stage": state.stage,
            "from": current,
            "mode": target,
            "autonomy": self.config.autonomy,
        }
        for applied in selfimprove.apply_stage(self.config, state):
            event.setdefault("applied", []).append(applied)
        self.set_mode(target)
        self.journal.append(event)
        return event

    def apply_correlation_policy(self) -> None:
        """Give the guard the concentration limit and the measured correlations."""
        from agentic_trading.correlation import load_state

        configured = self.config.max_correlated_positions
        self.guard.max_correlated_positions = configured
        self.guard.correlation_threshold = self.config.correlation_threshold
        self.guard._correlation_state = (  # noqa: SLF001 — guard-owned policy
            load_state(self.config.state_dir) if configured is not None else None
        )

    def set_mode(self, mode: str) -> None:
        """Switch shadow/live at runtime (autonomy promotion or demotion)."""
        if mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        if self.force_shadow and mode == "live":
            return
        self.mode = mode
        self.guard.mode = mode
        self.apply_stage_caps()
        self.write_live_gate_state()

    def write_live_gate_state(self) -> None:
        """Record whether the daemon is armed to submit real orders.

        The console runs in its own process and cannot read the daemon's
        environment, so the daemon publishes what it actually sees. Without
        this the difference between "shadow because the evidence is thin" and
        "live but disarmed" is invisible to the operator.
        """
        from agentic_trading import jsonio

        payload = {
            "mode": self.mode,
            "stage": self.stage,
            "autonomy": self.config.autonomy,
            "allow_live": self.armed_for_submission(),
            "allow_autonomy": os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = Path(self.config.state_dir) / "live_gate.json"
        try:
            jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")
        except OSError:
            return
        self.journal.append({"event": "live_gate", **payload})

    def note_agent(
        self,
        name: str,
        *,
        ok: bool,
        detail: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        """Record what an agent just did, so its health is observed not assumed."""
        stamp = datetime.now(timezone.utc).isoformat()
        entry = self.agent_runs.setdefault(
            name,
            {
                "last_run_at": "",
                "last_ok_at": "",
                "consecutive_failures": 0,
                "errors": 0,
                "last_error": "",
            },
        )
        entry["last_run_at"] = stamp
        if ok:
            entry["last_ok_at"] = stamp
            entry["consecutive_failures"] = 0
            entry["last_error"] = ""
        else:
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1
            )
            entry["errors"] = int(entry.get("errors", 0)) + 1
            entry["last_error"] = str(error)[:300]
        if detail:
            entry.setdefault("detail", {}).update(detail)

    def jev_context_views(self) -> int:
        return 0 if self.entry_advisor is None else len(self.entry_advisor.views())

    def write_agent_state(self) -> None:
        """Publish what every worker in this process is doing.

        The bot is not one loop: a mechanical strategy, an LLM veto, an LLM
        regime gate, a RiskGuard, an evolution worker, a regime worker and a
        notifier all make decisions together. The console can only show that if
        the daemon writes it down, so this is the roster it reads.
        """
        from agentic_trading import jsonio

        eval_thread = getattr(self, "eval_thread", None)
        regime_thread = getattr(self, "regime_thread", None)
        advisor_ok = self.advisor is not None
        channels: list[str] = []
        notifier = getattr(self, "notifier", None)
        if notifier is not None:
            channels = [channel.name for channel in notifier.channels]
        from agentic_trading import agents as fleet

        runs = self.agent_runs
        declared = {agent.name: agent.to_dict() for agent in fleet.FLEET}

        def _entry(name: str, **extra: Any) -> dict[str, Any]:
            run = dict(runs.get(name) or {})
            detail = {**(run.pop("detail", None) or {}), **extra.pop("detail", {})}
            return {**declared[name], **run, **extra, "detail": detail}

        agents = [
            _entry(
                "strategy",
                kind="local",
                status="running" if not self.should_stop() else "stopped",
                detail={"strategy": self.config.strategy},
            ),
            _entry(
                "execution",
                kind="local",
                status="tripped" if self.guard.kill_switch else "running",
                reason=self.guard.kill_reason,
                detail={
                    "max_order_pct": str(self.guard.max_order_pct),
                    "daily_notional_pct": str(self.guard.daily_notional_pct),
                    "armed": os.environ.get("AGENTIC_ALLOW_LIVE") == "1",
                },
            ),
            _entry("data", kind="worker"),
            _entry("research", kind="worker", stage=self.stage),
            _entry("backcheck", kind="worker"),
            {
                "name": "entry context (jev)",
                "role": "chase and participation judgments, cached",
                "kind": "llm",
                "status": "running" if self.entry_advisor is not None else "disabled",
                "model": getattr(self.entry_advisor, "model", ""),
                "views": self.jev_context_views(),
                "calls": getattr(self.entry_advisor, "calls", 0),
                "errors": getattr(self.entry_advisor, "errors", 0),
                "veto": self.config.jev_veto_chase,
            },
            {
                "name": "advisor",
                "role": "LLM entry veto, may only refuse",
                "kind": "llm",
                "status": "running" if advisor_ok else "disabled",
                "model": getattr(self.advisor, "model", ""),
                "calls": len(getattr(self.advisor, "decisions", None) or []),
                "errors": getattr(self.advisor, "errors", 0),
                "cache_hits": getattr(self.advisor, "cache_hits", 0),
                "reused": getattr(self.advisor, "cache_hits", 0),
                "budget_skips": getattr(self.advisor, "budget_skips", 0),
                "last_error": getattr(self.advisor, "last_error", ""),
            },
            {
                "name": "regime",
                "role": "LLM regime gate, may only block entries",
                "kind": "llm",
                "status": "running" if self.regime_gate is not None else "disabled",
                "model": getattr(self.regime_gate, "model", ""),
                "views": len(self.regime_gate.views()) if self.regime_gate else 0,
                "errors": getattr(self.regime_gate, "errors", 0),
                "worker": bool(regime_thread and regime_thread.is_alive()),
            },
            {
                "name": "risk guard",
                "role": "hard caps, kill switch, whitelist",
                "kind": "local",
                "status": "tripped" if self.guard.kill_switch else "running",
                "reason": self.guard.kill_reason,
                "max_order_pct": str(self.guard.max_order_pct),
                "daily_notional_pct": str(self.guard.daily_notional_pct),
            },
            {
                "name": "evolution",
                "role": "self-evaluation and promotion gate",
                "kind": "worker",
                "status": (
                    "running" if eval_thread and eval_thread.is_alive() else "idle"
                ),
                "stage": self.stage,
                "session_policy": self.session_policy,
            },
            {
                "name": "notifier",
                "role": "operator alerts",
                "kind": "worker",
                "status": "running" if notifier is not None else "disabled",
                "channels": channels,
                "sent": getattr(notifier, "sent", 0),
                "failures": getattr(notifier, "failures", 0),
            },
        ]
        payload = {
            "pid": os.getpid(),
            "mode": self.mode,
            "stage": self.stage,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "agents": agents,
        }
        try:
            jsonio.write_text(
                Path(self.config.state_dir) / "agents.json",
                jsonio.dumps(payload, indent=2) + "\n",
            )
        except OSError:
            return

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # The failure alert leaves a marker; a successful start is the other half
        # of that conversation, and how long the outage lasted is worth knowing.
        marker = Path(self.config.state_dir) / "STOPPED_AT.txt"
        if marker.is_file():
            try:
                stopped = marker.read_text(encoding="utf-8").strip()
            except OSError:
                stopped = ""
            marker.unlink(missing_ok=True)
            self.journal.append(
                {"event": "recovered_after_stop", "stopped_at": stopped}
            )
        from agentic_trading import account

        account.note_session_start(self.config.state_dir)
        self.resolve_account()
        self.refresh_equity()
        account.note_equity(self.config.state_dir, self.guard.current_equity)
        self.seed_strategy_positions()
        self.write_live_gate_state()
        self.write_agent_state()

    def seed_strategy_positions(self) -> None:
        """Tell the strategy what is actually held before it decides anything.

        A restarted strategy otherwise believes the book is empty: it re-runs
        the daily rebalance, and it can never emit an exit for a position it
        does not know it has. The runtime owns the truth, so it hands it over.

        Shadow truth is the complete simulated-fill replay. Live truth is only
        the broker's position book: an ``accepted`` live journal record is an
        approval written before arming/placement and must never become a fill.
        """
        seed = getattr(self.strategy, "seed_positions", None)
        if not callable(seed):
            return
        try:
            if self.mode == "shadow":
                snapshot = self.shadow_book.as_snapshot()
            else:
                snapshot = self.live_positions()
        except Exception as exc:  # noqa: BLE001 — never block start-up
            self.journal.append(
                {"event": "strategy_seed_failed", "error": str(exc)[:200]}
            )
            return
        if snapshot.positions_read_failed:
            self.journal.append(
                {
                    "event": "strategy_seed_failed",
                    "error": "the broker did not return a usable position book",
                }
            )
            return
        held = {
            symbol: quantity
            for symbol, quantity in snapshot.held.items()
            if quantity > 0
        }
        current = {symbol: str(quantity) for symbol, quantity in held.items()}
        try:
            count = seed(current)
        except Exception as exc:  # noqa: BLE001
            self.journal.append(
                {"event": "strategy_seed_failed", "error": str(exc)[:200]}
            )
            return
        self._strategy_book = current
        self.journal.append(
            {
                "event": "strategy_seeded",
                "positions": count,
                "held": sorted(current),
                "source": "shadow_journal" if self.mode == "shadow" else "broker",
                "mode": self.mode,
            }
        )

    def adopt_universe(self, symbols: Iterable[str]) -> bool:
        """Take on a universe the scout has changed — without a restart.

        Everything downstream reads ``config.effective_whitelist``, so the guard,
        the evidence gate, the self-check and the history refresh all follow the
        new list on their next pass. The two things built *from* the old list are
        the strategy (constructed for a symbol list) and the quote feed, so the
        strategy is rebuilt and re-seeded here and the daemon loop rebuilds the
        feed when it sees ``universe_dirty``.

        A strategy that cannot be rebuilt for the new universe is worse than one
        that is a pass behind, so a failure rolls the change back and says so.
        """
        adopted = (
            frozenset(str(s).upper() for s in symbols) - self.config.symbol_whitelist
        )
        if adopted == self.config.discovered_symbols:
            return False
        previous = self.config.discovered_symbols
        self.config = replace(self.config, discovered_symbols=adopted)
        self.guard.whitelist = self.config.effective_whitelist
        if callable(self.strategy_factory):
            try:
                self.strategy = self.strategy_factory(self.config)
            except Exception as exc:  # noqa: BLE001 — keep the working universe
                self.config = replace(self.config, discovered_symbols=previous)
                self.guard.whitelist = self.config.effective_whitelist
                self.journal.append(
                    {
                        "event": "universe_apply_failed",
                        "error": str(exc)[:200],
                        "kept": sorted(previous),
                    }
                )
                return False
            self.seed_strategy_positions()
        self.universe_dirty = True
        self.journal.append(
            {
                "event": "universe_changed",
                "adopted": sorted(adopted),
                "added": sorted(adopted - previous),
                "removed": sorted(previous - adopted),
                "tradeable": sorted(self.config.effective_whitelist),
                "note": (
                    "the scout changed which symbols the book may hold; the "
                    "guard, the strategy and the quote feed now use the new list"
                ),
            }
        )
        return True

    def reconcile_strategy_positions(self) -> bool:
        """Keep the strategy's position book equal to the broker's, not to startup.

        ``seed_strategy_positions`` ran once, at start-up. In shadow mode that is
        enough, because the runtime applies every simulated fill and calls
        ``note_fill``. In live mode it is not: a placed order never reached the
        strategy, so the strategy kept believing it held nothing. Two things
        followed from that, and both are the opposite of the design:

        - ``entering = targets - held`` re-emitted a buy for a symbol the book
          already owned, every single day, until the position-count limit
          stopped it — stacking one order per symbol per day;
        - ``exiting = held - targets`` could not fire for a live position at all,
          so an exit only ever happened if the daemon was restarted (which is
          exactly when the re-seed ran).

        The runtime owns the truth, so it hands it over whenever the truth
        changes. A failed read changes nothing: wiping the book on a broker
        hiccup would lose the exits it exists to produce.
        """
        if self.mode != "live":
            return False
        seed = getattr(self.strategy, "seed_positions", None)
        if not callable(seed):
            return False
        try:
            snapshot = self.live_positions()
        except Exception as exc:  # noqa: BLE001 — a read failure must not wipe it
            self.journal.append(
                {"event": "strategy_reconcile_failed", "error": str(exc)[:200]}
            )
            return False
        if snapshot.positions_read_failed:
            self.journal.append(
                {
                    "event": "strategy_reconcile_failed",
                    "error": "the broker did not return a usable position book",
                }
            )
            return False
        held = {
            symbol: quantity
            for symbol, quantity in snapshot.held.items()
            if quantity > 0
        }
        current = {symbol: str(quantity) for symbol, quantity in held.items()}
        if current == self._strategy_book:
            return False
        try:
            count = seed(current)
        except Exception as exc:  # noqa: BLE001
            self.journal.append(
                {"event": "strategy_reconcile_failed", "error": str(exc)[:200]}
            )
            return False
        added = sorted(set(current) - set(self._strategy_book))
        gone = sorted(set(self._strategy_book) - set(current))
        self._strategy_book = current
        self.journal.append(
            {
                "event": "strategy_reconciled",
                "positions": count,
                "held": sorted(current),
                "opened": added,
                "closed": gone,
                "note": (
                    "the strategy's book now matches the account, so it can "
                    "size the next entry and exit what it holds"
                ),
            }
        )
        return True

    def held_symbols(self) -> set[str]:
        """What the book holds now — the scout's input for its duplicate test.

        Never raises: a scout that cannot read the book grades against an empty
        one and finds fewer duplicates, which is the harmless direction.
        """
        try:
            if self.mode == "shadow":
                held = dict(self.shadow_book.as_snapshot().held)
            else:
                held = dict(self.live_positions().held)
        except Exception:  # noqa: BLE001
            return set()
        return {symbol.upper() for symbol, quantity in held.items() if quantity > 0}

    def held_from_journal(self, *, days: int = 10) -> dict[str, Decimal]:
        """Net position per symbol from recent *accepted* decisions.

        Accepted means "the runtime treated it as filled" in shadow, and "it was
        submitted" in live — either way it is the best record of what the book
        should contain once the day-scoped view has moved on.
        """
        held: dict[str, Decimal] = {}
        today = date.today()
        for offset in range(days - 1, -1, -1):
            path = (
                Path(self.config.journal_dir)
                / (today - timedelta(days=offset)).isoformat()
            )
            file = path.with_suffix(".jsonl")
            if not file.is_file():
                continue
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") != "accepted":
                    continue
                intent = (
                    record.get("intent")
                    if isinstance(record.get("intent"), dict)
                    else {}
                )
                symbol = str(record.get("symbol") or intent.get("symbol") or "").upper()
                quantity = record.get("quantity") or intent.get("quantity")
                if not symbol or quantity in (None, ""):
                    continue
                try:
                    amount = Decimal(str(quantity))
                except (TypeError, ValueError, ArithmeticError):
                    continue
                if str(record.get("side")).lower() == "sell":
                    amount = -amount
                held[symbol] = held.get(symbol, Decimal("0")) + amount
        return {symbol: quantity for symbol, quantity in held.items() if quantity > 0}

    def resolve_account(self) -> str:
        if self.account_number:
            return self.account_number
        self.account_number = self.broker.resolve_account_number()
        return self.account_number

    def finish(self) -> None:
        self.guard.persist(self.config.state_dir)

    def request_stop(self) -> None:
        self._stopping = True

    def should_stop(self) -> bool:
        return self._stopping or (
            self.stop_event is not None and self.stop_event.is_set()
        )

    # -- state ------------------------------------------------------------

    @property
    def trades_crypto(self) -> bool:
        # The effective universe, not just the operator's list: a pair the scout
        # adopted is a pair whose position book has to be read, whose exits have
        # to be routed through the crypto tools, and whose session never closes.
        return any(is_crypto_symbol(s) for s in self.config.effective_whitelist)

    def live_positions(self) -> PortfolioSnapshot:
        """Real holdings across both books, as the guard must see them.

        Robinhood reports equity and crypto holdings separately, so a live loop
        that reads only ``get_equity_positions`` sees a crypto position as
        nothing: entries stack past ``max_open_positions`` and exits are denied
        as ``would_short``. If the crypto book cannot be read while the
        whitelist trades pairs, the merged snapshot fails closed instead of
        reporting "flat".
        """
        equity = self.broker.get_positions()
        if not self.trades_crypto:
            return equity
        crypto = self.broker.get_crypto_position_snapshot()
        if crypto.positions_read_failed:
            return PortfolioSnapshot(
                open_positions=0, held={}, positions_read_failed=True
            )
        held = dict(equity.held)
        held.update({symbol: qty for symbol, qty in crypto.held.items() if qty > 0})
        return PortfolioSnapshot(
            open_positions=len(held),
            held=held,
            positions_read_failed=equity.positions_read_failed,
        )

    def refresh_equity(self) -> None:
        try:
            equity = self.broker.get_equity()
        except BrokerPayloadError as exc:
            self.journal.append(
                {
                    "event": "equity_read_failed",
                    "error": str(exc),
                    "mode": self.mode,
                }
            )
            raise
        self.guard.update_equity(equity)
        self.guard.persist(self.config.state_dir)

    def note_open_orders(self) -> None:
        """Record open orders and remember symbols with pending entries."""
        # Each broker read costs ~1.3s over the remote MCP gateway, and this one
        # runs twice once crypto is in play. Pending entries only change when we
        # place something or the broker fills it, so poll it on an interval
        # instead of on every cycle.
        now = time.monotonic()
        last = self._open_orders_read_at
        if last and (now - last) < self.config.open_order_refresh_seconds:
            return
        try:
            # Live schema rejects state_group; filter open states client-side.
            orders: list[dict[str, Any]] = [
                order
                for order in self.broker.get_orders()
                if str(order.get("state", "")).lower()
                in ("queued", "confirmed", "unconfirmed", "new", "partially_filled")
            ]
            if self.trades_crypto:
                # Crypto orders live in their own book; without this a pending
                # crypto entry is invisible and a second one could be stacked
                # on top of it before the first fills.
                crypto = self.broker.get_crypto_orders()
                orders.extend(
                    _with_pair_symbol(order)
                    for order in crypto
                    if str(order.get("state", "")).lower()
                    in (
                        "queued",
                        "confirmed",
                        "unconfirmed",
                        "new",
                        "partially_filled",
                    )
                )
        except Exception as exc:  # noqa: BLE001 — never crash the loop on reads
            self.open_orders_read_failed = True
            # Throttle a broken remote endpoint just like a successful read.
            # Leaving this at zero retries on every quote and can turn one
            # outage into a request storm.
            self._open_orders_read_at = time.monotonic()
            self.journal.append({"event": "open_orders_read_failed", "error": str(exc)})
            return

        symbols: set[str] = set()
        sides: dict[str, set[str]] = {}
        summary: list[dict[str, Any]] = []
        for order in orders:
            symbol = str(
                order.get("symbol") or order.get("instrument_symbol") or ""
            ).upper()
            if symbol:
                symbols.add(symbol)
                side = str(order.get("side") or "").strip().lower()
                if side:
                    sides.setdefault(symbol, set()).add(side)
            summary.append(
                {
                    "order_id": order.get("id") or order.get("order_id"),
                    "symbol": symbol,
                    "side": order.get("side"),
                    "state": order.get("state"),
                    "quantity": order.get("quantity"),
                }
            )
        self.open_order_symbols = symbols
        self.open_order_sides = sides
        self.open_orders_read_failed = False
        # A symbol that is no longer working can be noted again if it comes back.
        self._exit_pending_noted &= {
            symbol for symbol, working in sides.items() if "sell" in working
        }
        self._open_orders_read_at = time.monotonic()
        if summary:
            self.journal.append(
                {"event": "open_orders", "count": len(summary), "orders": summary}
            )

    # -- pipeline ---------------------------------------------------------

    def handle_quote(self, quote: dict, *, session: str = "regular") -> None:
        if self.should_stop():
            return
        # An approved order that could not be submitted outranks a new decision:
        # submit it first, so the strategy's fresh rebalance cannot take the
        # budget it was already promised.
        self.flush_deferred()
        # A retry is pending but not yet due. The strategy holds no "decided
        # today" marker at this point, so the rebalance runs again the moment
        # this window closes rather than being lost for the day.
        if time.monotonic() < self._rebalance_retry_at:
            return
        # A strategy cannot see the market's clock through a quote, and it must
        # not have to: the runtime knows the session, so it says so. The trend
        # strategy decides its equity book only while that book can be ordered.
        intents = self.strategy.on_quote({**quote, "market_session": session})
        outcomes: list[Optional[str]] = []
        for intent in intents:
            if self.should_stop():
                return
            outcomes.append(self.process_intent(intent, session=session))
        self.maybe_retry_rebalance(outcomes)

    def maybe_retry_rebalance(self, outcomes: list[Optional[str]]) -> bool:
        """Give a day's rebalance another attempt when nothing could be decided.

        A once-a-day strategy has exactly one shot. On 2026-09-18 the shot was
        taken at 00:00 UTC while the per-order cap was still below the broker's
        minimum, so all five entries were refused and the day traded nothing —
        a technical refusal that silently became a trading decision.

        Only an *all-technical* outcome is retried, and the retry is released to
        the strategy (so its day marker is cleared) rather than forced: the
        strategy re-decides from the tape, it is not told what to send.
        """
        if not outcomes or any(outcome != RETRY for outcome in outcomes):
            return False
        day = str(getattr(self.strategy, "last_decided_day", "") or "")
        release = getattr(self.strategy, "release_decision", None)
        if not day or not callable(release):
            return False
        if self._rebalance_retry_day != day:
            self._rebalance_retry_day = day
            self._rebalance_retries = 0
        limit = int(self.config.rebalance_max_retries)
        if self._rebalance_retries >= limit:
            self.journal.append(
                {
                    "event": "rebalance_abandoned",
                    "day": day,
                    "attempts": self._rebalance_retries,
                    "hint": "the next rebalance is the next trading day",
                }
            )
            return False
        if not release(day):
            return False
        self._rebalance_retries += 1
        wait = float(self.config.rebalance_retry_seconds)
        self._rebalance_retry_at = time.monotonic() + wait
        self.journal.append(
            {
                "event": "rebalance_retry",
                "day": day,
                "attempt": self._rebalance_retries,
                "of": limit,
                "retry_in_seconds": round(wait, 1),
                "reason": (
                    "the day's orders were refused for a technical reason, so "
                    "the decision was returned to the strategy instead of "
                    "being spent"
                ),
            }
        )
        return True

    def process_intent(
        self,
        intent: OrderIntent,
        *,
        session: str = "regular",
        advised: bool = False,
        reservation_of: Optional[str] = None,
        reserved_notional: Decimal = Decimal("0"),
        deferred_root: Optional[str] = None,
    ) -> Optional[str]:
        """Take one intent through sizing, judgement, the guard and placement.

        ``advised`` says the advisory layer (Jev's entry read, the regime gate,
        the LLM's entry veto) has already ruled on this exact decision and its
        verdicts are on record. It is set only by ``flush_deferred`` when an
        intent that had *already passed every judgement* is resubmitted after
        the mechanical obstacle cleared — so re-asking the models cannot become
        a way to shop for a different answer. The guard is never skipped.
        """
        if self.journal.has_decision(intent.decision_id):
            return

        side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
        is_entry = side is Side.BUY

        if self.mode == "live" and self.open_orders_read_failed:
            # An unknown order book is not an empty order book. Entries could
            # stack exposure and exits could duplicate a working sell, so no
            # live write is safe until the broker read recovers.
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "decision_deferred",
                    "reason": "open_orders_read_failed",
                    "symbol": intent.symbol,
                    "side": side.value,
                    "hint": "waiting for a successful broker open-order read",
                }
            )
            return RETRY

        # Fit entries to the per-order cap before the guard sees them: a small
        # account must trade smaller, not refuse to trade at all.
        if self.config.equity_sizing and is_entry:
            from agentic_trading.sizer import size_intent

            sized = size_intent(
                intent,
                equity=self.guard.current_equity,
                max_order_pct=self.guard.max_order_pct,
                min_notional=Decimal(
                    str(
                        getattr(
                            self,
                            "_effective_min_order_notional",
                            self.config.min_order_notional,
                        )
                    )
                ),
                proportional=self.config.sizing == "proportional",
            )
            if sized is None:
                if self.guard.current_equity <= 0:
                    # The account value has not been read yet, so "this order is
                    # too small" is not a fact about the trade — it is a fact
                    # about the clock. Defer it instead of spending the day.
                    self.journal.append(
                        {
                            "decision_id": intent.decision_id,
                            "event": "decision_deferred",
                            "reason": "equity_pending",
                            "symbol": intent.symbol,
                            "side": side.value,
                            "hint": (
                                "waiting for the first account-value read before "
                                "sizing this order"
                            ),
                        }
                    )
                    return RETRY
                self._journal_rejected(intent, "below_min_notional", Decimal("0"))
                return
            if sized.quantity != intent.quantity:
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "resized",
                        "reason": "equity_cap",
                        "from_quantity": str(intent.quantity),
                        "to_quantity": str(sized.quantity),
                        "equity": str(self.guard.current_equity),
                        "max_order_pct": str(self.guard.max_order_pct),
                    }
                )
            intent = sized

        reserved_entry = bool(is_entry and reservation_of)
        counted_orders = self.orders_today - (1 if reserved_entry else 0)
        if is_entry and counted_orders >= self.config.max_orders_per_day:
            self._journal_rejected(intent, "max_orders_per_day", Decimal("0"))
            return
        if is_entry:
            from agentic_trading.execution import (
                measured_cost_usd,
                required_notional_for_cost,
            )

            share = float(getattr(self.config, "max_cost_share_of_order", 0) or 0)
            required = required_notional_for_cost(
                self.config.state_dir, max_share=share, symbol=intent.symbol
            )
            notional = intent.resolved_notional()
            if required and notional < Decimal(str(required)):
                cost = (
                    measured_cost_usd(self.config.state_dir, symbol=intent.symbol)
                    or 0.0
                )
                self._journal_rejected(
                    intent,
                    "cost_too_high_for_size: "
                    f"a ${cost:.2f} round trip on ${notional:.2f} is "
                    f"{cost / max(float(notional), 1e-9) * 100:.1f}%",
                    notional,
                )
                return
        if (
            is_entry
            and not is_crypto_symbol(intent.symbol)
            and session != "regular"
            and intent.quantity is not None
            and intent.quantity != intent.quantity.to_integral_value()
        ):
            # Backstop for the strategy's own session gate. A *fractional* equity
            # order cannot be placed outside regular hours, and opening a
            # position that cannot be exited is the one thing this system does
            # not do. A whole-share order is fine out of hours — it goes in as a
            # marketable limit — so this defers only the case it has to.
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "decision_deferred",
                    "reason": "session_closed_for_equities",
                    "symbol": intent.symbol,
                    "session": session,
                    "hint": (
                        "equities are decided while the regular session is open, "
                        "where a fractional order can also be exited"
                    ),
                }
            )
            return RETRY
        if is_entry and intent.symbol.upper() in self.open_order_symbols:
            self._journal_rejected(intent, "open_order_pending", Decimal("0"))
            return

        # An exit is re-emitted every cycle by design, so the runtime is what
        # stops "every cycle" from becoming "an order every cycle". While the
        # first sell is still working there is nothing to add, and the guard's
        # fresh position read is the second line of defence if this one misses.
        if not is_entry and "sell" in self.open_order_sides.get(
            intent.symbol.upper(), set()
        ):
            symbol = intent.symbol.upper()
            if symbol not in self._exit_pending_noted:
                self._exit_pending_noted.add(symbol)
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "exit_skipped",
                        "symbol": symbol,
                        "note": (
                            "a sell for this position is already working; the "
                            "exit stays pending until it fills or is cancelled"
                        ),
                    }
                )
            return

        # Jev's entry judgment, read from cache: is this entry chasing a move
        # that has already run? Off by default — the judgment is recorded on
        # every order either way, and only vetoes when the operator asks it to.
        if is_entry and self.entry_advisor is not None and not advised:
            context = self.entry_advisor.view(intent.symbol) or {}
            chase = context.get("chase")
            if (
                self.config.jev_veto_chase
                and chase is not None
                and chase >= self.config.jev_chase_threshold
            ):
                self._journal_rejected(
                    intent,
                    f"jev_chase: p={float(chase):.2f}",
                    intent.resolved_notional(),
                )
                return

        # Regime gate: a bad regime may refuse entries, never create them. It
        # reads a cached classification, so this costs nothing on the order path.
        if is_entry and self.regime_gate is not None and not advised:
            blocked = self.regime_gate.blocks(intent.symbol)
            if blocked is not None:
                self._journal_rejected(
                    intent,
                    f"regime_block: {blocked.regime} c={blocked.confidence:.2f}"[:120],
                    intent.resolved_notional(),
                )
                return

        # Advisory veto: the model may refuse an entry (reduce risk) and its
        # hold opinion is recorded, but it never overrides a mechanical exit.
        advisor_payload: Optional[dict[str, Any]] = None
        if self.advisor is not None and not advised:
            features = self.market_features(intent.symbol)
            decision = self.advisor.review_entry(
                symbol=intent.symbol,
                side=side.value,
                ref_price=str(intent.ref_price or ""),
                quantity=str(intent.quantity or ""),
                reason=intent.reason,
                context={
                    "session": session,
                    "equity": str(self.guard.current_equity),
                    "stage": self.stage,
                    "role": "entry" if is_entry else "exit",
                    "market": features,
                },
            )
            if decision is not None:
                advisor_payload = {
                    "confidence": decision.confidence,
                    "action": decision.action,
                    "model": getattr(self.advisor, "model", ""),
                    "reused": bool(getattr(self.advisor, "last_reused", False)),
                }
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "advisor",
                        "model": getattr(self.advisor, "model", ""),
                        "role": "entry" if is_entry else "exit",
                        "symbol": intent.symbol,
                        "reused": bool(getattr(self.advisor, "last_reused", False)),
                        # What the model was shown, so its verdict is auditable
                        # rather than only quotable.
                        "market": features.to_dict() if features else None,
                        **decision.to_dict(),
                    }
                )
                if decision.vetoes and is_entry:
                    self._journal_rejected(
                        intent,
                        f"llm_veto: {decision.reason}"
                        if decision.reason
                        else "llm_veto",
                        intent.resolved_notional(),
                        advisor=advisor_payload,
                    )
                    return
            elif getattr(self.advisor, "last_error", ""):
                # An advisor that fails silently is indistinguishable from one
                # that does not exist; surface the reason instead.
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": (
                            "advisor_budget"
                            if "budget" in str(self.advisor.last_error)
                            else "advisor_error"
                        ),
                        "error": self.advisor.last_error,
                        "model": getattr(self.advisor, "model", ""),
                    }
                )

        if self.mode == "shadow":
            snapshot = self.shadow_book.as_snapshot()
        else:
            try:
                snapshot = self.live_positions()
            except Exception as exc:  # noqa: BLE001 — never trade without positions
                self._journal_rejected(
                    intent,
                    f"positions_read_error: {exc}",
                    Decimal("0"),
                    advisor=advisor_payload,
                )
                self.note_error("positions_read_error", exc)
                return RETRY

        try:
            decision = self.guard.evaluate(
                intent,
                snapshot,
                reserved_notional=(
                    reserved_notional if reserved_entry else Decimal("0")
                ),
            )
        except ValueError as exc:
            self._journal_rejected(
                intent, f"guard_error: {exc}", Decimal("0"), advisor=advisor_payload
            )
            return
        if not decision.allowed:
            self._journal_rejected(
                intent, decision.reason, decision.notional, advisor=advisor_payload
            )
            if not is_entry and decision.reason == "would_short":
                # We believed we held this and the broker says we do not: the
                # book moved under us. Re-read it instead of arguing with it.
                self.reconcile_strategy_positions()
            return

        try:
            account = (
                self.broker.resolve_rhs_account_number()
                if is_crypto_symbol(intent.symbol)
                else self.resolve_account()
            )
            request = build_order_request(
                intent,
                account_number=account,
                config=self.config,
                session=session,
            )
        except OrderValidationError as exc:
            self._journal_rejected(
                intent,
                f"order_invalid: {exc}",
                decision.notional,
                advisor=advisor_payload,
            )
            # A fractional order outside regular hours is refused by design —
            # the position could not be exited there. That is a "not yet", not
            # a rejection: the session opens and the same decision is valid.
            return RETRY if "regular_hours" in str(exc) else None

        # Review is a read-only simulation. It runs in both modes so the
        # operator sees the broker's pre-trade alerts before going live.
        try:
            review = self.broker.review_order(request)
        except Exception as exc:  # noqa: BLE001 — no review, no placement
            if (
                self.mode == "shadow"
                and side is Side.SELL
                and "invalid_request" in str(exc)
            ):
                # A paper position has no real counterpart, so the broker
                # refusing to preview selling it ("you can only sell up to 0")
                # says nothing about the paper exit. Record the exit with the
                # broker's answer attached rather than counting it towards the
                # kill switch. Transport failures still take the path below.
                review = {"shadow_only_position": True, "broker_error": str(exc)[:500]}
            else:
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "review_failed",
                        "error": str(exc),
                        "order_request": request.to_mcp_args(),
                    }
                )
                self.note_error("review_failed", exc)
                # The judgement is done and the guard said yes; only the broker
                # call failed. Hold it rather than making the strategy re-decide.
                if deferred_root is None:
                    self.defer_intent(intent, session=session, why="review_failed")
                return RETRY
        self.journal.append(
            {
                "decision_id": intent.decision_id,
                "event": "accepted",
                "reason": decision.reason,
                "would_place": True,
                "may_place": decision.may_place,
                "notional": str(decision.notional),
                "mode": self.mode,
                "session": session,
                "order_request": request.to_mcp_args(),
                "review": review,
                "intent": _intent_payload(intent),
                "confidence": self._confidence_payload(
                    advisor_payload, symbol=intent.symbol
                ),
                "symbol": intent.symbol,
                "side": side.value,
                "quantity": (
                    str(intent.quantity) if intent.quantity is not None else None
                ),
                "ref_price": (
                    str(intent.ref_price) if intent.ref_price is not None else None
                ),
                "reservation_of": reservation_of,
            }
        )
        if is_entry and not reserved_entry:
            self.guard.record_accepted(intent)
            self.orders_today += 1

        if self.mode == "shadow":
            # NEVER place_order when mode == shadow
            self.shadow_book.apply_accepted(intent)
            self.guard.note_shadow_realized(self.shadow_book.realized_pnl)
            # Tell a strategy that tracks its own positions what actually filled,
            # otherwise its exits carry a stale quantity and RiskGuard rejects
            # them as oversell (leaving the shadow book unable to flatten).
            note_fill = getattr(self.strategy, "note_fill", None)
            if callable(note_fill) and intent.quantity is not None:
                # Signed: a sell reduces the strategy's book, otherwise it
                # accumulates phantom positions and never exits them.
                note_fill(
                    intent.symbol,
                    -intent.quantity if side is Side.SELL else intent.quantity,
                )
            self.guard.persist(self.config.state_dir)
            return

        if self.should_stop():
            self.guard.persist(self.config.state_dir)
            return

        if not (decision.may_place and self.armed_for_submission()):
            if decision.may_place:
                # The evidence gate has cleared and the stage is live: the only
                # thing left is the operator's arming switch. Say so instead of
                # looking identical to a normal shadow cycle.
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "live_gate_blocked",
                        "reason": self.submission_gate_reason(),
                        "symbol": intent.symbol,
                        "side": side.value,
                        "notional": str(decision.notional),
                        "stage": self.stage,
                        "hint": (
                            "enable the machine submission capability and arm "
                            "the workspace after its evidence gate passes"
                        ),
                    }
                )
                # Everything passed except the operator's switch. Keep the
                # decision; if the switch closes later, the order still stands.
                self.defer_intent(
                    intent,
                    session=session,
                    why="not_armed",
                    reserved_notional=decision.notional,
                )
            self.guard.persist(self.config.state_dir)
            # The evidence gate and the broker both said yes; only the operator's
            # arming switch is closed. If they arm it later today, the decision
            # should still be live rather than a day old.
            return RETRY

        self._place(intent, request)
        self.guard.persist(self.config.state_dir)

    def _place(self, intent: OrderIntent, request: EquityOrderRequest) -> None:
        # Crypto never closes, so there is no session boundary to contain a bad
        # fill and no closing bell to flatten into. It therefore climbs the same
        # promotion ladder as everything else but has to reach the *top* of it:
        # probation-sized live trading is for instruments with a session close.
        # _place only runs in live mode, which makes this the right refusal point.
        if is_crypto_symbol(intent.symbol) and self.stage != "live":
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "place_refused",
                    "reason": "crypto_requires_live_stage",
                    "stage": self.stage,
                    "symbol": intent.symbol,
                }
            )
            return
        try:
            result = self.broker.place_order(request)
        except Exception as exc:  # noqa: BLE001 — record and count the failure
            # A transport failure does not prove the broker rejected the order:
            # it may have accepted the write and lost only the response. Treat
            # the order book as unknown immediately, so another intent in this
            # cycle cannot submit a duplicate. The daemon forces a fresh broker
            # read on the next cycle before allowing any more live writes.
            self.open_orders_read_failed = True
            self._open_orders_read_at = 0.0
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "place_failed",
                    "error": str(exc),
                    "order_request": request.to_mcp_args(),
                }
            )
            self.note_error("place_failed", exc)
            return

        self.consecutive_errors = 0
        # Remember in-process that this side is working: the broker's own
        # open-order list is only re-read every `open_order_refresh_seconds`,
        # and in that window an exit would otherwise be re-sent.
        self.open_order_sides.setdefault(intent.symbol.upper(), set()).add(
            "buy" if intent.side is Side.BUY else "sell"
        )
        self.journal.append(
            {
                "decision_id": intent.decision_id,
                "event": "placed",
                "order_request": request.to_mcp_args(),
                "order": result,
            }
        )
        # A fill is not instant, but reading the book straight after a placement
        # closes most of the window in which the strategy does not yet know what
        # it owns; the periodic reconcile closes the rest.
        self.reconcile_strategy_positions()

    def apply_auto_arm(self) -> Optional[dict[str, Any]]:
        """Run the pre-flight checklist and act on the verdict.

        This is the last link in the autonomous chain: the agent promotes
        itself on evidence, and — when the operator has enabled it — arms itself
        for real submission once every check passes. It undoes its own arming the
        moment a check fails, so a tripped kill switch or a stale report takes
        the account out of the market without waiting for anyone.
        """
        from agentic_trading import arming

        now = time.monotonic()
        if (now - getattr(self, "_last_auto_arm_at", 0.0)) < 30.0:
            return None
        self._last_auto_arm_at = now
        enabled = (
            bool(self.config.auto_arm)
            and os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1"
        )
        try:
            event = arming.maybe_auto_arm(
                self.config.state_dir,
                enabled=enabled,
                min_interval_hours=self.config.auto_arm_min_interval_hours,
            )
        except Exception as exc:  # noqa: BLE001 — never kill the loop
            self.journal.append({"event": "auto_arm_failed", "error": str(exc)[:200]})
            return None
        if event is None:
            return None
        self.journal.append({**event, "auto_arm_enabled": enabled})
        if event.get("event") == "auto_disarm":
            self.set_mode(self.mode)  # keep the guard in step
        return event

    def note_arming_snapshot(self) -> None:
        """Record the balance at each arming change, once per change."""
        from agentic_trading import account, arming

        state = arming.read_arm(self.config.state_dir) or {}
        account.note_armed(
            self.config.state_dir,
            self.guard.current_equity,
            armed=bool(state.get("armed")),
            at=str(state.get("at", "")),
            source=str(state.get("source", "")),
        )

    def armed_for_submission(self) -> bool:
        """May this session submit? Machine capability **and** eligible arm.

        The environment says this machine may ever submit; the workspace latch
        says this strategy has passed its current evidence, cost, stage, and
        kill-switch checks. Requiring both means neither a copied workspace nor
        a stale service environment can place an order by itself.
        """
        capability = (
            os.environ.get("AGENTIC_ALLOW_LIVE") == "1"
            or os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1"
        )
        if not capability:
            return False
        from agentic_trading.arming import is_armed

        return bool(is_armed(self.config.state_dir))

    def submission_gate_reason(self) -> str:
        capability = (
            os.environ.get("AGENTIC_ALLOW_LIVE") == "1"
            or os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1"
        )
        return (
            "workspace_not_eligible_or_armed"
            if capability
            else "submission_capability_not_enabled"
        )

    # -- intents that passed everything but could not be submitted ---------

    def defer_intent(
        self,
        intent: OrderIntent,
        *,
        session: str,
        why: str,
        reserved_notional: Decimal = Decimal("0"),
    ) -> None:
        """Park an intent that cleared every judgement but hit a mechanical wall.

        Two things land here: an order the evidence gate and the broker both
        approved that the arming switch refused, and one whose broker review
        call failed on the network. Both are *decisions already taken*, so the
        book is allowed to act on them when the wall comes down — on
        2026-09-18 the only entry that survived the daily rebalance was blocked
        by a switch that flipped 40 seconds later, and the approval was simply
        lost.

        Re-submission does **not** re-ask the models (see ``advised``): the way
        to keep a veto meaningful is to never shop for a second opinion.
        """
        if intent.decision_id in self.deferred_intents:
            return
        self.deferred_intents[intent.decision_id] = {
            "intent": intent,
            "session": session,
            "why": why,
            "at": time.monotonic(),
            "attempts": 0,
            "reserved_notional": Decimal(str(reserved_notional)),
        }

    def flush_deferred(self) -> int:
        """Resubmit deferred intents whose obstacle looks cleared. Returns count.

        Bounded in both directions: an intent older than
        ``deferred_max_age_seconds`` is dropped with a journal entry rather than
        submitted against a stale price, and an intent that keeps failing is
        dropped after ``deferred_max_attempts``.
        """
        if not self.deferred_intents:
            return 0
        if not self.armed_for_submission():
            return 0
        now = time.monotonic()
        max_age = float(self.config.deferred_max_age_seconds)
        max_attempts = int(self.config.deferred_max_attempts)
        flushed = 0
        for decision_id, entry in list(self.deferred_intents.items()):
            age = now - float(entry["at"])
            if age > max_age or int(entry["attempts"]) >= max_attempts:
                del self.deferred_intents[decision_id]
                self.journal.append(
                    {
                        "decision_id": decision_id,
                        "event": "deferral_expired",
                        "symbol": entry["intent"].symbol,
                        "reason": entry["why"],
                        "age_seconds": round(age, 1),
                        "attempts": entry["attempts"],
                    }
                )
                continue
            entry["attempts"] = int(entry["attempts"]) + 1
            retry = replace(
                entry["intent"],
                decision_id=new_decision_id(),
                metadata={
                    **(entry["intent"].metadata or {}),
                    "retry_of": decision_id,
                },
            )
            reserved = Decimal(str(entry.get("reserved_notional") or "0"))
            outcome = self.process_intent(
                retry,
                session=str(entry["session"]),
                advised=True,
                reservation_of=decision_id if reserved > 0 else None,
                reserved_notional=reserved,
                deferred_root=decision_id,
            )
            # Written *after* the attempt, and keyed on the original decision:
            # a record carrying the retry's own id would make the idempotency
            # guard treat the decision as already taken and swallow it.
            self.journal.append(
                {
                    "decision_id": decision_id,
                    "retry_decision_id": retry.decision_id,
                    "event": "resubmitted",
                    "symbol": retry.symbol,
                    "side": (
                        retry.side.value
                        if isinstance(retry.side, Side)
                        else str(retry.side)
                    ),
                    "attempt": entry["attempts"],
                    "was_blocked_by": entry["why"],
                    "outcome": "submitted" if outcome != RETRY else "still_blocked",
                    "note": (
                        "every judgement on this order stands; only the "
                        "mechanical obstacle is being retried"
                    ),
                }
            )
            if outcome != RETRY:
                del self.deferred_intents[decision_id]
                flushed += 1
        return flushed

    def note_error(self, event: str, error: Exception) -> None:
        """Count a broker/loop failure and trip the kill switch if it persists."""
        self.consecutive_errors += 1
        self.journal.append(
            {
                "event": "error_streak",
                "kind": event,
                "consecutive_errors": self.consecutive_errors,
                "max_consecutive_errors": self.config.max_consecutive_errors,
                "error": str(error),
            }
        )
        if self.consecutive_errors >= self.config.max_consecutive_errors:
            self.guard.trip_kill_switch(f"consecutive_{event}")
        self.guard.persist(self.config.state_dir)

    def _journal_rejected(
        self,
        intent: OrderIntent,
        reason: str,
        notional: Decimal,
        *,
        advisor: Optional[dict[str, Any]] = None,
    ) -> None:
        record: dict[str, Any] = {
            "decision_id": intent.decision_id,
            "event": "rejected",
            "reason": reason,
            "would_place": False,
            "may_place": False,
            "notional": str(notional),
            "mode": self.mode,
            "intent": _intent_payload(intent),
            "confidence": self._confidence_payload(
                advisor, symbol=intent.symbol, blocked_by=reason
            ),
        }
        self.journal.append(record)

    def _confidence_payload(
        self,
        advisor: Optional[dict[str, Any]] = None,
        *,
        symbol: Optional[str] = None,
        blocked_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """The confidences behind one decision, for the order table.

        ``evidence`` is the system-level grade the risk budget was sized from;
        ``advisor`` is the model's own confidence in this specific order, when
        the advisor was consulted; ``order`` is this order's own grade, built
        from the features the strategy saw. Reporting all three keeps "the edge
        looks real", "the tape looks right", and "the model likes it" from
        being mistaken for each other — and unlike ``evidence``, ``order``
        moves from one order to the next.
        """
        payload: dict[str, Any] = {"evidence": round(self.evidence_confidence, 4)}
        if advisor:
            payload["advisor"] = round(float(advisor.get("confidence", 0.0)), 3)
            payload["advisor_action"] = advisor.get("action")
            payload["advisor_model"] = advisor.get("model")
        if symbol:
            payload["order"] = self._order_confidence(
                symbol, advisor=advisor, blocked_by=blocked_by
            )
            if self.entry_advisor is not None:
                context = self.entry_advisor.view(symbol)
                if context:
                    payload["jev"] = {
                        "chase": context.get("chase"),
                        "participation": context.get("participation"),
                        "at": context.get("at", ""),
                        "model": context.get("model", ""),
                    }
        return payload

    def _order_confidence(
        self,
        symbol: str,
        *,
        advisor: Optional[dict[str, Any]] = None,
        blocked_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """This order's own grade. Reporting only — it gates nothing."""
        from agentic_trading.confidence import grade_order

        try:
            features = self.market_features(symbol)
        except Exception:  # noqa: BLE001 — a missing grade must not fail a trade
            features = None
        regime = None
        if self.regime_gate is not None:
            view = None
            look_up = getattr(self.regime_gate, "view", None)
            if callable(look_up):
                try:
                    view = look_up(symbol)
                except Exception:  # noqa: BLE001 — a grade is not worth a fault
                    view = None
            if view is not None:
                to_dict = getattr(view, "to_dict", None)
                regime = to_dict() if callable(to_dict) else None
        if features is None and regime is None:
            # No bar history and no regime read: an empty grade would render as
            # nothing at all, which reads like "fine". Say what is missing.
            return {
                "score": None,
                "verdict": "unknown",
                "parts": {},
                "notes": {"data": "no bar history for this symbol"},
            }
        payload = grade_order(
            features, regime=regime, advisor=advisor, blocked_by=blocked_by
        ).to_dict()
        payload["source"] = "live"
        return payload

    def market_features(
        self, symbol: str, quote: Optional[dict[str, Any]] = None
    ) -> Any:
        """Bars-derived features, memoised briefly: the tape does not change
        between two intents a second apart, and re-reading a symbol's file for
        every intent is pure overhead."""
        now = time.monotonic()
        cached = self._feature_cache.get(symbol)
        if cached is not None and (now - cached[0]) < 60.0:
            return cached[1]
        from agentic_trading.llm.market import features_for

        features = features_for(
            symbol, history_path=self.config.history_path, quote=quote
        )
        self._feature_cache[symbol] = (now, features)
        return features

    def refresh_entry_context(self, *, symbols: Optional[list[str]] = None) -> int:
        """Fill the Jev entry cache for the symbols that could be traded.

        Called from the worker thread, never the order path. One call covers the
        whole book, so the cost does not scale with how many symbols are stale.
        """
        if self.entry_advisor is None:
            return 0
        wanted = [
            s.upper() for s in (symbols or sorted(self.config.effective_whitelist))
        ]
        return len(
            self.entry_advisor.refresh_due(wanted, lambda s: self.market_features(s))
        )

    def refresh_regimes(self, *, max_per_pass: int = 2) -> list[dict[str, Any]]:
        """Refresh stale regime classifications. Worker thread only.

        Each refresh is a model call (~4s), so the caller runs this off the order
        path and only a couple of symbols are refreshed per pass: the gate is a
        filter, and a filter that is a few minutes stale is still a filter.
        """
        if self.regime_gate is None:
            return []
        refreshed = self.regime_gate.refresh_due(
            [s.upper() for s in self.config.effective_whitelist],
            lambda symbol: self.market_features(symbol),
            max_per_pass=max_per_pass,
        )
        return [
            {
                "event": "regime",
                "model": getattr(self.regime_gate, "model", ""),
                **view.to_dict(),
            }
            for view in refreshed
        ]

    def _count_orders_today(self) -> int:
        reservations: set[str] = set()
        anonymous = 0
        for record in self.journal.iter_today():
            if record.get("event") != "accepted":
                continue
            intent = record.get("intent")
            side = record.get("side")
            if side is None and isinstance(intent, dict):
                side = intent.get("side")
            # Unknown legacy acceptances count conservatively as entries; a
            # known sell never consumes the entry limit.
            if str(side or "buy").lower() == "sell":
                continue
            root = str(record.get("reservation_of") or record.get("decision_id") or "")
            if root:
                reservations.add(root)
            else:
                anonymous += 1
        return len(reservations) + anonymous


def run_loop(
    config: Config,
    *,
    broker: Broker,
    strategy: Optional[Strategy] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    stop_event: Optional[threading.Event] = None,
    force_shadow: bool = False,
) -> None:
    """Replay a local quote file once through the full pipeline."""
    if tools is not None:
        write_tools_snapshot(tools, config.tools_snapshot_path)

    loop = _Loop(config, broker, strategy, stop_event, force_shadow=force_shadow)

    def _request_stop(signum: int, frame: Any) -> None:
        loop.request_stop()

    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    try:
        loop.start()
        ticks_since_equity = 0
        last_equity_at = time.monotonic()
        for quote in iter_quotes(config.quotes_path):
            if loop.should_stop():
                break
            ticks_since_equity += 1
            now = time.monotonic()
            if (
                ticks_since_equity >= config.equity_refresh_ticks
                or (now - last_equity_at) >= config.equity_refresh_seconds
            ):
                loop.refresh_equity()
                ticks_since_equity = 0
                last_equity_at = now
            loop.handle_quote(quote, session=modeled_session(quote))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        loop.finish()


def _is_retryable_startup_error(exc: BaseException) -> bool:
    """Whether a startup failure is worth waiting out.

    The distinction that matters: a network that is down for a minute must not
    stop the agent for hours, while a configuration mistake must not spin
    forever pretending it will fix itself.
    """
    if isinstance(
        exc,
        (
            FileNotFoundError,
            PermissionError,
            IsADirectoryError,
            NotADirectoryError,
            ValueError,
            TypeError,
            KeyError,
        ),
    ):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # Most socket-level failures arrive as plain OSError (errno 111, 113, -2 for
    # DNS) or wrapped by httpx; both are worth waiting out.
    if isinstance(exc, OSError):
        return True
    name = type(exc).__name__
    return name in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "TransportError",
        "HTTPError",
        "gaierror",
        "SSLError",
    }


def _start_with_retry(
    loop: "_Loop",
    *,
    max_wait_seconds: float = 1800.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Bring the broker-facing parts of the loop up, surviving an outage.

    Before this, a DNS failure at startup raised straight out of ``run_daemon``
    and the service crash-looped: on 2026-09-18 a brief network drop stopped the
    agent for six and a half hours while systemd restarted it 419 times.

    Retries with backoff for up to ``max_wait_seconds``, journaling each attempt
    so the console shows why nothing is happening, then re-raises with the real
    error (not a masked one) so systemd can restart the process and try again.
    """
    started = clock()
    delay = 5.0
    attempt = 0
    while True:
        attempt += 1
        try:
            loop.start()
        except Exception as exc:  # noqa: BLE001 — classified immediately below
            if not _is_retryable_startup_error(exc):
                raise
            waited = clock() - started
            loop.journal.append(
                {
                    "event": "startup_retry",
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "waited_seconds": round(waited, 1),
                    "next_retry_seconds": delay,
                }
            )
            if waited >= max_wait_seconds:
                raise
            sleep(delay)
            delay = min(delay * 2, 60.0)
            continue
        if attempt > 1:
            loop.journal.append(
                {
                    "event": "startup_recovered",
                    "attempts": attempt,
                    "waited_seconds": round(clock() - started, 1),
                }
            )
        return


def run_daemon(
    config: Config,
    *,
    broker: Broker,
    strategy: Optional[Strategy] = None,
    feed: Optional[QuoteFeed] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    stop_event: Optional[threading.Event] = None,
    duration_seconds: Optional[float] = None,
    once: bool = False,
    force_shadow: bool = False,
    strategy_factory: Optional[Callable[[Config], Any]] = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    session_clock: Optional[Callable[[], str]] = None,
) -> None:
    """Continuous autonomous loop.

    ``session_clock`` (tests) overrides session detection; otherwise the real US
    equity session calendar is used and quotes are only acted on inside the
    configured ``session_policy``.
    """
    if tools is not None:
        write_tools_snapshot(tools, config.tools_snapshot_path)

    # One line on stdout per start. The daemon writes to the journal when it is
    # healthy and to stdout only when something goes wrong, so without a banner
    # `tail daemon.log` shows a crash from hours ago and reads like the present.
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] starting: "
        f"mode={effective_mode(config)} strategy={config.strategy} "
        f"symbols={len(config.effective_whitelist)} pid={os.getpid()}",
        flush=True,
    )

    loop = _Loop(
        config,
        broker,
        strategy,
        stop_event,
        force_shadow=force_shadow,
        strategy_factory=strategy_factory,
    )
    if feed is None:
        feed = build_quote_feed(config, broker)

    def _request_stop(signum: int, frame: Any) -> None:
        loop.request_stop()

    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    deadline = (
        time.monotonic() + duration_seconds if duration_seconds is not None else None
    )
    # "Due now", not "not since boot": initialising these to 0 made every
    # interval-gated event wait for the machine's monotonic clock to exceed the
    # interval, so on a freshly booted host the operator got no session-closed
    # notice and no back-check for up to 15 minutes.
    last_heartbeat = time.monotonic() - _HEARTBEAT_SECONDS
    last_selfcheck_due = time.monotonic() - max(0.0, config.selfcheck_minutes * 60)
    last_equity_at = 0.0
    # Declared before the try on purpose: the finally block joins these, and a
    # startup failure used to be replaced by "UnboundLocalError: workers" —
    # which hid the network error that actually stopped the agent.
    workers: list[threading.Thread] = []
    try:
        _start_with_retry(loop, sleep=sleep)
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] started: "
            f"stage={loop.stage} equity={loop.guard.current_equity} "
            f"per_order_cap={loop.guard.max_order_pct}",
            flush=True,
        )
        last_equity_at = time.monotonic()
        journal = loop.journal
        last_stats_at = time.monotonic()
        last_regime_at = 0.0
        last_selfcheck_at = last_selfcheck_due
        kill_state = loop.guard.kill_switch
        cycles = 0
        cycle_seconds = 0.0
        fresh_total = 0
        decisions_base = loop.orders_today
        from agentic_trading import account, discovery, selfimprove

        while not loop.should_stop():
            cycle_started = time.monotonic()
            # The day's budget follows the account size and the confidence the
            # evidence has earned; re-derive it before anything is sized.
            loop.apply_risk_budget()
            # A policy the operator edited is a new verdict, even when the bars
            # are the same ones that produced yesterday's.
            loop.refresh_assessment()
            # Equity moves while the loop runs; the size floor follows it.
            loop.apply_size_floor()
            # Autonomous execution: arm when every check is green, disarm the
            # moment one is not. Cheap (a few state reads) and cached internally.
            loop.apply_auto_arm()
            # Runtime and money: keep the session heartbeat fresh (so an unclean
            # stop still counts) and snapshot the balance whenever arming
            # changes, so "before / after arming" are two real numbers.
            account.note_equity(loop.config.state_dir, loop.guard.current_equity)
            loop.note_arming_snapshot()
            if deadline is not None and time.monotonic() >= deadline:
                break

            session = (
                session_clock() if session_clock is not None else session_for(clock())
            )
            # The effective policy comes from the loop: the agent may widen it
            # within the operator's bound as its confidence grows.
            equity_session_open = session_allows(loop.session_policy, session)
            # Crypto does not close. A pair that can only be sold while the
            # stock market is open is a position held hostage by the wrong
            # calendar, so outside the equity window the loop keeps polling and
            # acts on the pairs alone: stocks are untouched and their exits
            # still wait for the session, while crypto keeps its exit.
            crypto_session = (not equity_session_open) and loop.trades_crypto
            if not equity_session_open and not crypto_session:
                now = time.monotonic()
                if (now - last_heartbeat) >= _HEARTBEAT_SECONDS:
                    journal.append(
                        {
                            "event": "session_closed",
                            "session": session,
                            "policy": loop.session_policy,
                            "next_regular_open": next_session_open(clock()).isoformat(),
                            "mode": loop.mode,
                        }
                    )
                    last_heartbeat = now
                if once:
                    break
                sleep(config.poll_seconds)
                continue
            if crypto_session:
                now = time.monotonic()
                if (now - last_heartbeat) >= _HEARTBEAT_SECONDS:
                    journal.append(
                        {
                            "event": "crypto_session",
                            "session": session,
                            "equity_policy": loop.session_policy,
                            "note": (
                                "the stock market is closed under this policy; "
                                "crypto pairs are still watched and can still "
                                "be sold"
                            ),
                            "symbols": sorted(loop.config.effective_whitelist),
                            "mode": loop.mode,
                        }
                    )
                    last_heartbeat = now

            # Self-evaluation is offline research and must never block the live
            # path. With 30 symbol files it runs for minutes, which previously
            # delayed the first trade decision of every cycle; it now runs on a
            # worker thread while the quote loop keeps polling.
            worker = getattr(loop, "eval_thread", None)
            interval_due = (
                config.autonomy != "manual"
                and config.evolution_interval_minutes > 0
                and (
                    loop.last_evolution_at == 0
                    or (time.monotonic() - loop.last_evolution_at)
                    >= config.evolution_interval_minutes * 60
                )
            )
            if interval_due and not (worker and worker.is_alive()):
                if once:
                    # A single-cycle smoke test must finish its evaluation before
                    # exiting, so run it inline there and thread it in the daemon.
                    _self_improve_cycle(loop, config, journal)
                else:
                    loop.eval_thread = threading.Thread(  # type: ignore[attr-defined]
                        target=_self_improve_cycle,
                        args=(loop, config, journal),
                        daemon=True,
                        name="self-evaluation",
                    )
                    loop.eval_thread.start()
                    # Shutdown must join every writer. Without tracking this
                    # one, it could keep writing evidence/state after the
                    # daemon returned (and made temporary workspaces flaky).
                    workers.append(loop.eval_thread)
            now = time.monotonic()
            if (now - last_equity_at) >= config.equity_refresh_seconds:
                loop.refresh_equity()
                # The broker's book is the truth about what is held; the
                # strategy's copy of it has to be told, or live exits never
                # fire and live entries stack (see the method).
                loop.reconcile_strategy_positions()
                last_equity_at = now

            # Regime views expire; refresh a batch on a worker so the order path
            # only ever reads a cached classification.
            selfcheck_worker = getattr(loop, "selfcheck_thread", None)
            if (
                not once
                and config.selfcheck_minutes > 0
                and (now - last_selfcheck_at) >= config.selfcheck_minutes * 60
                and not (selfcheck_worker and selfcheck_worker.is_alive())
            ):
                last_selfcheck_at = now

                def _selfcheck(_loop: _Loop = loop) -> None:
                    try:
                        from agentic_trading.selfcheck import run_checks, write_report

                        report = run_checks(_loop.config, _loop.broker)
                        write_report(_loop.config, report)
                        _loop.note_agent(
                            "backcheck",
                            ok=report.healthy,
                            detail={
                                "ok": sum(1 for c in report.checks if c.status == "ok"),
                                "failed": [c.name for c in report.failures],
                            },
                            error="a check failed" if not report.healthy else "",
                        )
                        _loop.journal.append(
                            {
                                "event": "selfcheck",
                                "healthy": report.healthy,
                                "ok": sum(1 for c in report.checks if c.status == "ok"),
                                "warnings": [c.to_dict() for c in report.warnings],
                                "failures": [c.to_dict() for c in report.failures],
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        _loop.note_agent("backcheck", ok=False, error=str(exc))
                        _loop.journal.append(
                            {"event": "selfcheck_failed", "error": str(exc)[:200]}
                        )

                loop.selfcheck_thread = threading.Thread(  # type: ignore[attr-defined]
                    target=_selfcheck, daemon=True, name="selfcheck"
                )
                loop.selfcheck_thread.start()
                workers.append(loop.selfcheck_thread)

            # The scout re-reads the market's own discovery lists and may change
            # which symbols the book may hold. Its cadence comes from its state
            # file, so a restart resumes the schedule rather than re-running the
            # pass or silently skipping one.
            discovery_worker = getattr(loop, "discovery_thread", None)
            discovery_due = (
                not once
                and config.discovery_enabled
                and config.autonomy != "manual"
                and not (discovery_worker and discovery_worker.is_alive())
                and discovery.due_for_pass(loop.config)
            )
            # Adoption widens what the book may hold to instruments the operator
            # never named, so it needs the same per-session consent as every
            # other self-directed change: a config file alone is policy, the
            # switch is permission. The scout stays idle and the console is told
            # why, rather than the book quietly following the market.
            if discovery_due and not selfimprove.autonomy_enabled():
                if not getattr(loop, "_discovery_hold_noted", False):
                    loop._discovery_hold_noted = True
                    journal.append(
                        {
                            "event": "discovery_held",
                            "reason": "autonomy_switch_closed",
                            "note": (
                                "symbol discovery is enabled in the config, but "
                                "this session has not allowed autonomy, so the "
                                "book stays on the operator's list; set "
                                "AGENTIC_ALLOW_AUTONOMY=1 to let the scout pick "
                                "symbols"
                            ),
                        }
                    )
            elif discovery_due:

                def _discover(_loop: _Loop = loop) -> None:
                    from agentic_trading import discovery as scout

                    try:
                        report = scout.rebalance(
                            _loop.config,
                            _loop.broker,
                            held=_loop.held_symbols(),
                        )
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        scout.note_failure(_loop.config, str(exc))
                        _loop.note_agent("scout", ok=False, error=str(exc))
                        _loop.journal.append(
                            {"event": "discovery_failed", "error": str(exc)[:200]}
                        )
                        return
                    _loop.note_agent(
                        "scout",
                        ok=True,
                        detail={
                            "adopted": len(report.get("adopted") or []),
                            "added": report.get("added") or [],
                            "dropped": report.get("dropped") or [],
                            "considered": report.get("considered", 0),
                        },
                    )
                    _loop.journal.append(
                        {
                            "event": "discovery",
                            "adopted": report.get("adopted") or [],
                            "added": report.get("added") or [],
                            "dropped": report.get("dropped") or [],
                            "held_back": report.get("held_back") or [],
                            "considered": report.get("considered", 0),
                            "notes": (report.get("notes") or [])[:3],
                        }
                    )
                    # Adoption is queued for the loop's own thread: it owns the
                    # quote feed, the strategy rebuild and the guard's whitelist,
                    # and a strategy that is still being built must never be
                    # handed a quote.
                    _loop.pending_universe = [
                        str(symbol).upper() for symbol in (report.get("adopted") or [])
                    ]

                loop.discovery_thread = threading.Thread(  # type: ignore[attr-defined]
                    target=_discover, daemon=True, name="symbol-scout"
                )
                loop.discovery_thread.start()
                workers.append(loop.discovery_thread)

            if loop.pending_universe is not None:
                pending, loop.pending_universe = loop.pending_universe, None
                loop.adopt_universe(pending)

            if loop.universe_dirty:
                # The feed was built from the old symbol list; the new symbols
                # would never be quoted otherwise.
                feed = build_quote_feed(loop.config, broker)
                loop.universe_dirty = False
                journal.append(
                    {
                        "event": "quote_feed_rebuilt",
                        "symbols": len(loop.config.effective_whitelist),
                    }
                )

            regime_worker = getattr(loop, "regime_thread", None)
            if (
                loop.regime_gate is not None
                and config.regime_refresh_seconds > 0
                and (now - last_regime_at) >= config.regime_refresh_seconds
                and not (regime_worker and regime_worker.is_alive())
            ):
                last_regime_at = now

                def _refresh_regimes(_loop: _Loop = loop) -> None:
                    try:
                        for event in _loop.refresh_regimes():
                            _loop.journal.append(event)
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        _loop.journal.append(
                            {"event": "regime_failed", "error": str(exc)[:200]}
                        )
                    # Same cadence, same symbols: Jev's per-entry judgments for
                    # the whole book in one call. Advisory — it fills a cache the
                    # order path reads and never waits on.
                    try:
                        filled = _loop.refresh_entry_context()
                        if filled:
                            _loop.journal.append(
                                {
                                    "event": "entry_context",
                                    "refreshed": filled,
                                    "model": getattr(
                                        getattr(_loop, "entry_advisor", None),
                                        "model",
                                        "",
                                    ),
                                }
                            )
                    except Exception as exc:  # noqa: BLE001 — advisory only
                        _loop.journal.append(
                            {"event": "entry_context_failed", "error": str(exc)[:200]}
                        )

                loop.regime_thread = threading.Thread(  # type: ignore[attr-defined]
                    target=_refresh_regimes,
                    daemon=True,
                    name="regime-refresh",
                )
                loop.regime_thread.start()
                workers.append(loop.regime_thread)
            loop.note_open_orders()

            try:
                quotes = feed.poll()
            except Exception as exc:  # noqa: BLE001 — feed errors must not crash
                journal.append({"event": "quote_read_failed", "error": str(exc)})
                quotes = []
            if crypto_session:
                # The equity market is closed: only the pairs are actionable,
                # and reporting stock quotes as stale here would be noise.
                quotes = [
                    quote
                    for quote in quotes
                    if is_crypto_symbol(str(quote.get("symbol") or ""))
                ]

            now_dt = clock()
            fresh: list[dict] = []
            stale_symbols: set[str] = set()
            for quote in quotes:
                if quote_is_fresh(
                    quote,
                    now=now_dt,
                    max_age_seconds=config.max_quote_age_seconds,
                ):
                    fresh.append(quote)
                else:
                    stale_symbols.add(str(quote.get("symbol") or "?"))
            if stale_symbols:
                journal.append(
                    {
                        "event": "stale_quotes_rejected",
                        "count": len(quotes) - len(fresh),
                        "symbols": sorted(stale_symbols),
                        "max_quote_age_seconds": config.max_quote_age_seconds,
                    }
                )

            cycle_had_error = False
            errors_before_quotes = loop.consecutive_errors
            for quote in fresh:
                if loop.should_stop():
                    break
                try:
                    loop.handle_quote(quote, session=session)
                except Exception as exc:  # noqa: BLE001 — one bad tick is not fatal
                    cycle_had_error = True
                    loop.note_error("loop_error", exc)

            # A tick that raised is counted, and broker failures handled inside
            # the order path also increment ``consecutive_errors``. Only a
            # cycle that recorded no new error is evidence the connection is
            # back. Resetting merely because _place caught its own exception
            # made the consecutive-error kill switch ineffective across cycles.
            if (
                loop.consecutive_errors
                and not cycle_had_error
                and loop.consecutive_errors == errors_before_quotes
            ):
                loop.journal.append(
                    {
                        "event": "error_streak_cleared",
                        "consecutive_errors": loop.consecutive_errors,
                    }
                )
                loop.consecutive_errors = 0

            # Cadence heartbeat: every broker round trip is ~1.3s over the MCP
            # gateway, so the operator needs the measured cycle time to tell a
            # quiet strategy apart from a slow loop.
            # A kill-switch trip is a state change nothing else journaled, so
            # nobody could be told about it. Announce both edges.
            if loop.guard.kill_switch and not kill_state:
                journal.append(
                    {
                        "event": "kill_switch",
                        "reason": loop.guard.kill_reason,
                        "mode": loop.mode,
                        "stage": loop.stage,
                    }
                )
            kill_state = loop.guard.kill_switch

            # The strategy and execution agents report from where the work is.
            loop.note_agent(
                "strategy",
                ok=not cycle_had_error,
                detail={"fresh_quotes": len(fresh), "cycles": cycles + 1},
                error="tick raised" if cycle_had_error else "",
            )
            if fresh:
                loop.note_agent(
                    "execution",
                    ok=True,
                    detail={"decisions_today": loop.orders_today},
                )
            cycles += 1
            cycle_seconds += time.monotonic() - cycle_started
            fresh_total += len(fresh)
            stats_due = (
                not once
                and config.cycle_stats_seconds > 0
                and (time.monotonic() - last_stats_at) >= config.cycle_stats_seconds
            )
            if stats_due:
                loop.write_agent_state()
                journal.append(
                    {
                        "event": "cycle_stats",
                        "cycles": cycles,
                        "avg_cycle_seconds": round(cycle_seconds / max(cycles, 1), 3),
                        "fresh_quotes": fresh_total,
                        "decisions": loop.orders_today - decisions_base,
                        "window_seconds": round(time.monotonic() - last_stats_at, 1),
                        "mode": loop.mode,
                    }
                )
                last_stats_at = time.monotonic()
                cycles = 0
                cycle_seconds = 0.0
                fresh_total = 0
                decisions_base = loop.orders_today

            if once:
                break
            sleep(config.poll_seconds)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        # Let in-flight workers finish before the loop exits: a worker still
        # writing into a caller's directory after shutdown is how "the daemon
        # was stopped" turns into a half-written state file.
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=10)
        loop.finish()


def evaluation_state_path(config: Any) -> Path:
    return Path(config.state_dir) / "evaluation_state.json"


def load_evaluation_state(config: Any) -> dict[str, Any]:
    """What the last evaluation saw, so a restart can skip an unchanged search.

    Without this every restart re-runs a minutes-long CPU-bound search over
    identical data — which both wastes the work and starves the trading loop
    sharing the process.
    """
    path = evaluation_state_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_evaluation_state(config: Any, payload: dict[str, Any]) -> None:
    from agentic_trading import jsonio

    try:
        jsonio.write_text(
            evaluation_state_path(config),
            jsonio.dumps(payload, indent=2) + "\n",
        )
    except OSError:
        return


def _apply_promotion(
    config: Config, loop: _Loop, journal: DecisionJournal, state: Any
) -> None:
    """Apply a promotion or evidence demotion to the running mode.

    Two paths can promote — the scheduled search and the walk-forward regrade —
    and they must not each own their own copy of this. The regrade path used to
    record the promotion and stop there, which left the agent promoted in state
    and shadow in practice.
    """
    from agentic_trading import selfimprove

    lowering_risk = state.stage == "shadow"
    if not lowering_risk and (
        config.autonomy != "auto" or not selfimprove.autonomy_enabled()
    ):
        journal.append(
            {
                "event": "promotion_requires_consent",
                "stage": state.stage,
                "hint": 'set autonomy = "auto" and AGENTIC_ALLOW_AUTONOMY=1',
            }
        )
        return
    for event in selfimprove.apply_stage(config, state):
        journal.append(event)
    loop.set_mode("live" if state.stage != "shadow" else "shadow")
    journal.append(
        {
            "event": "autonomy_applied",
            "stage": state.stage,
            "mode": loop.mode,
            "caps": {"max_order_pct": str(loop.guard.max_order_pct)},
        }
    )


def _propose_system_changes(
    config: Config, loop: _Loop, journal: DecisionJournal
) -> None:
    """One evolution pass per day: propose, store, journal. Applies nothing.

    Rate-limited on attempt, like the evidence refresh: a provider outage must
    not turn into a retry loop, and a proposal queue that refills hourly is
    noise rather than a review list.
    """
    if config.evolution_agent_interval_hours <= 0:
        return
    now = time.monotonic()
    last = getattr(loop, "last_proposal_at", 0.0)
    if last and (now - last) < config.evolution_agent_interval_hours * 3600:
        return
    loop.last_proposal_at = now
    from agentic_trading import evolve_system

    proposals = evolve_system.run(config, journal=journal)
    if proposals:
        loop.note_agent(
            "evolution",
            ok=True,
            detail={
                "proposed": len(proposals),
                "pending": len(evolve_system.read(config).get("proposals") or []),
            },
        )
    else:
        loop.note_agent("evolution", ok=True, detail={"proposed": 0})


def _refresh_evidence(config: Config, loop: _Loop, journal: DecisionJournal) -> None:
    """Rebuild the walk-forward report when it is missing or too old.

    The promotion gate refuses a stale report, which is right — and a deadline
    unless something regenerates it. This is that something: the report is priced
    off the freshly synced bars in the worker thread, so the bot can keep earning
    its own promotion for years without an operator remembering a command.

    It is deliberately rate-limited on attempt, not on success: a report that
    cannot be produced must not be retried every cycle, and it must never touch
    the order path.
    """
    if config.evidence_refresh_days <= 0:
        return
    interval = config.evolution_interval_minutes * 60
    now = time.monotonic()
    last = getattr(loop, "last_evidence_refresh_at", 0.0)
    if last and (now - last) < max(interval, 1800):
        return
    loop.last_evidence_refresh_at = now  # set first: a failure must not spin
    from agentic_trading.evidence import refresh_if_stale

    try:
        report = refresh_if_stale(
            config,
            max_age_days=config.evidence_refresh_days,
            max_positions=config.max_open_positions,
        )
    except Exception as exc:  # noqa: BLE001 — never kill the trading loop
        journal.append({"event": "evidence_refresh_failed", "error": str(exc)[:200]})
        return
    if report is None:
        loop.note_agent(
            "research",
            ok=True,
            detail={"note": "report still current"},
        )
        return
    production = (report.get("configs") or {}).get("production") or {}
    loop.note_agent(
        "research",
        ok=True,
        detail={
            "trades": production.get("trades"),
            "expectancy_bps": production.get("expectancy_bps"),
            "max_drawdown_pct": production.get("max_drawdown_pct"),
        },
    )
    journal.append(
        {
            "event": "evidence_refreshed",
            "age_at_build_days": report.get("age_at_build_days"),
            "per_order_pct": production.get("per_order_pct"),
            "trades": production.get("trades"),
            "expectancy_bps": production.get("expectancy_bps"),
            "max_drawdown_pct": production.get("max_drawdown_pct"),
            "bootstrap_p_value": production.get("bootstrap_p_value"),
            "gate_size_pct": (report.get("gate_size") or {}).get("per_order_pct"),
            "symbols": len((report.get("series") or {}).get("symbols") or []),
            "bars": (report.get("series") or {}).get("bars"),
        }
    )


def _regrade_from_evidence(
    config: Config, loop: _Loop, journal: DecisionJournal
) -> None:
    """Grade promotion from the walk-forward report without running the search.

    The search is skipped while the bars are unchanged, but the evidence report
    is not: a new walk-forward run, a lowered ceiling, or a freshly measured
    cost model can all change the verdict with no new bars at all. Promotion
    has to answer to the newest evidence it has, so this re-grades from the
    report and only reports when the report itself changed.
    """
    from agentic_trading import selfimprove
    from agentic_trading.evidence import (
        effective_per_order_pct,
        read_report,
        with_current_forward,
    )
    from agentic_trading.limits import load_limits
    from agentic_trading.promotion import (
        apply_assessment,
        assess_walkforward,
        load_state,
        policy_from_config,
        save_state,
    )

    try:
        report = read_report(config)
    except Exception as exc:  # noqa: BLE001 — never kill the loop
        journal.append({"event": "evidence_regrade_failed", "error": str(exc)[:200]})
        return
    if not report:
        return
    report = with_current_forward(config, report)
    policy = policy_from_config(config)
    state = load_state(config.state_dir)
    previous = (state.last_assessment or {}).get("evidence") or {}
    stored = load_limits(config.state_dir)
    policy_size = Decimal(
        str(stored.max_order_pct if stored is not None else config.max_order_pct)
    )
    live = effective_per_order_pct(
        config,
        policy_pct=policy_size,
        equity=Decimal(str(loop.guard.current_equity or 0)),
    )
    assessment = assess_walkforward(report, policy, live_per_order_pct=live)
    # Grade the *evidence*, not the file. Re-running walkforward on the same
    # bars produces a new timestamp and the same numbers; letting that advance
    # the promotion streak would promote on re-typing, not on new information.
    if previous.get("report_key") == assessment.evidence.get("report_key"):
        return
    events = apply_assessment(
        state, assessment, policy, equity=loop.guard.current_equity
    )
    save_state(config.state_dir, state)
    events.extend(selfimprove.update_limits(config, assessment=assessment))
    journal.append(
        {
            "event": "evaluation",
            "source": "walkforward_regrade",
            "eligible": assessment.eligible,
            "score": round(assessment.score, 4),
            "reasons": assessment.reasons,
            "evidence": assessment.evidence,
            "stage": state.stage,
            "streak": state.streak,
        }
    )
    for event in events:
        journal.append(event)

    # Apply the freshly computed budget to the running guard, exactly as the
    # scheduled path does: two paths that can promote must converge afterwards.
    loop.apply_stage_caps()
    journal.append(
        {
            "event": "caps_applied",
            "max_order_pct": str(loop.guard.max_order_pct),
            "daily_notional_pct": str(loop.guard.daily_notional_pct),
            "session_policy": loop.session_policy,
            "confidence": round(float(assessment.confidence), 4),
            "stage": loop.stage,
        }
    )
    if any(event.get("event") in ("promotion", "demotion") for event in events):
        _apply_promotion(config, loop, journal, state)


def _self_improve_cycle(loop: _Loop, config: Config, journal: DecisionJournal) -> None:
    """Demote, evolve, assess, and (when permitted) promote — never silently."""
    if config.autonomy == "manual":
        return
    from agentic_trading import selfimprove

    # A tripped kill switch means "stop and let a human look" — never promote
    # out of it, and do not spend cycles optimising while writes are blocked.
    if loop.guard.kill_switch:
        journal.append(
            {
                "event": "self_improve_skipped",
                "reason": "kill_switch_active",
                "kill_reason": loop.guard.kill_reason,
            }
        )
        return

    # 1. Demotion is checked every cycle: live risk comes before optimisation.
    if config.autonomy == "auto" and selfimprove.autonomy_enabled():
        event = selfimprove.demotion_event(
            config,
            kill_switch=loop.guard.kill_switch,
            consecutive_errors=loop.consecutive_errors,
            current_equity=loop.guard.current_equity,
        )
        if event is not None:
            journal.append({**event, "autonomy": config.autonomy})
            from agentic_trading.promotion import load_state

            for applied in selfimprove.apply_stage(
                config, load_state(config.state_dir)
            ):
                journal.append(applied)
            # A live risk event resets the confidence-derived budget: after a
            # demotion the agent has to earn its size back from the floor.
            for reset_event in selfimprove.update_limits(config, reset=True):
                journal.append(reset_event)
            loop.set_mode("shadow")
            journal.append(
                {"event": "autonomy_applied", "stage": "shadow", "mode": "shadow"}
            )
            return

    # 2. Re-evolve on a schedule.
    now = time.monotonic()
    interval = config.evolution_interval_minutes * 60
    if interval <= 0 or (
        loop.last_evolution_at and (now - loop.last_evolution_at) < interval
    ):
        return
    loop.last_evolution_at = now

    # Refresh the bars first: without new data the same deterministic search
    # returns the same evidence every hour, so confidence and the risk budget
    # can never move.
    if config.history_refresh_hours > 0:
        if (
            not hasattr(loop, "last_history_sync")
            or (now - getattr(loop, "last_history_sync", 0.0))
            >= config.history_refresh_hours * 3600
        ):
            loop.last_history_sync = now  # set first: a failure must not spin
            try:
                from agentic_trading.history_sync import sync_history

                results, errors = sync_history(config, loop.broker)
                flagged = [result.to_dict() for result in results if result.issues]
                journal.append(
                    {
                        "event": "history_sync",
                        # NB: a count, not an array — the console renders
                        # `symbols` as a list and a number broke the stream.
                        "symbol_count": len(results),
                        "added": sum(result.added for result in results),
                        "details": [
                            {
                                "symbol": result.symbol,
                                "added": result.added,
                                "newest": result.last_start[:10],
                                "volume_usable": result.volume_usable,
                            }
                            for result in results
                        ],
                        "quality_issues": flagged,
                        "errors": errors,
                    }
                )
                reload_history = getattr(loop.strategy, "reload_history", None)
                if callable(reload_history):
                    reload_history()
                # Success heartbeat: the data agent's health comes from the sync
                # that just returned. (This line was previously inside the try
                # before `except`, so `exc` was unbound on the success path and
                # every good sync was reported as a failure.)
                loop.note_agent(
                    "data",
                    ok=True,
                    detail={
                        "symbols": len(results),
                        "added": sum(result.added for result in results),
                        "quality_issues": len(flagged),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                loop.note_agent("data", ok=False, error=str(exc))
                journal.append(
                    {"event": "history_sync_failed", "error": str(exc)[:200]}
                )

            # Correlations and execution costs are recomputed on the same
            # cadence as the data: both are derived from it.
            # Correlations are derived from the freshly synced data, so they
            # belong on the data cadence.
            try:
                from agentic_trading.correlation import compute_state, save_state

                state = compute_state(
                    config.history_path,
                    # Everything tradeable, not just the operator's core: a pair
                    # the scout adopted with no correlation entry would fail the
                    # guard's duplicate test as "unknown" and be refused.
                    config.effective_whitelist,
                    lookback=config.correlation_lookback_days,
                    threshold=config.correlation_threshold,
                )
                save_state(config.state_dir, state)
                loop.apply_correlation_policy()
                pairs = list(state.pairs.items())
                strongest = sorted(pairs, key=lambda item: -abs(item[1]))[:3]
                journal.append(
                    {
                        "event": "correlations",
                        "pairs": len(pairs),
                        "threshold": state.threshold,
                        "strongest": [
                            {"pair": name, "corr": value} for name, value in strongest
                        ],
                        "max_correlated_positions": (config.max_correlated_positions),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                journal.append(
                    {"event": "correlations_failed", "error": str(exc)[:200]}
                )

    # The bars are as fresh as they are going to get this cycle, so this is the
    # honest moment to re-price the rule — and the gate grades the report, not
    # the search, so a report nobody refreshed is a promotion that never comes.
    _refresh_evidence(config, loop, journal)
    # The evolution agent reads what the refresh just wrote, so it runs after it.
    _propose_system_changes(config, loop, journal)

    # Skip the (minutes-long) search when nothing the evaluation reads has
    # changed: the journal then explains why confidence is not moving.
    # What execution actually cost, against what the model assumes. Cheap (one
    # read) and worth re-checking every pass: the moment a fill lands, the
    # evidence switches from assumed costs to measured ones.
    try:
        from agentic_trading.execution import (
            decision_prices,
            load_report,
            measure,
            save_report,
        )

        trades = loop.broker.get_trade_history(span="month")
        report = measure(
            trades,
            decision_prices(journal.iter_today()),
            assumed_per_side_bps=2.0,
            existing=load_report(config.state_dir),
        )
        save_report(config.state_dir, report)
        journal.append(
            {
                "event": "execution_costs",
                "fills": report.fills,
                "measured_per_side_bps": report.measured_per_side_bps,
                "assumed_per_side_bps": report.assumed_per_side_bps,
                "usable": report.usable,
                "measured_round_trip_bps": report.measured_round_trip_bps,
                "per_side_cost_bps": report.per_side_cost_bps,
                "note": report.note,
            }
        )
    except Exception as exc:  # noqa: BLE001 — never kill the loop
        journal.append({"event": "execution_costs_failed", "error": str(exc)[:200]})

    if config.history_path is not None:
        from agentic_trading.history_sync import fingerprint
        from agentic_trading.selfimprove import history_plan

        directory = Path(config.history_path)
        if directory.is_dir():
            _, _, planned = history_plan(directory, config)
            current = fingerprint(directory, planned)
            previous = getattr(loop, "history_fingerprint", None)
            if previous is None:
                previous = load_evaluation_state(config).get("history_fingerprint")
            if current and current == previous:
                journal.append(
                    {
                        "event": "evaluation_skipped",
                        "reason": "history_unchanged",
                        "symbols": planned,
                    }
                )
                # Skipping the search is not the same as skipping the verdict.
                # The walk-forward report is cheap to grade and can change
                # without any new bars (a new run, a lowered ceiling, a fresh
                # size), so re-grade promotion from it and let the streak move.
                _regrade_from_evidence(config, loop, journal)
                return
            loop.history_fingerprint = current
            save_evaluation_state(config, {"history_fingerprint": current})

    if config.history_path is None:
        journal.append(
            {
                "event": "evolution_skipped",
                "reason": "no history_path configured",
            }
        )
        return

    try:
        assessment, promotion_state, events = selfimprove.evaluate_and_record(
            config, equity=loop.guard.current_equity
        )
    except Exception as exc:  # noqa: BLE001 — never kill the trading loop
        journal.append({"event": "evolution_failed", "error": str(exc)})
        return

    journal.append(
        {
            "event": "evaluation",
            "eligible": assessment.eligible,
            "score": round(assessment.score, 4),
            "reasons": assessment.reasons,
            "evidence": assessment.evidence,
            "stage": promotion_state.stage,
            "streak": promotion_state.streak,
        }
    )
    for event in events:
        journal.append(event)

    # Apply the freshly computed budget to the running guard: caps have to move
    # while the daemon is up, not on the next restart.
    loop.apply_stage_caps()
    journal.append(
        {
            "event": "caps_applied",
            "max_order_pct": str(loop.guard.max_order_pct),
            "daily_notional_pct": str(loop.guard.daily_notional_pct),
            "session_policy": loop.session_policy,
            "confidence": round(float(getattr(assessment, "confidence", 0.0)), 4),
            "stage": loop.stage,
        }
    )

    # 3. Apply a promotion only with explicit operator consent.
    transitioned = any(
        event.get("event") in ("promotion", "demotion") for event in events
    )
    if not transitioned:
        return
    _apply_promotion(config, loop, journal, promotion_state)
