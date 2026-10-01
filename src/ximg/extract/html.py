"""Image references, links and assets from an HTML document."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser, LexborNode

from ximg.extract import Ref
from ximg.extract.css import extract_css
from ximg.extract.jsonld import jsonld_images
from ximg.extract.srcset import parse_srcset, pick
from ximg.urls import looks_like_image, looks_like_non_page, resolve

ICON_RELS = frozenset({"icon", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon", "fluid-icon"})
META_IMAGE_KEYS = frozenset({
    "og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src",
    "msapplication-tileimage", "msapplication-square70x70logo", "msapplication-square150x150logo",
    "msapplication-wide310x150logo", "msapplication-square310x310logo", "thumbnail", "image",
})  # fmt: skip
SRCSET_ATTRS = frozenset({"srcset", "data-srcset", "data-lazy-srcset"})
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class HtmlOptions:
    lazy_attrs: frozenset[str] = frozenset()
    srcset: str = "largest"
    data_uris: bool = True
    css: bool = True
    jsonld: bool = True
    manifest: bool = True


@dataclass
class HtmlResult:
    base: str
    images: list[Ref] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    stylesheets: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    browserconfigs: list[str] = field(default_factory=list)
    text_chars: int = 0
    mount_nodes: list[str] = field(default_factory=list)
    noscript_js: bool = False


class _Collector:
    def __init__(self, base: str, opts: HtmlOptions) -> None:
        self.opts = opts
        self.res = HtmlResult(base=base)
        self._seen: set[tuple[str, str]] = set()

    def image(self, raw: str | None, kind: str, context: str = "", base: str | None = None) -> None:
        if not raw:
            return
        raw = raw.strip()
        if raw.startswith("data:"):
            if not self.opts.data_uris or not raw[5:].lower().startswith("image/"):
                return
            url: str | None = raw
        else:
            url = resolve(base or self.res.base, raw)
        if url is None or (url, kind) in self._seen:
            return
        self._seen.add((url, kind))
        self.res.images.append(Ref(url, kind, _SPACES.sub(" ", context).strip()[:300]))

    def images(self, raws: Iterable[str], kind: str, context: str = "") -> None:
        for raw in raws:
            self.image(raw, kind, context)

    def link(self, raw: str | None) -> None:
        if raw and (url := resolve(self.res.base, raw)):
            self.res.links.append(url)

    def css_text(self, text: str, kind_prefix: str) -> None:
        if not self.opts.css and kind_prefix == "html:style":
            return
        for raw, kind in extract_css(text, srcset_mode=self.opts.srcset, kind_prefix=kind_prefix).images:
            self.image(raw, kind)


def extract_html(html: str, page_url: str, opts: HtmlOptions) -> HtmlResult:
    tree = LexborHTMLParser(html)
    base_node = tree.css_first("base[href]")
    base = page_url
    if base_node is not None and (href := base_node.attributes.get("href")):
        base = resolve(page_url, href) or page_url
    c = _Collector(base, opts)
    for node in tree.css("*"):
        _visit(node, c)
    if tree.body is not None:
        c.res.text_chars = len("".join((tree.body.text(separator=" ") or "").split()))
    return c.res


def _attrs(node: LexborNode) -> dict[str, str]:
    return {k.lower(): (v or "") for k, v in node.attributes.items()}


def _visit(node: LexborNode, c: _Collector) -> None:
    tag = node.tag
    a = _attrs(node)
    opts = c.opts
    has_srcset = any(a.get(s) for s in SRCSET_ATTRS)

    if tag == "img":
        alt = a.get("alt", "") or a.get("title", "")
        c.images(pick(parse_srcset(a.get("srcset", "")), opts.srcset), "html:img@srcset", alt)
        if not has_srcset or opts.srcset == "all":
            c.image(a.get("src"), "html:img@src", alt)
    elif tag == "source":
        parent = node.parent
        if parent is not None and parent.tag == "picture":
            c.images(pick(parse_srcset(a.get("srcset", "")), opts.srcset), "html:source@srcset")
    elif tag in ("a", "area"):
        href = a.get("href", "")
        url = resolve(c.res.base, href) if href else None
        if url and looks_like_image(url):
            c.image(href, f"html:{tag}@href", (node.text() or "").strip() or a.get("title", ""))
        elif url and not looks_like_non_page(url):
            c.res.links.append(url)
    elif tag in ("iframe", "frame"):
        c.link(a.get("src"))
    elif tag == "link":
        rels = set(a.get("rel", "").lower().split())
        href = a.get("href", "")
        if rels & ICON_RELS:
            c.image(href, "html:link@icon", a.get("sizes", ""))
        if "preload" in rels and a.get("as", "").lower() == "image":
            cands = parse_srcset(a.get("imagesrcset", ""))
            if cands:
                c.images(pick(cands, opts.srcset), "html:link@preload")
            else:
                c.image(href, "html:link@preload")
        if "image_src" in rels:
            c.image(href, "html:link@image_src")
        if "stylesheet" in rels and href and opts.css and (url := resolve(c.res.base, href)):
            c.res.stylesheets.append(url)
        if "manifest" in rels and href and opts.manifest and (url := resolve(c.res.base, href)):
            c.res.manifests.append(url)
        if rels & {"next", "prev", "alternate"} and href and not a.get("type"):
            c.link(href)
    elif tag == "meta":
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        content = a.get("content", "")
        if key in META_IMAGE_KEYS:
            c.image(content, f"html:meta@{key}")
        elif (
            key == "msapplication-config"
            and opts.manifest
            and content.lower() != "none"
            and (url := resolve(c.res.base, content))
        ):
            c.res.browserconfigs.append(url)
    elif tag == "video":
        c.image(a.get("poster"), "html:video@poster")
    elif tag == "input" and a.get("type", "").lower() == "image":
        c.image(a.get("src"), "html:input@src")
    elif tag in ("image", "feimage"):  # SVG <image href / xlink:href>
        c.image(a.get("href") or a.get("xlink:href"), "html:svg-image@href")
    elif tag in ("object", "embed"):
        raw = a.get("data") or a.get("src") or ""
        if raw and looks_like_image(resolve(c.res.base, raw) or ""):
            c.image(raw, f"html:{tag}@data")
    elif tag == "style":
        c.css_text(node.text() or "", "html:style")
    elif tag == "script":
        if opts.jsonld and a.get("type", "").lower() == "application/ld+json":
            c.images(jsonld_images(node.text() or ""), "html:json-ld")
    elif tag == "noscript":
        if "javascript" in (node.text() or "").lower():
            c.res.noscript_js = True
    elif tag == "div" and a.get("id") in ("root", "app", "__next", "__nuxt", "___gatsby", "svelte"):
        c.res.mount_nodes.append(a["id"])

    if a.get("background") and tag in ("body", "table", "td", "th", "tr"):
        c.image(a["background"], f"html:{tag}@background")
    if style := a.get("style"):
        c.css_text(style, "html:style-attr")
    for name, value in a.items():
        if not value:
            continue
        if name in opts.lazy_attrs:
            if name in SRCSET_ATTRS or "srcset" in name:
                c.images(pick(parse_srcset(value), opts.srcset), f"html:{tag}@{name}")
            else:
                c.image(value, f"html:{tag}@{name}")
        elif (
            name.startswith("data-") and " " not in value.strip() and looks_like_image(resolve(c.res.base, value) or "")
        ):
            c.image(value, f"html:{tag}@data-*")
