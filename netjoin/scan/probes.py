"""Host probes. Windows: SendARP and IcmpSendEcho2Ex from iphlpapi (built in, no Npcap)."""

from __future__ import annotations

import ctypes
import socket
import struct
import time

from . import bacnet

# Common BMS / building-network TCP ports. Label shown in the UI.
BMS_PORTS = {
    80: "HTTP", 443: "HTTPS", 8080: "HTTP-alt", 502: "Modbus TCP", 1911: "Niagara Fox",
    4911: "Niagara Fox TLS", 22: "SSH", 23: "Telnet",
}
WEB_PORTS = (80, 443, 8080)


def tcp_open(ip: str, port: int, src_ip: str | None, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            if src_ip:
                s.bind((src_ip, 0))
            return s.connect_ex((ip, port)) == 0
        except OSError:
            return False


class Prober:
    """Base: generic TCP and BACnet; ARP / ping need an OS implementation."""
    kind = "base"

    def arp(self, ip: str, src_ip: str) -> str | None:
        return None

    def ping(self, ip: str, src_ip: str, timeout_ms: int = 600) -> float | None:
        return None

    def tcp(self, ip: str, port: int, src_ip: str) -> bool:
        return tcp_open(ip, port, src_ip)

    def who_is(self, src_ip: str, prefix: int, cancel, log) -> list[dict]:
        return bacnet.who_is(src_ip, prefix, cancel=cancel, log=log)


def _ipaddr(ip: str) -> int:
    """IPv4 string → IPAddr (network byte order in a ULONG)."""
    return struct.unpack("<I", socket.inet_aton(ip))[0]


class WindowsProber(Prober):
    kind = "windows"

    def __init__(self):
        lib = ctypes.WinDLL("iphlpapi")
        self._send_arp = lib.SendARP
        self._send_arp.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self._send_arp.restype = ctypes.c_ulong
        self._create = lib.IcmpCreateFile
        self._create.restype = ctypes.c_void_p
        self._close = lib.IcmpCloseHandle
        self._close.argtypes = [ctypes.c_void_p]
        self._echo = lib.IcmpSendEcho2Ex
        self._echo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        self._echo.restype = ctypes.c_ulong

    def arp(self, ip, src_ip):
        mac = (ctypes.c_ubyte * 8)()
        length = ctypes.c_ulong(8)
        if self._send_arp(_ipaddr(ip), _ipaddr(src_ip), mac, ctypes.byref(length)) != 0:
            return None
        if length.value < 6:
            return None
        return "-".join(f"{b:02X}" for b in mac[:6])

    def ping(self, ip, src_ip, timeout_ms=600):
        handle = self._create()
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            payload = b"NetJoin-ping"
            reply = ctypes.create_string_buffer(256)
            t0 = time.perf_counter()
            n = self._echo(handle, None, None, None, _ipaddr(src_ip), _ipaddr(ip), payload, len(payload),
                           None, reply, ctypes.sizeof(reply), timeout_ms)
            if n == 0:
                return None
            addr, status, rtt = struct.unpack_from("<III", reply.raw, 0)
            if status != 0 or addr != _ipaddr(ip):
                return None
            return float(rtt) if rtt else round((time.perf_counter() - t0) * 1000, 1)
        finally:
            self._close(handle)


class FakeProber(Prober):
    """Demo data: a few Siemens controllers plus a routed MS/TP device, on whatever subnet you're on."""
    kind = "fake"

    def _base(self, ip):
        return ip.rsplit(".", 1)[0]

    def _hosts(self, src_ip):
        b = self._base(src_ip)
        return {f"{b}.1": "00-1B-1B-00-00-01", f"{b}.10": "00-A0-03-11-22-10", f"{b}.11": "00-A0-03-11-22-11",
                f"{b}.12": "00-A0-03-11-22-12", f"{b}.200": "3C-52-82-AA-BB-CC"}

    def arp(self, ip, src_ip):
        time.sleep(0.004)
        return self._hosts(src_ip).get(ip)

    def ping(self, ip, src_ip, timeout_ms=600):
        time.sleep(0.004)
        return 2.0 if ip in self._hosts(src_ip) and not ip.endswith(".12") else None

    def tcp(self, ip, port, src_ip):
        return (ip.endswith(".10") or ip.endswith(".11")) and port in (80, 443) or (ip.endswith(".1") and port == 443)

    def who_is(self, src_ip, prefix, cancel, log):
        time.sleep(0.5)
        b = self._base(src_ip)
        out = []
        for last, inst, name, model in ((10, 2501, "AS01 East Panel", "PXC5.E003"),
                                        (11, 2502, "RA L2-01", "DXR2.E18-102A"),
                                        (12, 2503, "RA L2-02", "DXR2.E18-102A")):
            ip = f"{b}.{last}"
            out.append({"instance": inst, "vendor_id": 7, "ip": ip, "port": 47808, "snet": None, "sadr": None,
                        "key": ip, "route": None, "vendor": "Siemens Schweiz AG", "name": name, "model": model})
        out.append({"instance": 2601, "vendor_id": 7, "ip": f"{b}.10", "port": 47808, "snet": 2, "sadr": b"\x05",
                    "key": f"{b}.10|2:05", "route": "net 2, MAC 5", "vendor": "Siemens Schweiz AG",
                    "name": "RA L2-03 (MS/TP)", "model": "DXR2.M18-101A"})
        return out
