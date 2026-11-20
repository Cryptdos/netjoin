"""Optional Npcap + scapy features: fast raw ARP sweep and passive listening.

Everything here is optional. If Npcap isn't installed (or scapy isn't), these
features are reported as unavailable and the rest of NetJoin works as normal.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import bacnet

_scapy_lock = threading.Lock()
_scapy = None


def npcap_installed() -> bool:
    if sys.platform != "win32":
        return False
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.exists(os.path.join(root, "System32", "Npcap", "wpcap.dll"))


def scapy_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("scapy") is not None
    except Exception:
        return False


def status() -> dict:
    inst, sc = npcap_installed(), scapy_available()
    if inst and sc:
        note = "Npcap installed – passive listening and raw ARP sweep available"
    elif not inst:
        note = "Npcap not installed – passive listening and raw ARP sweep unavailable (install from npcap.com)"
    else:
        note = "scapy library missing – reinstall NetJoin's requirements"
    return {"installed": inst, "scapy": sc, "available": inst and sc, "note": note}


def _load():
    """Import scapy once (slow, ~1–3 s) and only when actually needed."""
    global _scapy
    with _scapy_lock:
        if _scapy is None:
            from scapy import all as scapy_all  # noqa: WPS433
            scapy_all.conf.verb = 0
            _scapy = scapy_all
    return _scapy


def _norm_mac(mac) -> str:
    return (mac or "").replace("-", ":").lower()


def _iface(sc, adapter_name: str, src_ip: str, mac: str | None = None):
    """Find Npcap's interface for this adapter, by name, IP or MAC.

    scapy builds its interface list when it's first loaded. Adapters plugged in later
    (USB dongles) or IPs changed by a Join aren't in it, so reload the list first.
    """
    try:
        sc.conf.ifaces.reload()
    except Exception:
        pass

    def matches(iface) -> bool:
        ips = getattr(iface, "ips", {}) or {}
        return (getattr(iface, "name", None) == adapter_name
                or src_ip in (ips.get(4, []) or [])
                or getattr(iface, "ip", None) == src_ip
                or (bool(mac) and _norm_mac(getattr(iface, "mac", "")) == _norm_mac(mac)))

    for iface in sc.conf.ifaces.values():
        if matches(iface):
            return iface
    seen = "; ".join(f"{getattr(i, 'name', '?')} [{getattr(i, 'ip', '') or '-'} {getattr(i, 'mac', '') or ''}]"
                     for i in sc.conf.ifaces.values())
    raise RuntimeError(f"Npcap can't see adapter '{adapter_name}' ({src_ip}, MAC {mac or '?'}). "
                       f"Adapters Npcap can see: {seen or 'none'}")


def arp_sweep(adapter_name: str, src_ip: str, cidr: str, timeout: float = 2.0, mac: str | None = None) -> dict[str, str]:
    """Raw ARP who-has for the whole subnet in one go → {ip: mac}."""
    sc = _load()
    iface = _iface(sc, adapter_name, src_ip, mac)
    ans, _ = sc.srp(sc.Ether(dst="ff:ff:ff:ff:ff:ff") / sc.ARP(pdst=cidr, psrc=src_ip), iface=iface,
                    timeout=timeout, retry=1, inter=0.002)
    return {r.psrc: r.hwsrc.upper().replace(":", "-") for _s, r in ans}


def passive_listen(adapter_name: str, src_ip: str, seconds: float, cancel, broad: bool = False,
                   mac: str | None = None) -> dict:
    """Listen without sending anything.

    → {'hosts': {ip: mac}, 'bacnet': [dev], 'broadcasts': {addr: count},
       'bcast_sources': {addr: [src ips]}, 'arp_asks': {target ip: [asking ips]}}
    broad=True also captures every link-layer broadcast UDP packet (NetBIOS, DHCP, discovery
    protocols...), which is what Discover uses to work out subnet masks.
    """
    sc = _load()
    iface = _iface(sc, adapter_name, src_ip, mac)
    hosts: dict[str, str] = {}
    devs: dict[str, dict] = {}
    bcasts: dict[str, int] = {}
    bsrc: dict[str, set] = {}
    asks: dict[str, set] = {}

    def handle(pkt):
        if sc.ARP in pkt:
            a = pkt[sc.ARP]
            if a.psrc and a.psrc != "0.0.0.0" and a.psrc != src_ip:
                hosts[a.psrc] = a.hwsrc.upper().replace(":", "-")
                if a.op == 1 and a.pdst and a.pdst != a.psrc:      # who-has: someone looking for pdst
                    asks.setdefault(a.pdst, set()).add(a.psrc)
            return
        if sc.IP in pkt and sc.UDP in pkt:
            ip, udp = pkt[sc.IP], pkt[sc.UDP]
            if ip.src == src_ip or ip.src == "0.0.0.0":
                return
            if sc.Ether in pkt:
                hosts.setdefault(ip.src, pkt[sc.Ether].src.upper().replace(":", "-"))
            if ip.dst == "255.255.255.255" or ip.dst.endswith(".255") or \
                    (sc.Ether in pkt and pkt[sc.Ether].dst.lower() == "ff:ff:ff:ff:ff:ff"):
                bcasts[ip.dst] = bcasts.get(ip.dst, 0) + 1
                bsrc.setdefault(ip.dst, set()).add(ip.src)
            d = bacnet.decode_iam_datagram(bytes(udp.payload), ip.src, udp.sport)
            if d:
                devs.setdefault(d["key"], d)

    bpf = "arp or (udp and (ether broadcast or portrange 47808-47823))" if broad else \
        "arp or udp portrange 47808-47823"
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cancel():
        sc.sniff(iface=iface, filter=bpf, prn=handle, store=False,
                 timeout=min(2.0, max(0.1, end - time.monotonic())))
    return {"hosts": hosts, "bacnet": list(devs.values()), "broadcasts": bcasts,
            "bcast_sources": {k: sorted(v) for k, v in bsrc.items()},
            "arp_asks": {k: sorted(v) for k, v in asks.items()}}
