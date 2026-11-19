"""MAC address → manufacturer, offline.

Uses the Wireshark manufacturer database that ships inside scapy (no download, no Npcap needed).
Note: this names the maker of the network chip, which is usually but not always the device brand.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_db = None
_tried = False

RANDOMISED = "Randomised MAC (privacy)"


def is_randomised(mac: str) -> bool:
    """Locally administered bit set: phones/PCs using private MACs, VMs, some docks."""
    try:
        return bool(int(mac.replace("-", ":").split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def _load():
    global _db, _tried
    with _lock:
        if not _tried:
            _tried = True
            try:
                from .npcap import _load as load_scapy
                _db = load_scapy().conf.manufdb
            except Exception:
                _db = None
    return _db


def available() -> bool:
    return _load() is not None


def lookup(mac: str | None) -> str | None:
    if not mac:
        return None
    if is_randomised(mac):
        return RANDOMISED
    db = _load()
    if db is None:
        return None
    m = mac.replace("-", ":").lower()
    try:                                   # scapy ≥ 2.5: (short, long)
        r = db.lookup(m)
        if isinstance(r, tuple):
            name = r[1] or r[0]
            if name and name.lower() != m:
                return name.strip()
    except Exception:
        pass
    try:                                   # older scapy: short name, or the MAC back if unknown
        r = db._get_manuf(m)
        if r and r.lower() != m:
            return r.strip()
    except Exception:
        pass
    return None
