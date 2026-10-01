"""Per-host politeness: concurrency cap, request spacing with jitter, and host health."""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

BAD_STATUSES = frozenset({403, 429, 503})


class HostLimiter:
    def __init__(
        self,
        *,
        rate: float,
        jitter: float,
        per_host: int,
        max_bad_streak: int = 15,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._base_interval = 1.0 / rate
        self._jitter = jitter
        self._per_host = per_host
        self._max_bad = max_bad_streak
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}
        self._interval: dict[str, float] = {}
        self._bad_streak: dict[str, int] = {}
        self.blocked: set[str] = set()

    def interval(self, host: str) -> float:
        return self._interval.get(host, self._base_interval)

    def set_min_interval(self, host: str, seconds: float) -> None:
        """E.g. robots.txt Crawl-delay; only ever slows a host down."""
        if seconds > self.interval(host):
            self._interval[host] = seconds

    def record(self, host: str, status: int) -> None:
        """Track consecutive 403/429/503s; slow down every 3, block the host at the limit."""
        if status not in BAD_STATUSES:
            self._bad_streak[host] = 0
            return
        streak = self._bad_streak.get(host, 0) + 1
        self._bad_streak[host] = streak
        if streak % 3 == 0:
            self._interval[host] = min(self.interval(host) * 2, 60.0)
        if streak >= self._max_bad:
            self.blocked.add(host)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        sem = self._sems.setdefault(host, asyncio.Semaphore(self._per_host))
        async with sem:
            await self._wait_turn(host)
            yield

    async def _wait_turn(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._next.get(host, 0.0) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            spacing = self.interval(host) * (1 + self._jitter * (2 * self._rng() - 1))
            self._next[host] = self._clock() + spacing
