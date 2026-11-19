"""Internet check through one specific adapter (by binding to its IP).

Two separate questions, because both are needed to load web pages:
  1. Route: can this adapter reach the internet by IP address?
  2. DNS:   do this adapter's DNS servers answer a real query (not Windows' cache)?

Result codes (stored in history.internet):
  INTERNET (1)  route and DNS both work
  IP_ONLY  (2)  route works but no DNS servers are set, or none answered: web pages won't load
  NONE     (0)  no route to the internet
"""

from __future__ import annotations

import random
import socket
import struct

NONE, INTERNET, IP_ONLY = 0, 1, 2

FALLBACKS = (("1.1.1.1", 443), ("8.8.8.8", 443))
DNS_TEST_NAME = "www.msftconnecttest.com"


def _tcp(src_ip: str, dst: tuple[str, int], timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.bind((src_ip, 0))
            s.connect(dst)
            return True
    except OSError:
        return False


def route_ok(src_ip: str, timeout: float = 2.5) -> bool:
    """Reach the internet *from src_ip*. Windows' strong host model keeps the traffic on that adapter."""
    return any(_tcp(src_ip, dst, timeout) for dst in FALLBACKS)


def dns_query(server: str, src_ip: str, name: str = DNS_TEST_NAME, timeout: float = 2.0) -> bool:
    """Send a real DNS A query to `server` from `src_ip` and check for a successful answer."""
    tid = random.randint(0, 0xFFFF)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    packet = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.bind((src_ip, 0))
            s.sendto(packet, (server, 53))
            data, _ = s.recvfrom(1500)
    except OSError:
        return False
    if len(data) < 12:
        return False
    rid, flags, _qd, ancount = struct.unpack(">HHHH", data[:8])
    return rid == tid and (flags & 0x8000) and (flags & 0x000F) == 0 and ancount > 0


def check(src_ip: str, dns_servers: list[str]) -> dict:
    """→ {'code': 0|1|2, 'route': bool, 'dns_ok': bool|None, 'dns_servers': [...], 'detail': str}"""
    route = route_ok(src_ip)
    if not route:
        return {"code": NONE, "route": False, "dns_ok": None, "dns_servers": dns_servers,
                "detail": "no route to the internet from this adapter"}
    if not dns_servers:
        return {"code": IP_ONLY, "route": True, "dns_ok": None, "dns_servers": [],
                "detail": "internet reachable by IP, but no DNS servers are set – web pages won't load"}
    for server in dns_servers:
        if dns_query(server, src_ip):
            return {"code": INTERNET, "route": True, "dns_ok": True, "dns_servers": dns_servers,
                    "detail": f"route and DNS ({server}) both working"}
    return {"code": IP_ONLY, "route": True, "dns_ok": False, "dns_servers": dns_servers,
            "detail": f"internet reachable by IP, but DNS server(s) {', '.join(dns_servers)} didn't answer"}


def describe(code) -> str:
    return {INTERNET: "yes", IP_ONLY: "by IP only (no working DNS)", NONE: "no"}.get(code, "unknown")


# Kept for compatibility with older callers.
def check_internet(src_ip: str, timeout: float = 2.5) -> bool:
    return route_ok(src_ip, timeout)
