"""Image URLs from JSON-LD structured data."""

import json
from typing import Any

IMAGE_KEYS = frozenset({"image", "logo", "thumbnailUrl", "thumbnail", "contentUrl", "photo", "primaryImageOfPage"})


def jsonld_images(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError, RecursionError:
        return []
    out: list[str] = []
    _walk(data, in_image=False, out=out)
    return list(dict.fromkeys(out))


def _walk(node: Any, *, in_image: bool, out: list[str], depth: int = 0) -> None:
    if depth > 50:
        return
    if isinstance(node, str):
        if in_image:
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            _walk(item, in_image=in_image, out=out, depth=depth + 1)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in IMAGE_KEYS or (in_image and key in ("url", "contentUrl")):
                _walk(value, in_image=True, out=out, depth=depth + 1)
            elif isinstance(value, (dict, list)):
                _walk(value, in_image=False, out=out, depth=depth + 1)
