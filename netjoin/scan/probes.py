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

    # ---- per-device BACnet reads (device pages); read-only
    def bacnet_device_props(self, src_ip: str, target, instance: int) -> dict:
        from . import bacnet_client as bc
        with bc.Client(src_ip) as c:
            return bc.read_device_properties(c, target, instance)


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

    def __init__(self):
        import os
        # last octets of devices "unplugged" (tests / demo of inactive devices), e.g. NETJOIN_FAKE_GONE=11,mstp
        self.gone: set[str] = {x.strip() for x in os.environ.get("NETJOIN_FAKE_GONE", "").split(",") if x.strip()}

    def _here(self, ip: str) -> bool:
        return ip.rsplit(".", 1)[-1] not in self.gone

    def _base(self, ip):
        return ip.rsplit(".", 1)[0]

    def _hosts(self, src_ip):
        b = self._base(src_ip)
        return {f"{b}.1": "00-1B-1B-00-00-01", f"{b}.10": "00-A0-03-11-22-10", f"{b}.11": "00-A0-03-11-22-11",
                f"{b}.12": "00-A0-03-11-22-12", f"{b}.200": "3C-52-82-AA-BB-CC"}

    def arp(self, ip, src_ip):
        time.sleep(0.004)
        return self._hosts(src_ip).get(ip) if self._here(ip) else None

    def ping(self, ip, src_ip, timeout_ms=600):
        import random
        time.sleep(0.004)
        if not (ip in self._hosts(src_ip) and not ip.endswith(".12") and self._here(ip)):
            return None
        return round(random.uniform(0.8, 3.6), 1) if random.random() > 0.03 else None    # the odd lost reply

    _FAKE_DEVICES = {
        2501: ("AS01 East Panel", "PXC5.E003", "FW=04.21.040", "AS01_East_v2.3"),
        2502: ("RA L2-01", "DXR2.E18-102A", "FW=02.20.010", "Hvac_01.31"),
        2503: ("RA L2-02", "DXR2.E18-102A", "FW=02.10.004", "Hvac_01.31"),     # older firmware than its twin
        2601: ("RA L2-03 (MS/TP)", "DXR2.M18-101A", "FW=02.20.010", "Hvac_01.29"),
    }

    def bacnet_device_props(self, src_ip, target, instance):
        from . import bacnet_client as bc
        time.sleep(0.15)
        if target.ip.rsplit(".", 1)[-1] in self.gone:
            raise bc.BacnetError("timeout", "no reply")
        name, model, fw, app = self._FAKE_DEVICES.get(instance, (f"Device {instance}", "Unknown", "1.0", "1.0"))
        now = time.localtime()
        skew = 0 if instance != 2503 else 437          # one controller with a clock seven minutes out
        t = time.localtime(time.time() + skew)
        return {"object-name": name, "vendor-name": "Siemens Schweiz AG", "vendor-identifier": 7, "model-name": model,
                "firmware-revision": fw, "application-software-version": app, "protocol-version": 1,
                "protocol-revision": 14 if instance != 2501 else 19, "location": "Level 2" if instance != 2501 else "Plant room",
                "description": "Room automation" if instance != 2501 else "Automation station",
                "system-status": {"enum": 0}, "max-apdu-length-accepted": 1476 if instance == 2501 else 480,
                "segmentation-supported": {"enum": 0 if instance == 2501 else 3}, "database-revision": 12,
                "local-date": {"date": time.strftime("%Y-%m-%d", t)}, "local-time": {"time": time.strftime("%H:%M:%S", t)},
                "utc-offset": 0, "serial-number": {"error": "unknown property"}}

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
        return [d for d in out if self._here(d["ip"]) and not (d["route"] and "mstp" in self.gone)]

    def discover_extra(self) -> dict:
        """Demo observations: a BMS subnet with broadcasts + BACnet, and a second subnet with no broadcasts."""
        hosts = {"10.20.5.1": "00-1B-1B-00-00-01", "10.20.5.10": "00-A0-03-11-22-10",
                 "10.20.5.11": "00-A0-03-11-22-11", "10.20.5.12": "00-A0-03-11-22-12",
                 "10.20.5.200": "3C-52-82-AA-BB-CC", "192.168.50.20": "00-10-70-15-CA-99",
                 "192.168.50.21": "00-10-70-15-CA-9A"}
        bac = []
        for last, inst in ((10, 2501), (11, 2502), (12, 2503)):
            ip = f"10.20.5.{last}"
            bac.append({"instance": inst, "vendor_id": 7, "ip": ip, "port": 47808, "snet": None, "sadr": None,
                        "key": ip, "route": None, "vendor": "Siemens", "name": None, "model": None})
        bac.append({"instance": 1011, "vendor_id": 0, "ip": "192.168.50.20", "port": 47808, "snet": None,
                    "sadr": None, "key": "192.168.50.20", "route": None, "vendor": None, "name": None, "model": None})
        return {"hosts": hosts, "bacnet": bac,
                "bcast_sources": {"10.20.5.255": ["10.20.5.10", "10.20.5.11", "10.20.5.12"]},
                "arp_asks": {"10.20.5.1": ["10.20.5.10", "10.20.5.11"]}}
