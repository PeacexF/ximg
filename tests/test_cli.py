"""CLI smoke tests (network replaced by the fixture site)."""

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tests.fixtures.site import build_site
from ximg import cli, crawler
from ximg.config import load_config


@pytest.fixture(autouse=True)
def fixture_network(monkeypatch: pytest.MonkeyPatch) -> None:
    site = build_site()
    original = crawler.Crawler.__init__

    def patched(self, cfg, store, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("transport", httpx.ASGITransport(app=site))
        original(self, cfg, store, **kw)

    monkeypatch.setattr(crawler.Crawler, "__init__", patched)


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, result.output


def test_crawl_status_export_purge(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code, output = _run(
        "crawl", "--seed", "http://site.test/", "--out", str(out), "--image-host", "cdn.test",
        "--rate", "10000", "--render", "never", "-q",
    )  # fmt: skip
    assert code == 0, output
    assert (out / "manifest.csv").exists() and any((out / "images" / "site.test").iterdir())

    code, output = _run("status", str(out))
    assert code == 0 and "finished" in output and "site.test" in output

    code, output = _run("crawl", "--seed", "http://site.test/", "--out", str(out), "-q")
    assert code == 1 and "finished run" in output

    code, _ = _run("export", str(out), "--layout", "friendly")
    assert code == 0
    assert (out / "friendly" / "site.test" / "img" / "plain.png").exists()

    code, _ = _run("purge", str(out), "--yes")
    assert code == 0 and not out.exists()


def test_init_and_config_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _ = _run("init", "out/site", "--seed", "https://www.example.com/")
    assert code == 0
    text = (tmp_path / "ximg.toml").read_text()
    assert 'hosts      = ["www.example.com"]' in text
    cfg = load_config(tmp_path / "ximg.toml")  # must be valid as generated
    assert cfg.robots == "respect" and cfg.seeds == ["https://www.example.com/"]
    code, output = _run("crawl", "-c", "ximg.toml", "--robots", "sometimes")
    assert code == 1 and "robots: Input should be 'respect' or 'ignore'" in output
    code, output = _run("crawl", "--seed", "http://site.test/")
    assert code == 1 and "output directory is required" in output


def test_resume_without_config_uses_stored_config(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code, _ = _run(
        "crawl", "--seed", "http://site.test/", "--out", str(out), "--rate", "10000", "--render", "never",
        "--max-pages", "1", "-q",
    )  # fmt: skip
    assert code == 0
    code, output = _run("crawl", "--out", str(out), "--max-pages", "100", "-q")
    assert code == 0, output
    code, output = _run("status", str(out))
    assert "finished" in output
