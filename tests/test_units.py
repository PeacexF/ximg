"""Unit tests for the pure modules."""

import pytest

from ximg.config import ConfigError, load_config, parse_size
from ximg.extract.css import extract_css
from ximg.extract.html import HtmlOptions, extract_html
from ximg.extract.jsonld import jsonld_images
from ximg.extract.sitemap import manifest_images, maybe_gunzip, parse_sitemap
from ximg.extract.srcset import parse_srcset, pick
from ximg.fetcher import parse_retry_after
from ximg.scope import ScopePolicy
from ximg.sniff import sniff
from ximg.upgrade import Upgrader
from ximg.urls import normalize, resolve, url_pattern

# -- urls --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM:80/a/./b/../c?x=1#frag", "http://example.com/a/c?x=1"),
        ("https://example.com:443", "https://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://user:pw@example.com/x", "https://example.com/x"),
        ("https://bücher.de/ü", "https://xn--bcher-kva.de/%C3%BC"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/%7Euser", "https://example.com/%7Euser"),
        ("https://example.com/../..", "https://example.com/"),
        ("https://example.com/a/", "https://example.com/a/"),
        ("ftp://example.com/x", None),
        ("javascript:alert(1)", None),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("https://example.com:99999/", None),
    ],
)
def test_normalize(raw: str, expected: str | None) -> None:
    assert normalize(raw) == expected


def test_normalize_strips_tracking_params_only() -> None:
    url = "https://e.com/p?utm_source=x&id=5&fbclid=y&utm_medium=z"
    assert normalize(url, strip_params=["utm_*", "fbclid"]) == "https://e.com/p?id=5"


def test_resolve() -> None:
    base = "https://e.com/dir/page.html"
    assert resolve(base, "img.png") == "https://e.com/dir/img.png"
    assert resolve(base, "//cdn.e.com/x.png") == "https://cdn.e.com/x.png"
    assert resolve(base, " /a\n/b.png ") == "https://e.com/a/b.png"
    for bad in ("#top", "javascript:void(0)", "mailto:a@b", "data:image/png;base64,xx", ""):
        assert resolve(base, bad) is None


def test_url_pattern() -> None:
    assert url_pattern("https://e.com/calendar/2024/05/12") == "e.com/calendar/N/N/N"
    assert url_pattern("https://e.com/s?color=red&size=m&color=b") == "e.com/s?color&size"
    assert url_pattern("https://e.com/p/3f2a9c0e-1d2b-4c5d-8e9f-001122334455") == "e.com/p/X"


# -- scope -------------------------------------------------------------------------------


def _scope(**over: object) -> ScopePolicy:
    base = {
        "seeds": ["https://www.e.com/"],
        "scope.pages.hosts": ["www.e.com", "e.com"],
        "scope.pages.exclude": ["/logout*", "/search*"],
        "scope.images.hosts": ["cdn.e.net"],
    }
    base.update(over)
    return ScopePolicy(load_config(None, base).scope)


def test_scope_pages_and_images() -> None:
    s = _scope()
    assert s.page("https://www.e.com/a") is None
    assert s.page("https://sub.e.com/a") == "scope"
    assert s.page("https://www.e.com/logout?x") == "exclude"
    assert s.page("https://www.e.com/search?q=1") == "exclude"
    assert s.page("https://cdn.e.net/a.html") == "scope"  # image hosts are never crawled as sites
    assert s.image("https://cdn.e.net/a.png") is None
    assert s.image("https://www.e.com/a.png") is None
    assert s.image("https://evil.com/a.png") == "scope"
    assert s.image("https://cdn.e.net.evil.com/a.png") == "scope"
    assert s.image("https://xe.com/a.png") == "scope"


def test_scope_subdomains_and_no_page_hosts_for_images() -> None:
    s = _scope(**{"scope.pages.subdomains": True, "scope.images.also_page_hosts": False})
    assert s.page("https://deep.sub.e.com/") is None
    assert s.page("https://notreallye.com/") == "scope"
    assert s.image("https://www.e.com/a.png") == "scope"
    assert s.image("https://cdn.e.net/a.png") is None


# -- config ------------------------------------------------------------------------------


def test_parse_size() -> None:
    assert parse_size("2KB") == 2048
    assert parse_size("50MB") == 50 * 1024**2
    assert parse_size("5GiB") == 5 * 1024**3
    assert parse_size("1.5k") == 1536
    assert parse_size(10) == 10
    with pytest.raises(ValueError):
        parse_size("lots")


def test_config_defaults_scope_to_seed_hosts_and_rejects_typos(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_config(None, {"seeds": ["https://Www.E.com/x"], "out": tmp_path})
    assert cfg.scope.pages.hosts == ["www.e.com"]
    assert cfg.filters.min_file_bytes == 2048 and cfg.filters.srcset == "largest" and cfg.robots == "respect"
    toml = tmp_path / "x.toml"
    toml.write_text('seeds = ["https://e.com/"]\n[limits]\nmax_pagez = 3\n')
    with pytest.raises(ConfigError, match="max_pagez"):
        load_config(toml)
    with pytest.raises(ConfigError, match="outside scope"):
        load_config(None, {"seeds": ["https://other.com/"], "scope.pages.hosts": ["e.com"]})
    toml.write_text('seeds = ["https://e.com/"]\nout = "rel/dir"\n')
    assert load_config(toml).out == (tmp_path / "rel/dir").resolve()


# -- srcset / css / html -----------------------------------------------------------------


def test_srcset_parsing_and_largest() -> None:
    c = parse_srcset("a.jpg 300w, b.jpg 1200w,c.jpg 800w")
    assert [x.url for x in c] == ["a.jpg", "b.jpg", "c.jpg"]
    assert pick(c, "largest") == ["b.jpg"]
    assert pick(parse_srcset("a.png, b.png 2x, c.png 1.5x"), "largest") == ["b.png"]
    commas = parse_srcset("https://r.cl/upload/w_300,c_fill/a.jpg 300w, https://r.cl/upload/w_900,c_fill/a.jpg 900w")
    assert pick(commas, "largest") == ["https://r.cl/upload/w_900,c_fill/a.jpg"]
    assert pick(parse_srcset("x.png 1x, y.png 2x"), "all") == ["x.png", "y.png"]
    assert pick([], "largest") == []


def test_css_extraction() -> None:
    css = """
    @import "a.css"; @import url('b.css') screen;
    /* url(commented.png) */
    .x{background:url( "one.png" )} .y{background:url(two.png),url('three.gif')}
    .z{background-image:-webkit-image-set(url(lo.png) 1x, url(hi.png) 2x)}
    .w{background-image:image-set("s.avif" type("image/avif") 1x, "l.avif" 2x)}
    @font-face{font-family:F;src:url(f.woff2) format("woff2"),url(f.svg#font)}
    .m{mask:url(#mask)} .d{background:url(data:image/png;base64,AAAA)} .f{x:url(other.woff)}
    """
    res = extract_css(css)
    assert res.imports == ["a.css", "b.css"]
    urls = [u for u, _ in res.images]
    assert urls == ["hi.png", "l.avif", "one.png", "two.png", "three.gif", "data:image/png;base64,AAAA"]


def test_html_extraction_kinds() -> None:
    html = """<html><head><base href="https://e.com/base/">
    <meta property="og:image" content="/og.jpg"><link rel="shortcut icon" href="fav.ico">
    </head><body>
    <img src="a.png" srcset="a-1x.png 1x, a-2x.png 2x" alt=" Hello
    world ">
    <img src="plain.png"><img data-src="lazy.png" src="ph.gif">
    <a href="big.JPG">x</a><a href="/page">p</a><a href="doc.pdf">d</a><a href="mailto:x@y">m</a>
    <div style="background-image:url(bg.png)"></div><p data-zoom="z.webp"></p>
    <img src="data:image/gif;base64,R0lGOD">
    </body></html>"""
    opts = HtmlOptions(lazy_attrs=frozenset({"data-src"}))
    r = extract_html(html, "https://e.com/x/y.html", opts)
    got = {(ref.url, ref.kind) for ref in r.images}
    assert ("https://e.com/og.jpg", "html:meta@og:image") in got
    assert ("https://e.com/base/fav.ico", "html:link@icon") in got
    assert ("https://e.com/base/a-2x.png", "html:img@srcset") in got
    assert ("https://e.com/base/a.png", "html:img@src") not in got  # src ignored when srcset present
    assert ("https://e.com/base/lazy.png", "html:img@data-src") in got
    assert ("https://e.com/base/ph.gif", "html:img@src") in got
    assert ("https://e.com/base/big.JPG", "html:a@href") in got
    assert ("https://e.com/base/bg.png", "html:style-attr:url") in got
    assert ("https://e.com/base/z.webp", "html:p@data-*") in got
    assert ("data:image/gif;base64,R0lGOD", "html:img@src") in got
    assert r.links == ["https://e.com/page"]
    alt = next(ref.context for ref in r.images if ref.url.endswith("a-2x.png"))
    assert alt == "Hello world"


def test_jsonld_and_manifest_and_sitemap() -> None:
    ld = '{"@graph":[{"@type":"Product","image":["a.jpg",{"url":"b.jpg"}],"name":"x",' \
         '"brand":{"logo":{"@type":"ImageObject","contentUrl":"c.png"}},"url":"https://e.com/page"}]}'  # fmt: skip
    assert jsonld_images(ld) == ["a.jpg", "b.jpg", "c.png"]
    assert jsonld_images("{broken") == []
    m = b'{"icons":[{"src":"i.png"}],"shortcuts":[{"icons":[{"src":"s.png"}]}]}'
    assert manifest_images(m) == [("i.png", "manifest:icon"), ("s.png", "manifest:shortcut-icon")]
    sm = parse_sitemap(b"https://e.com/a\nnot a url\nhttps://e.com/b\n")
    assert sm.pages == ["https://e.com/a", "https://e.com/b"]
    import gzip

    bomb = gzip.compress(b"\0" * (101 * 1024 * 1024))
    with pytest.raises(ValueError):
        maybe_gunzip(bomb)


# -- sniff -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "fmt"),
    [
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"GIF89a...", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf", "avif"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic", "heic"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp41", None),  # mp4
        (b"\x00\x00\x01\x00\x01\x00\x10\x10", "ico"),
        (b"BM" + b"\0" * 20, "bmp"),
        (b"II*\x00rest", "tiff"),
        (b"\xff\x0a", "jxl"),
        (b'\xef\xbb\xbf  <?xml version="1.0"?><!-- c --><svg xmlns="x"/>', "svg"),
        (b"<svg viewBox='0 0 1 1'></svg>", "svg"),
        (b"<!DOCTYPE html><html><body><svg></svg></body></html>", None),
        (b"<html>not found</html>", None),
        (b"", None),
    ],
)
def test_sniff(head: bytes, fmt: str | None) -> None:
    assert sniff(head) == fmt


# -- misc --------------------------------------------------------------------------------


def test_retry_after() -> None:
    assert parse_retry_after("120") == 120
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 60) == 60
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_upgrade_rules() -> None:
    from ximg.config import UpgradeRule

    cfg = load_config(None, {"seeds": ["https://e.com/"], "upgrade": ["all"]})
    rules = [UpgradeRule(name="custom", match=r"^(.*)/thumbs/(.*)$", replace=r"\1/full/\2")]
    up = Upgrader(rules, cfg.upgrade)
    with pytest.raises(ConfigError, match="unknown built-in"):
        load_config(None, {"seeds": ["https://e.com/"], "upgrade": ["nope"]})
    assert up.variants("https://e.com/wp-content/uploads/2024/05/cat-300x200.jpg") == [
        "https://e.com/wp-content/uploads/2024/05/cat.jpg"
    ]
    assert up.variants("https://e.com/i/cat.jpg?w=300&h=200") == ["https://e.com/i/cat.jpg"]
    assert up.variants("https://e.com/i/cat.jpg?id=7") == []
    assert up.variants("https://res.cloudinary.com/d/image/upload/w_300,c_fill/v123/cat.jpg") == [
        "https://res.cloudinary.com/d/image/upload/v123/cat.jpg"
    ]
    assert up.variants("https://cdn.shopify.com/s/files/1/cat_300x.jpg?v=1") == [
        "https://cdn.shopify.com/s/files/1/cat.jpg?v=1"
    ]
    assert up.variants("https://e.com/thumbs/a.png") == ["https://e.com/full/a.png"]
