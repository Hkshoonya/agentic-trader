"""Shadow/live runtime loop: quotes → strategy → RiskGuard → journal / broker."""

from __future__ import annotations

import os
import signal
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Protocol

from agentic_trading.broker import Broker
from agentic_trading.config import Config
from agentic_trading.journal import DecisionJournal
from agentic_trading.quotes import iter_quotes
from agentic_trading.rh_mcp.snapshot import write_tools_snapshot
from agentic_trading.risk import RiskGuard, ShadowBook
from agentic_trading.strategies.fixture import FixtureStrategy
from agentic_trading.types import OrderIntent, Side


class Strategy(Protocol):
    def on_quote(self, quote: dict) -> list[OrderIntent]: ...


_MODE_FILE = "mode"


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


def _order_kwargs(intent: OrderIntent, notional: Decimal) -> dict[str, Any]:
    side = intent.side.value if isinstance(intent.side, Side) else str(intent.side)
    kwargs: dict[str, Any] = {
        "symbol": intent.symbol,
        "side": side,
        "notional_usd": str(notional),
    }
    if intent.quantity is not None:
        kwargs["quantity"] = str(intent.quantity)
    if intent.ref_price is not None:
        kwargs["ref_price"] = str(intent.ref_price)
    return kwargs


def run_loop(
    config: Config,
    *,
    broker: Broker,
    strategy: Optional[Strategy] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Run the quote → strategy → guard → journal loop until feed ends or SIGINT.

    ``stop_event`` is optional (tests): when set, behaves like SIGINT — finish the
    current intent's review/journal but do not call ``place_order``.
    """
    mode = effective_mode(config)
    journal = DecisionJournal(Path(config.journal_dir))
    guard = build_guard(config, mode)
    shadow_book = ShadowBook.from_journal(journal)
    guard.attach_shadow_book(shadow_book)
    guard.load(config.state_dir, journal)
    if mode == "shadow":
        guard.note_shadow_realized(shadow_book.realized_pnl)

    snapshot_tools = tools if tools is not None else getattr(broker, "_tools", None)
    if snapshot_tools:
        write_tools_snapshot(snapshot_tools, config.tools_snapshot_path)

    equity = broker.get_equity()
    guard.update_equity(equity)
    guard.persist(config.state_dir)

    if strategy is None:
        strategy = FixtureStrategy()

    stop = False

    def _request_stop(signum: int, frame: Any) -> None:
        nonlocal stop
        stop = True

    def _should_stop() -> bool:
        return stop or (stop_event is not None and stop_event.is_set())

    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)

    ticks_since_equity = 0
    last_equity_at = time.monotonic()

    try:
        for quote in iter_quotes(config.quotes_path):
            if _should_stop():
                break

            ticks_since_equity += 1
            now = time.monotonic()
            refresh_ticks = ticks_since_equity >= config.equity_refresh_ticks
            refresh_time = (now - last_equity_at) >= config.equity_refresh_seconds
            if refresh_ticks or refresh_time:
                equity = broker.get_equity()
                guard.update_equity(equity)
                guard.persist(config.state_dir)
                ticks_since_equity = 0
                last_equity_at = now

            intents = strategy.on_quote(quote)
            for intent in intents:
                if _should_stop():
                    break

                if journal.has_decision(intent.decision_id):
                    continue

                if mode == "shadow":
                    snap = shadow_book.as_snapshot()
                else:
                    snap = broker.get_positions()

                decision = guard.evaluate(intent, snap)
                if not decision.allowed:
                    journal.append(
                        {
                            "decision_id": intent.decision_id,
                            "event": "rejected",
                            "reason": decision.reason,
                            "would_place": False,
                            "may_place": False,
                            "notional": str(decision.notional),
                            "mode": mode,
                            "intent": _intent_payload(intent),
                        }
                    )
                    continue

                review = broker.review_order(**_order_kwargs(intent, decision.notional))
                journal.append(
                    {
                        "decision_id": intent.decision_id,
                        "event": "accepted",
                        "reason": decision.reason,
                        "would_place": True,
                        "may_place": decision.may_place,
                        "notional": str(decision.notional),
                        "mode": mode,
                        "review": review,
                        "intent": _intent_payload(intent),
                        # Flat fields for ShadowBook / operators scanning JSONL
                        "symbol": intent.symbol,
                        "side": (
                            intent.side.value
                            if isinstance(intent.side, Side)
                            else str(intent.side)
                        ),
                        "quantity": (
                            str(intent.quantity)
                            if intent.quantity is not None
                            else None
                        ),
                        "ref_price": (
                            str(intent.ref_price)
                            if intent.ref_price is not None
                            else None
                        ),
                    }
                )
                guard.record_accepted(intent)

                if mode == "shadow":
                    # NEVER place_order when mode == shadow
                    shadow_book.apply_accepted(intent)
                    guard.note_shadow_realized(shadow_book.realized_pnl)
                elif _should_stop():
                    # Review + journal finished; do not start place after stop
                    guard.persist(config.state_dir)
                    break
                elif decision.may_place and os.environ.get("AGENTIC_ALLOW_LIVE") == "1":
                    broker.place_order(**_order_kwargs(intent, decision.notional))

                guard.persist(config.state_dir)

            if _should_stop():
                break
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        guard.persist(config.state_dir)
