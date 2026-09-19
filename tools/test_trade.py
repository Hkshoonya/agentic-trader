"""Buy something and sell it again, for real, to prove the order path works.

The console can say "accepted" all day while nothing reaches Robinhood: the
evidence gate, the risk guard and the broker's own review are all *simulations*
until an order is placed. This tool closes that gap on purpose — it buys a small
amount of a symbol the book already trades, waits for the fill, sells the exact
filled quantity, and reports what the round trip actually cost.

It is the operator's tool, run by hand:

    python tools/test_trade.py --config config/agentic.toml --symbol XLM-USD
    python tools/test_trade.py --config config/agentic.toml --notional 5 --confirm

Without ``--confirm`` it does the read-only half only: quote, build, and ask the
broker to *review* the order, which is the same simulation the live loop runs
before it places anything. Nothing reaches the market until the flag is given,
and every step is journaled so the console shows the round trip next to the
agent's own decisions.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentic_trading.cli import build_broker  # noqa: E402
from agentic_trading.config import load_config  # noqa: E402
from agentic_trading import execution  # noqa: E402
from agentic_trading.history_sync import bar_stem  # noqa: E402
from agentic_trading.journal import DecisionJournal  # noqa: E402
from agentic_trading.marketdata import normalize_quotes_payload  # noqa: E402
from agentic_trading.orders import is_crypto_symbol  # noqa: E402
from agentic_trading.runtime import build_order_request  # noqa: E402
from agentic_trading.types import OrderIntent, Side, new_decision_id  # noqa: E402

QUANTUM = Decimal("0.000001")


def quote_for(broker: Any, symbol: str) -> dict[str, Any]:
    """The live bid/ask, in the same shape the strategy sees."""
    payload = (
        broker.get_crypto_quotes([symbol])
        if is_crypto_symbol(symbol)
        else broker.get_quotes([symbol])
    )
    # The crypto namespace answers with the pair undashed (``XLMUSD``), the same
    # way CryptoQuoteFeed sees it, so compare on that spelling.
    wanted = [bar_stem(symbol)] if is_crypto_symbol(symbol) else [symbol]
    quotes = normalize_quotes_payload(
        payload, symbols=wanted, observed_at=datetime.now(timezone.utc)
    )
    if not quotes:
        raise SystemExit(f"no usable quote for {symbol}: {str(payload)[:200]}")
    return quotes[0]


def held_quantity(broker: Any, symbol: str) -> Decimal:
    snapshot = (
        broker.get_crypto_position_snapshot()
        if is_crypto_symbol(symbol)
        else broker.get_positions()
    )
    key = bar_stem(symbol)
    for held_symbol, quantity in snapshot.held.items():
        if bar_stem(str(held_symbol)) == key:
            return Decimal(str(quantity))
    return Decimal("0")


def alerts_of(review: Any) -> list[str]:
    """Robinhood's pre-trade warnings, flattened for a terminal."""
    payload = review.get("data") if isinstance(review, dict) else None
    if not isinstance(payload, dict):
        payload = review if isinstance(review, dict) else {}
    checks = payload.get("order_checks") or []
    out: list[str] = []
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        label = check.get("type") or check.get("name") or "alert"
        detail = check.get("detail") or check.get("message") or ""
        out.append(f"{label}: {detail}".strip(": "))
    return out


def placed_order(payload: Any) -> dict[str, Any]:
    """The order object inside whatever envelope the broker answered with.

    Crypto placement nests it under ``data.order``; the equity tools have
    answered flat in the past. Reading both keeps the journal from recording
    ``None`` as an order id, which is exactly the kind of gap that makes a real
    fill look like nothing happened.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("order"), dict):
        return data["order"]
    for key in ("order", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def executed_notional(
    broker: Any, order_id: Any, *, fallback: Decimal
) -> Decimal:
    """What the broker says it actually moved for one order, in dollars.

    ``total_executed_notional`` (fee included) is the cash figure; it is rounded
    by the venue, which is itself part of the cost being measured. Anything we
    cannot find falls back to the caller's own number rather than inventing one.
    """
    if not order_id:
        return fallback
    try:
        orders = broker.get_crypto_orders()
    except Exception:  # noqa: BLE001 — a cost we cannot read is not a failure
        return fallback
    for order in orders:
        if str(order.get("id")) != str(order_id):
            continue
        for key in ("total_executed_notional", "rounded_executed_notional_with_fee",
                    "rounded_executed_notional"):
            value = order.get(key)
            if value in (None, ""):
                continue
            try:
                return Decimal(str(value))
            except (ArithmeticError, ValueError):
                continue
    return fallback


def build(
    config: Any,
    broker: Any,
    *,
    symbol: str,
    side: Side,
    quantity: Optional[Decimal],
    dollars: Optional[Decimal],
    price: Decimal,
) -> Any:
    intent = OrderIntent(
        decision_id=new_decision_id(),
        symbol=symbol,
        side=side,
        quantity=quantity if quantity is not None else Decimal("1"),
        ref_price=price,
        reason="test_trade",
        created_at=datetime.now(timezone.utc),
        notional_usd=dollars,
    )
    account = (
        broker.resolve_rhs_account_number()
        if is_crypto_symbol(symbol)
        else broker.resolve_account_number()
    )
    return build_order_request(
        intent,
        account_number=account,
        config=config,
        session="regular",  # crypto ignores it; equities need it to place a buy
    )


def wait_for_position(
    broker: Any,
    symbol: str,
    *,
    at_least: Decimal,
    timeout: float,
    poll: float = 3.0,
) -> Decimal:
    """Poll until the held quantity reaches ``at_least`` (or the clock runs out)."""
    deadline = time.monotonic() + timeout
    seen = Decimal("0")
    while time.monotonic() < deadline:
        seen = held_quantity(broker, symbol)
        if seen >= at_least:
            return seen
        time.sleep(poll)
    return seen


def wait_for_flat(
    broker: Any,
    symbol: str,
    *,
    below: Decimal,
    timeout: float,
    poll: float = 3.0,
) -> Decimal:
    deadline = time.monotonic() + timeout
    seen = held_quantity(broker, symbol)
    while time.monotonic() < deadline:
        seen = held_quantity(broker, symbol)
        if seen <= below:
            return seen
        time.sleep(poll)
    return seen


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--symbol", default="XLM-USD")
    parser.add_argument(
        "--notional",
        default="5.00",
        help="dollars to buy (the sell then closes exactly what filled)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually place the orders; without it the broker only reviews them",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    broker, _tools = build_broker(config)
    journal = DecisionJournal(Path(config.journal_dir))
    symbol = args.symbol.upper()
    dollars = Decimal(str(args.notional))

    started = held_quantity(broker, symbol)
    quote = quote_for(broker, symbol)
    print(f"{symbol}: bid {quote['bid']} ask {quote['ask']} (held {started})")

    buy = build(
        config,
        broker,
        symbol=symbol,
        side=Side.BUY,
        quantity=None,
        dollars=dollars.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        price=Decimal(str(quote["ask"])),
    )
    print("buy request :", buy.to_mcp_args())
    review = broker.review_order(buy)
    for alert in alerts_of(review):
        print("  broker says:", alert)
    journal.append(
        {
            "event": "test_trade",
            "step": "buy_reviewed",
            "symbol": symbol,
            "dollar_amount": str(buy.dollar_amount),
            "alerts": alerts_of(review),
            "confirmed": bool(args.confirm),
        }
    )
    if not args.confirm:
        print("\nreview only — nothing was placed. Re-run with --confirm to trade.")
        return 0

    placed = broker.place_order(buy)
    order = placed_order(placed)
    order_id = order.get("id") or order.get("order_id")
    print(f"buy placed  : {order_id} state={order.get('state')}")
    journal.append(
        {
            "event": "test_trade",
            "step": "buy_placed",
            "symbol": symbol,
            "order_id": order_id,
            "order": placed,
        }
    )

    filled = wait_for_position(
        broker, symbol, at_least=started + QUANTUM, timeout=args.timeout
    )
    bought = filled - started
    print(f"filled      : held {filled} (+{bought})")
    if bought <= 0:
        journal.append(
            {
                "event": "test_trade",
                "step": "buy_unfilled",
                "symbol": symbol,
                "order_id": order_id,
            }
        )
        print("the buy did not fill; nothing to sell")
        return 1

    sell_quote = quote_for(broker, symbol)
    sell = build(
        config,
        broker,
        symbol=symbol,
        side=Side.SELL,
        quantity=bought,
        dollars=None,
        price=Decimal(str(sell_quote["bid"])),
    )
    print("sell request:", sell.to_mcp_args())
    sell_review = broker.review_order(sell)
    for alert in alerts_of(sell_review):
        print("  broker says:", alert)
    journal.append(
        {
            "event": "test_trade",
            "step": "sell_reviewed",
            "symbol": symbol,
            "quantity": str(bought),
            "alerts": alerts_of(sell_review),
        }
    )

    sold = broker.place_order(sell)
    sell_order = placed_order(sold)
    sell_id = sell_order.get("id") or sell_order.get("order_id")
    print(f"sell placed : {sell_id} state={sell_order.get('state')}")
    journal.append(
        {
            "event": "test_trade",
            "step": "sell_placed",
            "symbol": symbol,
            "order_id": sell_id,
            "quantity": str(bought),
            "order": sold,
        }
    )

    remaining = wait_for_flat(
        broker, symbol, below=started + QUANTUM, timeout=args.timeout
    )
    cost = Decimal(str(sell_quote["bid"])) - Decimal(str(quote["ask"]))
    spread_cost = cost * bought
    flat = remaining <= started + QUANTUM
    print(f"after sell  : held {remaining} → {'flat' if flat else 'STILL HELD'}")
    print(
        "round trip  : bought at "
        f"{quote['ask']}, sold at {sell_quote['bid']} → "
        f"${spread_cost:.4f} of spread on {bought} ("
        f"{abs(spread_cost) / dollars * 10_000:.1f} bps of the order)"
    )
    journal.append(
        {
            "event": "test_trade",
            "step": "round_trip_complete" if flat else "exit_incomplete",
            "symbol": symbol,
            "bought": str(bought),
            "remaining": str(remaining),
            "buy_price": str(quote["ask"]),
            "sell_price": str(sell_quote["bid"]),
            "spread_cost_usd": str(round(spread_cost, 6)),
            "buy_order_id": order_id,
            "sell_order_id": sell_id,
        }
    )
    # Price the trip from the broker's own cash figures, not from the quote: the
    # executed notionals are the only numbers that include what the venue kept.
    buy_notional = executed_notional(broker, order_id, fallback=dollars)
    sell_notional = executed_notional(broker, sell_id, fallback=Decimal("0"))
    if flat and buy_notional and sell_notional:
        report = execution.record_round_trip(
            config.state_dir,
            symbol=symbol,
            buy_notional=float(buy_notional),
            sell_notional=float(sell_notional),
            quantity=float(bought),
            buy_order_id=str(order_id or ""),
            sell_order_id=str(sell_id or ""),
        )
        bps = report.measured_round_trip_bps
        journal.append(
            {
                "event": "cost_measured",
                "symbol": symbol,
                "buy_notional": str(buy_notional),
                "sell_notional": str(sell_notional),
                "cost_usd": str(Decimal(str(sell_notional)) - Decimal(str(buy_notional))),
                "round_trip_bps": bps,
                "per_side_bps": None if bps is None else round(bps / 2, 2),
                "note": (
                    "measured from the broker's executed cash, not from the "
                    "quoted spread"
                ),
            }
        )
        print(
            f"all-in cost : ${Decimal(str(sell_notional)) - Decimal(str(buy_notional)):.2f} "
            f"({bps:.0f} bps round trip, {bps / 2:.0f} bps a side)"
        )
    return 0 if flat else 1


if __name__ == "__main__":
    raise SystemExit(main())
