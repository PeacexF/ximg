# Architecture

```
seeds / sitemaps ──► page queue (SQLite) ──► page workers ──► fetch (scope + robots + rate limit)
                          ▲                        │
                          │ links, stylesheets,    ├─ HTML → extractors ──┐
                          │ manifests, sitemaps    ├─ CSS / sitemap / manifest extractors
                          │                        └─ (SPA?) → Chromium renderer
                          │                                               │ image refs
                          └──────────────────────────────── scope ◄───────┘
                                                              │
                                     image queue (SQLite) ◄───┘
                                              │
                                       image workers ──► fetch ──► stream to temp + SHA-256 + sniff
                                                                     └─► images/<host>/<sha256>.<ext>
```

One process, one asyncio loop, one SQLite connection (WAL) used only from the loop thread. That makes every DB call atomic with respect to the other tasks and avoids lock contention.

## Modules (`src/ximg/`)

| Module | Role |
|---|---|
| `cli.py` | typer commands, Ctrl-C handling (1st: graceful stop, 2nd: abort), live progress line |
| `config.py` | pydantic models, TOML loading, size parsing, dotted CLI overrides |
| `scope.py` | `page()` / `image()` / `asset()` checks returning a denial reason or `None` |
| `urls.py` | normalization, resolution, trap patterns, extension heuristics |
| `fetcher.py` | httpx client, `ScopeGuardTransport`, manual redirects, retries/backoff, body caps |
| `ratelimit.py` | per-host semaphore + spacing with jitter, Crawl-delay, bad-status streaks → slow down / block host |
| `robots.py` | RFC 9309 robots.txt cache (Protego) |
| `extract/` | pure extractors: `html`, `srcset`, `css`, `jsonld`, `sitemap` (+ manifest/browserconfig) |
| `crawler.py` | orchestration: seeding, page/image worker pools, dispatch, status |
| `download.py` | streaming save: temp file, SHA-256, magic-byte sniff, filters, dedupe, atomic rename; `data:` URIs |
| `sniff.py` | magic-byte detection for jpeg/png/gif/webp/avif/heic/svg/ico/bmp/tiff/jxl |
| `store.py` | output dir lifecycle (new / resume / refuse / overwrite), schema, recovery |
| `render.py` | Playwright renderer (optional extra) |
| `upgrade.py` | "original resolution" URL rewrite rules |
| `wayback.py` | Wayback Machine CDX passive source |
| `export.py` | `manifest.csv`, `manifest.json`, `urls.csv`, friendly hardlink tree |

## Crawl loop

- **Page workers** (N = `concurrency.global`) claim queued rows from `page`. Assets come first, then pages in BFS order (`depth, id`). Each row is a page, sitemap, stylesheet, web manifest or browserconfig. `max_pages` counts HTML pages only.
- **Image workers** (another N) claim rows from `image_url`. Separate pools mean slow image hosts can't starve discovery, and vice versa. The global semaphore still caps total requests.
- Workers wait on an event that fires whenever new work is queued, with a 0.5 s timeout as a safety net. Page workers exit when nothing is queued or in flight. Image workers exit when pages are done and nothing is queued or in flight.
- **Final status:** `finished` (nothing left), `limit` (a page/byte budget stopped it; raise and re-run), `interrupted`, or `dry_run`. Every status except `finished` can be resumed.

## Fetching

- Every request goes through `ScopeGuardTransport`, which raises if the host isn't in scope. That's the last check before bytes leave the machine, and it applies even to bugs elsewhere.
- Redirects are followed manually (max 10). Each hop is checked against the scope for that request type. A denied hop is recorded as `scope:redirect`.
- The per-host slot (concurrency + spacing) is held for the whole response body, so `per_host = 2` really means two concurrent transfers.
- Retries happen on transport errors and 429/500/502/503/504, using exponential backoff with jitter or `Retry-After` (capped at 5 min).
- 15 consecutive 403/429/503 responses from a host stop all requests to it (`host_blocked`). Every 3rd bad response doubles the spacing for that host. ximg never rotates IPs, User-Agents or proxies to get around blocking.

## Saving images

1. Skip early if `Content-Length` is above the max or below the min.
2. Stream into `.ximg/tmp/<uuid>.part`, hashing as we go. Abort past `max_file_bytes` (this catches missing or wrong `Content-Length` and decompression bombs, since the cap applies to decoded bytes).
3. Sniff the first 4 KB. HTML error pages served as images are rejected (`not_image`).
4. Apply the format and min-size filters.
5. With no await between check and insert: if the SHA-256 is known, link the URL to the existing file; otherwise `os.replace` it into `images/<host>/<sha256>.<ext>` and insert the `file` row.

Files are never decoded or rendered, so image-parser vulnerabilities don't apply to ximg itself.

## Resumability

At startup, `in_progress` rows go back to `queued`, `.ximg/tmp` is emptied, and any file in `images/` without a `file` row (left by a crash between rename and commit) is deleted. Its URL is requeued and gets re-downloaded. Tests verify that a run cancelled mid-way and then resumed produces exactly the same files and DB states as an uninterrupted run.

## Rendering

`render.mode = auto | always` uses one headless Chromium with `render.pages` tabs. For each rendered page:
- requests to out-of-scope hosts are aborted
- image requests are recorded and answered with a 1×1 GIF, so lazy loaders keep working without the browser downloading images
- media, fonts and websockets are aborted
- the page is scrolled a viewport at a time until its height stops changing
- the final DOM goes through the normal HTML extractor, and recorded image URLs are queued as `render:network`
- with `scan_json`, XHR/fetch JSON bodies are scanned for image URLs (`render:json`)

If rendering fails, the static extraction is used instead.

## Database schema (`.ximg/ximg.db`)

| Table | Key columns |
|---|---|
| `meta` | `status`, `config_json`, `scope_key`, `seeded`, `created_at` |
| `run` | one row per invocation: `started_at`, `ended_at`, `status` |
| `page` | `url` (unique), `kind` (page/sitemap/css/manifest/browserconfig), `depth`, `discovered_from`, `pattern`, `state`, `skip_reason`, `status`, `final_url`, `rendered`, `error` |
| `image_url` | `url` (unique; `data:` URIs stored as `data:<mime>;sha256=<hash>`), `state`, `skip_reason`, `sha256`, `final_url`, `status`, `content_type`, `headers_json`, `url_filename`, `source` (live/list/wayback/wayback-archive/data-uri) |
| `file` | `sha256` (pk), `path`, `bytes`, `format`, `first_seen` |
| `occurrence` | `image_url_id`, `page_id` (nullable), `kind` (e.g. `html:img@srcset`, `css:url`, `render:network`), `context` (alt text etc.) |

`occurrence.page_id` points at whatever referenced the image: an HTML page, a stylesheet, a sitemap or a manifest. That row's `discovered_from` leads back to the page that linked it.
