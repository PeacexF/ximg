"""Image URLs and @imports from CSS text. A tokenizer-lite: we only need URLs."""

import re
from dataclasses import dataclass, field

from ximg.extract.srcset import Candidate, _descriptor, pick

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?(?:"([^"]+)"|'([^']+)'|([^\s'");]+))\s*\)?[^;]*;?""", re.I)
_FONT_FACE = re.compile(r"@font-face\s*\{[^}]*\}", re.I)
_IMAGE_SET = re.compile(r"(?:-webkit-)?image-set\(((?:[^()]|\((?:[^()]|\([^()]*\))*\))*)\)", re.I)
_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))\s*\)""", re.I)
_STRING = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)')""")
_NOT_IMAGE = re.compile(r"\.(woff2?|ttf|otf|eot|css|js|htc|cur)(?:[?#]|$)", re.I)


@dataclass
class CssResult:
    images: list[tuple[str, str]] = field(default_factory=list)  # (raw url, kind)
    imports: list[str] = field(default_factory=list)


def _first(m: re.Match[str]) -> str:
    return next((g for g in m.groups() if g is not None), "").strip()


def _split_top_level(s: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, c in enumerate(s):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def _usable(url: str) -> bool:
    if not url or url.startswith("#"):
        return False
    if url.startswith("data:"):
        return url[5:].lower().startswith("image/")
    return not _NOT_IMAGE.search(url)


def extract_css(text: str, *, srcset_mode: str = "largest", kind_prefix: str = "css") -> CssResult:
    res = CssResult()
    text = _COMMENT.sub(" ", text)

    def take_import(m: re.Match[str]) -> str:
        res.imports.append(_first(m))
        return " "

    text = _IMPORT.sub(take_import, text)
    text = _FONT_FACE.sub(" ", text)

    def take_image_set(m: re.Match[str]) -> str:
        cands: list[Candidate] = []
        for item in _split_top_level(m.group(1)):
            item = item.strip()
            um = _URL.match(item) or _STRING.match(item)
            if not um:
                continue
            url = _first(um)
            if _usable(url):
                cands.append(_descriptor(Candidate(url), item[um.end() :]))
        res.images.extend((u, f"{kind_prefix}:image-set") for u in pick(cands, srcset_mode))
        return " "

    text = _IMAGE_SET.sub(take_image_set, text)
    for m in _URL.finditer(text):
        url = _first(m)
        if _usable(url):
            res.images.append((url, f"{kind_prefix}:url"))
    return res
