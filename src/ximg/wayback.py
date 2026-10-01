"""Passive source: historical image URLs from the Wayback Machine CDX API.

mode = "live":    queue the historical URLs against the *live* site (scope applies). Images that
                  were unlinked but are still hosted are a classic recon find.
mode = "archive": queue the archived copies on web.archive.org (no traffic to the target).
"""

import logging

import httpx

from ximg.config import Config
from ximg.download import host_dir
from ximg.fetcher import user_agent
from ximg.scope import ScopePolicy
from ximg.store import Store
from ximg.urls import normalize

log = logging.getLogger("ximg")
CDX_URL = "https://web.archive.org/cdx/search/cdx"


async def fetch_cdx(cfg: Config, domain: str, *, transport: httpx.AsyncBaseTransport | None = None) -> list[list[str]]:
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("url", f"{domain}/*"),
        ("output", "json"),
        ("fl", "original,timestamp,mimetype"),
        ("filter", "mimetype:image/.*"),
        ("filter", "statuscode:200"),
        ("collapse", "digest"),
        ("limit", cfg.wayback.limit),
    ]
    async with httpx.AsyncClient(
        transport=transport, headers={"User-Agent": user_agent(cfg)}, timeout=120, follow_redirects=True
    ) as client:
        resp = await client.get(CDX_URL, params=params)
        resp.raise_for_status()
        rows = resp.json() if resp.content.strip() else []
    if not rows:
        return []
    header, *data = rows
    idx = {name: i for i, name in enumerate(header)}
    return [[r[idx["original"]], r[idx["timestamp"]], r[idx["mimetype"]]] for r in data]


async def queue_wayback(
    cfg: Config, store: Store, domain: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[int, int]:
    rows = await fetch_cdx(cfg, domain, transport=transport)
    scope = ScopePolicy(cfg.scope)
    added = skipped = 0
    with store.tx():
        for original, timestamp, _mime in rows:
            url = normalize(original if "://" in original else f"http://{original}")
            if url is None:
                skipped += 1
                continue
            if cfg.wayback.mode == "archive":
                archived = f"https://web.archive.org/web/{timestamp}id_/{url}"
                image_id, new = store.add_image(archived, source="wayback-archive")
            else:
                reason = scope.image(url)
                image_id, new = store.add_image(url, source="wayback", skip_reason=reason)
                new = new and reason is None
            store.add_occurrence(image_id, None, "wayback:cdx", f"{timestamp} {host_dir(domain)}")
            if new:
                added += 1
            else:
                skipped += 1
    log.info("wayback_queued", extra={"data": {"domain": domain, "rows": len(rows), "added": added}})
    return added, skipped
