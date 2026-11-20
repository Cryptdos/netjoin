"""Windows back-end: psutil for addresses, netsh for DHCP/gateway/DNS/DAD and for changes.

netsh output is parsed in English. On a non-English Windows some fields (DHCP flag,
gateway, DNS) may come back empty; addresses still work because they come from psutil.
"""

from __future__ import annotations

import csv
import ctypes
import io
import re
import socket
import subprocess
import time

import psutil

from .. import ipcalc
from .base import Adapter, Address, Backend, NetError, looks_virtual

CREATE_NO_WINDOW = 0x08000000
GW_METRIC = 500  # keep any BMS gateway well below Wi-Fi in route preference


def _run(cmd: str, timeout: float = 15) -> tuple[int, str]:
    """Run a command line string (not a list: netsh does its own argument parsing)."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, shell=False)
    except subprocess.TimeoutExpired:
        raise NetError(f"Timed out: {cmd}")
    raw = p.stdout + p.stderr
    try:
        text = raw.decode("oem", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return p.returncode, text.strip()


def _q(name: str) -> str:
    if '"' in name:
        raise NetError("Adapter name contains a quote character")
    return f'"{name}"'


def _parse_config(text: str) -> dict[str, dict]:
    """Parse `netsh interface ipv4 show config`."""
    out: dict[str, dict] = {}
    cur: dict | None = None
    last_key = None
    for line in text.splitlines():
        m = re.match(r'\s*Configuration for interface "(.+)"', line)
        if m:
            cur = {"dhcp": None, "gateways": [], "dns": []}
            out[m.group(1)] = cur
            last_key = None
            continue
        if cur is None or not line.strip():
            continue
        if ":" in line and not re.match(r"^\s{20,}\S", line):
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            last_key = key
        else:
            key, val = last_key, line.strip()
        if key is None:
            continue
        if key == "dhcp enabled":
            cur["dhcp"] = val.lower().startswith("yes")
        elif key == "default gateway" and ipcalc.valid_ip(val):
            cur["gateways"].append(val)
        elif key and "dns servers" in key:
            first = val.split()[0] if val else ""
            if ipcalc.valid_ip(first):
                cur["dns"].append(first)
    return out


def _parse_ipaddresses(text: str) -> dict[str, list[tuple[str, str, str]]]:
    """Parse `netsh interface ipv4 show ipaddresses` → {iface: [(type, dad, ip)]}."""
    out: dict[str, list] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"\s*Interface \d+: (.+)$", line)
        if m:
            cur = out.setdefault(m.group(1).strip(), [])
            continue
        toks = line.split()
        if cur is not None and len(toks) >= 5 and ipcalc.valid_ip(toks[-1]):
            cur.append((toks[0].lower(), toks[1].lower(), toks[-1]))
    return out


def _descriptions() -> dict[str, tuple[str, str]]:
    """Connection name → (description, MAC) via the built-in getmac tool."""
    try:
        code, text = _run("getmac /v /fo csv /nh", timeout=10)
    except NetError:
        return {}
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3:
            out[row[0]] = (row[1], row[2])
    return out


class WindowsBackend(Backend):
    kind = "windows"

    def is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def list_adapters(self) -> list[Adapter]:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        _, cfg_text = _run("netsh interface ipv4 show config")
        cfg = _parse_config(cfg_text)
        _, ip_text = _run("netsh interface ipv4 show ipaddresses")
        dad = _parse_ipaddresses(ip_text)
        desc = _descriptions()
        out = []
        for name, items in addrs.items():
            d, mac = desc.get(name, ("", ""))
            a = Adapter(name=name, description=d, mac=mac,
                        up=bool(stats.get(name) and stats[name].isup),
                        virtual=looks_virtual(name, d))
            for it in items:
                if it.family == socket.AF_INET and it.netmask:
                    prefix = sum(bin(int(x)).count("1") for x in it.netmask.split("."))
                    state = next(((t, s) for t, s, ip in dad.get(name, []) if ip == it.address), (None, None))
                    a.addresses.append(Address(it.address, prefix, dad=state[1], source=state[0]))
                elif it.family == psutil.AF_LINK and not a.mac:
                    a.mac = it.address
            c = cfg.get(name)
            if c:
                a.dhcp, a.gateways, a.dns = c["dhcp"], c["gateways"], c["dns"]
            if name.lower().startswith("loopback"):
                continue
            out.append(a)
        out.sort(key=lambda x: (x.virtual, not x.up, x.name.lower()))
        return out

    def _check(self, code: int, text: str, ok_phrases: tuple[str, ...] = ()) -> None:
        if code != 0 and not any(p in text.lower() for p in ok_phrases):
            if "elevation" in text.lower() or "administrator" in text.lower():
                raise NetError("NetJoin needs to run as administrator to change adapter settings")
            raise NetError(text or f"netsh failed with code {code}")

    def set_static(self, name: str, ip: str, prefix: int, gateway: str | None, dns: list[str]) -> None:
        mask = ipcalc.mask_of(prefix)
        gw = f"gateway={gateway} gwmetric={GW_METRIC}" if gateway else "gateway=none"
        self._check(*_run(f"netsh interface ipv4 set address name={_q(name)} source=static "
                          f"address={ip} mask={mask} {gw}"))
        self.set_dns(name, dns)

    def set_dns(self, name: str, dns: list[str]) -> None:
        if dns:
            self._check(*_run(f"netsh interface ipv4 set dnsservers name={_q(name)} source=static "
                              f"address={dns[0]} register=none validate=no"))
            for i, extra in enumerate(dns[1:], start=2):
                self._check(*_run(f"netsh interface ipv4 add dnsservers name={_q(name)} "
                                  f"address={extra} index={i} validate=no"))
        else:
            self._check(*_run(f"netsh interface ipv4 set dnsservers name={_q(name)} source=static "
                              f"address=none register=none validate=no"), ("element not found",))

    def set_gateway(self, name: str, ip: str, prefix: int, gateway: str | None) -> None:
        # Swap only the default route on this adapter. The address isn't removed and re-added (which
        # 'set address' would do), so TCP sessions to devices on the subnet stay up.
        _run(f"netsh interface ipv4 delete route prefix=0.0.0.0/0 interface={_q(name)} store=active")
        _run(f"netsh interface ipv4 delete route prefix=0.0.0.0/0 interface={_q(name)} store=persistent")
        if gateway:
            self._check(*_run(f"netsh interface ipv4 add route prefix=0.0.0.0/0 interface={_q(name)} "
                              f"nexthop={gateway} metric={GW_METRIC} store=persistent"), ("object already exists",))

    def set_dhcp(self, name: str) -> None:
        self._check(*_run(f"netsh interface ipv4 set address name={_q(name)} source=dhcp"),
                    ("already enabled",))
        self._check(*_run(f"netsh interface ipv4 set dnsservers name={_q(name)} source=dhcp"),
                    ("already enabled",))

    def dad_state(self, name: str, ip: str) -> str | None:
        _, text = _run("netsh interface ipv4 show ipaddresses")
        for _type, state, addr in _parse_ipaddresses(text).get(name, []):
            if addr == ip:
                return state
        return None

    def wait_dad(self, name: str, ip: str, timeout: float = 4.0) -> str | None:
        end = time.monotonic() + timeout
        state = None
        while time.monotonic() < end:
            state = self.dad_state(name, ip)
            if state in ("preferred", "duplicate"):
                return state
            time.sleep(0.4)
        return state
