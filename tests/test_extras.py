"""Phase 4 extras: upgrade rules, Wayback source, friendly export, retry."""

import json
from pathlib import Path

import httpx

from tests.conftest import image_rows, kinds_for, make_cfg, run_crawl
from tests.fixtures.site import S, Site, build_site, image
from ximg.export import export_friendly
from ximg.store import open_output
from ximg.wayback import queue_wayback


def _cdx(rows: list[list[str]]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "web.archive.org" and req.url.params["url"] == "site.test/*"
        assert req.url.params.get_list("filter") == ["mimetype:image/.*", "statuscode:200"]
        return httpx.Response(200, json=[["original", "timestamp", "mimetype"], *rows])

    return httpx.MockTransport(handler)


async def test_upgrade_rules_fetch_originals(tmp_path: Path) -> None:
    site = build_site(calendar=False)
    site.add(f"{S}/wp.html", '<html><body><img src="/wp-content/uploads/2024/05/cat-300x200.jpg"></body></html>')
    site.add(f"{S}/wp-content/uploads/2024/05/cat-300x200.jpg", image("cat-small", "jpeg"))
    site.add(f"{S}/wp-content/uploads/2024/05/cat.jpg", image("cat-original", "jpeg", 9000))
    site.routes[("site.test", "/")].body += b'<a href="/wp.html">wp</a>'
    _, store = await run_crawl(make_cfg(tmp_path / "out", upgrade=["wordpress-size-suffix"]), site)
    rows = image_rows(store)
    orig = f"{S}/wp-content/uploads/2024/05/cat.jpg"
    assert rows[orig]["state"] == "done"
    assert rows[f"{S}/wp-content/uploads/2024/05/cat-300x200.jpg"]["state"] == "done"
    assert kinds_for(store, orig) == {"html:img@src+upgrade"}


async def test_wayback_live_mode_queues_historical_urls_in_scope(tmp_path: Path) -> None:
    site = build_site(calendar=False)
    site.add(f"{S}/old/unlinked.png", image("unlinked-but-still-hosted"))
    cfg = make_cfg(tmp_path / "out")
    store = open_output(cfg)
    cdx = _cdx([
        ["http://site.test:80/old/unlinked.png", "20190101000000", "image/png"],
        ["http://site.test/old/gone.png", "20180101000000", "image/png"],
        ["http://elsewhere.test/x.png", "20180101000000", "image/png"],
    ])  # fmt: skip
    added, skipped = await queue_wayback(cfg, store, "site.test", transport=cdx)
    assert (added, skipped) == (2, 1)
    store.close()
    _, store = await run_crawl(cfg, site)
    rows = image_rows(store)
    assert rows[f"{S}/old/unlinked.png"]["state"] == "done"
    assert rows[f"{S}/old/unlinked.png"]["source"] == "wayback"
    assert rows[f"{S}/old/gone.png"]["state"] == "failed"
    assert rows["http://elsewhere.test/x.png"]["skip_reason"] == "scope"
    assert kinds_for(store, f"{S}/old/unlinked.png") == {"wayback:cdx"}


async def test_wayback_archive_mode_downloads_from_archive_only(tmp_path: Path) -> None:
    site = build_site(calendar=False)
    archived = "http://web.archive.org/web/20190101000000id_/http://site.test/old/a.png"
    site.add(archived, image("archived-copy"))
    cfg = make_cfg(tmp_path / "out", **{"wayback.mode": "archive", "wayback.rate": 1000})
    store = open_output(cfg)
    await queue_wayback(
        cfg, store, "site.test", transport=_cdx([["http://site.test/old/a.png", "20190101000000", "image/png"]])
    )
    store.conn.execute("UPDATE image_url SET url=replace(url, 'https://', 'http://') WHERE source='wayback-archive'")
    store.close()
    _, store = await run_crawl(cfg, site)
    row = image_rows(store)[archived]
    assert row["state"] == "done", row
    assert store.file_path(row["sha256"]).startswith("images/_wayback/")  # type: ignore[union-attr]
    assert ("site.test", "/old/a.png") not in site.requests


async def test_friendly_export_mirrors_site_paths(tmp_path: Path) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), build_site())
    n = export_friendly(store)
    root = store.out / "friendly"
    assert n > 40
    assert (root / "site.test" / "img" / "plain.png").read_bytes() == build_site().body(f"{S}/img/plain.png")
    assert (root / "cdn.test" / "og.jpg").exists()
    assert (root / "site.test" / "page-image.png").exists()  # extension added from the sniffed type
    assert (root / "site.test" / "img" / "plain.png").stat().st_ino == (
        store.out / store.file_path(image_rows(store)[f"{S}/img/plain.png"]["sha256"])  # type: ignore[operator]
    ).stat().st_ino  # hardlink, no extra disk
    # second run is idempotent
    assert export_friendly(store) == 0


async def test_retry_requeues_failures(tmp_path: Path) -> None:
    site = build_site(calendar=False)
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    assert image_rows(store)[f"{S}/img/missing.png"]["state"] == "failed"
    site.add(f"{S}/img/missing.png", image("now-present"))
    _, images = store.requeue_failed()
    assert images == 1
    store.set_meta("status", "limit")
    store.close()
    _, store = await run_crawl(make_cfg(tmp_path / "out"), site)
    assert image_rows(store)[f"{S}/img/missing.png"]["state"] == "done"


def test_manifest_json_is_valid(tmp_path: Path) -> None:
    import asyncio

    _, store = asyncio.run(run_crawl(make_cfg(tmp_path / "out"), Site()))
    doc = json.loads((store.out / "manifest.json").read_text())
    assert doc["files"] == [] and doc["target"] == ["http://site.test/"]
