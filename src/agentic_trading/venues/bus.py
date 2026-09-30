"""One publisher, many subscribers: a slow subscriber never stalls the rest."""

from __future__ import annotations

import asyncio

from agentic_trading.venues.model import Tick


class TickBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []
        self.dropped = 0

    def subscribe(self, maxsize: int = 10_000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append(queue)
        return queue

    def publish(self, tick: Tick) -> None:
        for queue in self._queues:
            if queue.full():
                try:
                    queue.get_nowait()  # this subscriber loses its oldest tick
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(tick)
