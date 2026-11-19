"""Where NetJoin keeps its data.

Default: %APPDATA%\\NetJoin (transparent to the user, survives moving the exe).
Portable mode: if a folder called ``data`` sits next to the exe (or the repo
root when running from source), that folder is used instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "netjoin" / "static"


def data_dir() -> Path:
    portable = app_dir() / "data"
    if portable.is_dir():
        return portable
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    d = root / "NetJoin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_portable() -> bool:
    return (app_dir() / "data").is_dir()
