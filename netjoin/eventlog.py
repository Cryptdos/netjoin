"""Activity log: in-memory ring buffer for the UI plus a rotating file."""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class EventLog:
    def __init__(self, file: Path | None = None, size: int = 300):
        self._buf: deque = deque(maxlen=size)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._file_log = logging.getLogger("netjoin")
        self._file_log.setLevel(logging.INFO)
        if file and not self._file_log.handlers:
            h = RotatingFileHandler(file, maxBytes=512_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._file_log.addHandler(h)

    def add(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self._buf.append({"id": next(self._ids), "ts": datetime.now().strftime("%H:%M:%S"),
                              "level": level, "msg": msg})
        self._file_log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, msg)

    def since(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._buf if e["id"] > after]
