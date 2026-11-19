"""Activity log: in-memory ring buffer for the UI plus a plain text file.

The file is written directly (not via the logging module) so nothing else in the
process – libraries that reconfigure logging, a second process – can silence it.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

MAX_BYTES = 512_000
KEEP = 3


class EventLog:
    def __init__(self, file: Path | None = None, size: int = 300):
        self._buf: deque = deque(maxlen=size)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.file = Path(file) if file else None

    def add(self, msg: str, level: str = "info") -> None:
        now = datetime.now()
        with self._lock:
            self._buf.append({"id": next(self._ids), "ts": now.strftime("%H:%M:%S"), "level": level, "msg": msg})
            self._write(f"{now:%Y-%m-%d %H:%M:%S} {level.upper():5} {msg}\n")

    def _write(self, line: str) -> None:
        if not self.file:
            return
        try:
            if self.file.exists() and self.file.stat().st_size > MAX_BYTES:
                self._rotate()
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass   # never let logging break the app

    def _rotate(self) -> None:
        for i in range(KEEP - 1, 0, -1):
            src = self.file.with_name(f"{self.file.name}.{i}")
            if src.exists():
                src.replace(self.file.with_name(f"{self.file.name}.{i + 1}"))
        self.file.replace(self.file.with_name(f"{self.file.name}.1"))

    def since(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._buf if e["id"] > after]
