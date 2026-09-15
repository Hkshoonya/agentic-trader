#!/usr/bin/env python3
"""Deterministic, local paper agent. Reads quotes; never connects to a broker."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
import sys
import time

D = Decimal
BPS = D("10000")
QUANTITY_STEP = D("0.00000001")
MIN_SUPPORTED = D("0.00000001")
MAX_SUPPORTED = D("1000000000")


@dataclass(frozen=True)
class Config:
    symbol: str = "SPY"
    initial_cash: Decimal = D("50")
    max_spread_bps: Decimal = D("2")
    slippage_bps: Decimal = D("1")
    fee_per_order: Decimal = D("0")
    take_profit_bps: Decimal = D("10")
    stop_loss_bps: Decimal = D("10")
    max_hold_seconds: int = 60
    cooldown_seconds: int = 30
    max_trades: int = 3
    daily_loss_limit: Decimal = D("1")
    max_quote_age_seconds: int = 10
    max_signal_gap_seconds: int = 30

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol or len(self.symbol) > 20:
            raise ValueError("symbol must be a nonempty ticker of at most 20 characters")
        object.__setattr__(self, "symbol", self.symbol.upper())
        positive = ("initial_cash", "max_spread_bps", "take_profit_bps", "stop_loss_bps", "daily_loss_limit")
        for name in (*positive, "slippage_bps", "fee_per_order"):
            try:
                value = D(str(getattr(self, name)))
            except InvalidOperation as exc:
                raise ValueError(f"{name} must be a finite number") from exc
            if not value.is_finite() or value < 0 or (name in positive and value == 0):
                raise ValueError(f"{name} has an invalid value")
            if value > MAX_SUPPORTED or (0 < value < MIN_SUPPORTED):
                raise ValueError(f"{name} exceeds supported decimal magnitude")
            object.__setattr__(self, name, value)
        if self.slippage_bps >= BPS:
            raise ValueError("slippage_bps must be less than 10000")
        if self.fee_per_order * 2 >= self.initial_cash:
            raise ValueError("initial cash must cover both order fees")
        for name in ("max_hold_seconds", "cooldown_seconds", "max_trades", "max_quote_age_seconds", "max_signal_gap_seconds"):
            value = getattr(self, name)
            minimum = 0 if name == "cooldown_seconds" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def serializable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def run_experiment(quotes: list[dict], config: Config | None = None, *, as_of: datetime | None = None) -> dict:
    """Replay observations causally, using their original observation timestamps.

    A risk threshold is evaluated at the next valid observation, not guaranteed
    at the configured price. Missing/stale data never fabricates a fill.
    """
    config = config or Config()
    cash = config.initial_cash
    realized = D(0)
    position = None
    trades, proposals, events, curve = [], [], [], []
    mids = deque(maxlen=3)
    last_observed = last_quote = last_exit = None
    first_observed = None
    accepted = rejected = 0
    loss_halted = False
    peak = config.initial_cash
    max_drawdown = D(0)
    equity = cash
    sources = set()
    last_seen_observation = None

    def proposal(action, at, bid, ask, quantity, price, reason):
        proposals.append({
            "id": f"paper-{len(proposals) + 1:04d}", "at": at,
            "action": action, "symbol": config.symbol, "quantity": quantity,
            "reference_bid": bid, "reference_ask": ask, "modeled_price": price,
            "reason": reason, "expires_at": last_quote + timedelta(seconds=config.max_quote_age_seconds),
            "mode": "paper/manual review only",
        })

    for index, raw in enumerate(quotes, 1):
        try:
            if not isinstance(raw, dict):
                raise ValueError("quote must be an object")
            if raw.get("symbol") != config.symbol:
                raise ValueError("wrong_symbol")
            observed = parse_time(raw["observed_at"])
            quoted = parse_time(raw["quote_at"])
            if as_of is not None and observed > as_of:
                raise ValueError("observation_ahead_of_watch_clock")
            if last_seen_observation is not None and observed <= last_seen_observation:
                raise ValueError("out_of_order_observation")
            # Clock ordering follows all parseable observations, including stale ones.
            last_seen_observation = observed
            bid, ask = D(str(raw["bid"])), D(str(raw["ask"]))
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                raise ValueError("invalid_or_crossed_prices")
            if bid < MIN_SUPPORTED or ask > MAX_SUPPORTED:
                raise ValueError("unsupported_price_magnitude")
            age = (observed - quoted).total_seconds()
            if age < 0:
                raise ValueError("future_quote")
            if age > config.max_quote_age_seconds:
                raise ValueError("stale_quote")
            if last_quote is not None and quoted <= last_quote:
                raise ValueError("duplicate_or_out_of_order_quote")
        except (ValueError, KeyError, TypeError, InvalidOperation, OverflowError) as exc:
            rejected += 1
            mids.clear()
            events.append({"type": "rejected_quote", "record": index, "reason": str(exc)})
            continue

        if last_observed is not None and (observed - last_observed).total_seconds() > config.max_signal_gap_seconds:
            mids.clear()
            events.append({"type": "signal_reset", "at": observed, "reason": "observation_gap"})
        first_observed = first_observed or observed
        last_observed, last_quote = observed, quoted
        accepted += 1
        sources.add(str(raw.get("source", "unspecified")))
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * BPS
        modeled_buy = ask * (1 + config.slippage_bps / BPS)
        modeled_sell = bid * (1 - config.slippage_bps / BPS)
        exited = False

        if position is not None:
            quantity = position["quantity"]
            liquidation = quantity * modeled_sell - config.fee_per_order
            position.update({"liquidation_value": liquidation, "mark_at": observed,
                             "age_seconds": (observed - position["entry_at"]).total_seconds()})
            equity = cash + liquidation
            pnl = liquidation - position["entry_cost"]
            return_bps = pnl / position["entry_cost"] * BPS
            reason = None
            if config.initial_cash - equity >= config.daily_loss_limit:
                reason = "loss_limit"
                loss_halted = True
            elif return_bps <= -config.stop_loss_bps:
                reason = "stop_loss"
            elif return_bps >= config.take_profit_bps:
                reason = "take_profit"
            elif position["age_seconds"] >= config.max_hold_seconds:
                reason = "timeout"
            if reason:
                proposal("SELL", observed, bid, ask, quantity, modeled_sell, reason)
                trades.append({
                    "entry_at": position["entry_at"], "exit_at": observed,
                    "quantity": quantity, "entry_price": position["entry_price"],
                    "exit_price": modeled_sell, "entry_cost": position["entry_cost"],
                    "exit_proceeds": liquidation, "entry_fee": config.fee_per_order,
                    "exit_fee": config.fee_per_order, "net_pnl": pnl,
                    "exit_reason": reason, "hold_seconds": position["age_seconds"],
                })
                cash += liquidation
                realized += pnl
                events.append({"type": "paper_exit", "at": observed, "reason": reason, "net_pnl": pnl})
                position = None
                last_exit = observed
                exited = True
                mids.clear()

        can_enter = (position is None and not exited and not loss_halted and len(trades) < config.max_trades)
        if can_enter and last_exit is not None:
            can_enter = (observed - last_exit).total_seconds() >= config.cooldown_seconds
        if can_enter and spread_bps <= config.max_spread_bps:
            mids.append(mid)
            if len(mids) == 3 and mids[0] < mids[1] < mids[2]:
                # Preserve an exit-fee reserve; never borrow or spend above $50.
                budget = min(cash, config.initial_cash)
                spendable = budget - config.fee_per_order * 2
                quantity = (spendable / modeled_buy).quantize(QUANTITY_STEP, rounding=ROUND_DOWN) if spendable > 0 else D(0)
                if quantity > 0:
                    cost = quantity * modeled_buy + config.fee_per_order
                    cash -= cost
                    position = {
                        "quantity": quantity, "entry_at": observed, "entry_price": modeled_buy,
                        "entry_cost": cost, "liquidation_value": quantity * modeled_sell - config.fee_per_order,
                        "mark_at": observed, "age_seconds": 0,
                    }
                    proposal("BUY", observed, bid, ask, quantity, modeled_buy, "three_rising_midquotes")
                    events.append({"type": "paper_entry", "at": observed, "quantity": quantity,
                                   "reason": "three_rising_midquotes", "spread_bps": spread_bps})
                    mids.clear()
        else:
            mids.clear()

        equity = cash + (position["liquidation_value"] if position else D(0))
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        curve.append({"at": observed, "equity": equity, "cash": cash})

    if loss_halted:
        status = "loss_limit_reached"
    elif len(trades) >= config.max_trades:
        status = "trade_limit_reached"
    elif position:
        status = "open_position_at_end"
    elif accepted:
        status = "flat_at_end"
    else:
        status = "no_valid_quotes"
    if sources == {"Robinhood MCP"}:
        data_source = "recorded_live_quotes"
    elif not sources or sources == {"unspecified"}:
        data_source = "synthetic_or_unspecified"
    else:
        data_source = "quote_file"
    return serializable({
        "status": status, "symbol": config.symbol, "config": asdict(config),
        "starting_cash": config.initial_cash, "cash": cash, "ending_equity": equity,
        "realized_pnl": realized, "total_pnl": equity - config.initial_cash,
        "open_position": position, "trades": trades, "proposals": proposals,
        "events": events, "equity_curve": curve, "accepted_quotes": accepted,
        "rejected_quotes": rejected, "first_observed_at": first_observed,
        "last_observed_at": last_observed, "last_quote_at": last_quote,
        "max_drawdown": max_drawdown, "cash_benchmark_equity": config.initial_cash,
        "data_source": data_source, "run_mode": "replay", "feed_status": "replay",
        "assumptions": [
            "All orders and profits/losses are simulated; this program has no broker connection or credentials.",
            "The momentum rule is an educational hypothesis, not a validated source of profit.",
            "Buy at the recorded ask plus configured slippage; sell at the bid minus slippage. Fills are hypothetical.",
            f"Slippage is {config.slippage_bps} basis points per side; fixed fee is ${config.fee_per_order} per order. These are model inputs, not verified account charges.",
            "Fractional shares are assumed available with eight decimal places; overnight eligibility, fills and broker rounding are unverified.",
            "Stops, profit targets and timeouts act only at the next fresh observation; gaps can exceed the loss threshold or holding limit.",
            "Unrealized positions remain open at end of data and use the last valid liquidation mark; no end-of-data sale is invented.",
            "The loss limit applies to this entire experiment and does not reset at midnight. There is no leverage or short selling.",
            "Quote polling is sparse and cannot establish second-by-second execution or a trading advantage. Taxes and interest are excluded.",
            "Source labels are supplied by the quote collector and are not independently authenticated by this program.",
        ],
    })


def read_quotes(path: Path, allow_partial: bool = False) -> list[dict]:
    """Keep bad lines visible to validation; tolerate only an in-progress tail."""
    data = path.read_text(encoding="utf-8")
    lines = data.splitlines(keepends=True)
    quotes = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if allow_partial and number == len(lines) and not line.endswith("\n"):
            continue
        try:
            quotes.append(json.loads(line))
        except json.JSONDecodeError:
            quotes.append({"_invalid_json_line": number})
    return quotes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotes", type=Path, required=True, help="JSONL quote file; may be appended by a read-only collector")
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--config", type=Path, help="Optional JSON overrides for Config")
    parser.add_argument("--watch", action="store_true", help="Recompute reports as the quote file grows")
    parser.add_argument("--duration-seconds", type=int, default=60, help="Bounded watch duration, default 60 seconds")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.duration_seconds < 1 or not 0.1 <= args.poll_seconds <= 30:
        parser.error("duration must be positive and poll interval between 0.1 and 30 seconds")
    try:
        config = Config(**json.loads(args.config.read_text())) if args.config else Config()
        from reporting import write_reports
        deadline = time.monotonic() + args.duration_seconds
        previous_fingerprint = None
        while True:
            observations = read_quotes(args.quotes, allow_partial=args.watch)
            result = run_experiment(observations, config, as_of=datetime.now(timezone.utc) if args.watch else None)
            result["report_generated_at"] = iso(datetime.now(timezone.utc))
            if args.watch:
                result["run_mode"] = "watch"
                if result["last_quote_at"]:
                    age = max(0, (datetime.now(timezone.utc) - parse_time(result["last_quote_at"])).total_seconds())
                    result["quote_feed_age_seconds"] = round(age, 3)
                    result["feed_status"] = "fresh" if age <= config.max_quote_age_seconds else "stale"
                else:
                    result["feed_status"] = "waiting"
            # A watch replays the immutable history, so restarts don't double trade.
            write_reports(result, args.output)
            fingerprint = (len(observations), result["status"], result["feed_status"])
            if fingerprint != previous_fingerprint:
                print(f"PAPER | {result['symbol']} | {result['accepted_quotes']} valid / {result['rejected_quotes']} rejected | "
                      f"{len(result['trades'])} closed trades | equity ${D(result['ending_equity']):.6f} | "
                      f"{result['status']} | feed {result['feed_status']}", flush=True)
                previous_fingerprint = fingerprint
            if not args.watch or time.monotonic() >= deadline:
                break
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
        print(f"Reports: {args.output.resolve()}")
        return 0
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped. Any paper position is preserved in the last report.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
