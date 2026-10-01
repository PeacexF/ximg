from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fixtures.site import Site, build_site
from ximg.config import Config, load_config
from ximg.crawler import Crawler, RunStatus
from ximg.export import export_all
from ximg.store import Store, open_output


def make_cfg(out: Path, **over: Any) -> Config:
    base: dict[str, Any] = {
        "seeds": ["http://site.test/"],
        "out": out,
        "scope.images.hosts": ["cdn.test"],
        "rate.per_host": 10000,
        "rate.jitter": 0,
        "http.retries": 1,
        "limits.max_file_bytes": "100KB",
        "limits.max_pages_per_pattern": 3,
        "render.mode": "never",
    }
    base.update(over)
    return load_config(None, base)


async def run_crawl(cfg: Config, site: Site, **kw: Any) -> tuple[RunStatus, Store]:
    store = open_output(cfg, overwrite=kw.pop("overwrite", False), force_config=kw.pop("force_config", False))
    crawler = Crawler(cfg, store, transport=httpx.ASGITransport(app=site), **kw)
    status = await crawler.run()
    export_all(store)
    return status, store


@pytest.fixture
def site() -> Site:
    return build_site()


def image_rows(store: Store) -> dict[str, dict[str, Any]]:
    return {r["url"]: dict(r) for r in store.conn.execute("SELECT * FROM image_url")}


def kinds_for(store: Store, url: str) -> set[str]:
    return {
        r[0]
        for r in store.conn.execute(
            "SELECT o.kind FROM occurrence o JOIN image_url u ON u.id=o.image_url_id WHERE u.url=?", (url,)
        )
    }
