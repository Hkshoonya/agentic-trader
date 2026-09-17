from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    mode: str
    symbol_whitelist: frozenset[str]
    max_order_pct: Decimal
    daily_notional_pct: Decimal
    daily_loss_pct: Decimal
    max_open_positions: int
    equity_refresh_ticks: int
    equity_refresh_seconds: int
    timezone: str
    quotes_path: Path
    journal_dir: Path
    state_dir: Path
    tools_snapshot_path: Path
    token_path: Path
    mcp_url: str
    strategy: str = "fixture"
    scalper_config: Path | None = None
    # Phase 3 — autonomous loop
    quote_source: str = "file"  # file | mcp
    poll_seconds: float = 5.0
    session_policy: str = "regular"  # regular | extended | all | any
    # Widest session policy the agent may move to on its own. Empty means "no
    # wider than session_policy", so widening trading hours is opt-in.
    max_session_policy: str = ""
    account_number: str | None = None
    order_type: str = "market"  # market (regular hours) | limit (marketable)
    max_orders_per_day: int = 10
    max_consecutive_errors: int = 3
    max_quote_age_seconds: float = 60.0
    # Every broker round trip costs ~1.3s over the remote MCP gateway, so the
    # open-order read (2 calls once crypto is in play) is throttled instead of
    # running on every poll.
    open_order_refresh_seconds: float = 15.0
    # Cadence heartbeat: how often the daemon journals measured cycle timing.
    cycle_stats_seconds: float = 60.0
    # How often the off-path worker refreshes one batch of LLM regime views.
    # Each refresh is a model call, so this trades freshness against API traffic.
    regime_refresh_seconds: float = 180.0
    # How often the daemon re-fetches daily bars so the evidence can change.
    # Without this the evaluation re-runs the same search over frozen files and
    # confidence can never move.
    history_refresh_hours: float = 24.0
    # How often the back-check agent verifies data, state and analysis paths.
    selfcheck_minutes: float = 30.0
    # Phase 4 — self-evaluation, promotion, autonomy
    autonomy: str = "manual"  # manual | assisted | auto
    history_path: Path | None = None
    evolution_interval_minutes: int = 60
    evolution_population: int = 24
    evolution_generations: int = 6
    promotion_cycles_required: int = 3
    min_oos_trades: int = 30
    equity_sizing: bool = True
    min_order_notional: Decimal = Decimal("1.00")

    def __post_init__(self) -> None:
        if self.mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        if self.strategy not in ("fixture", "spy_scalper", "llm", "trend_crypto"):
            raise ValueError(
                "strategy must be fixture|spy_scalper|llm|trend_crypto"
            )
        if self.quote_source not in ("file", "mcp"):
            raise ValueError("quote_source must be file|mcp")
        if self.session_policy not in ("regular", "extended", "all", "any"):
            raise ValueError("session_policy must be regular|extended|all|any")
        if self.max_session_policy:
            from agentic_trading.limits import SESSION_POLICIES

            if self.max_session_policy not in SESSION_POLICIES:
                raise ValueError("max_session_policy must be regular|extended|all|any")
            if SESSION_POLICIES.index(self.max_session_policy) < SESSION_POLICIES.index(
                self.session_policy
            ):
                raise ValueError(
                    "max_session_policy must be at least as wide as session_policy "
                    "(the agent may widen hours, never narrow below what you set)"
                )
        if self.order_type not in ("market", "limit"):
            raise ValueError("order_type must be market|limit")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.max_orders_per_day < 1:
            raise ValueError("max_orders_per_day must be >= 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.open_order_refresh_seconds < 0:
            raise ValueError("open_order_refresh_seconds must be >= 0")
        if self.cycle_stats_seconds < 0:
            raise ValueError("cycle_stats_seconds must be >= 0")
        if self.regime_refresh_seconds < 0:
            raise ValueError("regime_refresh_seconds must be >= 0")
        if self.history_refresh_hours < 0:
            raise ValueError("history_refresh_hours must be >= 0")
        if self.selfcheck_minutes < 0:
            raise ValueError("selfcheck_minutes must be >= 0")
        if self.autonomy not in ("manual", "assisted", "auto"):
            raise ValueError("autonomy must be manual|assisted|auto")
        if self.evolution_interval_minutes < 0:
            raise ValueError("evolution_interval_minutes must be >= 0")
        if self.evolution_population < 2:
            raise ValueError("evolution_population must be >= 2")
        if self.evolution_generations < 1:
            raise ValueError("evolution_generations must be >= 1")
        if self.promotion_cycles_required < 1:
            raise ValueError("promotion_cycles_required must be >= 1")
        if self.min_oos_trades < 1:
            raise ValueError("min_oos_trades must be >= 1")
        if self.min_order_notional <= 0:
            raise ValueError("min_order_notional must be positive")


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    scalper_raw = raw.get("scalper_config")
    return Config(
        mode=raw["mode"],
        symbol_whitelist=frozenset(s.upper() for s in raw["symbol_whitelist"]),
        max_order_pct=Decimal(str(raw["max_order_pct"])),
        daily_notional_pct=Decimal(str(raw["daily_notional_pct"])),
        daily_loss_pct=Decimal(str(raw["daily_loss_pct"])),
        max_open_positions=int(raw["max_open_positions"]),
        equity_refresh_ticks=int(raw["equity_refresh_ticks"]),
        equity_refresh_seconds=int(raw["equity_refresh_seconds"]),
        timezone=str(raw.get("timezone", "local")),
        quotes_path=Path(raw["quotes_path"]),
        journal_dir=Path(raw["journal_dir"]),
        state_dir=Path(raw["state_dir"]),
        tools_snapshot_path=Path(raw["tools_snapshot_path"]),
        token_path=Path(raw["token_path"]).expanduser(),
        mcp_url=str(raw["mcp_url"]),
        strategy=str(raw.get("strategy", "fixture")),
        scalper_config=Path(scalper_raw) if scalper_raw else None,
        quote_source=str(raw.get("quote_source", "file")),
        poll_seconds=float(raw.get("poll_seconds", 5.0)),
        session_policy=str(raw.get("session_policy", "regular")),
        max_session_policy=str(raw.get("max_session_policy", "")),
        account_number=(
            str(raw["account_number"]) if raw.get("account_number") else None
        ),
        order_type=str(raw.get("order_type", "market")),
        max_orders_per_day=int(raw.get("max_orders_per_day", 10)),
        max_consecutive_errors=int(raw.get("max_consecutive_errors", 3)),
        max_quote_age_seconds=float(raw.get("max_quote_age_seconds", 60.0)),
        open_order_refresh_seconds=float(raw.get("open_order_refresh_seconds", 15.0)),
        cycle_stats_seconds=float(raw.get("cycle_stats_seconds", 60.0)),
        regime_refresh_seconds=float(raw.get("regime_refresh_seconds", 180.0)),
        history_refresh_hours=float(raw.get("history_refresh_hours", 24.0)),
        selfcheck_minutes=float(raw.get("selfcheck_minutes", 30.0)),
        autonomy=str(raw.get("autonomy", "manual")),
        history_path=Path(raw["history_path"]) if raw.get("history_path") else None,
        evolution_interval_minutes=int(raw.get("evolution_interval_minutes", 60)),
        evolution_population=int(raw.get("evolution_population", 24)),
        evolution_generations=int(raw.get("evolution_generations", 6)),
        promotion_cycles_required=int(raw.get("promotion_cycles_required", 3)),
        min_oos_trades=int(raw.get("min_oos_trades", 30)),
        equity_sizing=bool(raw.get("equity_sizing", True)),
        min_order_notional=Decimal(str(raw.get("min_order_notional", "1.00"))),
    )
