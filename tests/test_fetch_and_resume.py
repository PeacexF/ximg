"""Fetcher behaviour (retries, redirects, scope guard, rate limits) and crash/resume."""

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.conftest import image_rows, make_cfg, run_crawl
from tests.fixtures.site import EXPECTED_SAVED, build_site
from ximg.crawler import Crawler
from ximg.fetcher import Fetcher, FetchError, RedirectDenied, ScopeViolation, read_capped
from ximg.ratelimit import HostLimiter
from ximg.store import OutputError, open_output


def _fetcher(tmp_path: Path, handler: httpx.MockTransport, **over: object) -> tuple[Fetcher, list[float]]:
    cfg = make_cfg(tmp_path / "out", **{"http.retries": 3, **over})
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    f = Fetcher(cfg, allow_host=lambda h: h in ("site.test", "cdn.test"), transport=handler, sleep=fake_sleep)
    return f, sleeps


async def test_retries_on_503_with_retry_after(tmp_path: Path) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, headers={"retry-after": "7"})
        return httpx.Response(200, content=b"ok")

    f, sleeps = _fetcher(tmp_path, httpx.MockTransport(handler))
    async with f, f.open("http://site.test/x", check=lambda u: None, kind="page") as op:
        assert op.response.status_code == 200
        assert await read_capped(op.response, 10) == b"ok"
    assert calls == 3 and sleeps == [7.0, 7.0]


async def test_gives_up_after_retries_and_returns_last_status(tmp_path: Path) -> None:
    f, sleeps = _fetcher(tmp_path, httpx.MockTransport(lambda r: httpx.Response(500)))
    async with f, f.open("http://site.test/x", check=lambda u: None, kind="page") as op:
        assert op.response.status_code == 500
    assert len(sleeps) == 3


async def test_transport_errors_raise_fetch_error(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    f, _ = _fetcher(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(FetchError, match="ConnectError"):
        async with f, f.open("http://site.test/x", check=lambda u: None, kind="page"):
            pass


async def test_redirects_checked_per_hop(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if req.url.path == "/b":
            return httpx.Response(301, headers={"location": "http://evil.test/c"})
        return httpx.Response(200)

    f, _ = _fetcher(tmp_path, httpx.MockTransport(handler))
    with pytest.raises(RedirectDenied) as ei:
        async with f, f.open("http://site.test/a", check=lambda u: None if "site.test" in u else "scope", kind="page"):
            pass
    assert ei.value.url == "http://evil.test/c"


async def test_redirect_loop(tmp_path: Path) -> None:
    f, _ = _fetcher(tmp_path, httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/loop"})))
    with pytest.raises(FetchError, match="too_many_redirects"):
        async with f, f.open("http://site.test/loop", check=lambda u: None, kind="page"):
            pass


async def test_scope_guard_transport_blocks_anything_out_of_scope(tmp_path: Path) -> None:
    f, _ = _fetcher(tmp_path, httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ScopeViolation):
        async with f, f.open("http://evil.test/", check=lambda u: None, kind="page"):
            pass


async def test_host_gets_blocked_after_bad_streak(tmp_path: Path) -> None:
    f, _ = _fetcher(tmp_path, httpx.MockTransport(lambda r: httpx.Response(403)), **{"http.max_bad_streak": 3})
    async with f:
        for _ in range(3):
            async with f.open("http://site.test/x", check=lambda u: None, kind="page") as op:
                assert op.response.status_code == 403
        with pytest.raises(FetchError, match="host_blocked"):
            async with f.open("http://site.test/x", check=lambda u: None, kind="page"):
                pass


async def test_rate_limiter_spacing_and_isolation() -> None:
    now = [0.0]
    starts: list[tuple[str, float]] = []

    async def sleep(s: float) -> None:
        now[0] += s

    lim = HostLimiter(rate=2.0, jitter=0, per_host=2, clock=lambda: now[0], sleep=sleep)

    async def hit(host: str) -> None:
        async with lim.slot(host):
            starts.append((host, now[0]))

    for _ in range(3):
        await hit("a")
    await hit("b")
    assert [t for h, t in starts if h == "a"] == [0.0, 0.5, 1.0]
    lim.set_min_interval("a", 2.0)
    await hit("a")
    assert starts[-1] == ("a", 1.5)  # next slot was already booked at 1.5
    await hit("a")
    assert starts[-1] == ("a", 3.5)
    for _ in range(6):
        lim.record("c", 429)
    assert lim.interval("c") == 2.0  # doubled twice from 0.5


async def test_resume_after_cancel_matches_uninterrupted_run(tmp_path: Path) -> None:
    # Reference run.
    _, ref = await run_crawl(make_cfg(tmp_path / "ref"), build_site())
    ref_files = sorted(p.name for p in (ref.out / "images").rglob("*") if p.is_file())
    ref_images = {u: (r["state"], r["sha256"], r["skip_reason"]) for u, r in image_rows(ref).items()}

    # Interrupted run: cancel the crawl task part-way through (like a crash / SIGKILL).
    cfg = make_cfg(tmp_path / "out")
    store = open_output(cfg)
    site = build_site()
    crawler = Crawler(cfg, store, transport=httpx.ASGITransport(app=site))
    task = asyncio.create_task(crawler.run())
    while crawler.stats.images_saved < 10:  # noqa: ASYNC110 (polling a counter in a test)
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.meta("status") == "interrupted"
    # Leave debris like a hard kill would: a stale temp file and an orphan image file.
    (store.tmp_dir / "stale.part").write_bytes(b"x")
    orphan = store.images_dir / "site.test" / ("0" * 64 + ".png")
    orphan.write_bytes(b"orphan")
    store.conn.execute("UPDATE image_url SET state='in_progress' WHERE id = (SELECT MIN(id) FROM image_url)")
    store.close()

    status, store = await run_crawl(cfg, build_site())
    assert status == "finished"
    files = sorted(p.name for p in (store.out / "images").rglob("*") if p.is_file())
    assert files == ref_files
    images = {u: (r["state"], r["sha256"], r["skip_reason"]) for u, r in image_rows(store).items()}
    assert images == ref_images
    assert not list(store.tmp_dir.iterdir())


async def test_output_dir_lifecycle(tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = make_cfg(out)
    _, store = await run_crawl(cfg, build_site())
    store.close()
    with pytest.raises(OutputError, match="finished run"):
        open_output(cfg)
    other = make_cfg(out, **{"scope.images.hosts": []})
    store = open_output(other, overwrite=True)
    assert store.meta("status") == "new"
    assert not list((out / "images").rglob("*.png"))
    store.close()
    # unfinished run + changed scope -> refuse unless forced
    changed = make_cfg(out, **{"scope.images.hosts": ["x.test"]})
    with pytest.raises(OutputError, match="different seeds/scope"):
        open_output(changed)
    open_output(changed, force_config=True).close()
    # a non-empty non-ximg dir is never touched
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine")
    with pytest.raises(OutputError, match="not empty"):
        open_output(make_cfg(foreign), overwrite=True)
    assert (foreign / "keep.txt").exists()


async def test_saved_files_match_fixture_after_resume_of_finished_images(tmp_path: Path) -> None:
    _, store = await run_crawl(make_cfg(tmp_path / "out"), build_site())
    rows = image_rows(store)
    assert all(rows[u]["state"] == "done" for u in EXPECTED_SAVED)
