"""JS rendering with real Chromium against a real localhost server.

Run with: uv sync --extra render && uv run playwright install chromium && uv run pytest -m render
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import image_rows, kinds_for
from tests.fixtures.site import Site, image, serve_http
from ximg.config import Config, load_config
from ximg.crawler import Crawler
from ximg.store import open_output

pytestmark = pytest.mark.render
pytest.importorskip("playwright", reason="needs the render extra: uv sync --extra render")

SPA = """<!doctype html><html><head><title>spa</title></head><body>
<div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript>
<script>
const root = document.getElementById('root');
fetch('/api/items.json').then(r => r.json()).then(items => {
  for (const it of items.slice(0, 3)) {
    const i = document.createElement('img'); i.src = it.image; root.appendChild(i);
  }
  const spacer = document.createElement('div'); spacer.style.height = '4000px'; root.appendChild(spacer);
  const lazy = document.createElement('img'); lazy.loading = 'lazy'; lazy.src = '/img/spa-lazy-bottom.png';
  root.appendChild(lazy);
  const bg = document.createElement('div');
  bg.style.cssText = 'width:10px;height:10px;background-image:url(/img/spa-bg.png)';
  root.appendChild(bg);
  window.addEventListener('scroll', () => {
    if (!window.more && window.scrollY + innerHeight >= document.documentElement.scrollHeight - 50) {
      window.more = 1; const i = document.createElement('img'); i.src = '/img/spa-scroll.png'; root.appendChild(i);
    }
  });
});
new Image().src = 'http://localhost:PORT/img/evil.png';
fetch('http://localhost:PORT/track').catch(() => {});
</script></body></html>"""

RENDERED = ["spa-1", "spa-2", "spa-3", "spa-lazy-bottom", "spa-bg", "spa-scroll"]


@pytest.fixture
def spa_site() -> Iterator[tuple[Site, int]]:
    site = Site()
    port, stop = serve_http(site)
    h = "http://127.0.0.1"
    site.add(f"{h}/", SPA.replace("PORT", str(port)))
    site.add(f"{h}/static.html", "<html><body><p>" + "Plenty of server-rendered text. " * 40 +
             '</p><img src="/img/static.png"></body></html>')  # fmt: skip
    items = [{"image": f"/img/spa-{n}.png"} for n in (1, 2, 3)] + [{"image": "/img/spa-json-only.png"}]
    site.add(f"{h}/api/items.json", json.dumps(items), "application/json")
    for name in [*RENDERED, "spa-json-only", "static"]:
        site.add(f"{h}/img/{name}.png", image(name))
    site.add("http://localhost/img/evil.png", image("evil"))
    yield site, port
    stop()


def _cfg(out: Path, port: int, **over: object) -> Config:
    base = {
        "seeds": [f"http://127.0.0.1:{port}/", f"http://127.0.0.1:{port}/static.html"],
        "out": out,
        "rate.per_host": 1000,
        "rate.jitter": 0,
        "discovery.sitemaps": False,
        "render.mode": "always",
        "render.idle_timeout": 2,
    }
    base.update(over)
    return load_config(None, base)


async def _crawl(cfg: Config) -> tuple[str, object]:
    from ximg.render import PlaywrightRenderer

    store = open_output(cfg)
    renderer = PlaywrightRenderer(cfg)
    async with renderer:
        status = await Crawler(cfg, store, renderer=renderer).run()
    return status, store


async def test_render_finds_js_lazy_scroll_and_css_images(tmp_path: Path, spa_site: tuple[Site, int]) -> None:
    site, port = spa_site
    status, store = await _crawl(_cfg(tmp_path / "out", port))
    assert status == "finished"
    rows = image_rows(store)  # type: ignore[arg-type]
    for name in RENDERED:
        url = f"http://127.0.0.1:{port}/img/{name}.png"
        assert rows.get(url, {}).get("state") == "done", (name, rows.get(url))
        assert "render:network" in kinds_for(store, url)  # type: ignore[arg-type]
    assert f"http://127.0.0.1:{port}/img/spa-json-only.png" not in rows  # scan_json is off
    # The browser was answered with placeholders: every image hit the server exactly once (our downloader).
    img_hits = [p for _, p in site.requests if p.startswith("/img/")]
    assert len(img_hits) == len(set(img_hits))
    # Out-of-scope requests from page JS never left the browser.
    assert "localhost" not in site.hosts_requested()


async def test_scan_json_finds_unrendered_gallery_items(tmp_path: Path, spa_site: tuple[Site, int]) -> None:
    _, port = spa_site
    _, store = await _crawl(_cfg(tmp_path / "out", port, **{"render.scan_json": True}))
    url = f"http://127.0.0.1:{port}/img/spa-json-only.png"
    assert image_rows(store)[url]["state"] == "done"  # type: ignore[arg-type]
    assert kinds_for(store, url) == {"render:json"}  # type: ignore[arg-type]


async def test_auto_mode_renders_only_spa_pages(tmp_path: Path, spa_site: tuple[Site, int]) -> None:
    _, port = spa_site
    _, store = await _crawl(_cfg(tmp_path / "out", port, **{"render.mode": "auto"}))
    rendered = dict(store.conn.execute("SELECT url, rendered FROM page WHERE kind='page'").fetchall())  # type: ignore[attr-defined]
    assert rendered == {f"http://127.0.0.1:{port}/": 1, f"http://127.0.0.1:{port}/static.html": 0}
    rows = image_rows(store)  # type: ignore[arg-type]
    assert rows[f"http://127.0.0.1:{port}/img/spa-scroll.png"]["state"] == "done"
    assert rows[f"http://127.0.0.1:{port}/img/static.png"]["state"] == "done"


async def test_without_rendering_spa_images_are_missed(tmp_path: Path, spa_site: tuple[Site, int]) -> None:
    _, port = spa_site
    cfg = _cfg(tmp_path / "out", port, **{"render.mode": "never"})
    store = open_output(cfg)
    await Crawler(cfg, store).run()
    assert not [u for u in image_rows(store) if "/spa-" in u]
