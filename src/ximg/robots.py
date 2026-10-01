"""robots.txt cache (RFC 9309 semantics) built on Protego."""

import asyncio
from collections.abc import Awaitable, Callable

from protego import Protego

from ximg.urls import origin_of

UA_TOKEN = "ximg"
_ALLOW_ALL = ""
_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"

# (status, body) for GET <origin>/robots.txt; status 0 = network error.
RobotsFetch = Callable[[str], Awaitable[tuple[int, str]]]


class RobotsCache:
    def __init__(self, fetch: RobotsFetch, *, respect: bool) -> None:
        self._fetch = fetch
        self._respect = respect
        self._parsed: dict[str, Protego] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _get(self, origin: str) -> Protego:
        if origin in self._parsed:
            return self._parsed[origin]
        async with self._locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._parsed:
                status, body = await self._fetch(origin + "/robots.txt")
                if status == 200:
                    text = body
                elif 400 <= status < 500:
                    text = _ALLOW_ALL  # RFC 9309: unavailable -> allow
                else:
                    text = _DISALLOW_ALL  # unreachable (5xx / network) -> assume disallow
                self._parsed[origin] = Protego.parse(text)
        return self._parsed[origin]

    async def allowed(self, url: str) -> bool:
        if not self._respect:
            return True
        rp = await self._get(origin_of(url))
        return bool(rp.can_fetch(url, UA_TOKEN))

    async def crawl_delay(self, origin: str) -> float | None:
        if not self._respect:
            return None
        delay = (await self._get(origin)).crawl_delay(UA_TOKEN)
        return float(delay) if delay else None

    async def sitemaps(self, origin: str) -> list[str]:
        """Sitemap: lines are hints and are read even when robots is set to ignore."""
        return [str(s) for s in (await self._get(origin)).sitemaps]
