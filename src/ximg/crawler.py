"""The crawl loop: page workers discover, image workers download."""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from ximg.config import Config
from ximg.download import ImageSaver, SaveResult, data_uri_key, decode_data_uri, host_dir
from ximg.extract import Ref
from ximg.extract.css import extract_css
from ximg.extract.html import HtmlOptions, HtmlResult, extract_html
from ximg.extract.sitemap import browserconfig_images, manifest_images, parse_sitemap
from ximg.fetcher import BodyTooLarge, Fetcher, FetchError, Kind, RedirectDenied, read_capped
from ximg.robots import RobotsCache
from ximg.scope import ScopePolicy
from ximg.store import ImageRow, PageRow, Store
from ximg.upgrade import Upgrader
from ximg.urls import host_of, looks_like_non_page, normalize, origin_of, resolve, url_filename, url_pattern

log = logging.getLogger("ximg")

RunStatus = Literal["finished", "limit", "interrupted", "dry_run"]
_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_-]+)""", re.I)
MAX_SITEMAPS = 1000
WAYBACK_HOST = "web.archive.org"


@dataclass
class Stats:
    pages: int = 0
    pages_failed: int = 0
    assets: int = 0
    images_saved: int = 0
    images_dup: int = 0
    images_skipped: int = 0
    images_failed: int = 0
    bytes_saved: int = 0
    rendered: int = 0
    current: set[str] = field(default_factory=set)


@dataclass
class RenderResult:
    html: str
    final_url: str
    network_images: list[str] = field(default_factory=list)
    json_images: list[str] = field(default_factory=list)


class Renderer(Protocol):
    def wants(self, url: str, static: HtmlResult) -> bool: ...

    async def render(self, url: str) -> RenderResult: ...


def decode_html(body: bytes, content_type: str) -> str:
    charset = None
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    if not charset and (m := _CHARSET.search(body[:4096])):
        charset = m.group(1).decode("ascii")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class Crawler:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        renderer: Renderer | None = None,
        dry_run: bool = False,
        image_list: list[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.scope = ScopePolicy(cfg.scope)
        self.extra_hosts = {WAYBACK_HOST} if cfg.wayback.mode == "archive" else set()
        self.fetcher = Fetcher(cfg, allow_host=self._allow_host, transport=transport)
        if self.extra_hosts:
            self.fetcher.limiter.set_min_interval(WAYBACK_HOST, 1 / cfg.wayback.rate)
        self.robots = RobotsCache(self._fetch_robots, respect=cfg.robots == "respect")
        self.saver = ImageSaver(store, cfg.filters, cfg.limits)
        self.upgrader = Upgrader(cfg.upgrade_rules, cfg.upgrade)
        self.renderer = renderer
        self.dry_run = dry_run
        self.image_list = image_list
        self.html_opts = HtmlOptions(
            lazy_attrs=frozenset(a.lower() for a in cfg.discovery.lazy_attrs),
            srcset=cfg.filters.srcset,
            data_uris=cfg.filters.data_uris,
            css=cfg.discovery.css,
            jsonld=cfg.discovery.jsonld,
            manifest=cfg.discovery.manifest,
        )
        self.stats = Stats()
        self._stopping = False
        self._stop_reason: RunStatus | None = None
        self._wake = asyncio.Event()
        self._pages_in_flight = 0
        self._images_in_flight = 0
        self._pages_done = False
        self._delay_applied: set[str] = set()
        self.on_event: Callable[[str, dict[str, object]], None] = lambda _e, _d: None

    def _allow_host(self, host: str) -> bool:
        return self.scope.any_host(host) or host in self.extra_hosts

    # -- lifecycle ---------------------------------------------------------------------
    def request_stop(self) -> None:
        """Graceful stop: finish in-flight work, claim nothing new."""
        self._stopping = True
        self._stop_reason = self._stop_reason or "interrupted"
        self._wake.set()

    async def run(self) -> RunStatus:
        recovered = self.store.recover()
        if any(recovered.values()):
            log.info("resuming", extra={"data": recovered})
        run_id = self.store.start_run()
        status: RunStatus = "interrupted"
        try:
            async with self.fetcher:
                if self.store.meta("seeded") != "1":
                    await self._seed()
                n = self.cfg.concurrency.global_
                page_workers = [asyncio.create_task(self._page_worker()) for _ in range(n)]
                image_workers = [] if self.dry_run else [asyncio.create_task(self._image_worker()) for _ in range(n)]
                try:
                    await asyncio.gather(*page_workers)
                    self._pages_done = True
                    self._wake.set()
                    await asyncio.gather(*image_workers)
                finally:
                    for t in (*page_workers, *image_workers):
                        t.cancel()
            status = self._final_status()
            return status
        finally:
            self.store.end_run(run_id, status)

    def _final_status(self) -> RunStatus:
        if self._stop_reason:
            return self._stop_reason
        if self.dry_run:
            return "dry_run"
        if self.store.queued_pages() or self.store.queued_images():
            return "limit"
        return "finished"

    # -- seeding -----------------------------------------------------------------------
    async def _seed(self) -> None:
        with self.store.tx():
            if self.image_list is not None:
                for raw in self.image_list:
                    if url := normalize(raw):
                        self._add_image_ref(Ref(url, "list"), None, source="list")
            else:
                for seed in self.cfg.seeds:
                    if url := normalize(seed, strip_params=self.cfg.http.strip_page_params):
                        self.store.add_page(url, depth=0, pattern=url_pattern(url))
        if self.image_list is None and self.cfg.discovery.sitemaps:
            for origin in dict.fromkeys(origin_of(s) for s in self.cfg.seeds):
                listed = await self.robots.sitemaps(origin)
                with self.store.tx():
                    for sm in listed or [origin + "/sitemap.xml"]:
                        if (url := normalize(sm)) and self.scope.page(url) is None:
                            self.store.add_page(url, kind="sitemap", depth=0)
        self.store.set_meta("seeded", "1")

    async def _fetch_robots(self, url: str) -> tuple[int, str]:
        if self.scope.asset(url) is not None:
            return 404, ""
        try:
            async with self.fetcher.open(url, check=self.scope.asset, kind="asset") as op:
                if op.response.status_code != 200:
                    return op.response.status_code, ""
                body = await read_capped(op.response, 512 * 1024)
                return 200, body.decode("utf-8", errors="replace")
        except RedirectDenied:
            return 404, ""
        except FetchError, BodyTooLarge, httpx.HTTPError:
            return 0, ""

    async def _robots_ok(self, url: str) -> bool:
        origin = origin_of(url)
        if origin not in self._delay_applied:
            self._delay_applied.add(origin)
            if delay := await self.robots.crawl_delay(origin):
                self.fetcher.limiter.set_min_interval(host_of(url), delay)
        return await self.robots.allowed(url)

    # -- waiting -----------------------------------------------------------------------
    async def _wait(self) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), 0.5)

    # -- pages -------------------------------------------------------------------------
    async def _page_worker(self) -> None:
        while not self._stopping:
            allow_pages = self.store.pages_started() < self.cfg.limits.max_pages
            row = self.store.claim_page(allow_pages=allow_pages)
            if row is None:
                if self._pages_in_flight == 0:
                    return
                await self._wait()
                continue
            self._pages_in_flight += 1
            self.stats.current.add(row.url)
            try:
                await self._process_page(row)
            except Exception as e:
                log.exception("page_error", extra={"data": {"url": row.url}})
                self.store.finish_page(row.id, "failed", error=f"internal: {type(e).__name__}: {e}")
            finally:
                self._pages_in_flight -= 1
                self.stats.current.discard(row.url)
                self._wake.set()

    async def _process_page(self, row: PageRow) -> None:
        if not await self._robots_ok(row.url):
            self.store.finish_page(row.id, "skipped", skip_reason="robots")
            return
        check = self.scope.page if row.kind in ("page", "sitemap") else self.scope.asset
        kind: Kind = "page" if row.kind == "page" else "asset"
        try:
            async with self.fetcher.open(row.url, check=check, kind=kind) as op:
                resp = op.response
                ctype = resp.headers.get("content-type", "").lower()
                final = normalize(op.final_url) or op.final_url
                if resp.status_code in (404, 410) and row.kind != "page":
                    # e.g. the guessed /sitemap.xml: absent, not broken
                    self.store.finish_page(row.id, "skipped", skip_reason="not_found", status=resp.status_code)
                    return
                if resp.status_code != 200:
                    self._page_failed(row, f"HTTP {resp.status_code}", status=resp.status_code, final_url=final)
                    return
                if row.kind == "page" and ctype.startswith("image/"):
                    await self._page_is_image(row, op.response, final)
                    return
                body = await read_capped(resp, self.cfg.limits.max_page_bytes)
        except RedirectDenied as e:
            self.store.finish_page(row.id, "skipped", skip_reason=f"{e.reason}:redirect", final_url=e.url)
            return
        except (FetchError, BodyTooLarge, httpx.HTTPError) as e:
            self._page_failed(row, f"{type(e).__name__}: {e}")
            return

        render_static: HtmlResult | None = None
        with self.store.tx():
            if row.kind == "page":
                if "html" in ctype or (not ctype and body.lstrip()[:1] == b"<"):
                    html = decode_html(body, ctype)
                    result = extract_html(html, final, self.html_opts)
                    if self.renderer is not None and self.renderer.wants(final, result):
                        render_static = result
                    else:
                        self._handle_html(row, result)
                if final != row.url:
                    self.store.add_page(final, depth=row.depth, parent=row.id, skip_reason="duplicate:redirect")
            elif row.kind == "sitemap":
                self._handle_sitemap(row, body, final)
            elif row.kind == "css":
                self._handle_css(row, decode_html(body, ctype), final)
            elif row.kind == "manifest":
                for raw, k in manifest_images(body):
                    self._add_raw_image(raw, final, row.id, k)
            elif row.kind == "browserconfig":
                for raw in browserconfig_images(body):
                    self._add_raw_image(raw, final, row.id, "browserconfig:tile")
        rendered = render_static is not None and await self._render_page(row, final, render_static)
        self.store.finish_page(
            row.id, "done", status=200, final_url=final, content_type=ctype[:100], rendered=int(rendered)
        )
        if row.kind == "page":
            self.stats.pages += 1
        else:
            self.stats.assets += 1
        self._wake.set()

    def _page_failed(self, row: PageRow, error: str, **fields: object) -> None:
        self.store.finish_page(row.id, "failed", error=error[:500], **fields)
        if row.kind == "page":
            self.stats.pages_failed += 1
        log.info("page_failed", extra={"data": {"url": row.url, "error": error}})

    async def _render_page(self, row: PageRow, url: str, static: HtmlResult) -> bool:
        """Render with the browser; on failure fall back to the static extraction. True if rendered."""
        assert self.renderer is not None
        try:
            rr = await self.renderer.render(url)
        except Exception as e:
            log.warning("render_failed", extra={"data": {"url": url, "error": f"{type(e).__name__}: {e}"}})
            with self.store.tx():
                self._handle_html(row, static)
            return False
        self.stats.rendered += 1
        with self.store.tx():
            self._handle_html(row, extract_html(rr.html, rr.final_url or url, self.html_opts))
            for img in rr.network_images:
                if n := normalize(img):
                    self._add_image_ref(Ref(n, "render:network"), row.id)
            for raw in rr.json_images:
                self._add_raw_image(raw, url, row.id, "render:json")
            # Static refs too: the rendered DOM can drop things (e.g. <noscript> fallbacks).
            for ref in static.images:
                self._add_image_ref(ref, row.id)
        return True

    def _handle_html(self, row: PageRow, res: HtmlResult) -> None:
        for ref in res.images:
            self._add_image_ref(ref, row.id)
        if row.depth < self.cfg.limits.max_depth:
            for link in res.links:
                self._enqueue_page(link, row.depth + 1, row.id)
        for css in res.stylesheets:
            self._enqueue_asset(css, "css", row)
        for m in res.manifests:
            self._enqueue_asset(m, "manifest", row)
        for b in res.browserconfigs:
            self._enqueue_asset(b, "browserconfig", row)

    def _handle_sitemap(self, row: PageRow, body: bytes, final: str) -> None:
        try:
            sm = parse_sitemap(body)
        except ValueError as e:
            log.info("sitemap_error", extra={"data": {"url": row.url, "error": str(e)}})
            return
        for child in sm.sitemaps:
            url = resolve(final, child)
            if url and self.scope.page(url) is None and self._count_kind("sitemap") < MAX_SITEMAPS:
                self.store.add_page(url, kind="sitemap", depth=row.depth, parent=row.id)
        if self.cfg.limits.max_depth >= 1:
            for page in sm.pages:
                self._enqueue_page(page, 1, row.id)
        for img in sm.images:
            self._add_raw_image(img, final, row.id, "sitemap:image")

    def _handle_css(self, row: PageRow, text: str, final: str) -> None:
        res = extract_css(text, srcset_mode=self.cfg.filters.srcset)
        for raw, kind in res.images:
            self._add_raw_image(raw, final, row.id, kind)
        for imp in res.imports:
            if url := resolve(final, imp):
                self._enqueue_asset(url, "css", row)

    def _count_kind(self, kind: str) -> int:
        return int(self.store.conn.execute("SELECT COUNT(*) FROM page WHERE kind=?", (kind,)).fetchone()[0])

    def _enqueue_page(self, raw: str, depth: int, parent: int) -> None:
        url = normalize(raw, strip_params=self.cfg.http.strip_page_params)
        if url is None or looks_like_non_page(url) or depth > self.cfg.limits.max_depth:
            return
        if self.scope.page(url) is not None:
            return
        pattern = url_pattern(url)
        if self.store.pattern_count(pattern) >= self.cfg.limits.max_pages_per_pattern:
            self.store.add_page(url, depth=depth, parent=parent, pattern=pattern, skip_reason="pattern_limit")
            return
        self.store.add_page(url, depth=depth, parent=parent, pattern=pattern)

    def _enqueue_asset(self, url: str, kind: str, row: PageRow) -> None:
        if self.scope.asset(url) is None:
            self.store.add_page(url, kind=kind, depth=row.depth, parent=row.id)

    async def _page_is_image(self, row: PageRow, response: httpx.Response, final: str) -> None:
        """A 'page' link that actually serves an image: save it from this response."""
        reason = self.scope.image(final)
        image_id, new = self.store.add_image(final, skip_reason=reason)
        parent = self.store.conn.execute("SELECT discovered_from FROM page WHERE id=?", (row.id,)).fetchone()
        self.store.add_occurrence(image_id, parent[0] if parent else None, "page:direct", row.url)
        if new and reason is None and not self.dry_run:
            self.store.finish_image(image_id, "in_progress")
            await self._save_response(ImageRow(image_id, final, "live"), response, final)
        self.store.finish_page(row.id, "done", status=200, final_url=final, content_type="image")
        self._wake.set()

    # -- images ------------------------------------------------------------------------
    def _add_raw_image(self, raw: str, base: str, page_id: int | None, kind: str) -> None:
        raw = raw.strip()
        if raw.startswith("data:"):
            if self.cfg.filters.data_uris:
                self._add_image_ref(Ref(raw, kind), page_id)
            return
        if url := resolve(base, raw):
            self._add_image_ref(Ref(url, kind), page_id)

    def _add_image_ref(self, ref: Ref, page_id: int | None, *, source: str = "live") -> None:
        if ref.url.startswith("data:"):
            self._add_data_uri(ref, page_id)
            return
        reason = self.scope.image(ref.url)
        image_id, _ = self.store.add_image(ref.url, source=source, skip_reason=reason)
        self.store.add_occurrence(image_id, page_id, ref.kind, ref.context)
        for upgraded in self.upgrader.variants(ref.url):
            if n := normalize(upgraded):
                up_id, _ = self.store.add_image(n, source=source, skip_reason=self.scope.image(n))
                self.store.add_occurrence(up_id, page_id, ref.kind + "+upgrade", ref.context)
        self._wake.set()

    def _add_data_uri(self, ref: Ref, page_id: int | None) -> None:
        decoded = decode_data_uri(ref.url)
        if decoded is None:
            return
        mime, data = decoded
        key = data_uri_key(mime, data)
        image_id, new = self.store.add_image(key, source="data-uri")
        self.store.add_occurrence(image_id, page_id, ref.kind + "(data-uri)", ref.context)
        if new:
            result = self.saver.save_bytes(data, host_folder="_data-uri")
            self._record_result(image_id, result, status=None)

    async def _image_worker(self) -> None:
        while not self._stopping:
            if self.store.total_bytes() >= self.cfg.limits.max_total_bytes:
                log.warning("budget_reached", extra={"data": {"max_total_bytes": self.cfg.limits.max_total_bytes}})
                self._stop_reason = "limit"
                self._stopping = True
                self._wake.set()
                return
            row = self.store.claim_image()
            if row is None:
                if self._pages_done and self._images_in_flight == 0:
                    return
                await self._wait()
                continue
            self._images_in_flight += 1
            self.stats.current.add(row.url)
            try:
                await self._process_image(row)
            except Exception as e:
                log.exception("image_error", extra={"data": {"url": row.url}})
                self.store.finish_image(row.id, "failed", error=f"internal: {type(e).__name__}: {e}")
                self.stats.images_failed += 1
            finally:
                self._images_in_flight -= 1
                self.stats.current.discard(row.url)
                self._wake.set()

    async def _process_image(self, row: ImageRow) -> None:
        check = self.scope.image
        if row.source == "wayback-archive":
            check = _wayback_only  # archived copies live on web.archive.org; redirects must stay there
        elif not await self._robots_ok(row.url):
            self._skip_image(row.id, "robots")
            return
        try:
            async with self.fetcher.open(row.url, check=check, kind="image") as op:
                final = normalize(op.final_url) or op.final_url
                await self._save_response(row, op.response, final)
        except RedirectDenied as e:
            self.store.finish_image(row.id, "skipped", skip_reason=f"{e.reason}:redirect", final_url=e.url)
            self.stats.images_skipped += 1
        except (FetchError, httpx.HTTPError) as e:
            self.store.finish_image(row.id, "failed", error=f"{type(e).__name__}: {e}"[:500])
            self.stats.images_failed += 1

    async def _save_response(self, row: ImageRow, resp: httpx.Response, final: str) -> None:
        headers = dict(resp.headers.items())
        common = {
            "status": resp.status_code,
            "final_url": final,
            "content_type": resp.headers.get("content-type", "")[:100],
            "headers_json": json.dumps(headers),
            "url_filename": _disposition_filename(resp.headers.get("content-disposition")) or url_filename(final),
        }
        if resp.status_code != 200:
            self.store.finish_image(row.id, "failed", error=f"HTTP {resp.status_code}", **common)
            self.stats.images_failed += 1
            return
        declared = resp.headers.get("content-length")
        if reason := self.saver.precheck(int(declared) if declared and declared.isdigit() else None):
            self._skip_image(row.id, reason, **common)
            return
        parts = urlsplit(final)
        folder = host_dir(parts.hostname or "_", parts.port)
        if row.source == "wayback-archive":
            folder = "_wayback"
        result = await self.saver.save_stream(resp.aiter_bytes(), host_folder=folder)
        self._record_result(row.id, result, **common)

    def _record_result(self, image_id: int, result: SaveResult, **common: object) -> None:
        if result.stored:
            self.store.finish_image(image_id, "done", sha256=result.sha256, **common)
            if result.outcome == "saved":
                self.stats.images_saved += 1
                self.stats.bytes_saved += result.size
            else:
                self.stats.images_dup += 1
        else:
            self._skip_image(image_id, result.skip_reason or "skipped", **common)

    def _skip_image(self, image_id: int, reason: str, **fields: object) -> None:
        self.store.finish_image(image_id, "skipped", skip_reason=reason, **fields)
        self.stats.images_skipped += 1


def _wayback_only(url: str) -> str | None:
    return None if host_of(url) == WAYBACK_HOST else "scope"


def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", value, re.I)
    return m.group(1).strip()[:255] if m else None
