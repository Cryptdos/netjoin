"""Small IPv4 helpers built on the standard ipaddress module."""

from __future__ import annotations

import ipaddress
from typing import Iterator


def parse_subnet(text: str) -> ipaddress.IPv4Network:
    """Accept '10.20.5.0/24', '10.20.5.7/24' or '10.20.5.0 255.255.255.0'."""
    text = text.strip().replace(" ", "/")
    return ipaddress.IPv4Network(text, strict=False)


def default_range(net: ipaddress.IPv4Network) -> tuple[str, str]:
    """Default laptop range: .200–.254 on a /24, else the top 55 hosts."""
    hosts_last = int(net.broadcast_address) - 1
    if net.prefixlen == 24:
        start = int(net.network_address) + 200
    else:
        start = max(int(net.network_address) + 1, hosts_last - 54)
    return str(ipaddress.IPv4Address(start)), str(ipaddress.IPv4Address(hosts_last))


def in_subnet(ip: str, net: ipaddress.IPv4Network) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return addr in net and addr not in (net.network_address, net.broadcast_address)


def iter_range(start: str, end: str, net: ipaddress.IPv4Network) -> Iterator[str]:
    a, b = int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end))
    if a > b:
        a, b = b, a
    for n in range(a, b + 1):
        ip = str(ipaddress.IPv4Address(n))
        if in_subnet(ip, net):
            yield ip


def mask_of(prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)


def valid_ip(text: str | None) -> bool:
    if not text:
        return False
    try:
        ipaddress.IPv4Address(text.strip())
        return True
    except ValueError:
        return False


def expand(text: str | None, net: ipaddress.IPv4Network) -> str:
    """Expand shorthand to a full address inside `net`: '35' / '.35' → x.y.z.35, '3.35' → x.y.3.35.

    A full address is returned as it is. Raises ValueError for anything else.
    """
    t = (text or "").strip().lstrip(".")
    if not t:
        return ""
    if valid_ip(t):
        return t
    parts = t.split(".")
    if len(parts) > 3 or not all(p.isdigit() and int(p) <= 255 for p in parts):
        raise ValueError(f"'{text}' isn't an address")
    base = str(net.network_address).split(".")
    return ".".join(base[:4 - len(parts)] + [str(int(p)) for p in parts])


def first_host(net: ipaddress.IPv4Network) -> str:
    return str(ipaddress.IPv4Address(int(net.network_address) + 1))
