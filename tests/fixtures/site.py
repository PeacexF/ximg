"""An in-process virtual website (ASGI) with one planted image per discovery source.

Hosts: site.test (pages), cdn.test (allowed image host), evil.test (out of scope; must never be hit).
"""

import base64
import gzip
import re
from collections.abc import Callable
from dataclasses import dataclass, field


def image(name: str, fmt: str = "png", size: int = 3000) -> bytes:
    """Deterministic bytes that sniff as `fmt` and are unique per name."""
    pad = (name.encode() * (size // max(len(name), 1) + 1))[:size]
    if fmt == "png":
        return b"\x89PNG\r\n\x1a\n" + pad
    if fmt == "jpeg":
        return b"\xff\xd8\xff\xe0" + pad
    if fmt == "gif":
        return b"GIF89a" + pad
    if fmt == "webp":
        return b"RIFF\x00\x00\x00\x00WEBPVP8 " + pad
    if fmt == "avif":
        return b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + pad
    if fmt == "ico":
        return b"\x00\x00\x01\x00\x01\x00" + pad
    if fmt == "svg":
        return (f'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><!-- {name} -->'.encode()
                + b"<desc>" + pad + b"</desc></svg>")  # fmt: skip
    raise ValueError(fmt)


@dataclass
class Resp:
    body: bytes
    ctype: str = "text/html; charset=utf-8"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    send_length: bool = True


class Site:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Resp] = {}
        self.dynamic: list[tuple[str, re.Pattern[str], Callable[[re.Match[str]], Resp]]] = []
        self.requests: list[tuple[str, str]] = []

    def add(self, url: str, body: bytes | str, ctype: str | None = None, **kw: object) -> None:
        host, path = _split(url)
        if isinstance(body, str):
            body = body.encode()
        if ctype is None:
            ctype = _guess(path)
        self.routes[(host, path)] = Resp(body, ctype, **kw)  # type: ignore[arg-type]

    def redirect(self, url: str, location: str, status: int = 302) -> None:
        host, path = _split(url)
        self.routes[(host, path)] = Resp(b"", "text/html", status, {"location": location})

    def route(self, host: str, pattern: str, fn: Callable[[re.Match[str]], Resp]) -> None:
        self.dynamic.append((host, re.compile(pattern), fn))

    def hosts_requested(self) -> set[str]:
        return {h for h, _ in self.requests}

    def body(self, url: str) -> bytes:
        return self.routes[_split(url)].body

    def respond(self, host: str, path: str) -> Resp:
        self.requests.append((host, path))
        resp = self.routes.get((host, path))
        if resp is None:
            for h, rx, fn in self.dynamic:
                if h == host and (m := rx.fullmatch(path)):
                    return fn(m)
        return resp or Resp(b"<html><body>not found</body></html>", status=404)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        assert scope["type"] == "http"
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host = headers.get("host", "").split(":")[0]
        qs = scope["query_string"].decode()
        path = scope["path"] + (f"?{qs}" if qs else "")
        resp = self.respond(host, path)
        hdrs = [(b"content-type", resp.ctype.encode())]
        if resp.send_length:
            hdrs.append((b"content-length", str(len(resp.body)).encode()))
        hdrs += [(k.encode(), v.encode()) for k, v in resp.headers.items()]
        await send({"type": "http.response.start", "status": resp.status, "headers": hdrs})
        await send({"type": "http.response.body", "body": resp.body})


def _split(url: str) -> tuple[str, str]:
    m = re.match(r"https?://([^/]+)(/.*)?$", url)
    assert m, url
    return m.group(1), m.group(2) or "/"


def _guess(path: str) -> str:
    ext = path.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
        "ico": "image/x-icon", "svg": "image/svg+xml", "css": "text/css", "xml": "application/xml",
        "webmanifest": "application/manifest+json", "txt": "text/plain", "gz": "application/gzip",
    }.get(ext, "text/html; charset=utf-8")  # fmt: skip


S = "http://site.test"
CDN = "http://cdn.test"
EVIL = "http://evil.test"

DATA_URI_PNG = image("data-uri-image")

# Every image that must end up saved: URL -> occurrence kind it must be recorded with.
EXPECTED_SAVED: dict[str, str] = {
    f"{S}/img/plain.png": "html:img@src",
    f"{S}/img/protocol-rel.png": "html:img@src",
    f"{S}/img/absolute.png": "html:img@src",
    f"{S}/img/inline-style.png": "html:style-attr:url",
    f"{S}/img/style-block.png": "html:style:url",
    f"{S}/img/preload.png": "html:link@preload",
    f"{S}/static/touch.png": "html:link@icon",
    f"{S}/static/favicon.ico": "html:link@icon",
    f"{CDN}/og.jpg": "html:meta@og:image",
    f"{S}/img/twitter.png": "html:meta@twitter:image",
    f"{S}/img/logo-ld.png": "html:json-ld",
    f"{S}/img/ld-obj.png": "html:json-ld",
    f"{S}/img/redirect-ok.png": "html:img@src",
    f"{S}/img/dup-a.png": "html:img@src",
    f"{CDN}/dup-b.png": "html:img@src",
    f"{S}/img/dup-c.png?v=2": "html:img@src",
    f"{S}/img/full-size.jpg": "html:a@href",
    f"{S}/img/s-1200.png": "html:img@srcset",
    f"{S}/img/x2.png": "html:img@srcset",
    f"{S}/img/pic.avif": "html:source@srcset",
    f"{S}/img/pic-960.webp": "html:source@srcset",
    f"{S}/img/pic.jpg": "html:img@src",
    f"{CDN}/upload/w_900,c_fill/cl.png": "html:img@srcset",
    f"{S}/img/lazy-src.png": "html:img@data-src",
    f"{S}/img/lazy-900.png": "html:img@data-srcset",
    f"{S}/img/lazy-bg.png": "html:div@data-bg",
    f"{S}/img/lazy-original.png": "html:div@data-original",
    f"{S}/img/data-generic.png": "html:span@data-*",
    f"{S}/img/poster.jpg": "html:video@poster",
    f"{S}/img/input.png": "html:input@src",
    f"{S}/img/svg-image.png": "html:svg-image@href",
    f"{S}/img/object.svg": "html:object@data",
    f"{S}/img/table-bg.png": "html:table@background",
    f"{S}/img/frame-img.png": "html:img@src",
    f"{S}/assets/based.png": "html:img@src",
    f"{S}/img/gallery-thumb.webp": "html:img@src",
    f"{S}/img/gallery-full.jpg": "html:a@href",
    f"{S}/img/css-rel.png": "css:url",
    f"{S}/img/set-2x.png": "css:image-set",
    f"{CDN}/css-cdn.png": "css:url",
    f"{S}/styles/imported-img.png": "css:url",
    f"{S}/icons/192.png": "manifest:icon",
    f"{S}/icons/shot.png": "manifest:screenshot",
    f"{S}/icons/tile.png": "browserconfig:tile",
    f"{S}/img/sitemap-image.png": "sitemap:image",
    f"{S}/img/sitemap-only.png": "html:img@src",
    f"{S}/page-image": "page:direct",
}
DUPLICATES = {f"{S}/img/dup-a.png", f"{CDN}/dup-b.png", f"{S}/img/dup-c.png?v=2"}

# URL -> (state, skip_reason)
EXPECTED_NOT_SAVED: dict[str, tuple[str, str | None]] = {
    f"{S}/img/tiny.gif": ("skipped", "filter_size"),
    f"{S}/img/placeholder.gif": ("skipped", "filter_size"),
    f"{S}/img/not-image.jpg": ("skipped", "not_image"),
    f"{S}/img/huge.png": ("skipped", "filter_size_max"),
    f"{S}/img/huge-nolength.png": ("skipped", "filter_size_max"),
    f"{S}/img/missing.png": ("failed", None),
    f"{EVIL}/outside.png": ("skipped", "scope"),
    f"{S}/img/redirect-to-evil.png": ("skipped", "scope:redirect"),
    f"{S}/img/robots-blocked/x.png": ("skipped", "robots"),
}
# Candidates that "largest only" must NOT pick; they must never even be requested.
NOT_REQUESTED = [
    "/img/s-small.png", "/img/s-300.png", "/img/s-800.png", "/img/x1.png", "/img/lazy-fallback.png",
    "/img/lazy-300.png", "/img/set-1x.png", "/fonts/x.woff2", "/upload/w_300,c_fill/cl.png", "/private/secret.html",
]  # fmt: skip


def build_site(*, calendar: bool = True) -> Site:
    site = Site()
    b64 = base64.b64encode(DATA_URI_PNG).decode()
    site.add(
        f"{S}/",
        f"""<!doctype html><html><head>
<title>Fixture</title>
<link rel="icon" href="/static/favicon.ico">
<link rel="apple-touch-icon" href="/static/touch.png">
<link rel="preload" as="image" href="/img/preload.png">
<link rel="stylesheet" href="/styles/main.css">
<link rel="manifest" href="/site.webmanifest">
<meta property="og:image" content="{CDN}/og.jpg">
<meta name="twitter:image" content="/img/twitter.png">
<meta name="msapplication-config" content="/browserconfig.xml">
<script type="application/ld+json">{{"@type":"Organization","logo":"/img/logo-ld.png",
  "image":[{{"@type":"ImageObject","url":"/img/ld-obj.png"}}]}}</script>
<style>.hero {{ background-image: url('/img/style-block.png') }}</style>
</head><body>
<img src="/img/plain.png" alt="Plain   image">
<img src="//site.test/img/protocol-rel.png">
<img src="{S}/img/absolute.png">
<img src="data:image/png;base64,{b64}">
<div style="background: url(/img/inline-style.png) no-repeat"></div>
<a href="/gallery.html">gallery</a> <a href="/srcset.html">srcset</a> <a href="/lazy.html">lazy</a>
<a href="/misc.html">misc</a> <a href="/base/page.html">base</a>
<a href="/private/secret.html">private</a>
<a href="{EVIL}/page.html">external</a>
<img src="{EVIL}/outside.png">
<a href="/redirect-out">redirect away</a>
<img src="/img/redirect-to-evil.png">
<img src="/img/redirect-ok.png">
{'<a href="/calendar/2024/1">calendar</a>' if calendar else ""}
<img src="/img/tiny.gif"><img src="/img/not-image.jpg"><img src="/img/huge.png"><img src="/img/huge-nolength.png">
<img src="/img/missing.png">
<img src="/img/dup-a.png"><img src="{CDN}/dup-b.png"><img src="/img/dup-c.png?v=2">
<a href="/img/full-size.jpg">full size</a>
<a href="/docs/file.pdf">pdf</a>
<a href="/page-image">a link that serves an image</a>
<img src="/img/robots-blocked/x.png">
<a href="/?utm_source=x">self with tracking param</a>
</body></html>""",
    )
    site.add(
        f"{S}/srcset.html",
        f"""<html><body>
<img src="/img/s-small.png" srcset="/img/s-300.png 300w, /img/s-800.png 800w, /img/s-1200.png 1200w">
<img srcset="/img/x1.png 1x, /img/x2.png 2x">
<picture>
  <source type="image/avif" srcset="/img/pic.avif">
  <source srcset="/img/pic-480.webp 480w, /img/pic-960.webp 960w">
  <img src="/img/pic.jpg">
</picture>
<img srcset="{CDN}/upload/w_300,c_fill/cl.png 300w, {CDN}/upload/w_900,c_fill/cl.png 900w">
</body></html>""",
    )
    site.add(
        f"{S}/lazy.html",
        """<html><body>
<img src="/img/placeholder.gif" data-src="/img/lazy-src.png">
<img src="/img/lazy-fallback.png" data-srcset="/img/lazy-300.png 300w, /img/lazy-900.png 900w">
<div data-bg="/img/lazy-bg.png"></div>
<div class="lazyload" data-original="/img/lazy-original.png"></div>
<span data-thumb="/img/data-generic.png"></span>
</body></html>""",
    )
    site.add(
        f"{S}/misc.html",
        """<html><body>
<video poster="/img/poster.jpg"></video>
<form><input type="image" src="/img/input.png"></form>
<svg><image xlink:href="/img/svg-image.png"/></svg>
<object data="/img/object.svg" type="image/svg+xml"></object>
<table background="/img/table-bg.png"><tr><td>x</td></tr></table>
<iframe src="/frame.html"></iframe>
</body></html>""",
    )
    site.add(f"{S}/frame.html", '<html><body><img src="/img/frame-img.png"></body></html>')
    site.add(
        f"{S}/base/page.html", '<html><head><base href="/assets/"></head><body><img src="based.png"></body></html>'
    )
    site.add(
        f"{S}/gallery.html",
        """<html><body>
<a href="/img/gallery-full.jpg"><img src="/img/gallery-thumb.webp" alt="thumb"></a>
</body></html>""",
    )
    site.add(
        f"{S}/styles/main.css",
        f"""@import url("/styles/imported.css");
/* a comment with url(/img/commented-out.png) */
.a {{ background: url(../img/css-rel.png) }}
.b {{ background-image: image-set("/img/set-1x.png" 1x, "/img/set-2x.png" 2x) }}
@font-face {{ font-family: X; src: url(/fonts/x.woff2) format("woff2") }}
.c {{ background: url({CDN}/css-cdn.png) }}
.d {{ mask: url(#svg-mask) }}""",
    )
    site.add(f"{S}/styles/imported.css", ".e { background: url('imported-img.png') }")
    site.add(f"{S}/site.webmanifest", '{"icons":[{"src":"/icons/192.png","sizes":"192x192"}],'
             '"screenshots":[{"src":"/icons/shot.png"}]}')  # fmt: skip
    site.add(f"{S}/browserconfig.xml", '<?xml version="1.0"?><browserconfig><msapplication><tile>'
             '<square150x150logo src="/icons/tile.png"/></tile></msapplication></browserconfig>')  # fmt: skip
    site.add(f"{S}/robots.txt", "User-agent: *\nDisallow: /private/\nDisallow: /img/robots-blocked/\n"
             f"Sitemap: {S}/sitemap_index.xml\n", "text/plain")  # fmt: skip
    site.add(
        f"{S}/sitemap_index.xml",
        f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{S}/sitemap-pages.xml.gz</loc></sitemap>
  <sitemap><loc>{EVIL}/sitemap.xml</loc></sitemap>
</sitemapindex>""",
    )
    site.add(
        f"{S}/sitemap-pages.xml.gz",
        gzip.compress(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url><loc>{S}/sitemap-only.html</loc>
       <image:image><image:loc>{S}/img/sitemap-image.png</image:loc></image:image></url>
  <url><loc>{S}/</loc></url>
</urlset>""".encode()
        ),
        "application/gzip",
    )
    site.add(f"{S}/sitemap-only.html", '<html><body><img src="/img/sitemap-only.png">&xxe;</body></html>')
    site.redirect(f"{S}/redirect-out", f"{EVIL}/landing.html")
    site.redirect(f"{S}/img/redirect-to-evil.png", f"{EVIL}/x.png")
    site.redirect(f"{S}/img/redirect-ok.png", "/img/redirected-target.png", 301)
    site.add(f"{S}/page-image", image("page-image"), "image/png")
    site.add(f"{S}/docs/file.pdf", b"%PDF-1.4", "application/pdf")
    site.add(f"{S}/img/tiny.gif", image("tiny", "gif", 40))
    site.add(f"{S}/img/placeholder.gif", image("placeholder", "gif", 40))
    site.add(f"{S}/img/not-image.jpg", "<html><body>" + "error page " * 400 + "</body></html>", "image/jpeg")
    site.add(f"{S}/img/huge.png", image("huge", size=300_000))
    site.add(f"{S}/img/huge-nolength.png", image("huge-nolength", size=300_000), send_length=False)
    site.add(f"{S}/img/robots-blocked/x.png", image("robots-blocked"))
    site.add(f"{S}/img/commented-out.png", image("commented-out"))
    dup = image("duplicate-content")
    site.add(f"{S}/img/dup-a.png", dup)
    site.add(f"{CDN}/dup-b.png", dup)
    site.add(f"{S}/img/dup-c.png?v=2", dup)
    site.add(f"{EVIL}/outside.png", image("evil"))
    site.add(f"{EVIL}/x.png", image("evil-x"))
    site.add(f"{EVIL}/landing.html", "<html><img src='/evil.png'></html>")
    site.add(f"{EVIL}/sitemap.xml", "<urlset/>")
    site.add(f"{S}/img/redirected-target.png", image("redirected-target"))

    fmts = {"jpg": "jpeg", "webp": "webp", "avif": "avif", "ico": "ico", "svg": "svg"}
    for url in EXPECTED_SAVED:
        if _split(url) in site.routes:
            continue
        ext = url.rsplit("?", 1)[0].rsplit(".", 1)[-1]
        site.add(url, image(url.rsplit("/", 1)[-1], fmts.get(ext, "png")))
    for path in NOT_REQUESTED:
        if path.startswith("/img/"):
            site.add(f"{S}{path}", image(path))
    site.add(f"{CDN}/upload/w_300,c_fill/cl.png", image("cl-300"))

    if calendar:
        # Crawler trap: every month links to the next, forever.
        def month(m: re.Match[str]) -> Resp:
            n = int(m.group(1))
            return Resp(f'<html><body><a href="/calendar/2024/{n + 1}">next</a></body></html>'.encode())

        site.route("site.test", r"/calendar/2024/(\d+)", month)
    return site


def serve_http(site: Site) -> tuple[int, Callable[[], None]]:
    """Serve `site` on a real localhost socket (for the browser). Host header port is ignored."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = self.headers.get("host", "").rsplit(":", 1)[0]
            resp = site.respond(host, self.path)
            self.send_response(resp.status)
            self.send_header("content-type", resp.ctype)
            self.send_header("content-length", str(len(resp.body)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return server.server_address[1], stop
