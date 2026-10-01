"""Saving image bytes: stream to temp, hash, sniff, filter, dedupe, atomic rename."""

import base64
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote_to_bytes

from ximg.config import Filters, Limits
from ximg.sniff import EXTENSIONS, SNIFF_BYTES, sniff
from ximg.store import IMAGES_DIR, Store

_HOST_SAFE = re.compile(r"[^a-z0-9.-]")


@dataclass(frozen=True)
class SaveResult:
    outcome: Literal["saved", "duplicate", "skipped"]
    sha256: str | None = None
    path: str | None = None
    size: int = 0
    format: str | None = None
    skip_reason: str | None = None

    @property
    def stored(self) -> bool:
        return self.outcome in ("saved", "duplicate")


def host_dir(host: str, port: int | None = None) -> str:
    name = _HOST_SAFE.sub("_", host.lower()).strip(".") or "_"
    return f"{name}_{port}" if port else name


class ImageSaver:
    def __init__(self, store: Store, filters: Filters, limits: Limits) -> None:
        self._store = store
        self._min = filters.min_file_bytes
        self._max = limits.max_file_bytes
        self._formats = frozenset(filters.formats)

    def precheck(self, declared_length: int | None) -> str | None:
        """Skip before reading the body when Content-Length already rules the file out."""
        if declared_length is None:
            return None
        if declared_length > self._max:
            return "filter_size_max"
        if declared_length < self._min:
            return "filter_size"
        return None

    async def save_stream(self, chunks: AsyncIterator[bytes], *, host_folder: str) -> SaveResult:
        tmp = self._store.tmp_dir / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        head = bytearray()
        size = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max:
                        return SaveResult("skipped", skip_reason="filter_size_max", size=size)
                    if len(head) < SNIFF_BYTES:
                        head += chunk[: SNIFF_BYTES - len(head)]
                        if len(head) >= SNIFF_BYTES and sniff(bytes(head)) is None:
                            return SaveResult("skipped", skip_reason="not_image", size=size)
                    digest.update(chunk)
                    fh.write(chunk)
            return self._finish(tmp, digest.hexdigest(), bytes(head), size, host_folder)
        finally:
            tmp.unlink(missing_ok=True)

    def save_bytes(self, data: bytes, *, host_folder: str) -> SaveResult:
        if len(data) > self._max:
            return SaveResult("skipped", skip_reason="filter_size_max", size=len(data))
        tmp = self._store.tmp_dir / f"{uuid.uuid4().hex}.part"
        try:
            tmp.write_bytes(data)
            return self._finish(tmp, hashlib.sha256(data).hexdigest(), data[:SNIFF_BYTES], len(data), host_folder)
        finally:
            tmp.unlink(missing_ok=True)

    def _finish(self, tmp: Path, sha: str, head: bytes, size: int, host_folder: str) -> SaveResult:
        # No awaits from here on: check-then-insert is atomic w.r.t. other tasks.
        fmt = sniff(head)
        if fmt is None:
            return SaveResult("skipped", skip_reason="not_image", size=size)
        if fmt not in self._formats:
            return SaveResult("skipped", skip_reason="filter_format", size=size, format=fmt)
        if size < self._min:
            return SaveResult("skipped", skip_reason="filter_size", size=size, format=fmt)
        if existing := self._store.file_path(sha):
            return SaveResult("duplicate", sha, existing, size, fmt)
        rel = f"{IMAGES_DIR}/{host_folder}/{sha}.{EXTENSIONS[fmt]}"
        final = self._store.out / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        self._store.add_file(sha, rel, size, fmt)
        return SaveResult("saved", sha, rel, size, fmt)


_DATA_URI = re.compile(r"^data:([^;,]*)((?:;[^;,]*)*),(.*)$", re.S)


def decode_data_uri(uri: str) -> tuple[str, bytes] | None:
    """data:image/png;base64,.... -> ("image/png", bytes); None if malformed."""
    m = _DATA_URI.match(uri.strip())
    if not m:
        return None
    mime, params, payload = m.group(1).lower(), m.group(2).lower(), m.group(3)
    try:
        if ";base64" in params:
            data = base64.b64decode(re.sub(r"\s+", "", unquote_to_bytes(payload).decode("ascii", "ignore")))
        else:
            data = unquote_to_bytes(payload)
    except ValueError:
        return None
    return mime, data


def data_uri_key(mime: str, data: bytes) -> str:
    """Short stable identifier stored in image_url.url instead of the (huge) data URI."""
    return f"data:{mime or 'application/octet-stream'};sha256={hashlib.sha256(data).hexdigest()}"
