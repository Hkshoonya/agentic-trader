"""The symbol scout: which instruments the agent trades, and why that changes.

The whitelist in the config file is the *core* book — instruments the operator
authorised. On top of that core this module maintains its own membership,
re-read from the market rather than from a hand-edited list, so a trend in
something nobody thought to configure is not automatically a trend the agent
misses.

Where candidates come from:

- **crypto**: the broker's own currency-pair list, filtered to what this
  account may actually trade, then priced from Coinbase's public candles;
- **equities**: the broker's curated discovery lists — trending stocks, daily
  movers, the most-popular 100, upcoming earnings — plus whatever the operator
  put in ``discovery_candidates``. The curated lists are Robinhood's own
  answer to "what is moving today", which is a better candidate pool than a
  list of tickers someone typed once.

The rules are deliberately dull, because the failure mode of a symbol scout is
exciting:

- A candidate is only considered if its history is real: at least
  ``discovery_min_bars`` daily bars, in order, with usable volume.
- An equity is only considered if the broker says this account may trade it
  **fractionally**. On a $50 account a whole-share-only stock is not
  tradeable, and admitting one would spend order budget on orders that cannot
  be placed.
- It is only added if the trend is up on at least ``discovery_enter_vote`` of
  the 50/100/200/252-bar horizons, it trades at least
  ``discovery_min_dollar_volume`` a day on the median of the last 60 bars, its
  quoted spread is inside ``discovery_max_spread_bps``, and it does not move
  with something already held (``discovery_max_correlation``).
- It is only dropped when the trend breaks — the vote falls to or below
  ``discovery_exit_vote`` — or a hard filter above fails, and never before
  ``discovery_min_hold_hours`` have passed. The gap between the enter and exit
  thresholds is what stops a symbol being added and dropped on the same wobble.
- It is never dropped while the book still holds it. ``RiskGuard`` now allows a
  sell outside the whitelist, so a position is not literally stranded — but the
  strategy can only price an exit it has bars and quotes for, so dropping a
  held symbol would leave the exit to a fallback price. Demotion waits for flat.

Everything the scout decides is written to ``state_dir/universe.json`` with a
plain-language reason per symbol. ``Config.effective_whitelist`` reads it, so
the guard, the quote feed, the strategy, the evidence gate and the self-check
all see one universe. The scout places no orders.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from agentic_trading import jsonio
from agentic_trading.correlation import correlation, returns_from_closes
from agentic_trading.history import Bar, load_bars
from agentic_trading.history_sync import (
    bar_stem,
    fetch_crypto_records,
    merge_records,
    read_records,
    write_records,
)
from agentic_trading.orders import is_crypto_symbol

UNIVERSE_FILE = "universe.json"
# Candidate history lives here rather than in the traded book's bar directory:
# screening forty candidates should not leave forty new files in the shipped
# data set. Only what the scout actually adopts is promoted into the book.
CANDIDATE_CACHE = "candidate_history"

# The same horizons the strategy votes on. Kept here rather than imported from
# the strategy so the scout stays importable without the strategy package; a
# test pins the two lists equal.
HORIZONS = (50, 100, 200, 252)
TARGET_VOL = 0.20
MAX_LEVERAGE = 1.5

# Robinhood-curated lists, by display name. Ids are per-account, so they are
# resolved at run time; a list this account does not offer is skipped with a
# note rather than failing the pass.
DEFAULT_LISTS: tuple[str, ...] = (
    "Trending stocks",
    "Daily movers",
    "100 most popular",
    "Upcoming earnings",
    "Tradable crypto",
    "Altcoins",
)

# Bases that are dollars (or another fiat) wearing a ticker. A pegged pair has
# no trend to ride, and "trade the dollar against the dollar" is not a strategy.
_PEGGED_BASES: frozenset[str] = frozenset(
    {
        "USD", "USDT", "USDC", "USDG", "DAI", "TUSD", "USDP", "GUSD", "PYUSD",
        "EUR", "EURC", "GBP", "JPY", "CAD", "AUD", "CHF", "BUSD", "FDUSD",
    }
)


@dataclass(frozen=True)
class Candidate:
    """One symbol worth looking at, and where the idea came from."""

    symbol: str
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "sources": list(self.sources)}


@dataclass
class Screen:
    """What one candidate looks like against the rules the book trades."""

    symbol: str
    action: str = "blocked"  # admit | keep | drop | blocked
    bars: int = 0
    vote: float = 0.0
    weight: float = 0.0
    volatility: float = 0.0
    median_dollar_volume: float = 0.0
    spread_bps: Optional[float] = None
    max_correlation: Optional[float] = None
    correlated_with: str = ""
    last_start: str = ""
    sources: tuple[str, ...] = ()
    code: str = ""
    reason: str = ""

    @property
    def adopted(self) -> bool:
        return self.action in ("admit", "keep")

    @property
    def score(self) -> float:
        return self.vote * self.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "bars": self.bars,
            "vote": round(self.vote, 4),
            "weight": round(self.weight, 4),
            "volatility": round(self.volatility, 4),
            "median_dollar_volume": round(self.median_dollar_volume, 2),
            "spread_bps": (
                None if self.spread_bps is None else round(self.spread_bps, 2)
            ),
            "max_correlation": (
                None if self.max_correlation is None else round(self.max_correlation, 3)
            ),
            "correlated_with": self.correlated_with,
            "last_start": self.last_start,
            "sources": list(self.sources),
            "code": self.code,
            "reason": self.reason,
            # The console reads this flag directly; ``action`` carries the same
            # information in the scout's own vocabulary (admit/keep/drop/blocked).
            "admitted": self.adopted,
        }


@dataclass
class Universe:
    """The scout's membership, plus how the last passes went."""

    adopted: tuple[str, ...] = ()
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    as_of: str = ""
    cycles: int = 0
    failures: int = 0
    last_error: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    def added_at(self, symbol: str) -> str:
        entry = self.members.get(symbol) or {}
        return str(entry.get("added_at") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adopted": list(self.adopted),
            "members": self.members,
            "as_of": self.as_of,
            "cycles": self.cycles,
            "failures": self.failures,
            "last_error": self.last_error,
            "changes": self.changes[-20:],
            "report": self.report,
        }

    @staticmethod
    def from_dict(payload: Any) -> "Universe":
        if not isinstance(payload, dict):
            return Universe()
        adopted: list[str] = []
        for value in payload.get("adopted") or []:
            symbol = str(value).strip().upper()
            if symbol and symbol not in adopted:
                adopted.append(symbol)
        raw_members = payload.get("members")
        members = {
            str(symbol).upper(): dict(entry)
            for symbol, entry in (raw_members or {}).items()
            if isinstance(entry, dict)
        } if isinstance(raw_members, dict) else {}
        changes = payload.get("changes")
        return Universe(
            adopted=tuple(adopted),
            members=members,
            as_of=str(payload.get("as_of") or ""),
            cycles=_int(payload.get("cycles")),
            failures=_int(payload.get("failures")),
            last_error=str(payload.get("last_error") or ""),
            changes=(
                [c for c in changes if isinstance(c, dict)]
                if isinstance(changes, list)
                else []
            ),
            report=(
                payload.get("report")
                if isinstance(payload.get("report"), dict)
                else {}
            ),
        )


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def universe_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / UNIVERSE_FILE


def load_universe(state_dir: Path | str) -> Universe:
    """Read the adopted set. Missing or corrupt means "nothing adopted"."""
    try:
        payload = json.loads(universe_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Universe()
    return Universe.from_dict(payload)


def save_universe(state_dir: Path | str, universe: Universe) -> Path:
    path = universe_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, jsonio.dumps(universe.to_dict(), indent=2) + "\n")
    return path


def adopted_symbols(state_dir: Path | str) -> frozenset[str]:
    """Just the adopted set, for ``Config.effective_whitelist``."""
    return frozenset(load_universe(state_dir).adopted)


def last_pass_age_seconds(
    state_dir: Path | str, *, now: Optional[datetime] = None
) -> Optional[float]:
    """How long since the last completed pass, or ``None`` if there never was one.

    The daemon's cadence is driven by this rather than by an in-memory timer, so
    a restart resumes the schedule instead of re-running the scout or skipping it.
    """
    stamp = load_universe(state_dir).as_of
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - seen).total_seconds()


def due_for_pass(config: Any, *, now: Optional[datetime] = None) -> bool:
    """Whether the scout's cadence says it is time to look again."""
    interval = float(getattr(config, "discovery_interval_hours", 6.0)) * 3600.0
    age = last_pass_age_seconds(config.state_dir, now=now)
    return age is None or age >= interval


# -- candidates ----------------------------------------------------------


def _list_index(broker: Any) -> dict[str, str]:
    """Curated and personal list name -> id, across both list surfaces."""
    index: dict[str, str] = {}
    for method in ("get_popular_watchlists", "get_watchlists"):
        fetch = getattr(broker, method, None)
        if not callable(fetch):
            continue
        try:
            entries = fetch()
        except Exception:  # noqa: BLE001 — one surface failing is not fatal
            continue
        for entry in entries:
            name = str(entry.get("display_name") or "").strip()
            identifier = str(entry.get("id") or "").strip()
            if name and identifier:
                index.setdefault(name.lower(), identifier)
    return index


def _normalise_item_symbol(item: dict[str, Any]) -> str:
    """Watchlist items spell crypto as the bare code (``BTC``)."""
    raw = str(item.get("symbol") or "").strip().upper()
    if not raw:
        return ""
    kind = str(item.get("object_type") or "").strip().lower()
    if kind == "currency_pair" or (kind != "instrument" and "-" not in raw):
        symbol = raw if "-" in raw else f"{raw}-USD"
        base = symbol.split("-")[0]
        if base in _PEGGED_BASES:
            return ""  # a dollar-pegged pair has no trend to ride
        return symbol
    return raw


def attention_candidates(
    broker: Any,
    *,
    lists: Sequence[str] = DEFAULT_LISTS,
    limit_per_list: int = 60,
) -> tuple[list[Candidate], list[str]]:
    """What the market is paying attention to, in the market's own order.

    Returns ``(candidates, notes)``. The notes matter: "no candidates" and "the
    broker call failed" look identical on a dashboard otherwise.
    """
    notes: list[str] = []
    index = _list_index(broker)
    if not index:
        return [], ["the broker's watchlist tools are unavailable"]
    fetch_items = getattr(broker, "get_watchlist_items", None)
    if not callable(fetch_items):
        return [], ["the broker's watchlist-items tool is unavailable"]

    collected: dict[str, list[str]] = {}
    for wanted in lists:
        identifier = index.get(wanted.strip().lower())
        if not identifier:
            notes.append(f"this account is not offered the {wanted} list")
            continue
        try:
            items = fetch_items(identifier)
        except Exception as exc:  # noqa: BLE001 — one list must not stop the rest
            notes.append(f"could not read {wanted}: {str(exc)[:80]}")
            continue
        seen = 0
        for item in items:
            if seen >= limit_per_list:
                break
            symbol = _normalise_item_symbol(item)
            if not symbol:
                continue
            seen += 1
            collected.setdefault(symbol, []).append(wanted)
    return (
        [
            Candidate(symbol=symbol, sources=tuple(sources))
            for symbol, sources in collected.items()
        ],
        notes,
    )


def crypto_candidates(broker: Any) -> tuple[list[Candidate], list[str]]:
    """Every crypto pair this account may trade, from the broker itself."""
    fetch = getattr(broker, "get_currency_pairs", None)
    if not callable(fetch):
        return [], ["the broker's currency-pair tool is unavailable"]
    try:
        pairs = fetch()
    except Exception as exc:  # noqa: BLE001
        return [], [f"could not read the crypto pair list: {str(exc)[:80]}"]
    out: list[Candidate] = []
    for entry in pairs:
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        base = symbol.split("-")[0].split("/")[0].strip().upper()
        if base in _PEGGED_BASES:
            continue
        tradability = str(entry.get("tradability") or "tradable").lower()
        if tradability and tradability not in ("tradable", "trading"):
            continue
        if "-" not in symbol:
            symbol = f"{symbol}-USD"
        out.append(Candidate(symbol=symbol, sources=("broker crypto list",)))
    return out, []


# -- history -------------------------------------------------------------


def bars_path(bar_dir: Path | str, symbol: str) -> Path:
    return Path(bar_dir) / f"{bar_stem(symbol)}_day.jsonl"


def candidate_cache_dir(config: Any) -> Path:
    """Where the scout keeps the history of symbols it has not adopted."""
    return Path(config.state_dir) / CANDIDATE_CACHE


def promote_history(
    cache_dir: Path | str, bar_dir: Path | str, symbols: Iterable[str]
) -> list[str]:
    """Move adopted symbols' bars into the traded book's history directory.

    The strategy, the evidence gate and the self-check all read ``history_path``,
    and the self-check *requires* every whitelisted symbol to have a live bar
    file. An adopted symbol with no file there would fail the back-check and
    disarm the book, so adoption and promotion happen together.
    """
    promoted: list[str] = []
    for symbol in symbols:
        source = bars_path(cache_dir, symbol)
        if not source.is_file():
            continue
        records = read_records(source)
        if not records:
            continue
        target = bars_path(bar_dir, symbol)
        merged, _added = merge_records(read_records(target), records)
        write_records(target, merged)
        promoted.append(str(symbol).upper())
    return promoted


def _fetch_crypto_pages(
    symbol: str, *, years: float, pages: int = 6
) -> list[dict[str, Any]]:
    """Coinbase serves at most ~300 daily candles per call, so page backwards."""
    if years <= 0:
        return []
    end = datetime.now(timezone.utc)
    floor = end - timedelta(days=int(years * 365) + 5)
    collected: list[dict[str, Any]] = []
    for _ in range(max(1, pages)):
        if end <= floor:
            break
        start = max(floor, end - timedelta(days=300))
        try:
            page = fetch_crypto_records(symbol, start=start, end=end)
        except Exception:  # noqa: BLE001 — an unlisted pair is not an error
            break
        if not page:
            break
        collected.extend(page)
        if start <= floor:
            break
        end = start - timedelta(seconds=1)
    return collected


def history_for(
    broker: Any,
    symbol: str,
    *,
    bar_dir: Path | str,
    years: float = 3.0,
    fetch_crypto: Any = None,
) -> list[Bar]:
    """Daily bars for a symbol the book may not have traded before.

    Merged into the same file the strategy and the evidence gate read, so an
    adopted symbol is graded on exactly the bars it will be traded on. A fetch
    that fails leaves any existing file untouched.
    """
    path = bars_path(bar_dir, symbol)
    existing = read_records(path)
    incoming: list[dict[str, Any]] = []
    if is_crypto_symbol(symbol):
        if callable(fetch_crypto):
            incoming = fetch_crypto(symbol)
        else:
            incoming = _fetch_crypto_pages(symbol, years=years)
    else:
        from agentic_trading.history import parse_bars_payload

        start = (
            datetime.now(timezone.utc) - timedelta(days=int(years * 365))
        ).isoformat()
        payload = broker.get_historicals(
            [symbol], start_time=start, interval="day", bounds="regular"
        )
        incoming = [
            bar.to_record() for bar in parse_bars_payload(payload, symbol=symbol)
        ]
    if incoming:
        merged, _added = merge_records(existing, incoming)
        write_records(path, merged)
    return load_bars(path) if path.is_file() else []


# -- grading -------------------------------------------------------------


def median_dollar_volume(bars: Sequence[Bar], *, lookback: int = 60) -> float:
    """Median dollars traded per bar: the plainest read on exitability."""
    values: list[float] = []
    for bar in bars[-lookback:]:
        try:
            volume = float(bar.volume or 0.0)
            close = float(bar.close)
        except (TypeError, ValueError):
            continue
        if volume > 0 and close > 0:
            values.append(volume * close)
    return statistics.median(values) if values else 0.0


def trend_vote(closes: Sequence[float]) -> float:
    """Share of horizons whose latest close is above its own lookback close."""
    if len(closes) < 2:
        return 0.0
    votes = [
        closes[-1] > closes[-1 - horizon]
        for horizon in HORIZONS
        if len(closes) > horizon
    ]
    return sum(votes) / len(votes) if votes else 0.0


def inverse_vol_weight(closes: Sequence[float]) -> float:
    """``min(MAX_LEVERAGE, TARGET_VOL / sigma)`` — the strategy's own sizing."""
    if len(closes) < 22:
        return 0.0
    rets = returns_from_closes(list(closes[-61:]))
    if len(rets) < 20:
        return 0.0
    var = statistics.pvariance(rets) or 1e-6
    for value in rets:
        var = 0.94 * var + 0.06 * value * value
    sigma = math.sqrt(var * 252)
    return min(MAX_LEVERAGE, TARGET_VOL / sigma) if sigma > 0 else 0.0


def annualised_vol(closes: Sequence[float]) -> float:
    if len(closes) < 22:
        return 0.0
    rets = returns_from_closes(list(closes[-61:]))
    if len(rets) < 20:
        return 0.0
    var = statistics.pvariance(rets) or 0.0
    for value in rets:
        var = 0.94 * var + 0.06 * value * value
    return math.sqrt(var * 252)


def spread_bps(bid: Any, ask: Any) -> Optional[float]:
    """Cost of crossing the quoted spread once, in basis points of the mid."""
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return None
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return None
    mid = (bid_f + ask_f) / 2.0
    return (ask_f - bid_f) / mid * 10_000.0 if mid > 0 else None


def aligned_correlation(
    left: Sequence[Bar], right: Sequence[Bar], *, lookback: int = 120
) -> Optional[float]:
    """Correlation of returns on the days *both* symbols actually traded.

    ``correlation.correlation`` compares the tails of two series, which is only
    right when both end on the same bar. A crypto pair trades every day and an
    equity does not, so the tails drift apart; aligning on the timestamp is the
    difference between measuring co-movement and measuring the calendar.
    """
    left_closes = {bar.start.date(): float(bar.close) for bar in left[-lookback * 2 :]}
    right_closes = {
        bar.start.date(): float(bar.close) for bar in right[-lookback * 2 :]
    }
    shared = sorted(set(left_closes) & set(right_closes))
    if len(shared) < 30:
        return None
    return correlation(
        returns_from_closes([left_closes[day] for day in shared][-lookback:]),
        returns_from_closes([right_closes[day] for day in shared][-lookback:]),
    )


def grade_symbol(
    symbol: str,
    bars: Sequence[Bar],
    *,
    config: Any,
    held_series: dict[str, list[Bar]],
    spread_bps_value: Optional[float],
    sources: Sequence[str] = (),
    fractional_ok: bool = True,
    adopted: bool = False,
) -> Screen:
    """Grade one candidate, in the order the filters bind."""
    screen = Screen(symbol=symbol, sources=tuple(sources))
    screen.bars = len(bars)
    if bars:
        screen.last_start = bars[-1].start.isoformat()

    def blocked(code: str, reason: str) -> Screen:
        screen.action = "drop" if adopted else "blocked"
        screen.code, screen.reason = code, reason
        return screen

    minimum_bars = int(getattr(config, "discovery_min_bars", 260))
    if len(bars) < minimum_bars:
        return blocked(
            "needs_history",
            f"only {len(bars)} days of prices so far, and the rule needs "
            f"{minimum_bars} before it can judge this one",
        )

    closes = [float(bar.close) for bar in bars]
    screen.vote = trend_vote(closes)
    screen.weight = inverse_vol_weight(closes)
    screen.volatility = annualised_vol(closes)
    screen.median_dollar_volume = median_dollar_volume(bars)
    screen.spread_bps = spread_bps_value

    if not is_crypto_symbol(symbol) and not fractional_ok:
        return blocked(
            "no_fractional",
            "the broker will not take a fractional order for this one, and a "
            "whole share costs more than this account commits to a single bet",
        )

    floor_volume = float(getattr(config, "discovery_min_dollar_volume", 5_000_000))
    if screen.median_dollar_volume < floor_volume:
        return blocked(
            "too_illiquid",
            f"only about ${screen.median_dollar_volume / 1e6:.1f}M trades a day, "
            f"below the ${floor_volume / 1e6:.0f}M needed to leave a position "
            "without moving the price",
        )

    ceiling_spread = float(getattr(config, "discovery_max_spread_bps", 25.0))
    if screen.spread_bps is not None and screen.spread_bps > ceiling_spread:
        return blocked(
            "spread_too_wide",
            f"the quoted spread costs {screen.spread_bps:.0f} basis points to "
            f"cross, above the {ceiling_spread:.0f} the strategy can pay and "
            "keep its edge",
        )

    threshold = float(getattr(config, "discovery_max_correlation", 0.85))
    worst: Optional[float] = None
    worst_symbol = ""
    for other, other_bars in held_series.items():
        if bar_stem(other) == bar_stem(symbol):
            continue
        value = aligned_correlation(bars, other_bars)
        if value is None:
            continue
        if worst is None or value > worst:
            worst, worst_symbol = value, other
    screen.max_correlation = worst
    screen.correlated_with = worst_symbol
    if worst is not None and worst >= threshold:
        return blocked(
            "duplicate_bet",
            f"moves {worst:.0%} in step with {worst_symbol}, which is already in "
            "the book or was picked earlier in this pass, so it would double one "
            "bet rather than add a new one",
        )

    enter_vote = float(getattr(config, "discovery_enter_vote", 0.75))
    exit_vote = float(getattr(config, "discovery_exit_vote", 0.25))
    if screen.vote >= enter_vote:
        screen.action = "keep" if adopted else "admit"
        screen.code = "admitted"
        screen.reason = (
            f"trend is up on {screen.vote:.0%} of the timeframes, about "
            f"${screen.median_dollar_volume / 1e6:.0f}M trades a day, and it is "
            "independent of what the book already holds"
        )
        return screen
    if screen.vote <= exit_vote:
        screen.action = "drop" if adopted else "blocked"
        screen.code = "trend_broken"
        screen.reason = (
            f"the trend has broken — only {screen.vote:.0%} of the timeframes "
            "are still rising"
        )
        return screen
    screen.action = "keep" if adopted else "blocked"
    screen.code = "no_trend_yet" if not adopted else "holding"
    screen.reason = (
        f"{screen.vote:.0%} of the timeframes are rising, between the "
        f"{enter_vote:.0%} needed to add it and the {exit_vote:.0%} that would "
        "drop it"
    )
    return screen


def _query_spreads(broker: Any, symbols: Sequence[str]) -> dict[str, float]:
    """Live spreads, batched by asset class. Missing data stays missing."""
    out: dict[str, float] = {}
    equity = [s for s in symbols if not is_crypto_symbol(s)]
    crypto = [s for s in symbols if is_crypto_symbol(s)]
    for start in range(0, len(equity), 20):
        batch = equity[start : start + 20]
        try:
            payload = broker.get_quotes(batch)
        except Exception:  # noqa: BLE001 — an unreadable spread is not fatal
            continue
        out.update(_spreads_from_payload(payload, batch))
    if crypto:
        try:
            payload = broker.get_crypto_quotes(crypto)
        except Exception:  # noqa: BLE001
            payload = {}
        out.update(_spreads_from_payload(payload, crypto))
    return out


def _spreads_from_payload(payload: Any, symbols: Sequence[str]) -> dict[str, float]:
    from agentic_trading.marketdata import normalize_quotes_payload

    try:
        quotes = normalize_quotes_payload(
            payload, symbols=list(symbols), observed_at=datetime.now(timezone.utc)
        )
    except Exception:  # noqa: BLE001 — an unrecognised shape is not fatal here
        return {}
    out: dict[str, float] = {}
    for quote in quotes:
        value = spread_bps(quote.get("bid"), quote.get("ask"))
        if value is not None:
            out[str(quote.get("symbol", "")).upper()] = value
    return out


def _fractional_ok(broker: Any, symbols: Sequence[str]) -> dict[str, bool]:
    """Whether the broker will take a fractional order for each equity."""
    out: dict[str, bool] = {}
    fetch = getattr(broker, "get_tradability", None)
    if not callable(fetch):
        return out
    for start in range(0, len(symbols), 10):
        batch = list(symbols[start : start + 10])
        try:
            payload = fetch(batch)
        except Exception:  # noqa: BLE001
            continue
        for entry in _iter_dicts(payload):
            symbol = str(
                entry.get("symbol") or entry.get("instrument_symbol") or ""
            ).upper()
            if not symbol:
                continue
            for key in ("fractional", "fractional_eligible", "is_fractional"):
                value = entry.get(key)
                if isinstance(value, bool):
                    out[symbol] = value
                    break
    return out


def _iter_dicts(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and depth < 3:
        for key in ("results", "data", "symbols", "tradability", "items", "instruments"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                found = _iter_dicts(value, depth + 1)
                if found:
                    return found
    return []


# -- the pass ------------------------------------------------------------


def _held_series(bar_dir: Path | str, symbols: Iterable[str]) -> dict[str, list[Bar]]:
    series: dict[str, list[Bar]] = {}
    for symbol in symbols:
        path = bars_path(bar_dir, symbol)
        if not path.is_file():
            continue
        try:
            bars = load_bars(path)
        except OSError:
            continue
        if bars:
            series[symbol] = bars
    return series


def _age_hours(stamp: str, *, now: datetime) -> Optional[float]:
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds() / 3600.0


def rebalance(
    config: Any,
    broker: Any,
    *,
    held: Iterable[str] = (),
    now: Optional[datetime] = None,
    fetch_crypto: Any = None,
) -> dict[str, Any]:
    """One discovery pass: look, grade, adopt, and say what changed.

    Never raises for a market-data reason. The caller is a background worker on
    a live book, and a scout that takes the loop down is worse than a scout
    that reports it could not look.
    """
    moment = now or datetime.now(timezone.utc)
    state_dir = Path(config.state_dir)
    bar_dir = Path(config.history_path or "data/bars")
    cache_dir = candidate_cache_dir(config)
    universe = load_universe(state_dir)
    configured = {s.upper() for s in config.symbol_whitelist}
    held_symbols = {str(s).upper() for s in held}
    limit = int(getattr(config, "discovery_max_symbols", 6))
    max_candidates = int(getattr(config, "discovery_max_candidates", 40))
    report: dict[str, Any] = {
        "as_of": moment.isoformat(),
        "adopted": [],
        "added": [],
        "dropped": [],
        "held_back": [],
        "considered": 0,
        "candidates": [],
        "notes": [],
    }
    if limit <= 0:
        report["notes"].append("discovery is on but the adopted-symbol limit is 0")
        return report

    lists = tuple(getattr(config, "discovery_lists", None) or DEFAULT_LISTS)
    curated, notes = attention_candidates(broker, lists=lists)
    report["notes"].extend(notes)
    crypto, crypto_notes = crypto_candidates(broker)
    report["notes"].extend(crypto_notes)

    ordered: list[Candidate] = []
    seen: set[str] = set()
    for candidate in curated + crypto:
        if candidate.symbol in configured or candidate.symbol in seen:
            continue
        seen.add(candidate.symbol)
        ordered.append(candidate)
    # The operator's own pool, for symbols the curated lists have not reached.
    for symbol in getattr(config, "discovery_candidates", ()) or ():
        symbol = str(symbol).strip().upper()
        if symbol and symbol not in configured and symbol not in seen:
            seen.add(symbol)
            ordered.append(Candidate(symbol=symbol, sources=("operator pool",)))
    # What is already adopted is always re-graded: the only way to notice that
    # an adopted name has stopped working is to keep looking at it.
    current = list(universe.adopted)
    for symbol in current:
        if symbol not in seen and symbol not in configured:
            seen.add(symbol)
            ordered.append(Candidate(symbol=symbol, sources=("already adopted",)))

    report["considered"] = len(ordered)
    if not ordered:
        report["notes"].append("nothing new was on the market's lists")
        report["adopted"] = list(current)
        return report

    must_grade = [c for c in ordered if c.symbol in current]
    fresh = [c for c in ordered if c.symbol not in current]
    room = max(0, max_candidates - len(must_grade))
    # Share the budget between the two books instead of letting whichever list
    # the broker returns first eat all of it: the curated equity lists alone run
    # to hundreds of names, and a scout that only ever grades equities would
    # never notice a crypto trend again.
    fresh_equity = [c for c in fresh if not is_crypto_symbol(c.symbol)]
    fresh_crypto = [c for c in fresh if is_crypto_symbol(c.symbol)]
    fresh_ranked: list[Candidate] = []
    for index in range(max(len(fresh_equity), len(fresh_crypto))):
        if index < len(fresh_equity):
            fresh_ranked.append(fresh_equity[index])
        if index < len(fresh_crypto):
            fresh_ranked.append(fresh_crypto[index])
    selected = must_grade + fresh_ranked[:room]
    report["notes"].append(
        f"graded {len(selected)} of {len(ordered)} candidates "
        f"({len(must_grade)} already adopted)"
    )

    equities = [c.symbol for c in selected if not is_crypto_symbol(c.symbol)]
    fractional = _fractional_ok(broker, equities) if equities else {}
    spreads = _query_spreads(broker, [c.symbol for c in selected])
    held_series = _held_series(
        bar_dir, sorted(configured | held_symbols | set(current))
    )

    screens: list[Screen] = []
    for candidate in selected:
        symbol = candidate.symbol
        try:
            bars = history_for(
                broker, symbol, bar_dir=cache_dir, fetch_crypto=fetch_crypto
            )
        except Exception as exc:  # noqa: BLE001 — one symbol must not stop the pass
            report["notes"].append(f"{symbol}: no history ({str(exc)[:60]})")
            continue
        screen = grade_symbol(
            symbol,
            bars,
            config=config,
            held_series=held_series,
            spread_bps_value=spreads.get(symbol),
            sources=candidate.sources,
            fractional_ok=fractional.get(symbol, True),
            adopted=symbol in current,
        )
        # The series just loaded changes the book the next candidate is compared
        # against, so one pass cannot admit four copies of the same move.
        if bars:
            held_series[symbol] = bars
        # A freshly adopted symbol cannot be dropped on the same wobble it was
        # added on: the enter/exit thresholds differ, and this is the backstop.
        if screen.action == "drop":
            age = _age_hours(universe.added_at(symbol), now=moment)
            minimum = float(getattr(config, "discovery_min_hold_hours", 48.0))
            if (
                screen.code == "trend_broken"
                and age is not None
                and age < minimum
            ):
                screen.action = "keep"
                screen.code = "too_soon_to_drop"
                screen.reason = (
                    f"the trend has cooled, but it was added {age:.0f}h ago and "
                    f"the book holds a new symbol for at least {minimum:.0f}h"
                )
        screens.append(screen)

    ranked = sorted(
        (s for s in screens if s.adopted), key=lambda s: s.score, reverse=True
    )
    chosen = [s.symbol for s in ranked[:limit]]
    for screen in ranked[limit:]:
        screen.action = "blocked"
        screen.code = "below_the_cut"
        screen.reason = (
            f"clears every filter, but the book only makes room for {limit} "
            "discovered symbols and stronger ones ranked ahead of it"
        )

    # A held symbol stays in the universe until the position is flat, whatever
    # the trend now says. It is not that the scout cannot see the broken trend —
    # it is that dropping the symbol would also drop its bars and its quotes, and
    # an exit needs both to be priced. The position is still finished; it is
    # finished at a price rather than at a guess. Held names also sit outside the
    # room limit, because that limit is about *new* picks.
    for symbol in current:
        if symbol not in held_symbols or symbol in chosen:
            continue
        chosen.append(symbol)
        report["held_back"].append(symbol)
        for screen in screens:
            if screen.symbol != symbol:
                continue
            screen.action = "keep"
            screen.code = "held_until_flat"
            screen.reason = (
                "the trend has broken, but the book still holds this position, "
                "so it stays in the universe until the position is closed"
            )

    dropped: list[str] = []
    for symbol in current:
        if symbol in chosen:
            continue
        dropped.append(symbol)
    added = [symbol for symbol in chosen if symbol not in current]

    reasons = {s.symbol: s.reason for s in screens}
    members: dict[str, dict[str, Any]] = {}
    for symbol in chosen:
        previous = universe.members.get(symbol) or {}
        members[symbol] = {
            "added_at": (
                previous.get("added_at") if symbol in current else moment.isoformat()
            )
            or moment.isoformat(),
            "score": round(
                next((s.score for s in screens if s.symbol == symbol), 0.0), 4
            ),
            "sources": list(
                next((s.sources for s in screens if s.symbol == symbol), ())
            ),
            "reason": reasons.get(symbol, ""),
        }
    universe.adopted = tuple(chosen)
    universe.members = members
    universe.as_of = moment.isoformat()
    universe.cycles += 1
    universe.failures = 0
    universe.last_error = ""
    report["adopted"] = list(chosen)
    report["added"] = added
    report["dropped"] = dropped
    # An adopted symbol is only tradeable once its history is in the book's own
    # bar directory — the self-check refuses a universe it cannot read bars for.
    report["promoted"] = promote_history(cache_dir, bar_dir, chosen)
    report["candidates"] = [
        s.to_dict() for s in sorted(screens, key=lambda s: s.symbol)
    ]
    if added or dropped:
        universe.changes.append(
            {
                "at": moment.isoformat(),
                "added": added,
                "dropped": dropped,
                "why": {symbol: reasons.get(symbol, "") for symbol in added},
            }
        )
    universe.report = report
    save_universe(state_dir, universe)
    return report


def note_failure(config: Any, error: str) -> Universe:
    """Record a failed pass. Repeated failure freezes the universe, not widens it."""
    state_dir = Path(config.state_dir)
    universe = load_universe(state_dir)
    universe.failures += 1
    universe.cycles += 1
    universe.last_error = str(error)[:200]
    try:
        save_universe(state_dir, universe)
    except OSError:
        pass
    return universe
