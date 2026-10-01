"""Sitemaps (index, urlset, image sitemaps, .gz, plain-text) and site-level asset files."""

import json
import zlib
from dataclasses import dataclass, field

from lxml import etree

MAX_DECOMPRESSED = 100 * 1024 * 1024
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)


@dataclass
class SitemapResult:
    sitemaps: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)


def maybe_gunzip(body: bytes) -> bytes:
    if body[:2] != b"\x1f\x8b":
        return body
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, MAX_DECOMPRESSED)
    if d.unconsumed_tail:
        raise ValueError("sitemap decompresses beyond the size cap")
    return out


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_sitemap(body: bytes) -> SitemapResult:
    res = SitemapResult()
    body = maybe_gunzip(body).lstrip()
    if not body.startswith(b"<"):
        # Plain-text sitemap: one URL per line.
        for line in body.decode("utf-8", "replace").splitlines():
            if line.strip().startswith(("http://", "https://")):
                res.pages.append(line.strip())
        return res
    try:
        root = etree.fromstring(body, _PARSER)
    except etree.XMLSyntaxError:
        return res
    if root is None:
        return res
    root_name = _local(root.tag)
    for el in root.iter():
        name = _local(el.tag)
        if name != "loc" or not el.text:
            continue
        parent = _local(el.getparent().tag) if el.getparent() is not None else ""
        url = el.text.strip()
        if parent == "sitemap" or root_name == "sitemapindex":
            res.sitemaps.append(url)
        elif parent == "image":
            res.images.append(url)
        elif parent == "url":
            res.pages.append(url)
    return res


def manifest_images(body: bytes) -> list[tuple[str, str]]:
    """Web app manifest: icons[].src, screenshots[].src, shortcuts[].icons[].src."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError, RecursionError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for key in ("icons", "screenshots"):
        for item in data.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("src"), str):
                out.append((item["src"], f"manifest:{key[:-1]}"))
    for sc in data.get("shortcuts") or []:
        for item in (sc.get("icons") or []) if isinstance(sc, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("src"), str):
                out.append((item["src"], "manifest:shortcut-icon"))
    return out


def browserconfig_images(body: bytes) -> list[str]:
    """browserconfig.xml: <square150x150logo src="..."/> etc."""
    try:
        root = etree.fromstring(body.lstrip(), _PARSER)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []
    return [str(el.get("src")) for el in root.iter() if isinstance(el.tag, str) and el.get("src")]
