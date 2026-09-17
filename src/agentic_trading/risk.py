"""RiskGuard — equity % caps, whitelist, kill-switch, shadow book.

Accepted journal event shape (for ShadowBook.from_journal / daily rebuild)::

    {
      "event": "accepted",
      "decision_id": "...",
      "would_place": true,          # shadow
      "may_place": false,
      "notional": "50",             # optional top-level; else intent notional
      "intent": {
        "symbol": "SPY",
        "side": "buy",
        "quantity": "0.5",          # required for ShadowBook.apply_accepted
        "ref_price": "100"          # required for ShadowBook.apply_accepted
      }
    }

Only records with event == "accepted" (and shadow would_place or live may_place /
accepted writes) are applied to the shadow book and daily notional rebuild.

Day roll / shadow PnL
---------------------
At local midnight (operator ``timezone``), RiskGuard resets daily notional,
baseline equity (re-seeded on next ``update_equity``), and day-scoped shadow
realized PnL used by ``note_shadow_realized``. Open shadow positions
(``ShadowBook.held`` / avg cost) are **not** cleared.

Runtime should either:

1. ``guard.attach_shadow_book(book)`` so day-roll calls ``ShadowBook.roll_day()``
   (clears ``realized_pnl``, keeps held), then pass the book's day realized into
   ``note_shadow_realized`` after each accepted shadow fill; or
2. Rebuild ``ShadowBook.from_journal(today)`` after the day changes (journal files
   are day-scoped) and feed ``note_shadow_realized`` from that book's realized.

``note_shadow_realized`` expects **today's** cumulative shadow realized (not
lifetime across days).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from agentic_trading.types import OrderIntent, Side

_STATE_FILE = "risk_guard.json"


@dataclass
class PortfolioSnapshot:
    open_positions: int
    held: dict[str, Decimal]
    positions_read_failed: bool = False


@dataclass
class GuardDecision:
    allowed: bool
    reason: str
    would_place: bool
    may_place: bool
    notional: Decimal


@dataclass
class ShadowBook:
    """Local shadow positions.

    ``realized_pnl`` is **day-scoped** — RiskGuard's daily-loss kill switch needs
    today's number, not the lifetime one. ``realized_total`` is the running
    figure that survives midnight, so the forward record the strategy is being
    judged on is not thrown away every night.
    """

    held: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    realized_total: Decimal = Decimal("0")
    _avg_cost: dict[str, Decimal] = field(default_factory=dict, repr=False)

    @classmethod
    def from_journal(cls, journal: Any, *, days: int = 1) -> "ShadowBook":
        """Rebuild from the journal. ``days > 1`` makes positions persist.

        Replaying only today is what made holdings and P&L vanish at midnight
        and on every restart.
        """
        book = cls()
        iterate = (
            journal.iter_recent(days=days)
            if days > 1 and hasattr(journal, "iter_recent")
            else journal.iter_today()
        )
        for record in iterate:
            if not _is_accepted_record(record):
                continue
            intent = _intent_from_record(record, require_qty_price=True)
            if intent is None:
                continue
            book.apply_accepted(intent)
        return book

    def apply_accepted(self, intent: OrderIntent) -> None:
        symbol = intent.symbol.upper()
        qty, price = _qty_and_price(intent)
        side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))

        if side is Side.BUY:
            prev_qty = self.held.get(symbol, Decimal("0"))
            prev_cost = self._avg_cost.get(symbol, Decimal("0"))
            new_qty = prev_qty + qty
            if new_qty > 0:
                self._avg_cost[symbol] = (
                    (prev_cost * prev_qty) + (price * qty)
                ) / new_qty
            self.held[symbol] = new_qty
            return

        if side is Side.SELL:
            prev_qty = self.held.get(symbol, Decimal("0"))
            if prev_qty <= 0:
                return
            if qty > prev_qty:
                raise ValueError(
                    f"ShadowBook oversell: qty {qty} > held {prev_qty} for {symbol}"
                )
            avg = self._avg_cost.get(symbol, Decimal("0"))
            realized = (price - avg) * qty
            self.realized_pnl += realized
            self.realized_total += realized
            remaining = prev_qty - qty
            if remaining <= 0:
                self.held.pop(symbol, None)
                self._avg_cost.pop(symbol, None)
            else:
                self.held[symbol] = remaining
            return

        raise ValueError(f"unknown side: {side!r}")

    def roll_day(self) -> None:
        """Reset day-scoped realized PnL; held and avg cost persist across midnight."""
        self.realized_pnl = Decimal("0")

    def as_snapshot(self) -> PortfolioSnapshot:
        held = {k: v for k, v in self.held.items() if v > 0}
        return PortfolioSnapshot(open_positions=len(held), held=dict(held))


class RiskGuard:
    def __init__(
        self,
        *,
        mode: str,
        whitelist: frozenset[str],
        max_order_pct: Decimal,
        daily_notional_pct: Decimal,
        daily_loss_pct: Decimal,
        max_open_positions: int,
        baseline_equity: Decimal,
        current_equity: Decimal,
        timezone: str = "local",
    ) -> None:
        if mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        self.mode = mode
        self.whitelist = frozenset(s.upper() for s in whitelist)
        self.max_order_pct = Decimal(str(max_order_pct))
        self.daily_notional_pct = Decimal(str(daily_notional_pct))
        self.daily_loss_pct = Decimal(str(daily_loss_pct))
        self.max_open_positions = int(max_open_positions)
        self.baseline_equity = Decimal(str(baseline_equity))
        self.current_equity = Decimal(str(current_equity))
        self.timezone = timezone or "local"
        self._kill_switch = False
        self._kill_reason = ""
        self._daily_notional = Decimal("0")
        self._shadow_realized_today = Decimal("0")
        self._shadow_book: Optional[ShadowBook] = None
        self._day_key = self._today_key()
        # Correlation-aware position limits. None means "not configured", which
        # preserves the plain max_open_positions behaviour.
        self.max_correlated_positions: Optional[int] = None
        self.correlation_threshold: float = 0.7
        self._correlation_state: Any = None

    @property
    def shadow_realized_today(self) -> Decimal:
        return self._shadow_realized_today

    @property
    def shadow_realized_total(self) -> Decimal:
        """Lifetime shadow P&L: the forward record, not today's slice."""
        book = self._shadow_book
        return book.realized_total if book is not None else Decimal("0")

    @property
    def kill_switch(self) -> bool:
        return self._kill_switch

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def attach_shadow_book(self, book: ShadowBook) -> None:
        """Optional: day-roll will call ``book.roll_day()`` (keeps held)."""
        self._shadow_book = book

    def evaluate(
        self, intent: OrderIntent, snapshot: PortfolioSnapshot
    ) -> GuardDecision:
        self._roll_day_if_needed()
        notional = intent.resolved_notional()

        if self._kill_switch:
            return self._deny(notional, "kill_switch")

        symbol = intent.symbol.upper()
        if symbol not in self.whitelist:
            return self._deny(notional, "symbol_not_whitelisted")

        equity = self._cap_equity()
        max_order = self.max_order_pct * equity
        if notional > max_order:
            return self._deny(notional, "over_max_order")

        daily_cap = self.daily_notional_pct * equity
        if self._daily_notional + notional > daily_cap:
            return self._deny(notional, "over_daily_notional")

        side = intent.side if isinstance(intent.side, Side) else Side(str(intent.side))
        if side is Side.BUY:
            if snapshot.positions_read_failed:
                return self._deny(notional, "positions_read_failed")
            if snapshot.open_positions >= self.max_open_positions:
                return self._deny(notional, "max_open_positions")
            # Concentration: a correlated cluster is one bet, so count it as one.
            if self.max_correlated_positions is not None and snapshot.held:
                from agentic_trading.correlation import correlated_with

                correlated, unknown = correlated_with(
                    symbol,
                    snapshot.held.keys(),
                    self._correlation_state,
                    threshold=self.correlation_threshold,
                )
                if len(correlated) >= self.max_correlated_positions:
                    return self._deny(
                        notional, f"correlated_exposure: {sorted(correlated)}"
                    )
                if unknown:
                    # No measurement, no extra room: an unmeasured pair is
                    # treated as if it were perfectly correlated.
                    return self._deny(
                        notional, f"correlation_unknown: {sorted(unknown)}"
                    )
        elif side is Side.SELL:
            held_qty = Decimal(str(snapshot.held.get(symbol, Decimal("0"))))
            if held_qty <= 0:
                return self._deny(notional, "would_short")
            if intent.quantity is not None:
                qty = Decimal(str(intent.quantity))
                if qty > held_qty:
                    return self._deny(notional, "oversell")
            # positions_read_failed: close only if held confirms symbol (checked above)
        else:
            return self._deny(notional, "unknown_side")

        return self._allow(notional)

    def record_accepted(self, intent: OrderIntent) -> None:
        self._roll_day_if_needed()
        self._daily_notional += intent.resolved_notional()

    def update_equity(self, equity: Decimal) -> None:
        self._roll_day_if_needed()
        eq = Decimal(str(equity))
        self.current_equity = eq
        # First successful read of the day sets baseline if unset that day
        if self.baseline_equity <= 0:
            self.baseline_equity = eq
        if self.mode == "live":
            self._check_live_loss()

    def note_shadow_realized(self, realized_pnl: Decimal) -> None:
        """Update day-scoped shadow realized and trip kill if loss limit hit.

        ``realized_pnl`` must be **today's** cumulative shadow realized PnL
        (see module docstring on day roll).
        """
        self._roll_day_if_needed()
        self._shadow_realized_today = Decimal(str(realized_pnl))
        if self.baseline_equity <= 0:
            return
        limit = -self.daily_loss_pct * self.baseline_equity
        if self._shadow_realized_today <= limit:
            self.trip_kill_switch("shadow_daily_loss")

    def trip_kill_switch(self, reason: str) -> None:
        self._kill_switch = True
        self._kill_reason = str(reason)

    def reset_kill_switch(self) -> None:
        self._kill_switch = False
        self._kill_reason = ""

    def persist(self, state_dir: Path | str) -> None:
        path = Path(state_dir)
        path.mkdir(parents=True, exist_ok=True)
        payload = {
            "kill_switch": self._kill_switch,
            "kill_reason": self._kill_reason,
            "baseline_equity": str(self.baseline_equity),
            "current_equity": str(self.current_equity),
            "day_key": self._day_key,
            "daily_notional": str(self._daily_notional),
            "shadow_realized_today": str(self._shadow_realized_today),
            "shadow_realized_total": str(self.shadow_realized_total),
            "mode": self.mode,
            "timezone": self.timezone,
        }
        (path / _STATE_FILE).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def load(self, state_dir: Path | str, journal: Any = None) -> None:
        path = Path(state_dir) / _STATE_FILE
        today = self._today_key()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._kill_switch = bool(raw.get("kill_switch", False))
            self._kill_reason = str(raw.get("kill_reason", ""))
            if "baseline_equity" in raw:
                self.baseline_equity = Decimal(str(raw["baseline_equity"]))
            if "current_equity" in raw:
                self.current_equity = Decimal(str(raw["current_equity"]))
            stored_day = str(raw.get("day_key", ""))
            if stored_day == today:
                self._day_key = today
                self._daily_notional = Decimal(str(raw.get("daily_notional", "0")))
                self._shadow_realized_today = Decimal(
                    str(raw.get("shadow_realized_today", "0"))
                )
            else:
                # New day: reset daily notional / PnL window; keep kill
                self._day_key = today
                self._daily_notional = Decimal("0")
                self._shadow_realized_today = Decimal("0")
                self.baseline_equity = Decimal("0")
                if self._shadow_book is not None:
                    self._shadow_book.roll_day()

        if journal is not None:
            rebuilt = Decimal("0")
            for record in journal.iter_today():
                if not _is_accepted_record(record):
                    continue
                n = _notional_from_record(record)
                if n is not None:
                    rebuilt += n
            if rebuilt > self._daily_notional:
                self._daily_notional = rebuilt

    def _today_key(self) -> str:
        if self.timezone in ("local", ""):
            return date.today().isoformat()
        from agentic_trading.tz import zone

        return datetime.now(zone(self.timezone)).date().isoformat()

    def _cap_equity(self) -> Decimal:
        eq = self.current_equity if self.current_equity > 0 else self.baseline_equity
        if eq <= 0:
            raise ValueError("equity must be positive for risk caps")
        return eq

    def _roll_day_if_needed(self) -> None:
        today = self._today_key()
        if self._day_key != today:
            self._day_key = today
            self._daily_notional = Decimal("0")
            self.baseline_equity = Decimal("0")
            self._shadow_realized_today = Decimal("0")
            if self._shadow_book is not None:
                self._shadow_book.roll_day()

    def _check_live_loss(self) -> None:
        if self.baseline_equity <= 0:
            return
        daily_pnl = self.current_equity - self.baseline_equity
        limit = -self.daily_loss_pct * self.baseline_equity
        if daily_pnl <= limit:
            self.trip_kill_switch("live_daily_loss")

    def _allow(self, notional: Decimal) -> GuardDecision:
        if self.mode == "shadow":
            return GuardDecision(
                allowed=True,
                reason="ok",
                would_place=True,
                may_place=False,
                notional=notional,
            )
        if self.mode == "live":
            return GuardDecision(
                allowed=True,
                reason="ok",
                would_place=True,
                may_place=True,
                notional=notional,
            )
        raise ValueError(f"unknown mode: {self.mode!r}")

    def _deny(self, notional: Decimal, reason: str) -> GuardDecision:
        return GuardDecision(
            allowed=False,
            reason=reason,
            would_place=False,
            may_place=False,
            notional=notional,
        )


def _is_accepted_record(record: Mapping[str, Any]) -> bool:
    if record.get("event") != "accepted":
        return False
    if record.get("would_place") is True:
        return True
    if record.get("may_place") is True:
        return True
    return "intent" in record or "notional" in record


def _notional_from_record(record: Mapping[str, Any]) -> Optional[Decimal]:
    if record.get("notional") is not None:
        return Decimal(str(record["notional"]))
    intent = record.get("intent")
    if isinstance(intent, Mapping):
        if intent.get("notional_usd") is not None:
            return Decimal(str(intent["notional_usd"]))
        if intent.get("quantity") is not None and intent.get("ref_price") is not None:
            return Decimal(str(intent["quantity"])) * Decimal(str(intent["ref_price"]))
    return None


def _intent_from_record(
    record: Mapping[str, Any], *, require_qty_price: bool = False
) -> Optional[OrderIntent]:
    raw = record.get("intent")
    if not isinstance(raw, Mapping):
        return None
    symbol = str(raw.get("symbol", "")).upper()
    if not symbol:
        return None
    side_raw = raw.get("side", "buy")
    side = side_raw if isinstance(side_raw, Side) else Side(str(side_raw))

    created = raw.get("created_at")
    if isinstance(created, datetime):
        created_at = created
    else:
        created_at = datetime.now(timezone.utc)

    kwargs: dict[str, Any] = {
        "decision_id": str(
            record.get("decision_id") or raw.get("decision_id") or "replay"
        ),
        "symbol": symbol,
        "side": side,
        "reason": str(raw.get("reason", "journal_replay")),
        "created_at": created_at,
    }
    has_qty_price = raw.get("quantity") is not None and raw.get("ref_price") is not None
    if require_qty_price and not has_qty_price:
        return None
    if has_qty_price:
        kwargs["quantity"] = Decimal(str(raw["quantity"]))
        kwargs["ref_price"] = Decimal(str(raw["ref_price"]))
    elif raw.get("notional_usd") is not None:
        kwargs["notional_usd"] = Decimal(str(raw["notional_usd"]))
    elif record.get("notional") is not None:
        kwargs["notional_usd"] = Decimal(str(record["notional"]))
    else:
        return None
    return OrderIntent(**kwargs)


def _qty_and_price(intent: OrderIntent) -> tuple[Decimal, Decimal]:
    if intent.quantity is not None and intent.ref_price is not None:
        return Decimal(str(intent.quantity)), Decimal(str(intent.ref_price))
    raise ValueError(
        "ShadowBook requires quantity and ref_price; notional-only intents are not supported"
    )
