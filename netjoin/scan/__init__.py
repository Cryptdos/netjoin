"""Phase 2 scanning: ARP / ping sweeps, BACnet Who-Is, BMS port check, passive listen (Npcap)."""

from .manager import KINDS, ScanError, ScanManager  # noqa: F401


def get_prober(backend_kind: str):
    if backend_kind == "windows":
        from .probes import WindowsProber
        return WindowsProber()
    from .probes import FakeProber
    return FakeProber()
