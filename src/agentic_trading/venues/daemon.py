"""The venues daemon: every stream supervised, health written every second.

Each SDK stream runs its blocking ``run(emit)`` in a *daemon* thread, so a stuck
SDK can never keep the process alive at shutdown. Ticks cross into asyncio
through ``loop.call_soon_threadsafe``. The rules:
- a stream that ends or fails reconnects with backoff;
- a rate limit waits ``rate_delay``;
- an authentication failure stops that stream (a retry storm against a bad key
  helps nobody).
"""

from __future__ import annotations

import asyncio
import random
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agentic_trading.session import session_for
from agentic_trading.venues import arming
from agentic_trading.venues.bus import TickBus
from agentic_trading.venues.health import HealthBoard
from agentic_trading.venues.model import Tick
from agentic_trading.venues.recorder import StreamRecorder
from agentic_trading.venues.secrets import Credentials, redact

_AUTH_WORDS = ("401", "403", "unauthorized", "forbidden", "authentication", "invalid api key", "auth failed")
_RATE_WORDS = ("429", "rate limit", "too many requests")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Backoff:
    def __init__(self, *, base: float = 1.0, cap: float = 60.0, jitter: float = 0.2,
                 rand: Callable[[], float] = random.random) -> None:
        self.base, self.cap, self.jitter, self.rand = base, cap, jitter, rand
        self.attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2 ** self.attempt))
        self.attempt += 1
        return delay * (1 + self.jitter * (2 * self.rand() - 1))

    def reset(self) -> None:
        self.attempt = 0


def classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, PermissionError) or any(word in text for word in _AUTH_WORDS):
        return "auth"
    if any(word in text for word in _RATE_WORDS):
        return "rate"
    return "other"


def stock_market_open(now: datetime) -> bool:
    return session_for(now) == "regular"


def build_venues(config: Any, credentials: dict[str, Credentials], *,
                 factories: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from agentic_trading.venues.alpaca import AlpacaVenue
    from agentic_trading.venues.coinbase import CoinbaseVenue

    factories = factories or {}
    venues: dict[str, Any] = {}
    if config.use_alpaca_paper and "alpaca_paper" in credentials:
        venues["alpaca_paper"] = AlpacaVenue(credentials["alpaca_paper"], paper=True,
                                             client_factory=factories.get("alpaca_trading"))
    if config.use_alpaca_live and "alpaca_live" in credentials:
        venues["alpaca_live"] = AlpacaVenue(credentials["alpaca_live"], paper=False,
                                            client_factory=factories.get("alpaca_trading"))
    if config.use_coinbase and "coinbase" in credentials:
        venues["coinbase"] = CoinbaseVenue(credentials["coinbase"],
                                           client_factory=factories.get("coinbase_rest"))
    return venues


def build_sources(config: Any, credentials: dict[str, Credentials], *,
                  factories: Optional[dict[str, Any]] = None) -> list[Any]:
    from agentic_trading.venues.alpaca import AlpacaCryptoTicks, AlpacaStockTicks
    from agentic_trading.venues.coinbase import CoinbaseTicks

    factories = factories or {}
    sources: list[Any] = []
    # Market data is the same for both Alpaca accounts: paper keys first.
    data_keys = credentials.get("alpaca_paper") or credentials.get("alpaca_live")
    wants_alpaca = config.use_alpaca_paper or config.use_alpaca_live
    if data_keys and wants_alpaca and config.alpaca_stock_symbols:
        sources.append(AlpacaStockTicks(data_keys, config.alpaca_stock_symbols, feed=config.alpaca_feed,
                                        stream_factory=factories.get("alpaca_stock_stream")))
    if data_keys and wants_alpaca and config.alpaca_crypto_symbols:
        sources.append(AlpacaCryptoTicks(data_keys, config.alpaca_crypto_symbols,
                                         stream_factory=factories.get("alpaca_crypto_stream")))
    if config.use_coinbase and "coinbase" in credentials and config.coinbase_products:
        sources.append(CoinbaseTicks(credentials["coinbase"], config.coinbase_products,
                                     client_factory=factories.get("coinbase_ws")))
    return sources


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def _settle(future: asyncio.Future, error: Optional[BaseException]) -> None:
    if future.done():
        return
    if error is None:
        future.set_result(None)
    else:
        future.set_exception(error)


async def _run_in_thread(source: Any, emit: Callable[[Tick], None]) -> None:
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def target() -> None:
        try:
            source.run(emit)
        except BaseException as exc:  # noqa: BLE001 - handed to the supervisor
            loop.call_soon_threadsafe(_settle, done, exc)
        else:
            loop.call_soon_threadsafe(_settle, done, None)

    threading.Thread(target=target, name=f"venue-{source.key}", daemon=True).start()
    await done


async def supervise(source: Any, *, deliver: Callable[[str, Tick], None], board: HealthBoard,
                    credentials: list[Credentials], stop: asyncio.Event, backoff: Backoff,
                    rate_delay: float = 30.0, clock: Optional[Callable[[], datetime]] = None) -> None:
    loop = asyncio.get_running_loop()
    clock = clock or _utcnow
    health = board.stream(source.key, source.venue, always_open=source.always_open)

    def emit(tick: Tick) -> None:
        loop.call_soon_threadsafe(deliver, source.key, tick)

    while not stop.is_set():
        seen_before = health.last_tick_at
        health.begin(clock())  # "reconnecting" covers only the wait before this
        try:
            await _run_in_thread(source, emit)
            if stop.is_set():
                break
            raise ConnectionError("stream ended")
        except Exception as exc:  # noqa: BLE001 - classified below
            if stop.is_set():
                break
            health.connected = False
            health.last_error = redact(f"{type(exc).__name__}: {exc}", credentials)[:200]
            kind = classify_error(exc)
            if kind == "auth":
                health.status = "auth_failed"
                return
            if health.last_tick_at != seen_before:
                backoff.reset()  # it was working: start the backoff over
            health.reconnects += 1
            health.status = "reconnecting"
            await _wait(stop, rate_delay if kind == "rate" else backoff.next())
    if health.status != "auth_failed":
        health.status = "off"


async def run_venues(*, state_dir: Path | str, venues_config: Any, credentials: dict[str, Credentials],
                     venues: dict[str, Any], sources: list[Any], stop: asyncio.Event,
                     now: Optional[Callable[[], datetime]] = None,
                     stock_open: Callable[[datetime], bool] = stock_market_open,
                     health_every: float = 1.0, account_every: float = 30.0,
                     backoff_factory: Callable[[], Backoff] = Backoff, rate_delay: float = 30.0) -> None:
    clock = now or _utcnow
    state_dir = Path(state_dir)
    creds = list(credentials.values())
    board, bus = HealthBoard(), TickBus()
    recorder = StreamRecorder(venues_config.stream_dir)
    queue = bus.subscribe()
    health_path = state_dir / "venues.json"
    for name, venue in venues.items():
        board.set_venue(name, mode=venue.mode, status="starting")

    def deliver(key: str, tick: Tick) -> None:
        board.on_tick(key, tick)
        bus.publish(tick)

    async def record_loop() -> None:
        while not (stop.is_set() and queue.empty()):
            try:
                tick = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            error = recorder.record(tick, now=clock())
            if error:
                board.set_venue("recorder", mode="-", status="error", last_error=error)

    async def health_loop() -> None:
        while not stop.is_set():
            current = clock()
            board.evaluate(current, stale_after=venues_config.stale_after_seconds,
                           market_open=lambda stream, at: stream.always_open or stock_open(at))
            for name in venues:
                board.set_venue(name, armed=arming.is_armed(state_dir, name, now=current))
            board.write(health_path, current)
            await _wait(stop, health_every)

    async def account_loop() -> None:
        while not stop.is_set():
            for name, venue in venues.items():
                try:
                    account = await asyncio.to_thread(venue.account)
                    board.set_venue(name, status="ok", equity=str(account.equity), cash=str(account.cash),
                                    day_trades=account.day_trades, last_error="")
                except Exception as exc:  # noqa: BLE001 - reported, never raised
                    board.set_venue(name, status="auth_failed" if classify_error(exc) == "auth" else "error",
                                    last_error=redact(f"{type(exc).__name__}: {exc}", creds)[:200])
            await _wait(stop, account_every)

    async def stopper() -> None:
        await stop.wait()
        for source in sources:
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass

    try:
        await asyncio.gather(
            stopper(), record_loop(), health_loop(), account_loop(),
            *(supervise(s, deliver=deliver, board=board, credentials=creds, stop=stop,
                        backoff=backoff_factory(), rate_delay=rate_delay, clock=clock) for s in sources),
        )
    finally:
        recorder.flush(now=clock())
        for stream in board.streams.values():
            if stream.status != "auth_failed":
                stream.status = "off"
        board.write(health_path, clock())


def main_run(config_path: str) -> int:
    """Blocking entry point for ``agentic-trading venues run``."""
    from agentic_trading.config import load_config
    from agentic_trading.venues.secrets import CredentialsError, load_credentials
    from agentic_trading.venues.settings import load_venues_config

    config = load_config(config_path)
    venues_config = load_venues_config(config_path)
    if not venues_config.enabled:
        print("venues are disabled: set [venues] enabled = true in the config")
        return 0
    try:
        credentials = load_credentials(venues_config.secrets_path)
    except CredentialsError as exc:
        print(f"venues: {exc}")
        return 2
    venues = build_venues(venues_config, credentials)
    sources = build_sources(venues_config, credentials)
    if not venues and not sources:
        print("venues: no keys for any enabled venue; see config/secrets.example.toml")
        return 2

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
                pass
        await run_venues(state_dir=config.state_dir, venues_config=venues_config, credentials=credentials,
                         venues=venues, sources=sources, stop=stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0
