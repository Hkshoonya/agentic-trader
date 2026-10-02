"""Replay the switchboard over past prices, with the same code the live service runs.

Two sources:
* **recorded** (exact): the venues recorder's gzip files under
  ``data/stream``. Alpaca and Coinbase ticks are merged in arrival order.
* **bars** (approximate): Alpaca's historical 1-minute crypto bars. Each bar
  becomes four prices 15 s apart: the open; then the low before the high for
  an up bar (the high before the low for a down bar); then the close. They use
  the median spread seen in recordings. The order of prices inside a minute
  is a guess, so the report says "approximate".
"""

from __future__ import annotations

import gzip
import heapq
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.mirror import CoinbaseMirror
from agentic_trading.fast.settings import FastConfig
from agentic_trading.fast.switchboard import Event, Switchboard, tick_price
from agentic_trading.venues.model import Tick

DEFAULT_SPREAD = Decimal("0.0010")  # 10 bps until something has been recorded
STEP = timedelta(seconds=15)
SPREAD_SAMPLE = 5000


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


@dataclass
class Report:
    label: str
    start: str
    end: str
    ticks: int
    playbooks: dict[str, dict[str, Any]]
    alpaca_return_pct: float
    coinbase_return_pct: Optional[float]
    hold_return_pct: Optional[float]
    skipped: int
    unpriced: int
    events: list[Event] = field(default_factory=list, repr=False)

    def lines(self) -> list[str]:
        out = [f"switchboard replay ({self.label}) {self.start} → {self.end}: {self.ticks:,} prices"]
        if not self.playbooks:
            out.append("  no trades")
        for name, stats in sorted(self.playbooks.items()):
            out.append(f"  {name}: {stats['trades']} trades, {stats['wins']} won, "
                       f"average win {_pct(stats['avg_win_pct'])}, average loss {_pct(stats['avg_loss_pct'])}")
        out.append(f"  at Alpaca costs: {_pct(self.alpaca_return_pct)}")
        out.append(f"  at Coinbase costs: {_pct(self.coinbase_return_pct)} ({self.unpriced} trades not copied)")
        out.append(f"  just holding the same coins: {_pct(self.hold_return_pct)}")
        out.append(f"  setups skipped by the cost gate: {self.skipped}")
        return out


def _return(book: MemberBook) -> float:
    return round(float(book.equity / book.starting_equity - 1) * 100, 3)


def replay(ticks: Iterable[Tick], config: FastConfig, *, label: str, start: str = "", end: str = "",
           starting_equity: Decimal = Decimal("50")) -> Report:
    book = MemberBook("switchboard", starting_equity=starting_equity)
    mirror = CoinbaseMirror(MemberBook("switchboard@coinbase", starting_equity=starting_equity), config.coinbase_fee)
    board = Switchboard(config, book, mirror=mirror)
    first: dict[str, Decimal] = {}
    last: dict[str, Decimal] = {}
    events: list[Event] = []
    exits: dict[str, list[float]] = {}
    count = 0
    for tick in ticks:
        count += 1
        if tick.venue == "alpaca" and tick.symbol in config.symbols:
            price = tick_price(tick)
            if price is not None:
                first.setdefault(tick.symbol, price)
                last[tick.symbol] = price
        for event in board.on_tick(tick):
            events.append(event)
            if event.kind == "fast_exit":
                exits.setdefault(event.data["playbook"], []).append(float(event.data["net_pct"]))
    playbooks: dict[str, dict[str, Any]] = {}
    for name, results in exits.items():
        wins = [r for r in results if r > 0]
        losses = [r for r in results if r <= 0]
        playbooks[name] = {
            "trades": len(results), "wins": len(wins),
            "avg_win_pct": round(statistics.fmean(wins), 3) if wins else None,
            "avg_loss_pct": round(statistics.fmean(losses), 3) if losses else None,
        }
    holds = [float(last[s] / first[s] - 1) * 100 for s in first if first[s] > 0]
    copied_nothing = book.entries > 0 and mirror.book.entries == 0
    return Report(
        label, start, end, count, playbooks, _return(book),
        None if copied_nothing else _return(mirror.book),
        round(statistics.fmean(holds), 3) if holds else None,
        board.skipped_total, mirror.unpriced, events,
    )


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _row_tick(row: Any) -> Optional[Tick]:
    if not isinstance(row, dict):
        return None

    def number(key: str) -> Optional[Decimal]:
        value = row.get(key)
        return None if value in (None, "") else Decimal(str(value))

    try:
        return Tick(str(row["venue"]), str(row["symbol"]), number("bid"), number("ask"), number("last"),
                    number("size"), datetime.fromisoformat(str(row["exchange_at"])),
                    datetime.fromisoformat(str(row["received_at"])))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _read_rows(path: Path) -> Iterator[Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError):
        return  # a file cut short by a crash: keep what was read


def recorded_ticks(stream_dir: Path | str, symbols: Iterable[str], start: date, end: date) -> Iterator[Tick]:
    """Every recorded Alpaca and Coinbase tick for ``symbols``, a day at a time, in arrival order."""
    folders = [s.replace("/", "") for s in symbols]
    for day in _days(start, end):
        ticks: list[Tick] = []
        # Coinbase first: at equal arrival times the stable sort keeps it ahead, so the
        # mirror holds that moment's Coinbase quote when the Alpaca tick fills.
        for venue in ("coinbase", "alpaca"):
            for folder in folders:
                path = Path(stream_dir) / venue / folder / f"{day.isoformat()}.jsonl.gz"
                if path.is_file():
                    ticks.extend(t for t in map(_row_tick, _read_rows(path)) if t is not None)
        ticks.sort(key=lambda t: t.received_at)
        yield from ticks


def median_spread(stream_dir: Path | str, symbols: Iterable[str]) -> Decimal:
    """The median relative Alpaca spread in the newest recording, else 10 bps."""
    spreads: list[float] = []
    for folder in (s.replace("/", "") for s in symbols):
        files = sorted((Path(stream_dir) / "alpaca" / folder).glob("*.jsonl.gz"))
        if not files:
            continue
        for index, row in enumerate(_read_rows(files[-1])):
            if index >= SPREAD_SAMPLE:
                break
            tick = _row_tick(row)
            if tick is not None and tick.bid and tick.ask and tick.ask >= tick.bid > 0:
                spreads.append(float((tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2)))
    return Decimal(str(round(statistics.median(spreads), 6))) if spreads else DEFAULT_SPREAD


def _bar_prices(row: dict[str, Any]) -> list[Decimal]:
    o, h, l, c = (Decimal(str(row[key])) for key in ("o", "h", "l", "c"))
    return [o, l, h, c] if c >= o else [o, h, l, c]


def _symbol_ticks(symbol: str, rows: list[dict[str, Any]], spread: Decimal) -> Iterator[Tick]:
    product = CoinbaseMirror.product(symbol)
    half = spread / 2
    for row in rows:
        try:
            at = datetime.fromisoformat(str(row["t"]))
            prices = _bar_prices(row)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        for index, price in enumerate(prices):
            when = at + STEP * index
            bid, ask = price * (1 - half), price * (1 + half)
            # Coinbase first, so the mirror holds this moment's quote when the Alpaca tick fills
            yield Tick("coinbase", product, bid, ask, None, None, when, when)
            yield Tick("alpaca", symbol, bid, ask, None, None, when, when)


def bar_ticks(rows: dict[str, list[dict[str, Any]]], spread: Decimal) -> Iterator[Tick]:
    return heapq.merge(*(_symbol_ticks(s, r, spread) for s, r in rows.items()), key=lambda t: t.received_at)


def _alpaca_minutes(symbol: str, start: datetime, end: datetime) -> list[Any]:  # pragma: no cover - network
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end)
    response = CryptoHistoricalDataClient().get_crypto_bars(request)
    return list(response.data.get(symbol, []))


def fetch_bars(symbols: Iterable[str], start: date, end: date, cache_dir: Path | str, *,
               fetch: Callable[[str, datetime, datetime], list[Any]] = _alpaca_minutes) -> dict[str, list[dict[str, Any]]]:
    """1-minute bars per symbol for ``start``..``end``, cached as one file per symbol per day.

    Only days with no cache file are fetched (one request per symbol). A day
    is cached even when it has no bars, so a quiet day is not requested again.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        folder = Path(cache_dir) / symbol.replace("/", "")
        missing = [d for d in _days(start, end) if not (folder / f"{d.isoformat()}.json").is_file()]
        if missing:
            begin = datetime.combine(missing[0], time.min, tzinfo=timezone.utc)
            stop = datetime.combine(missing[-1] + timedelta(days=1), time.min, tzinfo=timezone.utc)
            by_day: dict[str, list[dict[str, Any]]] = {d.isoformat(): [] for d in missing}
            for bar in fetch(symbol, begin, stop):
                at = bar.timestamp.astimezone(timezone.utc)
                if at.date().isoformat() in by_day:
                    by_day[at.date().isoformat()].append({
                        "t": at.isoformat(), "o": str(bar.open), "h": str(bar.high),
                        "l": str(bar.low), "c": str(bar.close)})
            folder.mkdir(parents=True, exist_ok=True)
            for key, rows in by_day.items():
                (folder / f"{key}.json").write_text(json.dumps(rows), encoding="utf-8")
        rows_all: list[dict[str, Any]] = []
        for day in _days(start, end):
            try:
                rows_all.extend(json.loads((folder / f"{day.isoformat()}.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        out[symbol] = rows_all
    return out
