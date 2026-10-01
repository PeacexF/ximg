"""Structured JSON-lines logging to <out>/.ximg/logs plus a human console handler."""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            entry.update(data)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = getattr(record, "data", None)
        extra = " ".join(f"{k}={v}" for k, v in data.items()) if isinstance(data, dict) else ""
        return f"{record.levelname.lower()}: {record.getMessage()} {extra}".rstrip()


def setup(log_dir: Path | None, verbosity: int) -> Path | None:
    logger = logging.getLogger("ximg")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    console.setLevel({-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG))
    logger.addHandler(console)
    if log_dir is None:
        return None
    path = log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)
    return path
