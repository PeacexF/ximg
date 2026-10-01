"""URL normalization and classification helpers."""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SAFE_PATH = "/%:@!$&'()*+,;=~-._"
_SAFE_QUERY = _SAFE_PATH + "?"
_CTRL_WS = re.compile(r"[\t\n\r]")

IMAGE_EXT = re.compile(r"\.(jpe?g|jfif|pjpeg|png|apng|gif|webp|avif|svgz?|ico|cur|bmp|tiff?|heic|heif|jxl)$", re.I)
NON_PAGE_EXT = re.compile(
    r"\.(pdf|zip|gz|tgz|rar|7z|tar|dmg|exe|msi|apk|iso|docx?|xlsx?|pptx?|odt|ods|csv|rtf|"
    r"mp3|mp4|m4a|m4v|mov|avi|mkv|webm|wav|ogg|flac|woff2?|ttf|otf|eot|js|mjs|css|json|map|wasm)$",
    re.I,
)


def normalize(url: str, *, strip_params: Iterable[str] = ()) -> str | None:
    """Canonical form used for dedupe and scope checks, or None if not an http(s) URL.

    Lowercases scheme/host, IDNA-encodes the host, drops default ports, user info and
    the fragment, resolves dot segments, and percent-encodes unsafe characters. The query
    string is kept as-is except for parameters matching `strip_params` (globs).
    """
    try:
        parts = urlsplit(_CTRL_WS.sub("", url.strip()))
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    path = quote(_remove_dot_segments(parts.path or "/"), safe=_SAFE_PATH)
    query = parts.query
    if query and strip_params:
        globs = tuple(strip_params)
        query = "&".join(
            seg for seg in query.split("&")
            if seg and not any(fnmatchcase(unquote_plus(seg.split("=", 1)[0]), g) for g in globs)
        )  # fmt: skip
    query = quote(query, safe=_SAFE_QUERY + "=&")
    return urlunsplit((scheme, netloc, path, query, ""))


def resolve(base: str, ref: str, *, strip_params: Iterable[str] = ()) -> str | None:
    """Resolve a (possibly relative) reference against `base` and normalize it."""
    ref = _CTRL_WS.sub("", ref.strip())
    if not ref or ref.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return None
    try:
        return normalize(urljoin(base, ref), strip_params=strip_params)
    except ValueError:
        return None


def _remove_dot_segments(path: str) -> str:
    if "." not in path:
        return path
    segs = path.split("/")
    out: list[str] = []
    last = len(segs) - 1
    for i, seg in enumerate(segs):
        if seg in (".", ".."):
            if seg == ".." and len(out) > 1:
                out.pop()
            if i == last:
                out.append("")
            continue
        out.append(seg)
    res = "/".join(out)
    return res if res.startswith("/") else "/" + res


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def path_and_query(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def looks_like_image(url: str) -> bool:
    return bool(IMAGE_EXT.search(urlsplit(url).path))


def looks_like_non_page(url: str) -> bool:
    return bool(NON_PAGE_EXT.search(urlsplit(url).path)) or looks_like_image(url)


def url_filename(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name[:255]


_DIGITS = re.compile(r"\d+")
_TOKEN = re.compile(r"^[0-9a-f-]{16,}$|^[A-Za-z0-9_-]{24,}$")


def url_pattern(url: str) -> str:
    """Collapse a URL into a coarse pattern for crawler-trap detection.

    /calendar/2024/05/12        -> host/calendar/N/N/N
    /search?color=red&size=m    -> host/search?color&size
    /p/3f2a9c0e-...-uuid         -> host/p/X
    """
    p = urlsplit(url)
    segs = []
    for seg in p.path.split("/"):
        if _TOKEN.match(seg):
            segs.append("X")
        else:
            segs.append(_DIGITS.sub("N", seg))
    pattern = (p.hostname or "") + "/".join(segs)
    if p.query:
        keys = sorted({kv.split("=", 1)[0] for kv in p.query.split("&") if kv})
        pattern += "?" + "&".join(keys)
    return pattern
