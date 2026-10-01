"""HTTP fetching: scope-guarded transport, manual redirects, retries, politeness."""

import asyncio
import email.utils
import http.cookiejar
import json
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

import httpx

from ximg import __version__
from ximg.config import Config
from ximg.ratelimit import HostLimiter
from ximg.urls import host_of, resolve

Check = Callable[[str], str | None]
Kind = Literal["page", "image", "asset"]

ACCEPT: dict[Kind, str] = {
    "page": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "image": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "asset": "text/css,application/manifest+json,application/json,application/xml,*/*;q=0.5",
}
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10
MAX_RETRY_AFTER = 300.0


class ScopeViolation(Exception):
    """Raised by the transport if anything tries to reach a host outside scope. Always a bug."""


class FetchError(Exception):
    pass


class RedirectDenied(Exception):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"redirect to {url} denied ({reason})")
        self.url = url
        self.reason = reason


class BodyTooLarge(Exception):
    pass


class ScopeGuardTransport(httpx.AsyncBaseTransport):
    """Defense in depth: the last check before any byte leaves the machine."""

    def __init__(self, inner: httpx.AsyncBaseTransport, allow_host: Callable[[str], bool]) -> None:
        self._inner = inner
        self._allow_host = allow_host

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._allow_host(request.url.host):
            raise ScopeViolation(f"refusing out-of-scope request: {request.url}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Opened:
    response: httpx.Response
    final_url: str
    redirects: list[str] = field(default_factory=list)


def user_agent(cfg: Config) -> str:
    if cfg.http.user_agent:
        return cfg.http.user_agent
    contact = cfg.engagement.contact
    return f"ximg/{__version__} (+{contact})" if contact else f"ximg/{__version__}"


def load_cookies(path: Path) -> http.cookiejar.CookieJar:
    """Netscape cookies.txt, or a JSON list of {name, value, domain, path?} (browser-extension export)."""
    text = path.read_text()
    if text.lstrip().startswith(("[", "{")):
        jar = http.cookiejar.CookieJar()
        items = json.loads(text)
        for c in items if isinstance(items, list) else items.get("cookies", []):
            domain = c["domain"]
            jar.set_cookie(http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, domain, True, domain.startswith("."),
                c.get("path", "/"), True, bool(c.get("secure")), None, False, None, None, {},
            ))  # fmt: skip
        return jar
    mjar = http.cookiejar.MozillaCookieJar()
    mjar.load(str(path), ignore_discard=True, ignore_expires=True)
    return mjar


def extra_headers(env_name: str) -> dict[str, str]:
    raw = os.environ.get(env_name, "")
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"${env_name} must be a JSON object of header names to values")
    return {str(k): str(v) for k, v in data.items()}


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    return max(0.0, dt.timestamp() - (now if now is not None else time.time()))


class Fetcher:
    def __init__(
        self,
        cfg: Config,
        *,
        allow_host: Callable[[str], bool],
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: HostLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._retries = cfg.http.retries
        self._sleep = sleep
        self.limiter = limiter or HostLimiter(
            rate=cfg.rate.per_host, jitter=cfg.rate.jitter, per_host=cfg.concurrency.per_host,
            max_bad_streak=cfg.http.max_bad_streak,
        )  # fmt: skip
        self._global = asyncio.Semaphore(cfg.concurrency.global_)
        inner = transport or httpx.AsyncHTTPTransport(http2=True, proxy=cfg.http.proxy or None, retries=0)
        headers = {"User-Agent": user_agent(cfg), **extra_headers(cfg.auth.headers_env)}
        cookies = load_cookies(cfg.auth.cookies_file) if cfg.auth.cookies_file else None
        self._client = httpx.AsyncClient(
            transport=ScopeGuardTransport(inner, allow_host),
            headers=headers,
            cookies=cookies,
            timeout=httpx.Timeout(cfg.http.timeout),
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def open(self, url: str, *, check: Check, kind: Kind) -> AsyncIterator[Opened]:
        """GET `url` and yield the streaming response after redirects and retries.

        Holds the global and per-host concurrency slots until the caller is done
        reading the body. Raises FetchError, RedirectDenied.
        """
        async with self._global:
            attempt = 0
            while True:
                delay = 0.0
                async with AsyncExitStack() as stack:
                    try:
                        opened = await self._follow(stack, url, check, kind)
                    except httpx.TransportError as e:
                        if attempt >= self._retries:
                            raise FetchError(f"{type(e).__name__}: {e}".rstrip(": ")) from e
                        delay = self._backoff(attempt, None)
                    else:
                        status = opened.response.status_code
                        if status in RETRY_STATUSES and attempt < self._retries:
                            delay = self._backoff(attempt, opened.response.headers.get("retry-after"))
                        else:
                            yield opened
                            return
                attempt += 1
                await self._sleep(delay)

    async def _follow(self, stack: AsyncExitStack, url: str, check: Check, kind: Kind) -> Opened:
        current = url
        redirects: list[str] = []
        for _ in range(MAX_REDIRECTS + 1):
            host = host_of(current)
            if host in self.limiter.blocked:
                raise FetchError(f"host_blocked: {host} kept answering 403/429/503")
            hop = AsyncExitStack()
            try:
                await hop.enter_async_context(self.limiter.slot(host))
                request = self._client.build_request("GET", current, headers={"Accept": ACCEPT[kind]})
                response = await self._client.send(request, stream=True)
                hop.push_async_callback(response.aclose)
            except BaseException:
                await hop.aclose()
                raise
            self.limiter.record(host, response.status_code)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                await hop.aclose()
                target = resolve(current, location)
                if target is None:
                    raise FetchError(f"bad redirect location: {location!r}")
                if (reason := check(target)) is not None:
                    raise RedirectDenied(target, reason)
                redirects.append(current)
                current = target
                continue
            await stack.enter_async_context(hop)
            return Opened(response, current, redirects)
        raise FetchError("too_many_redirects")

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        ra = parse_retry_after(retry_after)
        if ra is not None:
            return min(ra, MAX_RETRY_AFTER)
        return min(30.0, 2.0 * float(2**attempt)) * (0.5 + random.random() / 2)


async def read_capped(response: httpx.Response, cap: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise BodyTooLarge(f"body is {declared} bytes, cap is {cap}")
    buf = bytearray()
    async for chunk in response.aiter_bytes():  # decoded: caps gzip bombs too
        buf += chunk
        if len(buf) > cap:
            raise BodyTooLarge(f"body exceeds cap of {cap} bytes")
    return bytes(buf)
