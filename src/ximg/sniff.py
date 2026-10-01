"""Image type detection from magic bytes (never trust the URL or Content-Type)."""

FORMATS = frozenset({"jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tiff", "heic", "jxl"})
EXTENSIONS = {
    "jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "avif": "avif", "svg": "svg",
    "ico": "ico", "bmp": "bmp", "tiff": "tiff", "heic": "heic", "jxl": "jxl",
}  # fmt: skip

# Enough bytes to decide every format, including SVGs with an XML prolog / comments / doctype.
SNIFF_BYTES = 4096

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff(head: bytes) -> str | None:
    """Return the image format of `head` (the first bytes of a file), or None if it isn't an image."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return _sniff_ftyp(head)
    if head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00") and len(head) >= 6 and head[4:6] != b"\x00\x00":
        return "ico"
    if head[:2] == b"BM" and len(head) >= 14:
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "jxl"
    if _looks_like_svg(head):
        return "svg"
    return None


def _sniff_ftyp(head: bytes) -> str | None:
    size = int.from_bytes(head[:4], "big")
    brands = {head[8:12]}
    brands.update(head[i : i + 4] for i in range(16, min(size, len(head)) - 3, 4))
    if brands & {b"avif", b"avis"}:
        return "avif"
    if brands & _HEIF_BRANDS:
        return "heic"
    return None


def _looks_like_svg(head: bytes) -> bool:
    text = head[:SNIFF_BYTES].decode("utf-8", errors="ignore").lstrip("﻿ \t\r\n").lower()
    if not text.startswith(("<?xml", "<!--", "<svg", "<!doctype svg")):
        return False
    svg_at = text.find("<svg")
    html_at = text.find("<html")
    return svg_at != -1 and (html_at == -1 or svg_at < html_at)
