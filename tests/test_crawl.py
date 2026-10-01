"""Full crawls of the fixture site."""

import csv
import hashlib
import json
from pathlib import Path

from tests.conftest import image_rows, kinds_for, make_cfg, run_crawl
from tests.fixtures.site import (
    DATA_URI_PNG,
    DUPLICATES,
    EXPECTED_NOT_SAVED,
    EXPECTED_SAVED,
    NOT_REQUESTED,
    Site,
    build_site,
)
from ximg.download import data_uri_key


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _served_body(site: Site, url: str) -> bytes:
    if url == "http://site.test/img/redirect-ok.png":
        url = "http://site.test/img/redirected-target.png"
    return site.body(url)


async def test_every_planted_image_is_saved_byte_identical(tmp_path: Path, site: Site) -> None:
    status, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    assert status == "finished"  # pattern-limited trap pages are skipped, not left queued
    rows = image_rows(store)

    missing = [u for u in EXPECTED_SAVED if rows.get(u, {}).get("state") != "done"]
    assert not missing, f"not saved: {missing}\n{[(u, rows.get(u)) for u in missing]}"

    for url, kind in EXPECTED_SAVED.items():
        row = rows[url]
        path = store.out / store.file_path(row["sha256"])  # type: ignore[operator]
        assert path.read_bytes() == _served_body(site, url), url
        assert kind in kinds_for(store, url), (url, kinds_for(store, url))

    # data: URI decoded and saved
    key = data_uri_key("image/png", DATA_URI_PNG)
    assert rows[key]["state"] == "done"
    assert rows[key]["sha256"] == _sha(DATA_URI_PNG)


async def test_files_are_deduplicated_and_laid_out_per_host(tmp_path: Path, site: Site) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    rows = image_rows(store)
    shas = {rows[u]["sha256"] for u in DUPLICATES}
    assert len(shas) == 1
    files = sorted(p.relative_to(store.out).as_posix() for p in (store.out / "images").rglob("*") if p.is_file())
    # one file per unique content
    unique_contents = {_sha(_served_body(site, u)) for u in EXPECTED_SAVED} | {_sha(DATA_URI_PNG)}
    assert len(files) == len(unique_contents)
    for f in files:
        host, name = f.split("/")[1:]
        assert host in ("site.test", "cdn.test", "_data-uri")
        stem, ext = name.rsplit(".", 1)
        assert len(stem) == 64 and ext in ("png", "jpg", "webp", "avif", "ico", "svg")
    # extensions come from sniffing, not the URL
    assert rows["http://site.test/img/object.svg"]["sha256"]
    assert store.file_path(rows["http://site.test/img/object.svg"]["sha256"]).endswith(".svg")  # type: ignore[union-attr]
    assert not list((store.out / ".ximg" / "tmp").iterdir())


async def test_skips_and_failures_are_recorded_with_reasons(tmp_path: Path, site: Site) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    rows = image_rows(store)
    for url, (state, reason) in EXPECTED_NOT_SAVED.items():
        assert rows[url]["state"] == state, (url, rows[url])
        if reason:
            assert rows[url]["skip_reason"] == reason, (url, rows[url])
    assert not list((store.out / "images").rglob("*huge*"))


async def test_never_leaves_scope_or_requests_unwanted_urls(tmp_path: Path, site: Site) -> None:
    await run_crawl(make_cfg(tmp_path / "out"), site)
    assert site.hosts_requested() <= {"site.test", "cdn.test"}
    requested = {p for _, p in site.requests}
    for path in NOT_REQUESTED:
        assert path not in requested, path
    assert "/img/commented-out.png" not in requested
    assert "/docs/file.pdf" not in requested
    # each URL fetched once (no duplicate downloads for repeated references)
    image_requests = [p for h, p in site.requests if p.startswith("/img/")]
    assert len(image_requests) == len(set(image_requests))


async def test_crawler_trap_is_capped_by_pattern_limit(tmp_path: Path, site: Site) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    cal = store.conn.execute("SELECT state, skip_reason FROM page WHERE url LIKE '%/calendar/%'").fetchall()
    done = [r for r in cal if r[0] == "done"]
    skipped = [r for r in cal if r[1] == "pattern_limit"]
    assert len(done) == 3 and len(skipped) == 1


async def test_pages_robots_redirects_and_sitemaps(tmp_path: Path, site: Site) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    pages = {r["url"]: dict(r) for r in store.conn.execute("SELECT * FROM page")}
    assert pages["http://site.test/private/secret.html"]["skip_reason"] == "robots"
    assert pages["http://site.test/redirect-out"]["skip_reason"] == "scope:redirect"
    assert pages["http://site.test/sitemap-only.html"]["state"] == "done"
    assert pages["http://site.test/sitemap_index.xml"]["kind"] == "sitemap"
    assert "http://evil.test/sitemap.xml" not in pages
    assert "http://site.test/?utm_source=x" not in pages  # tracking param stripped -> same as /


async def test_exports(tmp_path: Path, site: Site) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    with (store.out / "manifest.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    by_url = {r["image_url"] for r in rows}
    assert set(EXPECTED_SAVED) <= by_url
    plain = next(r for r in rows if r["image_url"] == "http://site.test/img/plain.png")
    assert plain["page_url"] == "http://site.test/" and plain["kind"] == "html:img@src"
    assert plain["context"] == "Plain image"
    assert (store.out / plain["file"]).exists()
    doc = json.loads((store.out / "manifest.json").read_text())
    dup = next(f for f in doc["files"] if len(f["urls"]) == 3)
    assert {u["url"] for u in dup["urls"]} == DUPLICATES
    with (store.out / "urls.csv").open() as fh:
        urls = {r["image_url"]: r for r in csv.DictReader(fh)}
    assert urls["http://evil.test/outside.png"]["skip_reason"] == "scope"


async def test_limits_max_pages_and_depth(tmp_path: Path) -> None:
    site = build_site()
    status, store = await run_crawl(make_cfg(tmp_path / "a", **{"limits.max_pages": 2}), site)
    assert status == "limit"
    assert store.pages_started() == 2
    site = build_site()
    _, store = await run_crawl(make_cfg(tmp_path / "b", **{"limits.max_depth": 0}), site)
    done = store.conn.execute("SELECT url FROM page WHERE kind='page' AND state='done'").fetchall()
    assert [r[0] for r in done] == ["http://site.test/"]


async def test_budget_stops_cleanly_and_resumes(tmp_path: Path) -> None:
    out = tmp_path / "out"
    status, store = await run_crawl(make_cfg(out, **{"limits.max_total_bytes": "20KB"}), build_site())
    assert status == "limit"
    first = store.total_bytes()
    assert 20 * 1024 <= first < 200 * 1024
    store.close()
    status, store = await run_crawl(make_cfg(out, **{"limits.max_total_bytes": "50MB"}), build_site())
    assert status == "finished"
    assert store.queued_images() == 0
    assert all(image_rows(store)[u]["state"] == "done" for u in EXPECTED_SAVED)


async def test_dry_run_records_but_does_not_download(tmp_path: Path) -> None:
    site = build_site()
    status, store = await run_crawl(make_cfg(tmp_path / "out"), site, dry_run=True)
    assert status == "dry_run"
    assert store.queued_images() > 40
    assert not [p for _, p in site.requests if p.startswith("/img/")]
    store.close()
    status, store = await run_crawl(make_cfg(tmp_path / "out"), build_site())
    assert store.queued_images() == 0


async def test_srcset_all_mode_keeps_every_candidate(tmp_path: Path) -> None:
    site = build_site()
    _, store = await run_crawl(make_cfg(tmp_path / "out", **{"filters.srcset": "all"}), site)
    rows = image_rows(store)
    for u in ("s-300", "s-800", "s-1200", "s-small", "x1", "x2", "lazy-300", "lazy-900"):
        assert f"http://site.test/img/{u}.png" in rows, u


async def test_robots_ignore(tmp_path: Path) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out", robots="ignore"), build_site())
    rows = image_rows(store)
    assert rows["http://site.test/img/robots-blocked/x.png"]["state"] == "done"


async def test_images_only_list(tmp_path: Path) -> None:
    site = build_site()
    urls = ["http://site.test/img/plain.png", "http://cdn.test/og.jpg", "http://evil.test/outside.png"]
    status, store = await run_crawl(make_cfg(tmp_path / "out"), site, image_list=urls)
    assert status == "finished"
    rows = image_rows(store)
    assert rows[urls[0]]["state"] == "done" and rows[urls[1]]["state"] == "done"
    assert rows[urls[2]]["skip_reason"] == "scope"
    assert not [p for _, p in site.requests if p.endswith(".html") or p == "/"]
