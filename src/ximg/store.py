"""Output directory lifecycle and the SQLite state/provenance database.

All access happens on the event-loop thread through one connection, so every
method is atomic with respect to other tasks (there are no awaits inside).
"""

import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ximg import __version__
from ximg.config import Config

SCHEMA_VERSION = "1"
STATE_DIR = ".ximg"
IMAGES_DIR = "images"
EXPORT_FILES = ("manifest.csv", "manifest.json", "urls.csv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS run (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  status       TEXT NOT NULL,
  ximg_version TEXT NOT NULL
);

-- Everything fetched that isn't an image: HTML pages, sitemaps, stylesheets, manifests.
CREATE TABLE IF NOT EXISTS page (
  id              INTEGER PRIMARY KEY,
  url             TEXT NOT NULL UNIQUE,
  kind            TEXT NOT NULL DEFAULT 'page',  -- page|sitemap|css|manifest|browserconfig
  depth           INTEGER NOT NULL,
  discovered_from INTEGER,
  pattern         TEXT,
  state           TEXT NOT NULL DEFAULT 'queued', -- queued|in_progress|done|failed|skipped
  skip_reason     TEXT,
  status          INTEGER,
  final_url       TEXT,
  content_type    TEXT,
  rendered        INTEGER NOT NULL DEFAULT 0,
  fetched_at      TEXT,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS page_queue ON page(state, kind, depth, id);
CREATE INDEX IF NOT EXISTS page_pattern ON page(pattern, state);

CREATE TABLE IF NOT EXISTS file (
  sha256     TEXT PRIMARY KEY,
  path       TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  format     TEXT NOT NULL,
  first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_url (
  id           INTEGER PRIMARY KEY,
  url          TEXT NOT NULL UNIQUE,
  state        TEXT NOT NULL DEFAULT 'queued',  -- queued|in_progress|done|failed|skipped
  skip_reason  TEXT,
  sha256       TEXT,
  final_url    TEXT,
  status       INTEGER,
  content_type TEXT,
  headers_json TEXT,
  url_filename TEXT,
  source       TEXT NOT NULL DEFAULT 'live',    -- live|wayback|data-uri|list
  fetched_at   TEXT,
  error        TEXT
);
CREATE INDEX IF NOT EXISTS image_queue ON image_url(state, id);
CREATE INDEX IF NOT EXISTS image_sha ON image_url(sha256);

CREATE TABLE IF NOT EXISTS occurrence (
  image_url_id INTEGER NOT NULL,
  page_id      INTEGER,
  kind         TEXT NOT NULL,
  context      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS occurrence_key ON occurrence(image_url_id, IFNULL(page_id, 0), kind);
CREATE INDEX IF NOT EXISTS occurrence_page ON occurrence(page_id);
"""


class OutputError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PageRow:
    id: int
    url: str
    kind: str
    depth: int


@dataclass(frozen=True)
class ImageRow:
    id: int
    url: str
    source: str


class Store:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.state_dir = out / STATE_DIR
        self.tmp_dir = self.state_dir / "tmp"
        self.log_dir = self.state_dir / "logs"
        self.images_dir = out / IMAGES_DIR
        for d in (self.state_dir, self.tmp_dir, self.log_dir, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "ximg.db"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._tx_depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Group many writes into one transaction (re-entrant)."""
        if self._tx_depth == 0:
            self.conn.execute("BEGIN")
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self.conn.execute("COMMIT")

    # -- meta --------------------------------------------------------------------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO run(started_at, status, ximg_version) VALUES (?, 'running', ?)", (now(), __version__)
        )
        self.set_meta("status", "running")
        return int(cur.lastrowid or 0)

    def end_run(self, run_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE run SET ended_at=?, status=? WHERE id=?", (now(), status, run_id))
            self.set_meta("status", status)

    # -- pages -------------------------------------------------------------------------
    def add_page(
        self,
        url: str,
        *,
        kind: str = "page",
        depth: int = 0,
        parent: int | None = None,
        pattern: str | None = None,
        skip_reason: str | None = None,
    ) -> int | None:
        """Insert if new; returns the new row id, or None if the URL was already known."""
        state = "skipped" if skip_reason else "queued"
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO page(url, kind, depth, discovered_from, pattern, state, skip_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, kind, depth, parent, pattern, state, skip_reason),
        )
        return int(cur.lastrowid) if cur.rowcount and cur.lastrowid else None

    def pattern_count(self, pattern: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM page WHERE pattern=? AND state != 'skipped'", (pattern,)
        ).fetchone()
        return int(row[0])

    def pages_started(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM page WHERE kind='page' AND state IN ('in_progress', 'done', 'failed')"
        ).fetchone()
        return int(row[0])

    def claim_page(self, *, allow_pages: bool) -> PageRow | None:
        """Next queued resource: non-page assets first (cheap, needed for coverage), then BFS pages."""
        row = self.conn.execute(
            "SELECT id, url, kind, depth FROM page WHERE state='queued' AND kind != 'page' ORDER BY depth, id LIMIT 1"
        ).fetchone()
        if row is None and allow_pages:
            row = self.conn.execute(
                "SELECT id, url, kind, depth FROM page WHERE state='queued' AND kind='page' ORDER BY depth, id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE page SET state='in_progress' WHERE id=?", (row["id"],))
        return PageRow(row["id"], row["url"], row["kind"], row["depth"])

    def finish_page(self, page_id: int, state: str, **fields: Any) -> None:
        fields = {"state": state, "fetched_at": now(), **fields}
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE page SET {cols} WHERE id=?", (*fields.values(), page_id))

    def queued_pages(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM page WHERE state='queued'").fetchone()[0])

    # -- images ------------------------------------------------------------------------
    def add_image(self, url: str, *, source: str = "live", skip_reason: str | None = None) -> tuple[int, bool]:
        state = "skipped" if skip_reason else "queued"
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO image_url(url, state, skip_reason, source) VALUES (?, ?, ?, ?)",
            (url, state, skip_reason, source),
        )
        if cur.rowcount and cur.lastrowid:
            return int(cur.lastrowid), True
        row = self.conn.execute("SELECT id FROM image_url WHERE url=?", (url,)).fetchone()
        return int(row[0]), False

    def add_occurrence(self, image_id: int, page_id: int | None, kind: str, context: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO occurrence(image_url_id, page_id, kind, context) VALUES (?, ?, ?, ?)",
            (image_id, page_id, kind, context[:300] or None),
        )

    def claim_image(self) -> ImageRow | None:
        row = self.conn.execute(
            "SELECT id, url, source FROM image_url WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE image_url SET state='in_progress' WHERE id=?", (row["id"],))
        return ImageRow(row["id"], row["url"], row["source"])

    def finish_image(self, image_id: int, state: str, **fields: Any) -> None:
        fields = {"state": state, "fetched_at": now(), **fields}
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE image_url SET {cols} WHERE id=?", (*fields.values(), image_id))

    def queued_images(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM image_url WHERE state='queued'").fetchone()[0])

    def requeue_failed(self) -> tuple[int, int]:
        with self.tx():
            p = self.conn.execute("UPDATE page SET state='queued', error=NULL WHERE state='failed'").rowcount
            i = self.conn.execute("UPDATE image_url SET state='queued', error=NULL WHERE state='failed'").rowcount
        return p, i

    # -- files -------------------------------------------------------------------------
    def file_path(self, sha256: str) -> str | None:
        row = self.conn.execute("SELECT path FROM file WHERE sha256=?", (sha256,)).fetchone()
        return row[0] if row else None

    def add_file(self, sha256: str, path: str, size: int, fmt: str) -> None:
        self.conn.execute(
            "INSERT INTO file(sha256, path, bytes, format, first_seen) VALUES (?, ?, ?, ?, ?)",
            (sha256, path, size, fmt, now()),
        )

    def total_bytes(self) -> int:
        return int(self.conn.execute("SELECT IFNULL(SUM(bytes), 0) FROM file").fetchone()[0])

    # -- recovery ----------------------------------------------------------------------
    def recover(self) -> dict[str, int]:
        """After a crash: requeue in-flight work, drop temp files and files with no DB row."""
        with self.tx():
            pages = self.conn.execute("UPDATE page SET state='queued' WHERE state='in_progress'").rowcount
            images = self.conn.execute("UPDATE image_url SET state='queued' WHERE state='in_progress'").rowcount
        for tmp in self.tmp_dir.iterdir():
            tmp.unlink(missing_ok=True)
        known = {r[0] for r in self.conn.execute("SELECT path FROM file")}
        orphans = 0
        for f in self.images_dir.rglob("*"):
            if f.is_file() and f.relative_to(self.out).as_posix() not in known:
                f.unlink()
                orphans += 1
        return {"pages": pages, "images": images, "orphans": orphans}

    # -- reporting ---------------------------------------------------------------------
    def counts(self) -> dict[str, Any]:
        c = self.conn
        pages = dict(c.execute("SELECT state, COUNT(*) FROM page WHERE kind='page' GROUP BY state").fetchall())
        assets = dict(c.execute("SELECT state, COUNT(*) FROM page WHERE kind!='page' GROUP BY state").fetchall())
        images = dict(c.execute("SELECT state, COUNT(*) FROM image_url GROUP BY state").fetchall())
        skips = dict(
            c.execute(
                "SELECT skip_reason, COUNT(*) FROM image_url WHERE state='skipped' GROUP BY skip_reason"
            ).fetchall()
        )
        files, size = c.execute("SELECT COUNT(*), IFNULL(SUM(bytes), 0) FROM file").fetchone()
        return {"pages": pages, "assets": assets, "images": images, "image_skips": skips,
                "files": files, "bytes": size}  # fmt: skip


def open_output(cfg: Config, *, overwrite: bool = False, force_config: bool = False) -> Store:
    """Create, resume or (with overwrite) reset an output directory. Never touches unrelated files."""
    assert cfg.out is not None
    out = cfg.out
    db = out / STATE_DIR / "ximg.db"
    if db.exists():
        store = Store(out)
        status = store.meta("status")
        if status == "finished":
            if not overwrite:
                store.close()
                raise OutputError(
                    f"{out} already holds a finished run. Use a new --out directory, or --overwrite to wipe it."
                )
            store.close()
            wipe_output(out)
            return _init(Store(out), cfg)
        if store.meta("scope_key") != cfg.scope_key() and not force_config:
            store.close()
            raise OutputError(
                f"{out} holds an unfinished run with different seeds/scope. "
                "Resume with the same config, pass --force-config, or use a new --out."
            )
        store.set_meta("config_json", json.dumps(cfg.snapshot()))
        store.set_meta("scope_key", cfg.scope_key())
        return store
    if out.exists() and any(out.iterdir()):
        raise OutputError(f"{out} exists and is not empty (and isn't an ximg output directory)")
    return _init(Store(out), cfg)


def _init(store: Store, cfg: Config) -> Store:
    with store.tx():
        store.set_meta("schema_version", SCHEMA_VERSION)
        store.set_meta("created_at", now())
        store.set_meta("config_json", json.dumps(cfg.snapshot()))
        store.set_meta("scope_key", cfg.scope_key())
        store.set_meta("status", "new")
    return store


def wipe_output(out: Path) -> None:
    """Remove exactly what ximg writes; leave anything else (and the directory) alone."""
    for name in (STATE_DIR, IMAGES_DIR):
        shutil.rmtree(out / name, ignore_errors=True)
    for name in (*EXPORT_FILES, "friendly"):
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def is_output_dir(out: Path) -> bool:
    return (out / STATE_DIR / "ximg.db").exists()
