"""Shadow/live runtime: quotes → strategy → RiskGuard → journal / broker.

Two entry points:

- :func:`run_loop` — replay a local JSONL quote file once (soak / tests / replay)
- :func:`run_daemon` — continuous autonomous loop over a live quote feed with
  session gating, equity refresh, fill reconciliation and error kill-switch

Live placement requires **all** of: mode ``live``, ``AGENTIC_ALLOW_LIVE=1``, a
session permitted by ``session_policy``, and a RiskGuard allow decision.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

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
from agentic_trading.types import OrderIntent, Side


class Strategy(Protocol):
    def on_quote(self, quote: dict) -> list[OrderIntent]: ...


_MODE_FILE = "mode"
_HEARTBEAT_SECONDS = 900.0


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
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / _MODE_FILE).write_text(mode + "\n", encoding="utf-8")


def build_guard(config: Config, mode: str) -> RiskGuard:
    return RiskGuard(
        mode=mode,
        whitelist=config.symbol_whitelist,
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
    """True only for quotes observed within ``max_age_seconds`` of ``now``.

    Applied in the live daemon so a restarted process, a stalled collector, or a
    replayed recording can never drive a real order. Unparseable timestamps fail
    closed.
    """
    raw = quote.get("observed_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = (now - observed.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= max_age_seconds


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
    ) -> None:
        self.config = config
        self.broker = broker
        self.strategy = strategy or FixtureStrategy()
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
        # A month-long window: positions and realized P&L are meant to
        # accumulate, and only the day-scoped counters belong to one file.
        self.shadow_book = ShadowBook.from_journal(self.journal, days=30)
        self.guard.attach_shadow_book(self.shadow_book)
        self.guard.load(config.state_dir, self.journal)
        if self.mode == "shadow":
            self.guard.note_shadow_realized(self.shadow_book.realized_pnl)
        self.account_number: Optional[str] = config.account_number
        self.orders_today = self._count_orders_today()
        self.consecutive_errors = 0
        self.open_order_symbols: set[str] = set()
        self._open_orders_read_at = 0.0
        self._stopping = False
        self.stage = "shadow"
        self.session_policy = config.session_policy
        self.evidence_confidence: float = 0.0
        self.last_evolution_at = 0.0
        self.advisor = None
        self.regime_gate = None
        try:
            from agentic_trading.llm.advisor import build_advisor, build_regime_gate

            self.advisor = build_advisor()
            # Same opt-in as the advisor: the console of LLM features is one
            # switch, and every part of it is bounded to reducing risk.
            self.regime_gate = build_regime_gate(
                state_path=Path(config.state_dir) / "regimes.json"
            )
        except Exception:  # noqa: BLE001 — advisory layer must never block startup
            self.advisor = None
            self.regime_gate = None
        self._feature_cache: dict[str, tuple[float, Any]] = {}
        self.apply_stage_caps()

    def apply_stage_caps(self) -> None:
        """Probation trades live but with a reduced per-order cap."""
        from agentic_trading.promotion import load_state, policy_from_config

        state = load_state(self.config.state_dir)
        self.stage = state.stage
        if state.stage == "probation":
            probation_cap = policy_from_config(self.config).probation_max_order_pct
            self.guard.max_order_pct = min(self.config.max_order_pct, probation_cap)
        else:
            self.guard.max_order_pct = self.config.max_order_pct
        # The cap the *evidence* justifies, before the small-account floor is
        # considered. Kept separately so the floor can never ratchet: it is
        # always derived from this value, not from its own previous output.
        self.policy_max_order_pct = Decimal(self.guard.max_order_pct)
        # The agent's stored limits may only ever tighten what the operator set.
        from agentic_trading.limits import apply_to_guard, load_limits, reconcile

        self.guard.daily_notional_pct = self.config.daily_notional_pct
        # A ceiling lowered in the config must show up on the console now, not
        # at the next evaluation — evaluations are skipped while the bars are
        # unchanged, which is most days.
        reconcile(self.config)
        apply_to_guard(self.guard, self.config)
        stored = load_limits(self.config.state_dir)
        # Confidence in force right now, so every decision can record the
        # evidence level it was taken under.
        self.evidence_confidence = float(
            (stored.confidence if stored else "") or 0.0
        )
        # The agent may widen trading hours only up to the operator's bound, and
        # only when its own confidence ladder says the evidence earned it.
        self.session_policy = str(
            (stored.session_policy if stored else "")
            or self.config.session_policy
        )
        self.apply_correlation_policy()
        self.apply_size_floor()
        self.reconcile_stage_mode()

    def apply_size_floor(self) -> Optional[dict[str, Any]]:
        """Raise the per-order cap when the account is too small to place one.

        On a $50 account with a 0.92% ceiling the order is $0.46 and the broker
        minimum is $1.00, so every entry is refused `below_min_notional`. That is
        the guard being honest, and it is useless if you want to see results: the
        operator can authorise a higher temporary ceiling
        (`small_account_max_order_pct`), and this raises the cap to just enough
        to place one order and no further — then drops it the moment the account
        clears the minimum on its own.

        Derived, never accumulated: always computed from ``policy_max_order_pct``
        and the current equity, so it cannot ratchet and it survives restarts.
        """
        from agentic_trading import sizer

        policy = Decimal(
            str(getattr(self, "policy_max_order_pct", self.guard.max_order_pct))
        )
        ceiling = Decimal(str(self.config.small_account_max_order_pct))
        equity = Decimal(str(self.guard.current_equity or 0))
        if equity <= 0:
            # Before the first equity refresh there is nothing to divide by, and
            # guessing a size is worse than waiting one cycle.
            return None

        margin = Decimal(str(getattr(sizer, "FLOOR_MARGIN", Decimal("0.02"))))
        minimum = Decimal(str(self.config.min_order_notional))

        if ceiling <= 0:
            # Disabled means off: restore the ladder's cap and say so only if the
            # floor had been engaged, so switching it off is visible in the
            # journal but a disabled rule never chatters.
            self.guard.max_order_pct = policy
            if not getattr(self, "_size_floor_active", False):
                return None
            self._size_floor_active = False
            self._size_floor_sufficient = True
            event = {
                "event": "small_account_mode",
                "engaged": False,
                "sufficient": True,
                "policy_max_order_pct": str(policy),
                "effective_max_order_pct": str(policy),
                "equity": str(equity),
                "min_order_notional": str(minimum),
                "small_account_max_order_pct": str(ceiling),
                "reason": (
                    "small-account mode is off; the evidence-compliant cap is "
                    "back in force"
                ),
            }
            self.journal.append(event)
            return event

        effective = sizer.floor_cap(
            equity=equity,
            policy_cap=policy,
            min_notional=minimum,
            max_cap=ceiling,
        )
        self.guard.max_order_pct = effective
        engaged = effective > policy
        # Two different failures look identical from the journal otherwise: a
        # floor that is engaged and placeable, and one whose authorised ceiling
        # is still too small for the minimum (a $1.00 order at 2.00% of $50 lands
        # on exactly $1.00 and rounds a hair under it). Say which one this is.
        needed = (minimum * (Decimal(1) + margin)) / equity
        sufficient = bool(effective >= needed)
        was_active = bool(getattr(self, "_size_floor_active", False))
        was_sufficient = bool(getattr(self, "_size_floor_sufficient", True))
        # Only an engagement whose placeability changed is news. An unengaged
        # floor is not "insufficient" — there is nothing to be insufficient for.
        if engaged == was_active and (not engaged or sufficient == was_sufficient):
            return None
        self._size_floor_active = engaged
        self._size_floor_sufficient = sufficient if engaged else True
        event = {
            "event": "small_account_mode",
            "engaged": engaged,
            "sufficient": sufficient,
            "needed_max_order_pct": str(needed.quantize(Decimal("0.000001"))),
            "policy_max_order_pct": str(policy),
            "effective_max_order_pct": str(effective),
            "equity": str(equity),
            "min_order_notional": str(minimum),
            "small_account_max_order_pct": str(ceiling),
            "reason": (
                (
                    "the evidence-compliant order is below the broker minimum, so "
                    "the cap is raised to place one order (this trades outside the "
                    "size the walk-forward supports)"
                    if sufficient
                    else "even the authorised small-account ceiling cannot place "
                    "an order at this equity: raise small_account_max_order_pct "
                    f"to at least {needed.quantize(Decimal('0.0001'))}"
                )
                if engaged
                else "the account can now place an evidence-compliant order"
            ),
        }
        if engaged:
            # Attach the measured cost of the bigger size, from the same report
            # the promotion gate reads. "You authorised 2.04%" is half a
            # sentence; "...and the walk-forward measures 23.5% drawdown there"
            # is the other half.
            event["drawdown_at_effective_pct"] = self._evidence_drawdown(effective)
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
        if not (
            self.config.autonomy == "auto" and selfimprove.autonomy_enabled()
        ):
            # Raising risk needs the operator's consent switch.
            event = {
                "event": "stage_mode_blocked",
                "stage": state.stage,
                "mode": current,
                "hint": "set autonomy = \"auto\" and AGENTIC_ALLOW_AUTONOMY=1",
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
            "allow_live": os.environ.get("AGENTIC_ALLOW_LIVE") == "1",
            "allow_autonomy": os.environ.get("AGENTIC_ALLOW_AUTONOMY") == "1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = Path(self.config.state_dir) / "live_gate.json"
        try:
            jsonio.write_text(path, jsonio.dumps(payload, indent=2) + "\n")
        except OSError:
            return
        self.journal.append({"event": "live_gate", **payload})

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
        agents = [
            {
                "name": "strategy",
                "role": f"mechanical signals ({self.config.strategy})",
                "kind": "local",
                "status": "running" if not self.should_stop() else "stopped",
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
                    "running"
                    if eval_thread and eval_thread.is_alive()
                    else "idle"
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
        self.resolve_account()
        self.refresh_equity()
        self.seed_strategy_positions()
        self.write_live_gate_state()
        self.write_agent_state()

    def seed_strategy_positions(self) -> None:
        """Tell the strategy what is actually held before it decides anything.

        A restarted strategy otherwise believes the book is empty: it re-runs
        the daily rebalance, and it can never emit an exit for a position it
        does not know it has. The runtime owns the truth, so it hands it over.

        The runtime's own shadow book is day-scoped, so "what is held" is taken
        from the journal across recent days as well, which is where a position
        opened yesterday actually lives.
        """
        seed = getattr(self.strategy, "seed_positions", None)
        if not callable(seed):
            return
        held: dict[str, Decimal] = {}
        try:
            if self.mode == "shadow":
                held.update(dict(self.shadow_book.as_snapshot().held))
            else:
                held.update(dict(self.live_positions().held))
        except Exception as exc:  # noqa: BLE001 — never block start-up
            self.journal.append({"event": "strategy_seed_failed", "error": str(exc)[:200]})
            return
        replayed = self.held_from_journal()
        for symbol, quantity in replayed.items():
            # The broker view wins when both exist; the journal covers the
            # positions it has already forgotten (a new day, a restart).
            held.setdefault(symbol, quantity)
        if not held:
            return
        try:
            count = seed({symbol: str(quantity) for symbol, quantity in held.items()})
        except Exception as exc:  # noqa: BLE001
            self.journal.append({"event": "strategy_seed_failed", "error": str(exc)[:200]})
            return
        if count:
            self.journal.append(
                {
                    "event": "strategy_seeded",
                    "positions": count,
                    "from_journal": sorted(replayed),
                    "mode": self.mode,
                }
            )

    def held_from_journal(self, *, days: int = 10) -> dict[str, Decimal]:
        """Net position per symbol from recent *accepted* decisions.

        Accepted means "the runtime treated it as filled" in shadow, and "it was
        submitted" in live — either way it is the best record of what the book
        should contain once the day-scoped view has moved on.
        """
        held: dict[str, Decimal] = {}
        today = date.today()
        for offset in range(days - 1, -1, -1):
            path = Path(self.config.journal_dir) / (
                today - timedelta(days=offset)
            ).isoformat()
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
                intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
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
        return any(is_crypto_symbol(s) for s in self.config.symbol_whitelist)

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
            self.journal.append(
                {"event": "open_orders_read_failed", "error": str(exc)}
            )
            return

        symbols: set[str] = set()
        summary: list[dict[str, Any]] = []
        for order in orders:
            symbol = str(
                order.get("symbol") or order.get("instrument_symbol") or ""
            ).upper()
            if symbol:
                symbols.add(symbol)
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
        self._open_orders_read_at = time.monotonic()
        if summary:
            self.journal.append(
                {"event": "open_orders", "count": len(summary), "orders": summary}
            )

    # -- pipeline ---------------------------------------------------------

    def handle_quote(self, quote: dict, *, session: str = "regular") -> None:
        if self.should_stop():
            return
        intents = self.strategy.on_quote(quote)
        for intent in intents:
            if self.should_stop():
                return
            self.process_intent(intent, session=session)

    def process_intent(self, intent: OrderIntent, *, session: str = "regular") -> None:
        if self.journal.has_decision(intent.decision_id):
            return

        side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
        is_entry = side is Side.BUY

        # Fit entries to the per-order cap before the guard sees them: a small
        # account must trade smaller, not refuse to trade at all.
        if self.config.equity_sizing and is_entry:
            from agentic_trading.sizer import size_intent

            sized = size_intent(
                intent,
                equity=self.guard.current_equity,
                max_order_pct=self.guard.max_order_pct,
                min_notional=self.config.min_order_notional,
            )
            if sized is None:
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

        if is_entry and self.orders_today >= self.config.max_orders_per_day:
            self._journal_rejected(intent, "max_orders_per_day", Decimal("0"))
            return
        if is_entry and intent.symbol.upper() in self.open_order_symbols:
            self._journal_rejected(intent, "open_order_pending", Decimal("0"))
            return

        # Regime gate: a bad regime may refuse entries, never create them. It
        # reads a cached classification, so this costs nothing on the order path.
        if is_entry and self.regime_gate is not None:
            blocked = self.regime_gate.blocks(intent.symbol)
            if blocked is not None:
                self._journal_rejected(
                    intent,
                    f"regime_block: {blocked.regime} "
                    f"c={blocked.confidence:.2f}"[:120],
                    intent.resolved_notional(),
                )
                return

        # Advisory veto: the model may refuse an entry (reduce risk) and its
        # hold opinion is recorded, but it never overrides a mechanical exit.
        advisor_payload: Optional[dict[str, Any]] = None
        if self.advisor is not None:
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
                        f"llm_veto: {decision.reason}" if decision.reason else "llm_veto",
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
                return

        try:
            decision = self.guard.evaluate(intent, snapshot)
        except ValueError as exc:
            self._journal_rejected(
                intent, f"guard_error: {exc}", Decimal("0"), advisor=advisor_payload
            )
            return
        if not decision.allowed:
            self._journal_rejected(
                intent, decision.reason, decision.notional, advisor=advisor_payload
            )
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
            return

        # Review is a read-only simulation. It runs in both modes so the
        # operator sees the broker's pre-trade alerts before going live.
        try:
            review = self.broker.review_order(request)
        except Exception as exc:  # noqa: BLE001 — no review, no placement
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "review_failed",
                    "error": str(exc),
                    "order_request": request.to_mcp_args(),
                }
            )
            self.note_error("review_failed", exc)
            return
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
            }
        )
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

        if not (decision.may_place and os.environ.get("AGENTIC_ALLOW_LIVE") == "1"):
            if decision.may_place:
                # The evidence gate has cleared and the stage is live: the only
                # thing left is the operator's arming switch. Say so instead of
                # looking identical to a normal shadow cycle.
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "live_gate_blocked",
                        "reason": "AGENTIC_ALLOW_LIVE_not_set",
                        "symbol": intent.symbol,
                        "side": side.value,
                        "notional": str(decision.notional),
                        "stage": self.stage,
                        "hint": "arm with AGENTIC_ALLOW_LIVE=1 in the daemon environment",
                    }
                )
            self.guard.persist(self.config.state_dir)
            return

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
        self.journal.append(
            {
                "decision_id": intent.decision_id,
                "event": "placed",
                "order_request": request.to_mcp_args(),
                "order": result,
            }
        )

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

    def market_features(self, symbol: str, quote: Optional[dict[str, Any]] = None) -> Any:
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

    def refresh_regimes(self, *, max_per_pass: int = 2) -> list[dict[str, Any]]:
        """Refresh stale regime classifications. Worker thread only.

        Each refresh is a model call (~4s), so the caller runs this off the order
        path and only a couple of symbols are refreshed per pass: the gate is a
        filter, and a filter that is a few minutes stale is still a filter.
        """
        if self.regime_gate is None:
            return []
        refreshed = self.regime_gate.refresh_due(
            [s.upper() for s in self.config.symbol_whitelist],
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
        count = 0
        for record in self.journal.iter_today():
            if record.get("event") in ("accepted", "placed"):
                count += 1
        return count


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
        f"symbols={len(config.symbol_whitelist)} pid={os.getpid()}",
        flush=True,
    )

    loop = _Loop(config, broker, strategy, stop_event, force_shadow=force_shadow)
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
        while not loop.should_stop():
            cycle_started = time.monotonic()
            # Equity moves while the loop runs; the size floor follows it.
            loop.apply_size_floor()
            if deadline is not None and time.monotonic() >= deadline:
                break

            session = (
                session_clock() if session_clock is not None else session_for(clock())
            )
            # The effective policy comes from the loop: the agent may widen it
            # within the operator's bound as its confidence grows.
            if not session_allows(loop.session_policy, session):
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
            now = time.monotonic()
            if (now - last_equity_at) >= config.equity_refresh_seconds:
                loop.refresh_equity()
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
                        _loop.journal.append(
                            {
                                "event": "selfcheck",
                                "healthy": report.healthy,
                                "ok": sum(
                                    1 for c in report.checks if c.status == "ok"
                                ),
                                "warnings": [c.to_dict() for c in report.warnings],
                                "failures": [c.to_dict() for c in report.failures],
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        _loop.journal.append(
                            {"event": "selfcheck_failed", "error": str(exc)[:200]}
                        )

                loop.selfcheck_thread = threading.Thread(  # type: ignore[attr-defined]
                    target=_selfcheck, daemon=True, name="selfcheck"
                )
                loop.selfcheck_thread.start()
                workers.append(loop.selfcheck_thread)

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
            for quote in fresh:
                if loop.should_stop():
                    break
                try:
                    loop.handle_quote(quote, session=session)
                except Exception as exc:  # noqa: BLE001 — one bad tick is not fatal
                    cycle_had_error = True
                    loop.note_error("loop_error", exc)

            # A tick that raised is counted; a cycle that raises nothing is
            # evidence the connection is back. Without this reset, three
            # transient errors spread across an outage would accumulate into a
            # kill switch and stop the agent until a human cleared it.
            if loop.consecutive_errors and not cycle_had_error:
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
    """Flip the run mode and record it, for any path that advanced a stage.

    Two paths can promote — the scheduled search and the walk-forward regrade —
    and they must not each own their own copy of this. The regrade path used to
    record the promotion and stop there, which left the agent promoted in state
    and shadow in practice.
    """
    from agentic_trading import selfimprove

    if config.autonomy != "auto" or not selfimprove.autonomy_enabled():
        journal.append(
            {
                "event": "promotion_requires_consent",
                "stage": state.stage,
                "hint": "set autonomy = \"auto\" and AGENTIC_ALLOW_AUTONOMY=1",
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
        return
    production = (report.get("configs") or {}).get("production") or {}
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
    from agentic_trading.evidence import read_report
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
    policy = policy_from_config(config)
    state = load_state(config.state_dir)
    previous = (state.last_assessment or {}).get("evidence") or {}
    stored = load_limits(config.state_dir)
    live = (
        float(stored.max_order_pct)
        if stored is not None
        else float(config.max_order_pct)
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
    if any(event.get("event") == "promotion" for event in events):
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
            journal.append({"event": "autonomy_applied", "stage": "shadow", "mode": "shadow"})
            return

    # 2. Re-evolve on a schedule.
    now = time.monotonic()
    interval = config.evolution_interval_minutes * 60
    if interval <= 0 or (loop.last_evolution_at and (now - loop.last_evolution_at) < interval):
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
                flagged = [
                    result.to_dict()
                    for result in results
                    if result.issues
                ]
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
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                journal.append({"event": "history_sync_failed", "error": str(exc)[:200]})

            # Correlations and execution costs are recomputed on the same
            # cadence as the data: both are derived from it.
            # Correlations are derived from the freshly synced data, so they
            # belong on the data cadence.
            try:
                from agentic_trading.correlation import compute_state, save_state

                state = compute_state(
                    config.history_path,
                    config.symbol_whitelist,
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
                        "max_correlated_positions": (
                            config.max_correlated_positions
                        ),
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

    # Skip the (minutes-long) search when nothing the evaluation reads has
    # changed: the journal then explains why confidence is not moving.
    # What execution actually cost, against what the model assumes. Cheap (one
    # read) and worth re-checking every pass: the moment a fill lands, the
    # evidence switches from assumed costs to measured ones.
    try:
        from agentic_trading.execution import decision_prices, measure, save_report

        trades = loop.broker.get_trade_history(span="month")
        report = measure(
            trades,
            decision_prices(journal.iter_today()),
            assumed_per_side_bps=2.0,
        )
        save_report(config.state_dir, report)
        journal.append(
            {
                "event": "execution_costs",
                "fills": report.fills,
                "measured_per_side_bps": report.measured_per_side_bps,
                "assumed_per_side_bps": report.assumed_per_side_bps,
                "usable": report.usable,
                "note": report.note,
            }
        )
    except Exception as exc:  # noqa: BLE001 — never kill the loop
        journal.append(
            {"event": "execution_costs_failed", "error": str(exc)[:200]}
        )

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
    promoted = any(event.get("event") == "promotion" for event in events)
    if not promoted:
        return
    _apply_promotion(config, loop, journal, promotion_state)
