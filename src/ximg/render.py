"""Headless Chromium rendering for JS-built pages (optional extra: `ximg[render]`).

The browser never downloads images itself: image requests are recorded and answered with a
1x1 placeholder (so lazy-loaders keep working), and the normal downloader fetches each image
once, through the rate limiter and scope checks. Requests to out-of-scope hosts are aborted.
"""

import asyncio
import base64
import contextlib
import re
from fnmatch import fnmatchcase
from typing import Any, Self

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Request, Response, Route, async_playwright

from ximg.config import Config
from ximg.crawler import RenderResult
from ximg.extract.html import HtmlResult
from ximg.fetcher import extra_headers, load_cookies, user_agent
from ximg.scope import ScopePolicy
from ximg.urls import host_of, looks_like_image, path_and_query, resolve

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_JSON_IMAGE = re.compile(r'"((?:https?:)?/?/?[^"\s]+?\.(?:jpe?g|png|gif|webp|avif|svg)(?:\?[^"\s]*)?)"', re.I)
SPA_TEXT_THRESHOLD = 500


class PlaywrightRenderer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.scope = ScopePolicy(cfg.scope)
        self._sem = asyncio.Semaphore(cfg.render.pages)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.blocked_requests: list[str] = []

    async def __aenter__(self) -> Self:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=user_agent(self.cfg),
            extra_http_headers=extra_headers(self.cfg.auth.headers_env),
            service_workers="block",
            viewport={"width": 1366, "height": 900},
        )
        if self.cfg.auth.cookies_file:
            await self._context.add_cookies(_playwright_cookies(self.cfg))
        return self

    async def __aexit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()

    def wants(self, url: str, static: HtmlResult) -> bool:
        mode = self.cfg.render.mode
        if mode == "always":
            return True
        if mode == "never":
            return False
        pq = path_and_query(url)
        if any(fnmatchcase(pq, p) for p in self.cfg.render.patterns):
            return True
        if static.mount_nodes and static.text_chars < SPA_TEXT_THRESHOLD:
            return True
        return static.noscript_js and len(static.images) < 3

    async def render(self, url: str) -> RenderResult:
        assert self._context is not None, "use `async with renderer:`"
        async with self._sem:
            page = await self._context.new_page()
            images: list[str] = []
            json_images: list[str] = []
            json_tasks: list[asyncio.Task[None]] = []

            async def on_route(route: Route, request: Request) -> None:
                req_url = request.url
                if req_url.startswith(("data:", "blob:")):
                    await route.continue_()
                    return
                if not self.scope.any_host(host_of(req_url)):
                    self.blocked_requests.append(req_url)
                    await route.abort("blockedbyclient")
                    return
                rtype = request.resource_type
                if rtype == "image" or (
                    rtype not in ("document", "script", "stylesheet") and looks_like_image(req_url)
                ):
                    images.append(req_url)
                    await route.fulfill(status=200, content_type="image/gif", body=PIXEL)
                    return
                if rtype in ("media", "font", "websocket", "eventsource"):
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            def on_response(response: Response) -> None:
                if response.request.resource_type in ("xhr", "fetch") and "json" in (
                    response.headers.get("content-type", "")
                ):
                    json_tasks.append(asyncio.ensure_future(_scan_json(response, json_images)))

            await page.route("**/*", on_route)
            if self.cfg.render.scan_json:
                page.on("response", on_response)
            try:
                timeout_ms = self.cfg.http.timeout * 1000
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await _settle(page, self.cfg.render.idle_timeout)
                await _scroll(page, self.cfg.render.max_scrolls)
                await _settle(page, min(self.cfg.render.idle_timeout, 3))
                dom_images: list[str] = await page.evaluate(
                    "() => Array.from(document.images).map(i => i.currentSrc || i.src).filter(Boolean)"
                )
                images.extend(u for u in dom_images if not u.startswith("data:"))
                html = await page.content()
                final = page.url
                if json_tasks:
                    await asyncio.gather(*json_tasks, return_exceptions=True)
            finally:
                await page.close()
        return RenderResult(
            html=html,
            final_url=final,
            network_images=list(dict.fromkeys(images)),
            json_images=list(dict.fromkeys(json_images)),
        )


async def _settle(page: Page, seconds: float) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=seconds * 1000)


async def _scroll(page: Page, max_scrolls: int) -> None:
    """Scroll a viewport at a time until the page stops growing (infinite scroll / lazy loading)."""
    stable = 0
    last_height = -1
    for _ in range(max_scrolls):
        state: dict[str, Any] = await page.evaluate(
            """() => { window.scrollBy(0, window.innerHeight);
                       const h = document.documentElement.scrollHeight;
                       return {h, bottom: window.scrollY + window.innerHeight >= h - 2}; }"""
        )
        await page.wait_for_timeout(250)
        if state["bottom"] and state["h"] == last_height:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_height = state["h"]


async def _scan_json(response: Response, out: list[str]) -> None:
    with contextlib.suppress(Exception):
        text = await response.text()
        for m in _JSON_IMAGE.finditer(text[:5_000_000]):
            raw = m.group(1).replace("\\/", "/")
            if url := resolve(response.url, raw):
                out.append(url)


def _playwright_cookies(cfg: Config) -> list[Any]:
    assert cfg.auth.cookies_file is not None
    return [
        {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
        }
        for c in load_cookies(cfg.auth.cookies_file)
    ]
