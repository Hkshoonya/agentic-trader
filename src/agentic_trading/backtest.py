"""Cost-aware bar backtester for strategy genomes.

Execution conventions (deliberately conservative, and the reason results are
trustworthy enough to *refuse* promotion):

- signals are computed from bar closes up to and including bar ``i``
- entries happen at ``bar[i+1].open`` — never at a price the signal already saw
- if a bar's range touches both take-profit and stop, the **stop wins**
- costs are charged on both sides: half the configured spread plus slippage
- no shorting, no leverage, one position at a time
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from agentic_trading.history import Bar

BPS = Decimal("10000")


@dataclass(frozen=True)
class Genome:
    """Searchable strategy parameters."""

    lookback: int = 3
    entry_bps: int = 10
    tp_bps: int = 40
    sl_bps: int = 40
    max_hold_bars: int = 6
    vol_window: int = 10
    max_vol_bps: int = 400
    trend_window: int = 50
    use_trend_filter: bool = True
    position_pct: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "lookback": self.lookback,
            "entry_bps": self.entry_bps,
            "tp_bps": self.tp_bps,
            "sl_bps": self.sl_bps,
            "max_hold_bars": self.max_hold_bars,
            "vol_window": self.vol_window,
            "max_vol_bps": self.max_vol_bps,
            "trend_window": self.trend_window,
            "use_trend_filter": self.use_trend_filter,
            "position_pct": self.position_pct,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Genome":
        return cls(
            lookback=int(raw["lookback"]),
            entry_bps=int(raw["entry_bps"]),
            tp_bps=int(raw["tp_bps"]),
            sl_bps=int(raw["sl_bps"]),
            max_hold_bars=int(raw["max_hold_bars"]),
            vol_window=int(raw["vol_window"]),
            max_vol_bps=int(raw["max_vol_bps"]),
            trend_window=int(raw["trend_window"]),
            use_trend_filter=bool(raw["use_trend_filter"]),
            position_pct=int(raw["position_pct"]),
        )

    def key(self) -> tuple[Any, ...]:
        return tuple(sorted(self.to_dict().items()))

    @property
    def warmup(self) -> int:
        basis = max(self.lookback + 1, self.vol_window + 1)
        if self.use_trend_filter:
            basis = max(basis, self.trend_window + 1)
        return basis


@dataclass(frozen=True)
class CostModel:
    """Round-trip trading costs. Defaults reflect a liquid ETF at retail size."""

    spread_bps: Decimal = Decimal("2")
    slippage_bps: Decimal = Decimal("1")
    fee_per_order: Decimal = Decimal("0")

    @property
    def per_side_bps(self) -> Decimal:
        return self.spread_bps / 2 + self.slippage_bps

    def buy_price(self, price: Decimal) -> Decimal:
        return price * (1 + self.per_side_bps / BPS)

    def sell_price(self, price: Decimal) -> Decimal:
        return price * (1 - self.per_side_bps / BPS)


@dataclass
class Trade:
    entry_time: Any
    exit_time: Any
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    return_bps: float
    bars_held: int
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "quantity": str(self.quantity),
            "pnl": str(self.pnl),
            "return_bps": round(self.return_bps, 3),
            "bars_held": self.bars_held,
            "exit_reason": self.exit_reason,
        }


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: Decimal = Decimal("0")
    return_pct: float = 0.0
    expectancy_bps: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    final_equity: Decimal = Decimal("0")
    bootstrap_p_value: float = 1.0
    equity_curve: list[tuple[Any, Decimal]] = field(default_factory=list)
    trade_returns_bps: list[float] = field(default_factory=list)
    trades_detail: list["Trade"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "net_pnl": str(self.net_pnl),
            "return_pct": round(self.return_pct, 4),
            "expectancy_bps": round(self.expectancy_bps, 3),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "final_equity": str(self.final_equity),
            "bootstrap_p_value": round(self.bootstrap_p_value, 4),
        }

    @property
    def statistically_positive(self) -> bool:
        """True when >95% of bootstrap resamples of trade returns have mean > 0."""
        return self.trades > 0 and self.bootstrap_p_value < 0.05


def momentum_bps(bars: list[Bar], index: int, lookback: int) -> Optional[float]:
    if index - lookback < 0:
        return None
    past = bars[index - lookback].close
    if past <= 0:
        return None
    return float((bars[index].close - past) / past * BPS)


def realized_vol_bps(bars: list[Bar], index: int, window: int) -> Optional[float]:
    if index - window < 0:
        return None
    moves = []
    for position in range(index - window + 1, index + 1):
        previous = bars[position - 1].close
        if previous <= 0:
            return None
        moves.append(abs(float((bars[position].close - previous) / previous * BPS)))
    if not moves:
        return None
    return sum(moves) / len(moves)


def simple_mean(bars: list[Bar], index: int, window: int) -> Optional[Decimal]:
    if index - window < 0:
        return None
    window_bars = bars[index - window : index]
    if not window_bars:
        return None
    total = sum((bar.close for bar in window_bars), Decimal("0"))
    return total / Decimal(len(window_bars))


def run_backtest(
    bars: list[Bar],
    genome: Genome,
    *,
    costs: Optional[CostModel] = None,
    starting_cash: Decimal = Decimal("50"),
    bootstrap_samples: int = 1000,
    seed: int = 7,
) -> Metrics:
    """Simulate ``genome`` over ``bars`` and return honest performance metrics."""
    costs = costs or CostModel()
    if len(bars) <= genome.warmup + 2:
        return Metrics(final_equity=starting_cash)

    cash = starting_cash
    position_qty = Decimal("0")
    entry_price = Decimal("0")
    entry_index = 0
    entry_time = bars[0].start
    stop_price = Decimal("0")
    target_price = Decimal("0")
    trades: list[Trade] = []
    equity_curve: list[tuple[Any, Decimal]] = []

    for index in range(genome.warmup, len(bars) - 1):
        bar = bars[index]

        if position_qty > 0:
            exit_price: Optional[Decimal] = None
            reason = ""
            if bar.low <= stop_price:
                exit_price, reason = costs.sell_price(stop_price), "stop"
            elif bar.high >= target_price:
                exit_price, reason = costs.sell_price(target_price), "take_profit"
            elif index - entry_index >= genome.max_hold_bars:
                exit_price, reason = costs.sell_price(bar.close), "timeout"
            elif genome.use_trend_filter:
                mean = simple_mean(bars, index, genome.trend_window)
                if mean is not None and bar.close < mean:
                    exit_price, reason = costs.sell_price(bar.close), "trend_break"

            if exit_price is not None:
                proceeds = exit_price * position_qty - costs.fee_per_order
                cost_basis = entry_price * position_qty
                pnl = proceeds - cost_basis
                cash += proceeds
                trades.append(
                    Trade(
                        entry_time=entry_time,
                        exit_time=bar.start,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        quantity=position_qty,
                        pnl=pnl,
                        return_bps=(
                            float(pnl / cost_basis * BPS) if cost_basis > 0 else 0.0
                        ),
                        bars_held=index - entry_index,
                        exit_reason=reason,
                    )
                )
                position_qty = Decimal("0")
            equity_curve.append((bar.start, cash + position_qty * bar.close))
            continue

        momentum = momentum_bps(bars, index, genome.lookback)
        vol = realized_vol_bps(bars, index, genome.vol_window)
        if momentum is None or vol is None or momentum < genome.entry_bps:
            equity_curve.append((bar.start, cash))
            continue
        if vol > genome.max_vol_bps:
            equity_curve.append((bar.start, cash))
            continue
        if genome.use_trend_filter:
            mean = simple_mean(bars, index, genome.trend_window)
            if mean is None or bar.close < mean:
                equity_curve.append((bar.start, cash))
                continue

        next_open = bars[index + 1].open
        budget = cash * Decimal(genome.position_pct) / Decimal("100")
        price = costs.buy_price(next_open)
        quantity = (budget - costs.fee_per_order) / price if price > 0 else Decimal("0")
        if quantity <= 0:
            equity_curve.append((bar.start, cash))
            continue
        cash -= price * quantity + costs.fee_per_order
        position_qty = quantity
        entry_price = price
        entry_index = index + 1
        entry_time = bars[index + 1].start
        stop_price = price * (1 - Decimal(genome.sl_bps) / BPS)
        target_price = price * (1 + Decimal(genome.tp_bps) / BPS)
        equity_curve.append((bar.start, cash + position_qty * bars[index + 1].open))

    final_equity = cash + position_qty * bars[-1].close
    return _summarize(
        trades,
        equity_curve,
        starting_cash=starting_cash,
        final_equity=final_equity,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )


def _summarize(
    trades: list[Trade],
    equity_curve: list[tuple[Any, Decimal]],
    *,
    starting_cash: Decimal,
    final_equity: Decimal,
    bootstrap_samples: int,
    seed: int,
) -> Metrics:
    returns = [trade.return_bps for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    peak = starting_cash
    max_drawdown = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = float((peak - equity) / peak * 100)
            max_drawdown = max(max_drawdown, drawdown)

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    metrics = Metrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(returns)) if returns else 0.0,
        net_pnl=final_equity - starting_cash,
        return_pct=(
            float((final_equity - starting_cash) / starting_cash * 100)
            if starting_cash > 0
            else 0.0
        ),
        expectancy_bps=(sum(returns) / len(returns)) if returns else 0.0,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown,
        final_equity=final_equity,
        equity_curve=equity_curve,
        trade_returns_bps=returns,
        trades_detail=list(trades),
    )
    metrics.bootstrap_p_value = bootstrap_p_value(
        returns, samples=bootstrap_samples, seed=seed
    )
    return metrics


def bootstrap_p_value(
    returns: list[float], *, samples: int = 1000, seed: int = 7
) -> float:
    """P(mean return <= 0) under resampling with replacement.

    A high value means the observed edge is indistinguishable from noise.
    """
    if not returns or samples <= 0:
        return 1.0
    rng = random.Random(seed)
    count = len(returns)
    worse = 0
    for _ in range(samples):
        total = 0.0
        for _ in range(count):
            total += returns[rng.randrange(count)]
        if total / count <= 0:
            worse += 1
    return worse / samples


def fold_metrics(trades: list[Trade], *, folds: int) -> list[Metrics]:
    """Split trades chronologically and summarize each fold (out-of-sample view)."""
    if folds <= 0:
        raise ValueError("folds must be positive")
    if not trades:
        return []
    size = max(1, len(trades) // folds)
    out: list[Metrics] = []
    for start in range(0, len(trades), size):
        chunk = trades[start : start + size]
        if not chunk:
            continue
        running = Decimal("0")
        curve: list[tuple[Any, Decimal]] = []
        for trade in chunk:
            running += trade.pnl
            curve.append((trade.exit_time, running))
        out.append(
            _summarize(
                chunk,
                curve,
                starting_cash=max(abs(running), Decimal("1")),
                final_equity=running,
                bootstrap_samples=200,
                seed=11 + start,
            )
        )
    return out
