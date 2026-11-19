from __future__ import annotations

import sys

from .base import Adapter, Address, Backend, NetError  # noqa: F401


def get_backend(fake: bool = False) -> Backend:
    if fake or sys.platform != "win32":
        from .fake import FakeBackend
        return FakeBackend()
    from .windows import WindowsBackend
    return WindowsBackend()
