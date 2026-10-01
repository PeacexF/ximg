"""Manifest exports written next to images/ for downstream tools."""

import csv
import json
import os
from pathlib import Path
from typing import Any

from ximg.store import Store

MANIFEST_COLUMNS = [
    "file", "sha256", "bytes", "format", "image_url", "final_url", "url_filename",
    "page_url", "kind", "context", "status", "content_type", "fetched_at",
]  # fmt: skip
URL_COLUMNS = ["image_url", "state", "skip_reason", "error", "sha256", "file", "status", "source"]

_MANIFEST_SQL = """
SELECT f.path AS file, f.sha256, f.bytes, f.format,
       u.url AS image_url, u.final_url, u.url_filename, u.status, u.content_type, u.fetched_at,
       p.url AS page_url, o.kind, o.context
FROM image_url u
JOIN file f ON f.sha256 = u.sha256
LEFT JOIN occurrence o ON o.image_url_id = u.id
LEFT JOIN page p ON p.id = o.page_id
WHERE u.state = 'done'
ORDER BY f.path, u.url, p.url, o.kind
"""


def _atomic_write(path: Path, write: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        write(fh)
    os.replace(tmp, path)


def export_all(store: Store) -> list[Path]:
    rows = [dict(r) for r in store.conn.execute(_MANIFEST_SQL)]
    out = store.out

    def write_csv(fh: Any) -> None:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    _atomic_write(out / "manifest.csv", write_csv)

    files: dict[str, dict[str, Any]] = {}
    for r in rows:
        f = files.setdefault(r["sha256"], {
            "file": r["file"], "sha256": r["sha256"], "bytes": r["bytes"], "format": r["format"], "urls": {},
        })  # fmt: skip
        u = f["urls"].setdefault(r["image_url"], {
            "url": r["image_url"], "final_url": r["final_url"], "url_filename": r["url_filename"],
            "status": r["status"], "content_type": r["content_type"], "fetched_at": r["fetched_at"],
            "referenced_by": [],
        })  # fmt: skip
        if r["kind"]:
            u["referenced_by"].append({"page": r["page_url"], "kind": r["kind"], "context": r["context"]})
    doc = {
        "target": json.loads(store.meta("config_json") or "{}").get("seeds", []),
        "status": store.meta("status"),
        "files": [{**f, "urls": list(f["urls"].values())} for f in files.values()],
    }
    _atomic_write(out / "manifest.json", lambda fh: json.dump(doc, fh, indent=1))

    url_rows = store.conn.execute(
        "SELECT u.url AS image_url, u.state, u.skip_reason, u.error, u.sha256, f.path AS file, u.status, u.source"
        " FROM image_url u LEFT JOIN file f ON f.sha256 = u.sha256 ORDER BY u.id"
    ).fetchall()

    def write_urls(fh: Any) -> None:
        w = csv.writer(fh)
        w.writerow(URL_COLUMNS)
        w.writerows(tuple(r) for r in url_rows)

    _atomic_write(out / "urls.csv", write_urls)
    return [out / "manifest.csv", out / "manifest.json", out / "urls.csv"]


def export_friendly(store: Store) -> int:
    """Hardlink tree mirroring the site: friendly/<host>/<url path>/<original filename>."""
    from urllib.parse import unquote, urlsplit

    root = store.out / "friendly"
    count = 0
    seen: set[Path] = set()
    for r in store.conn.execute(
        "SELECT u.final_url, u.url, f.path FROM image_url u JOIN file f ON f.sha256=u.sha256 WHERE u.state='done'"
    ):
        url = r[0] or r[1]
        if url.startswith("data:"):
            continue
        parts = urlsplit(url)
        segs = [s for s in (_safe(unquote(x)) for x in parts.path.split("/")) if s]
        if not segs:
            segs = ["index"]
        src = store.out / r[2]
        ext = src.suffix
        name = segs[-1] if segs[-1].lower().endswith(ext.lower()) else segs[-1] + ext
        target = root.joinpath(_safe(parts.hostname or "_"), *segs[:-1], name)
        stem, n = target.with_suffix(""), 1
        while target in seen or target.exists():
            if target.exists() and target.samefile(src):
                break
            target = stem.with_name(f"{stem.name}~{n}").with_suffix(ext)
            n += 1
        seen.add(target)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, target)
        except OSError:
            import shutil

            shutil.copy2(src, target)
        count += 1
    return count


def _safe(seg: str) -> str:
    seg = seg.replace("\\", "_").replace("\x00", "")
    if seg in ("", ".", ".."):
        return ""
    return "".join(c if c.isprintable() and c not in '<>:"|?*' else "_" for c in seg)[:150]
