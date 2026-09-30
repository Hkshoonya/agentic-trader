"""The venue guard: the checks every venue order passes, paper included.

Paper orders go through the same caps so that paper results describe what the
live venue would have been allowed to do. Checks run in a fixed order and the
first refusal is the reason given:
kill switch, arming, price, order cap, daily notional, daily loss, open
orders, then the pattern-day-trader rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentic_trading import jsonio
from agentic_trading.venues import arming
from agentic_trading.venues.model import AccountView, VenueOrder

PDT_EQUITY = Decimal("25000")
PDT_MAX_DAY_TRADES = 3
PDT_WINDOW_WEEKDAYS = 5


@dataclass(frozen=True)
class Limits:
    max_order_usd: Decimal
    max_daily_notional_usd: Decimal
    max_daily_loss_usd: Decimal
    max_open_orders: int
    live: bool
    pdt_applies: bool

    @classmethod
    def for_venue(cls, config: Any, venue: str) -> "Limits":
        return cls(
            max_order_usd=config.max_order_usd(venue),
            max_daily_notional_usd=config.max_daily_notional_usd,
            max_daily_loss_usd=config.max_daily_loss_usd,
            max_open_orders=config.max_open_orders,
            live=venue in arming.LIVE_VENUES,
            pdt_applies=venue == "alpaca_live",
        )


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def _fresh() -> dict[str, Any]:
    return {"day": "", "notional": "0", "realized_pnl": "0", "kill": False,
            "kill_reason": "", "bought": {}, "day_trades": []}


class VenueGuard:
    def __init__(self, venue: str, limits: Limits, state_dir: Path | str) -> None:
        self.venue = venue
        self.limits = limits
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / f"venue_guard_{venue}.json"
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        state = _fresh()
        if not self.path.exists():
            return state
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("not an object")
            Decimal(str(loaded.get("notional", "0")))
            Decimal(str(loaded.get("realized_pnl", "0")))
            state.update(loaded)
        except (OSError, ValueError, InvalidOperation):
            # Today's spending is unknown: refuse until someone looks.
            state["kill"] = True
            state["kill_reason"] = f"guard state unreadable; check then delete {self.path}"
        return state

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        jsonio.write_text(self.path, jsonio.dumps(self._state, indent=2) + "\n")

    def _roll(self, now: datetime) -> None:
        day = now.date().isoformat()
        if self._state["day"] != day:
            self._state.update(day=day, notional="0", realized_pnl="0", bought={})

    def day_trades_in_window(self, now: datetime) -> int:
        days: list[str] = []
        cursor = now.date()
        while len(days) < PDT_WINDOW_WEEKDAYS:
            if cursor.weekday() < 5:
                days.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        return sum(1 for day, _symbol in self._state["day_trades"] if day in days)

    def check(
        self,
        order: VenueOrder,
        *,
        price: Decimal,
        account: AccountView,
        open_orders: int,
        now: datetime,
    ) -> Verdict:
        self._roll(now)
        limits = self.limits
        if self._state["kill"]:
            return Verdict(False, f"kill switch: {self._state['kill_reason']}")
        if limits.live and not arming.is_armed(self.state_dir, self.venue, now=now):
            return Verdict(False, f"{self.venue} is not armed")
        estimate = order.estimated_notional(price)
        if estimate <= 0:
            return Verdict(False, "no price to size the order")
        if estimate > limits.max_order_usd:
            return Verdict(False, f"order ${estimate:.2f} is over the ${limits.max_order_usd} cap")
        spent = Decimal(self._state["notional"])
        if spent + estimate > limits.max_daily_notional_usd:
            return Verdict(False, f"would pass the ${limits.max_daily_notional_usd} daily limit")
        if Decimal(self._state["realized_pnl"]) <= -limits.max_daily_loss_usd:
            return Verdict(False, f"daily loss limit of ${limits.max_daily_loss_usd} reached")
        if open_orders >= limits.max_open_orders:
            return Verdict(False, f"{open_orders} orders already open")
        if (
            limits.pdt_applies
            and "/" not in order.symbol
            and order.side == "sell"
            and self._state["bought"].get(order.symbol) == self._state["day"]
            and account.equity < PDT_EQUITY
            and self.day_trades_in_window(now) >= PDT_MAX_DAY_TRADES
        ):
            return Verdict(False, "would be a 4th day trade in 5 trading days under $25,000")
        return Verdict(True, "ok")

    def record_fill(self, order: VenueOrder, *, notional: Decimal, now: datetime) -> None:
        self._roll(now)
        self._state["notional"] = str(Decimal(self._state["notional"]) + notional)
        day = self._state["day"]
        if order.side == "buy":
            self._state["bought"][order.symbol] = day
        elif self._state["bought"].get(order.symbol) == day:
            self._state["day_trades"].append([day, order.symbol])
        self._save()

    def record_pnl(self, amount: Decimal, *, now: datetime) -> None:
        self._roll(now)
        self._state["realized_pnl"] = str(Decimal(self._state["realized_pnl"]) + amount)
        self._save()

    def trip(self, reason: str) -> None:
        self._state.update(kill=True, kill_reason=reason)
        self._save()

    def reset(self) -> None:
        self._state.update(kill=False, kill_reason="")
        self._save()
