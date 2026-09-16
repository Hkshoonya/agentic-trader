"""Shadow/live runtime: quotes → strategy → RiskGuard → journal / broker.

Two entry points:

- :func:`run_loop` — replay a local JSONL quote file once (soak / tests / replay)
- :func:`run_daemon` — continuous autonomous loop over a live quote feed with
  session gating, equity refresh, fill reconciliation and error kill-switch

Live placement requires **all** of: mode ``live``, ``AGENTIC_ALLOW_LIVE=1``, a
session permitted by ``session_policy``, and a RiskGuard allow decision.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from agentic_trading.broker import Broker, BrokerPayloadError
from agentic_trading.config import Config
from agentic_trading.journal import DecisionJournal
from agentic_trading.marketdata import QuoteFeed, build_quote_feed
from agentic_trading.orders import EquityOrderRequest, OrderValidationError
from agentic_trading.quotes import iter_quotes
from agentic_trading.rh_mcp.snapshot import write_tools_snapshot
from agentic_trading.risk import RiskGuard, ShadowBook
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
    symbol = intent.symbol.upper()
    is_crypto = "-" in symbol or symbol.endswith("USD")
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
        self.guard = build_guard(config, self.mode)
        self.shadow_book = ShadowBook.from_journal(self.journal)
        self.guard.attach_shadow_book(self.shadow_book)
        self.guard.load(config.state_dir, self.journal)
        if self.mode == "shadow":
            self.guard.note_shadow_realized(self.shadow_book.realized_pnl)
        self.account_number: Optional[str] = config.account_number
        self.orders_today = self._count_orders_today()
        self.consecutive_errors = 0
        self.open_order_symbols: set[str] = set()
        self._stopping = False
        self.stage = "shadow"
        self.last_evolution_at = 0.0
        self.advisor = None
        try:
            from agentic_trading.llm.advisor import build_advisor

            self.advisor = build_advisor()
        except Exception:  # noqa: BLE001 — advisory layer must never block startup
            self.advisor = None
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
        # The agent's stored limits may only ever tighten what the operator set.
        from agentic_trading.limits import apply_to_guard

        self.guard.daily_notional_pct = self.config.daily_notional_pct
        apply_to_guard(self.guard, self.config)

    def set_mode(self, mode: str) -> None:
        """Switch shadow/live at runtime (autonomy promotion or demotion)."""
        if mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        if self.force_shadow and mode == "live":
            return
        self.mode = mode
        self.guard.mode = mode
        self.apply_stage_caps()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.resolve_account()
        self.refresh_equity()

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
        try:
            # Live schema rejects state_group; filter open states client-side.
            orders = [
                order
                for order in self.broker.get_orders()
                if str(order.get("state", "")).lower()
                in ("queued", "confirmed", "unconfirmed", "new", "partially_filled")
            ]
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

        # Advisory veto: the model may refuse an entry (reduce risk) and its
        # hold opinion is recorded, but it never overrides a mechanical exit.
        if self.advisor is not None:
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
                },
            )
            if decision is not None:
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "advisor",
                        "model": getattr(self.advisor, "model", ""),
                        "role": "entry" if is_entry else "exit",
                        "symbol": intent.symbol,
                        **decision.to_dict(),
                    }
                )
                if decision.vetoes and is_entry:
                    self._journal_rejected(
                        intent,
                        f"llm_veto: {decision.reason}" if decision.reason else "llm_veto",
                        intent.resolved_notional(),
                    )
                    return
            elif getattr(self.advisor, "last_error", ""):
                # An advisor that fails silently is indistinguishable from one
                # that does not exist; surface the reason instead.
                self.journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "advisor_error",
                        "error": self.advisor.last_error,
                        "model": getattr(self.advisor, "model", ""),
                    }
                )

        if self.mode == "shadow":
            snapshot = self.shadow_book.as_snapshot()
        else:
            try:
                snapshot = self.broker.get_positions()
            except Exception as exc:  # noqa: BLE001 — never trade without positions
                self._journal_rejected(intent, f"positions_read_error: {exc}", Decimal("0"))
                self.note_error("positions_read_error", exc)
                return

        try:
            decision = self.guard.evaluate(intent, snapshot)
        except ValueError as exc:
            self._journal_rejected(intent, f"guard_error: {exc}", Decimal("0"))
            return
        if not decision.allowed:
            self._journal_rejected(intent, decision.reason, decision.notional)
            return

        try:
            symbol = intent.symbol.upper()
            is_crypto = "-" in symbol or symbol.endswith("USD")
            account = (
                self.broker.resolve_rhs_account_number()
                if is_crypto
                else self.resolve_account()
            )
            request = build_order_request(
                intent,
                account_number=account,
                config=self.config,
                session=session,
            )
        except OrderValidationError as exc:
            self._journal_rejected(intent, f"order_invalid: {exc}", decision.notional)
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
                note_fill(intent.symbol, intent.quantity)
            self.guard.persist(self.config.state_dir)
            return

        if self.should_stop():
            self.guard.persist(self.config.state_dir)
            return

        if not (decision.may_place and os.environ.get("AGENTIC_ALLOW_LIVE") == "1"):
            self.guard.persist(self.config.state_dir)
            return

        self._place(intent, request)
        self.guard.persist(self.config.state_dir)

    def _place(self, intent: OrderIntent, request: EquityOrderRequest) -> None:
        # Crypto has no historicals to validate against, so it may be simulated
        # (shadow) but never submitted. _place only runs in live mode, which
        # makes this the correct place for that refusal.
        from agentic_trading.marketdata import is_crypto_pair

        if is_crypto_pair(intent.symbol):
            self.journal.append(
                {
                    "decision_id": intent.decision_id,
                    "event": "place_refused",
                    "reason": "crypto_execution_disabled",
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
        self, intent: OrderIntent, reason: str, notional: Decimal
    ) -> None:
        self.journal.append(
            {
                "decision_id": intent.decision_id,
                "event": "rejected",
                "reason": reason,
                "would_place": False,
                "may_place": False,
                "notional": str(notional),
                "mode": self.mode,
                "intent": _intent_payload(intent),
            }
        )

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
    last_heartbeat = 0.0
    last_equity_at = 0.0
    try:
        loop.start()
        last_equity_at = time.monotonic()
        journal = loop.journal
        while not loop.should_stop():
            if deadline is not None and time.monotonic() >= deadline:
                break

            session = (
                session_clock() if session_clock is not None else session_for(clock())
            )
            if not session_allows(config.session_policy, session):
                now = time.monotonic()
                if (now - last_heartbeat) >= _HEARTBEAT_SECONDS:
                    journal.append(
                        {
                            "event": "session_closed",
                            "session": session,
                            "policy": config.session_policy,
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

            for quote in fresh:
                if loop.should_stop():
                    break
                try:
                    loop.handle_quote(quote, session=session)
                except Exception as exc:  # noqa: BLE001 — one bad tick is not fatal
                    loop.note_error("loop_error", exc)

            if once:
                break
            sleep(config.poll_seconds)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        loop.finish()


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
            loop.set_mode("shadow")
            journal.append({"event": "autonomy_applied", "stage": "shadow", "mode": "shadow"})
            return

    # 2. Re-evolve on a schedule.
    now = time.monotonic()
    interval = config.evolution_interval_minutes * 60
    if interval <= 0 or (loop.last_evolution_at and (now - loop.last_evolution_at) < interval):
        return
    loop.last_evolution_at = now

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

    # 3. Apply a promotion only with explicit operator consent.
    promoted = any(event.get("event") == "promotion" for event in events)
    if not promoted:
        return
    if config.autonomy != "auto" or not selfimprove.autonomy_enabled():
        journal.append(
            {
                "event": "promotion_requires_consent",
                "stage": promotion_state.stage,
                "hint": "set autonomy = \"auto\" and AGENTIC_ALLOW_AUTONOMY=1",
            }
        )
        return

    for event in selfimprove.apply_stage(config, promotion_state):
        journal.append(event)
    loop.set_mode("live" if promotion_state.stage != "shadow" else "shadow")
    journal.append(
        {
            "event": "autonomy_applied",
            "stage": promotion_state.stage,
            "mode": loop.mode,
            "caps": {"max_order_pct": str(loop.guard.max_order_pct)},
        }
    )
