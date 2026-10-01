"""Original-resolution rewrite rules for known CDN resize URL patterns."""

import re
from collections.abc import Sequence

from ximg.config import UpgradeRule

# Built-in rules, enabled with `upgrade = ["name", ...]` (or ["all"]) in the config.
BUILTIN: dict[str, tuple[str, str]] = {
    # WordPress: image-300x200.jpg -> image.jpg
    "wordpress-size-suffix": (r"^(.*/wp-content/uploads/.*?)-\d+x\d+(\.\w+)(\?.*)?$", r"\1\2"),
    # Generic sizing query params: ?w=300&h=200 / ?width= / ?resize= / ?fit=
    "strip-size-params": (
        r"^([^?]+)\?(?:(?:w|h|width|height|resize|fit|size|quality|q|crop)=[^&]*&?)+$",
        r"\1",
    ),
    # Cloudinary transformations: /upload/w_300,c_fill/ -> /upload/
    "cloudinary": (r"^(.*/image/upload/)(?:[a-z]{1,3}_[^/]+/)+(v\d+/.*)$", r"\1\2"),
    # Shopify: image_300x.jpg / image_small.jpg -> image.jpg
    "shopify-size-suffix": (
        r"^(.*cdn\.shopify\.com/.*?)_(?:\d+x\d*|\d*x\d+|pico|icon|thumb|small|compact|medium|large|grande)"
        r"(?:@\dx)?(\.\w+)(\?.*)?$",
        r"\1\2\3",
    ),
}


class Upgrader:
    def __init__(self, rules: Sequence[UpgradeRule], builtins: Sequence[str] = ()) -> None:
        pairs = [BUILTIN[name] for name in builtins] + [(r.match, r.replace) for r in rules]
        self._rules = [(re.compile(pattern), repl) for pattern, repl in pairs]

    def variants(self, url: str) -> list[str]:
        out: list[str] = []
        for rx, repl in self._rules:
            if rx.search(url):
                new = rx.sub(repl, url)
                if new != url and new not in out:
                    out.append(new)
        return out
