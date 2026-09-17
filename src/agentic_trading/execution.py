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
        if not self.assumed_per_side_bps or self.measured_per_side_bps is None:
            return None
        return self.measured_per_side_bps / self.assumed_per_side_bps

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
            "updated_at": self.updated_at,
            "note": self.note,
            "samples": self.samples[-20:],
        }


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
