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

    def __post_init__(self) -> None:
        if self.mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow|live")
        if self.strategy not in ("fixture", "spy_scalper"):
            raise ValueError("strategy must be fixture|spy_scalper")


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
    )
