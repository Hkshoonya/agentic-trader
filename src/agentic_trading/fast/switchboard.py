"""The switchboard: for each coin, read the market, pick a playbook, act on each price.

A paper desk member that runs inside the venues service:
* It reads each coin's market as each bar closes (regime.py). On every price, it
  lets that regime's playbooks propose an entry.
* It keeps an open trade with the playbook that opened it until that
  playbook's own exit fires.
* Fills come from PaperFiller: the real ask or bid, 250 ms after the
  decision, plus the Alpaca fee. A CoinbaseMirror copies each fill at
  Coinbase prices for comparison.

Every rule here is about not paying fees for nothing. It stands aside unless
the market is trending or squeezed, and enters only when the expected move is
several times the round-trip cost. It enters on no price that follows 30 s of
silence, stops entering for the day after a 3% loss, and cools down after
each exit.

The time base is each tick's ``received_at``, so a replay of recorded ticks
makes the same decisions the live run made.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.bars import Bar, MinuteBars, bar_start
from agentic_trading.fast.fills import Fill, Order, PaperFiller, usable_quote
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.playbooks import ENTRIES, RECOVERED, Plan, Trade, exit_reason
from agentic_trading.fast.regime import STAND_ASIDE, WARMING, history_bars, read_regime
from agentic_trading.fast.settings import FastConfig
from agentic_trading.venues.model import Tick

STALE_AFTER = timedelta(seconds=30)
RECOVER_BAND = 0.02
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Event:
    kind: str
    symbol: str
    at: datetime
    text: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "event": self.kind, "symbol": self.symbol,
                "text": self.text, **self.data}


def tick_price(tick: Tick) -> Optional[Decimal]:
    """The mid of a usable quote, else a positive trade price, else None."""
    if usable_quote(tick):
        return (tick.bid + tick.ask) / 2
    if tick.last is not None and tick.last > 0:
        return tick.last
    return None


def _book_view(book: MemberBook) -> dict[str, Any]:
    start, equity = book.starting_equity, book.equity
    return {
        "equity": str(equity.quantize(CENT)),
        "return_pct": round(float(equity / start - 1) * 100, 3) if start > 0 else None,
        "entries": book.entries,
        "exits": book.exits,
    }


class Switchboard:
    def __init__(
        self,
        config: FastConfig,
        book: MemberBook,
        *,
        mirror: Optional[CoinbaseMirror] = None,
        regime_reader: Callable[[Sequence[Bar]], str] = read_regime,
    ) -> None:
        self.config = config
        self.book = book
        self.mirror = mirror
        self.read_regime = regime_reader
        self.fee = float(config.alpaca_fee)
        self.filler = PaperFiller(book, config.alpaca_fee, delay=timedelta(milliseconds=config.fill_delay_ms))
        self.bars = MinuteBars(keep=history_bars(config.bar_minutes), minutes=config.bar_minutes)
        self.regimes: dict[str, str] = {symbol: WARMING for symbol in config.symbols}
        self.trades: dict[str, Trade] = {}
        self.entering: dict[str, Plan] = {}
        self.exiting: dict[str, str] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.quotes: dict[str, Tick] = {}
        self.last_seen: dict[str, datetime] = {}
        self.skipped: dict[str, int] = {}
        self._skip_bar: dict[str, datetime] = {}
        self.skipped_total = 0
        self.day = ""
        self.day_start_equity = book.equity
        self.halted_day = ""

    # -- the tick path -----------------------------------------------------

    def on_tick(self, tick: Tick) -> list[Event]:
        if tick.venue == "coinbase":
            if self.mirror is not None:
                self.mirror.on_tick(tick)
            return []
        if tick.venue != "alpaca" or tick.symbol not in self.config.symbols:
            return []
        price = tick_price(tick)
        if price is None:
            return []
        symbol, now = tick.symbol, tick.received_at
        previous = self.last_seen.get(symbol)
        self.last_seen[symbol] = now
        if usable_quote(tick):
            self.quotes[symbol] = tick
        events = self._roll_day(now)
        self.book.mark({symbol: price}, now)
        if self.mirror is not None:
            self.mirror.mark(symbol, price, now)
        for fill in self.filler.on_tick(tick):
            events.extend(self._on_fill(fill))
        if self.bars.add(symbol, float(price), tick.exchange_at) is not None:
            events.extend(self._on_bar(symbol, now))
        value = float(price)
        trade = self.trades.get(symbol)
        if trade is not None:
            if symbol not in self.exiting:
                reason = exit_reason(trade, self.bars.closed(symbol), value)
                if reason:
                    self.exiting[symbol] = reason
                    self.filler.submit(Order(symbol, "sell", now, quantity=trade.quantity))
        elif symbol not in self.entering:
            if previous is not None and now - previous <= STALE_AFTER:
                events.extend(self._consider_entry(symbol, value, now))
        return events

    def _roll_day(self, now: datetime) -> list[Event]:
        day = now.astimezone(timezone.utc).date().isoformat()
        if day != self.day:
            self.day = day
            self.day_start_equity = self.book.equity
        start = self.day_start_equity
        if self.halted_day != day and start > 0 and self.book.equity <= start * (1 - self.config.daily_loss_stop):
            self.halted_day = day
            drop = float(1 - self.book.equity / start) * 100
            return [Event("fast_halted", "*", now,
                          f"The switchboard is down {drop:.1f}% today: no new entries until tomorrow (UTC)",
                          {"drop_pct": round(drop, 2)})]
        return []

    def _on_bar(self, symbol: str, now: datetime) -> list[Event]:
        events: list[Event] = []
        regime = self.read_regime(self.bars.closed(symbol))
        was = self.regimes.get(symbol, WARMING)
        if regime != was:
            self.regimes[symbol] = regime
            events.append(Event("fast_regime", symbol, now, f"{symbol} is now {regime} (was {was})",
                                {"regime": regime, "was": was}))
        count = self.skipped.pop(symbol, 0)
        if count:
            noun = "setup" if count == 1 else "setups"
            events.append(Event(
                "fast_skipped", symbol, now,
                f"{symbol}: {count} {noun} skipped by the cost gate "
                f"(expected move under {self.config.cost_gate_multiple}x the round-trip cost)",
                {"count": count}))
        return events

    def _consider_entry(self, symbol: str, price: float, now: datetime) -> list[Event]:
        regime = self.regimes.get(symbol, WARMING)
        if regime in STAND_ASIDE or self.halted_day == self.day:
            return []
        if now < self.cooldown_until.get(symbol, now):
            return []
        if len(self.trades) + len(self.entering) >= self.config.max_positions:
            return []
        quote = self.quotes.get(symbol)
        if quote is None:
            return []
        bars = self.bars.closed(symbol)
        gate = float(self.config.cost_gate_multiple) * (2 * self.fee * price + float(quote.ask - quote.bid))
        plan, proposed = None, False
        for _, entry in ENTRIES.get(regime, ()):  # each playbook's plan is gated on its own merits
            candidate = entry(bars, price)
            if candidate is None:
                continue
            proposed = True
            if candidate.expected_move >= gate:
                plan = candidate
                break
        if plan is None:
            if not proposed:
                return []
            bar = bar_start(now, self.config.bar_minutes)
            if self._skip_bar.get(symbol) != bar:  # one count per coin per bar
                self._skip_bar[symbol] = bar
                self.skipped[symbol] = self.skipped.get(symbol, 0) + 1
                self.skipped_total += 1
            return []
        notional = min(self.book.equity / self.config.max_positions, self.book.cash)
        self.entering[symbol] = plan
        self.filler.submit(Order(symbol, "buy", now, notional=notional))
        return []

    def _on_fill(self, fill: Fill) -> list[Event]:
        symbol, now = fill.symbol, fill.at
        if fill.side == "buy":
            plan = self.entering.pop(symbol, None)
            if plan is None:
                return []
            if fill.quantity <= 0:
                return [Event("fast_unfilled", symbol, now,
                              f"{symbol}: the {plan.playbook} entry was too small for the book to fill",
                              {"playbook": plan.playbook})]
            entry = float(fill.price)
            self.trades[symbol] = Trade(symbol, plan.playbook, entry, plan.stop, plan.target,
                                        fill.quantity, now, entry)
            if self.mirror is not None:
                self.mirror.copy(fill, now)
            quote = self.quotes.get(symbol)
            spread = float((quote.ask - quote.bid) / quote.ask) if quote is not None else 0.0
            cost_pct = (2 * self.fee + spread) * 100
            move_pct = plan.expected_move / entry * 100
            regime = self.regimes.get(symbol, WARMING)
            text = (f"Bought ${float(fill.price * fill.quantity):,.2f} of {symbol} at {fill.price} "
                    f"({plan.playbook}, {regime}): stop {plan.stop:,.2f}, expecting {move_pct:+.2f}% "
                    f"against {cost_pct:.2f}% round-trip cost")
            return [Event("fast_entry", symbol, now, text, {
                "playbook": plan.playbook, "regime": regime, "price": str(fill.price),
                "quantity": str(fill.quantity), "stop": round(plan.stop, 6),
                "target": None if plan.target is None else round(plan.target, 6),
                "expected_move_pct": round(move_pct, 3), "cost_pct": round(cost_pct, 3),
            })]
        reason = self.exiting.pop(symbol, "exit")
        trade = self.trades.pop(symbol, None)
        if trade is None:
            return []
        self.cooldown_until[symbol] = now + timedelta(minutes=self.config.cooldown_minutes)
        if fill.quantity <= 0:
            return [Event("fast_unfilled", symbol, now, f"{symbol}: the exit found nothing to sell",
                          {"playbook": trade.playbook})]
        if self.mirror is not None:
            self.mirror.copy(fill, now)
        net = float(fill.price) * (1 - self.fee) / (trade.entry * (1 + self.fee)) - 1
        net_pct = round(net * 100, 3)
        text = f"Sold {symbol} at {fill.price} ({reason}, {trade.playbook}): {net_pct:+.2f}% after fees"
        return [Event("fast_exit", symbol, now, text, {
            "playbook": trade.playbook, "reason": reason, "price": str(fill.price),
            "quantity": str(fill.quantity), "net_pct": net_pct,
        })]

    # -- persistence ----------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "trades": [trade.to_dict() for trade in self.trades.values()],
            "cooldown_until": {s: at.isoformat() for s, at in self.cooldown_until.items()},
            "day": self.day,
            "day_start_equity": str(self.day_start_equity),
            "halted_day": self.halted_day,
            "skipped_total": self.skipped_total,
        }

    def restore(self, state: dict[str, Any], now: datetime) -> list[Event]:
        """Pick up where a saved run left off. The book is the truth for holdings.

        A saved trade with no holding behind it is dropped. A holding with no
        saved trade (a crash between the book save and the engine save) is
        managed as ``recovered`` with a 2% stop and target, so it is never
        left unmanaged.
        """
        if not isinstance(state, dict):
            raise ValueError("the engine state is not an object")
        if state.get("version", 1) != 1:
            raise ValueError(f"unknown engine state version {state.get('version')!r}")
        raw_trades, raw_cooldowns = state.get("trades") or [], state.get("cooldown_until") or {}
        if not isinstance(raw_trades, list) or not isinstance(raw_cooldowns, dict):
            raise ValueError("the engine state has the wrong shape")
        cooldowns: dict[str, datetime] = {}
        for symbol, raw in raw_cooldowns.items():
            at = datetime.fromisoformat(str(raw))
            if at.tzinfo is None:
                raise ValueError("a cooldown time has no timezone")
            cooldowns[str(symbol)] = at
        try:
            day_start = Decimal(str(state.get("day_start_equity", self.book.equity)))
        except ArithmeticError:
            raise ValueError("day_start_equity is not a number") from None
        if not day_start.is_finite() or day_start < 0:
            raise ValueError("day_start_equity is not a usable number")
        skipped_total = int(state.get("skipped_total") or 0)
        trades: dict[str, Trade] = {}
        for raw in raw_trades:
            try:
                trade = Trade.from_dict(raw)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            numbers = [trade.entry, trade.stop, trade.high] + ([] if trade.target is None else [trade.target])
            if not all(math.isfinite(n) and n > 0 for n in numbers) or trade.opened_at.tzinfo is None:
                continue  # unusable: its holding (if any) is recovered below
            held = self.book.positions.get(trade.symbol.upper(), Decimal("0"))
            if trade.symbol in self.config.symbols and held > 0:
                trade.quantity = held
                trades[trade.symbol] = trade
        events: list[Event] = []
        for symbol, held in self.book.positions.items():
            price = float(self.book.prices.get(symbol) or 0)
            if symbol in trades or symbol not in self.config.symbols or held <= 0 or price <= 0:
                continue
            trades[symbol] = Trade(symbol, RECOVERED, price, price * (1 - RECOVER_BAND),
                                   price * (1 + RECOVER_BAND), held, now, price)
            events.append(Event("fast_recovered", symbol, now,
                                f"{symbol}: a holding with no saved trade is now managed with a "
                                f"{RECOVER_BAND:.0%} stop and target", {"quantity": str(held)}))
        self.trades = trades
        self.cooldown_until.update(cooldowns)
        self.day = str(state.get("day") or "")
        self.day_start_equity = day_start
        self.halted_day = str(state.get("halted_day") or "")
        self.skipped_total = skipped_total
        return events

    # -- reporting -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        coins = []
        for symbol in self.config.symbols:
            regime = self.regimes.get(symbol, WARMING)
            trade = self.trades.get(symbol)
            row: dict[str, Any] = {"symbol": symbol, "regime": regime,
                                   "standing_aside": trade is None and regime in STAND_ASIDE, "trade": None}
            if trade is not None:
                price = self.book.prices.get(symbol.upper())
                row["trade"] = {
                    "playbook": trade.playbook, "entry": trade.entry, "stop": round(trade.stop, 6),
                    "target": None if trade.target is None else round(trade.target, 6),
                    "pnl_pct": None if price is None else round((float(price) / trade.entry - 1) * 100, 3),
                    "opened_at": trade.opened_at.isoformat(),
                }
            coins.append(row)
        mirror = None
        if self.mirror is not None:
            mirror = {**_book_view(self.mirror.book), "unpriced": self.mirror.unpriced}
        return {
            "coins": coins,
            "book": _book_view(self.book),
            "mirror": mirror,
            "skipped": self.skipped_total,
            "halted": bool(self.day) and self.halted_day == self.day,
        }
