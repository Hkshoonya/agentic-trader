"""What did execution actually cost, versus what the backtest assumes?

Every piece of evidence this bot produces runs through a cost model that assumes
2 bps of spread and 1 bp of slippage per side. That number decides whether a
strategy looks profitable or not, and it has never been checked against a real
fill — the bot has not placed one.

This joins the broker's own trade history to the prices the decisions were taken
at, and reports the difference. When there are enough fills it becomes the cost
model the evidence is graded with, so the strategy is judged on the costs it
actually pays rather than the ones we assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading import jsonio

FILE_NAME = "execution_costs.json"
# Fills further than this from a decision are not attributable to it.
ATTRIBUTION_WINDOW_MINUTES = 30
# Below this many fills the measurement is reported but not used.
MIN_FILLS_FOR_MODEL = 5
# Sanity clamp: a measurement outside this is a data problem, not a cost.
MAX_TRUSTED_PER_SIDE_BPS = 100.0


@dataclass
class CostReport:
    fills: int = 0
    measured_per_side_bps: Optional[float] = None
    assumed_per_side_bps: Optional[float] = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    # Completed buy-then-sell round trips, measured from the broker's own
    # executed notionals. Slippage against a decision price cannot see the
    # spread the broker charges *inside* the fill, and a real round trip is the
    # only thing that prices the whole trip from cash out to cash back in.
    round_trips: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        return (
            self.fills >= MIN_FILLS_FOR_MODEL
            and self.measured_per_side_bps is not None
            and abs(self.measured_per_side_bps) <= MAX_TRUSTED_PER_SIDE_BPS
        )

    @property
    def ratio(self) -> Optional[float]:
        """Measured cost against the model's assumption. ``None`` without both.

        Prefers the all-in per-side cost from a real round trip over slippage
        against a decision price: slippage cannot see what the venue keeps, and
        on a small account that is the number that decides whether an edge
        survives at all.
        """
        measured = self.per_side_cost_bps
        if not self.assumed_per_side_bps or measured is None:
            return None
        return measured / self.assumed_per_side_bps

    @property
    def measured_round_trip_bps(self) -> Optional[float]:
        """Median all-in round-trip cost across completed test trades."""
        values = [
            float(row["round_trip_bps"])
            for row in self.round_trips
            if isinstance(row.get("round_trip_bps"), (int, float))
        ]
        if not values:
            return None
        values.sort()
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2

    @property
    def per_side_cost_bps(self) -> Optional[float]:
        """What one side really costs: measured round trip first, then slippage."""
        trip = self.measured_round_trip_bps
        if trip is not None:
            return trip / 2
        return self.measured_per_side_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "fills": self.fills,
            "measured_per_side_bps": (
                round(self.measured_per_side_bps, 3)
                if self.measured_per_side_bps is not None
                else None
            ),
            "assumed_per_side_bps": self.assumed_per_side_bps,
            "ratio": round(self.ratio, 3) if self.ratio is not None else None,
            "usable": self.usable,
            "round_trips": self.round_trips[-10:],
            "measured_round_trip_bps": (
                round(self.measured_round_trip_bps, 3)
                if self.measured_round_trip_bps is not None
                else None
            ),
            "per_side_cost_bps": (
                round(self.per_side_cost_bps, 3)
                if self.per_side_cost_bps is not None
                else None
            ),
            "updated_at": self.updated_at,
            "note": self.note,
            "samples": self.samples[-20:],
        }


def record_round_trip(
    state_dir: Path | str,
    *,
    symbol: str,
    buy_notional: float,
    sell_notional: float,
    quantity: float = 0.0,
    buy_order_id: str = "",
    sell_order_id: str = "",
    at: Optional[str] = None,
) -> CostReport:
    """Record one completed buy-then-sell trip, priced by the broker's cash.

    ``buy_notional`` and ``sell_notional`` are what the broker says it moved —
    not what the quote suggested. On a $50 account the difference is the whole
    story: a $5 XLM round trip cost $0.10 (200 bps) while the quoted spread was
    8 bps, because the effective price sits inside the fill, in the rounding,
    and in whatever the venue keeps.
    """
    report = load_report(state_dir) or CostReport()
    if buy_notional <= 0:
        return report
    # Positive means "this cost money", the same sign convention ``measure``
    # uses for slippage, so a reader never has to remember which way is worse.
    cost = buy_notional - sell_notional
    report.round_trips.append(
        {
            "symbol": symbol,
            "quantity": round(quantity, 8),
            "buy_notional": round(buy_notional, 4),
            "sell_notional": round(sell_notional, 4),
            "cost_usd": round(cost, 4),
            "round_trip_bps": round(cost / buy_notional * 10_000, 2),
            "buy_order_id": buy_order_id,
            "sell_order_id": sell_order_id,
            "at": at or datetime.now(timezone.utc).isoformat(),
        }
    )
    report.updated_at = datetime.now(timezone.utc).isoformat()
    report.note = (
        f"{len(report.round_trips)} measured round trip(s); "
        f"median {report.measured_round_trip_bps:.1f} bps all-in"
    )
    save_report(state_dir, report)
    return report


def measured_cost_usd(state_dir: Path | str) -> Optional[float]:
    """Median dollars a completed round trip cost, or ``None`` if never measured."""
    report = load_report(state_dir)
    if report is None or not report.round_trips:
        return None
    values = [
        float(row["cost_usd"])
        for row in report.round_trips
        if isinstance(row.get("cost_usd"), (int, float))
    ]
    if not values:
        return None
    values.sort()
    middle = len(values) // 2
    median = (
        values[middle]
        if len(values) % 2
        else (values[middle - 1] + values[middle]) / 2
    )
    return max(0.0, median)


def required_notional_for_cost(
    state_dir: Path | str, *, max_share: float
) -> Optional[float]:
    """The smallest order whose measured round-trip cost stays inside ``max_share``.

    A round trip on this account cost $0.10 whether the order was $5 or $50 —
    the venue's part is closer to a fixed charge than a percentage — so a $1
    order pays 10% to enter and leave, and no edge survives that. The rule
    deliberately refuses rather than scaling the order up: the strategy asked
    for a size, and answering with five times as much is a different decision.
    """
    cost = measured_cost_usd(state_dir)
    if cost is None or max_share <= 0:
        return None
    return cost / max_share


def parse_trade_history(payload: Any) -> list[dict[str, Any]]:
    """Rows from ``get_pnl_trade_history``: timestamp, symbol, side, price, qty."""
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    rows = data.get("trades") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    trades: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or price <= 0:
            continue
        trades.append(
            {
                "timestamp": str(row.get("timestamp") or ""),
                "symbol": symbol,
                "side": str(row.get("side") or "").lower(),
                "quantity": str(row.get("quantity") or ""),
                "price": price,
            }
        )
    return trades


def decision_prices(records: Iterable[dict[str, Any]]) -> dict[str, list[dict]]:
    """Reference prices per symbol, taken from the journal's own decisions."""
    by_symbol: dict[str, list[dict]] = {}
    for record in records:
        if record.get("event") != "accepted":
            continue
        intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
        symbol = str(record.get("symbol") or intent.get("symbol") or "").upper()
        price = record.get("ref_price") or intent.get("ref_price")
        at = intent.get("created_at") or record.get("at")
        if not symbol or price in (None, "") or not at:
            continue
        try:
            reference = float(price)
            when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        by_symbol.setdefault(symbol, []).append(
            {"at": when, "price": reference, "side": str(record.get("side") or "")}
        )
    return by_symbol


def _normalise_symbol(symbol: str) -> str:
    """Broker fills come back as BTC or BTCUSD; decisions as BTC-USD."""
    text = symbol.upper().replace("-", "")
    return text


def measure(
    trades: Iterable[dict[str, Any]],
    decisions: dict[str, list[dict]],
    *,
    assumed_per_side_bps: Optional[float] = None,
    window_minutes: int = ATTRIBUTION_WINDOW_MINUTES,
) -> CostReport:
    """Per-side slippage in bps between the decision price and the fill price."""
    report = CostReport(
        assumed_per_side_bps=assumed_per_side_bps,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    lookup = {
        _normalise_symbol(symbol): entries for symbol, entries in decisions.items()
    }
    slippages: list[float] = []
    window = timedelta(minutes=window_minutes)
    for trade in trades:
        entries = lookup.get(_normalise_symbol(str(trade.get("symbol") or "")))
        if not entries:
            continue
        try:
            when = datetime.fromisoformat(
                str(trade.get("timestamp") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        candidates = [
            entry for entry in entries if abs(entry["at"] - when) <= window
        ]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda entry: abs(entry["at"] - when))
        reference = nearest["price"]
        if reference <= 0:
            continue
        side = str(trade.get("side") or "").lower()
        # Buying above the reference and selling below it both cost money, so
        # the sign convention is "positive = worse than the decision price".
        signed = (trade["price"] - reference) / reference * 10_000
        slippage = -signed if side == "sell" else signed
        slippages.append(slippage)
        report.samples.append(
            {
                "symbol": trade["symbol"],
                "side": side,
                "at": trade["timestamp"],
                "decision_price": reference,
                "fill_price": trade["price"],
                "per_side_bps": round(slippage, 3),
            }
        )

    report.fills = len(slippages)
    if slippages:
        report.measured_per_side_bps = sum(slippages) / len(slippages)
    elif trades:
        report.note = "fills found but none matched a decision within the window"
    else:
        report.note = "no fills yet — nothing has traded"
    return report


def save_report(state_dir: Path | str, report: CostReport) -> Path:
    path = Path(state_dir) / FILE_NAME
    jsonio.write_text(path, jsonio.dumps(report.to_dict(), indent=2) + "\n")
    return path


def load_report(state_dir: Path | str) -> Optional[CostReport]:
    path = Path(state_dir) / FILE_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return CostReport(
        fills=int(raw.get("fills", 0)),
        measured_per_side_bps=(
            float(raw["measured_per_side_bps"])
            if raw.get("measured_per_side_bps") is not None
            else None
        ),
        samples=[
            row for row in (raw.get("samples") or []) if isinstance(row, dict)
        ],
        round_trips=[
            row for row in (raw.get("round_trips") or []) if isinstance(row, dict)
        ],
        assumed_per_side_bps=(
            float(raw["assumed_per_side_bps"])
            if raw.get("assumed_per_side_bps") is not None
            else None
        ),
        updated_at=str(raw.get("updated_at", "")),
        note=str(raw.get("note", "")),
    )


def cost_model_for(state_dir: Path | str, base: Any = None) -> Any:
    """The cost model the evidence should use: measured when trustworthy.

    Falls back to the default assumption, so a strategy is never graded on a
    single lucky (or broken) fill.
    """
    from agentic_trading.backtest import CostModel

    model = base or CostModel()
    report = load_report(state_dir)
    if report is None or not report.usable:
        return model
    measured = float(report.measured_per_side_bps)
    # Clamp loosely: whatever we measured, the model should not be able to claim
    # execution is free or absurd on the strength of a few fills.
    measured = max(0.0, min(MAX_TRUSTED_PER_SIDE_BPS, measured))
    # The measurement compares the fill against the price the decision was taken
    # at — which is already the executable side of the quote — so it *is* the
    # all-in per-side cost. Keep the spread term at zero rather than charging
    # half a spread a second time on top of a number that already contains it.
    kind = type(model.spread_bps)
    return CostModel(
        spread_bps=kind("0"),
        slippage_bps=kind(str(round(measured, 4))),
        fee_per_order=model.fee_per_order,
    )
