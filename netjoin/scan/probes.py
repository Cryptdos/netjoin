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

    def bacnet_objects(self, src_ip: str, target, instance: int, cancel, progress) -> dict:
        from . import bacnet_client as bc
        with bc.Client(src_ip) as c:
            objs = bc.read_object_list(c, target, instance, cancel=cancel, progress=progress)
            return bc.read_objects(c, target, objs, cancel=cancel, progress=progress)

    def bacnet_values(self, src_ip: str, target, objects: list[tuple[int, int]]) -> dict:
        from . import bacnet_client as bc
        P = bc.P
        with bc.Client(src_ip) as c:
            out = {o: {} for o in objects}
            for k in range(0, len(objects), 16):
                chunk = objects[k:k + 16]
                specs = [(t, i, [P["present-value"], P["status-flags"], P["out-of-service"], P["event-state"], P["reliability"]]
                          + ([P["priority-array"]] if t in bc.COMMANDABLE else [])
                          + ([P["state-text"]] if t in bc.MULTISTATE else [])
                          + ([P["active-text"], P["inactive-text"]] if t in bc.BINARY else [])) for t, i in chunk]
                try:
                    got = c.rpm(target, specs)
                except bc.BacnetError as e:
                    if e.kind == "timeout":
                        raise
                    items = [(t, i, p, None) for t, i, ps in specs for p in ps]
                    vals = c.read_many(target, items)
                    got = {}
                    for (t, i, p, _), v in zip(items, vals):
                        got.setdefault((t, i), {})[p] = v
                for o in chunk:
                    out[o].update(got.get(o, {}))
            return out

    def bacnet_who_has(self, src_ip: str, broadcast: str, name: str) -> list[dict]:
        from . import bacnet_client as bc
        return bc.who_has(src_ip, broadcast, name)


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

    # ---- demo object lists (Desigo-style names)
    _FAKE_IPS = {2501: (10, None), 2502: (11, None), 2503: (12, None), 2601: (10, (2, b"\x05"))}

    def _fake_objects(self, instance: int) -> dict:
        import random
        from . import bacnet_client as bc
        P = bc.P
        rnd = random.Random(instance)
        objs: dict = {}
        ok = {"bits": [False, False, False, False]}

        def add(t, i, name, desc, pv, units=None, cmd=False, prio=None, states=None, texts=None, flags=None, oos=False, rel=0, es=0,
                relinquish=None):
            if prio:                                  # commanded: the highest priority in use wins
                pv = prio[min(prio)]
            p = {P["object-name"]: name, P["description"]: desc, P["present-value"]: pv,
                 P["status-flags"]: flags or ({"bits": [False, False, False, oos]}),
                 P["out-of-service"]: oos, P["event-state"]: {"enum": es}, P["reliability"]: {"enum": rel}}
            if units is not None:
                p[P["units"]] = {"enum": units}
            if cmd:
                pa = [None] * 16
                for lvl, v in (prio or {}).items():
                    pa[lvl - 1] = v
                p[P["priority-array"]] = pa
                p[P["relinquish-default"]] = relinquish if relinquish is not None else pv
            if states:
                p[P["state-text"]] = states
                p[P["number-of-states"]] = len(states)
            if texts:
                p[P["inactive-text"]], p[P["active-text"]] = texts
            objs[(t, i)] = p

        if instance == 2501:                        # PXC automation station: an AHU's worth of points
            pre = "AS01'AHU1'"
            for k, (nm, d, u, lo, hi) in enumerate([("SupTmp", "Supply air temperature", 62, 16, 20), ("RetTmp", "Return air temperature", 62, 20, 23),
                                                     ("OaTmp", "Outside air temperature", 62, 4, 12), ("ExhTmp", "Exhaust air temperature", 62, 19, 22),
                                                     ("SupHum", "Supply air humidity", 29, 35, 50), ("FltDp", "Filter differential pressure", 53, 80, 260),
                                                     ("SupPrs", "Supply duct pressure", 53, 250, 320), ("FrstTmp", "Frost coil temperature", 62, 8, 14)], start=1):
                add(0, k, pre + nm, d, round(rnd.uniform(lo, hi), 1), u)
            add(0, 9, pre + "CO2Ret", "Return air CO2", 712.0, 96, flags={"bits": [False, True, False, False]}, rel=1, es=1)   # no sensor
            for k in range(10, 41):
                add(0, k, f"{pre}Zn{k - 9:02d}Tmp", f"Zone {k - 9} temperature", round(rnd.uniform(19.5, 23.5), 1), 62)
            add(1, 1, pre + "VlvHtg", "Heating valve", 34.0, 98, cmd=True)
            add(1, 2, pre + "VlvClg", "Cooling valve", 0.0, 98, cmd=True)
            add(1, 3, pre + "SupFanSpd", "Supply fan speed", 65.0, 98, cmd=True, prio={8: 80.0}, relinquish=0.0)
            add(1, 4, pre + "ExhFanSpd", "Extract fan speed", 60.0, 98, cmd=True)
            add(1, 5, pre + "DmprOa", "Outside air damper", 100.0, 98, cmd=True)
            for k, (nm, d) in enumerate([("SupFanRun", "Supply fan running"), ("ExhFanRun", "Extract fan running"), ("FrstStat", "Frost stat"),
                                         ("FltAlm", "Filter dirty alarm"), ("FireAlm", "Fire alarm input"), ("PmpHtgRun", "Heating pump running")], start=1):
                add(3, k, pre + nm, d, {"enum": 0 if "Alm" in nm or "Frst" in nm else 1}, texts=("Normal", "Alarm") if "Alm" in nm or "Frst" in nm else ("Off", "On"),
                    flags={"bits": [nm == "FltAlm", False, False, False]}, es=2 if nm == "FltAlm" else 0)
            objs[(3, 4)][P["present-value"]] = {"enum": 1}
            for k, (nm, d) in enumerate([("SupFanCmd", "Supply fan command"), ("ExhFanCmd", "Extract fan command"), ("PmpHtgCmd", "Heating pump command")], start=1):
                add(4, k, pre + nm, d, {"enum": 1}, cmd=True, texts=("Off", "On"), prio={16: {"enum": 1}},
                    flags={"bits": [False, False, nm == "ExhFanCmd", False]})      # extract fan on its hand switch
            add(4, 4, pre + "PmpClgCmd", "Cooling pump command", {"enum": 0}, cmd=True, texts=("Off", "On"), oos=True)
            add(2, 1, pre + "SpSupTmp", "Supply temperature setpoint", 18.0, 62, cmd=True)
            add(2, 2, pre + "SpSupPrs", "Supply pressure setpoint", 280.0, 53, cmd=True, prio={8: 300.0}, relinquish=280.0)
            for k in range(3, 26):
                add(2, k, f"{pre}Prm{k:02d}", f"Parameter {k}", round(rnd.uniform(0, 100), 1), 95)
            add(19, 1, pre + "OpMod", "Plant operating mode", 2, states=["Off", "Comfort", "Economy", "Night purge", "Frost protection"], cmd=True)
            add(17, 1, "AS01'Sch'AHU1", "AHU1 time schedule", {"enum": 1})
            for k in range(1, 9):
                add(20, k, f"AS01'TL'AHU1'{k:02d}", f"Trend log {k}", None)
                del objs[(20, k)][P["present-value"]]
            add(15, 1, "AS01'NC'Urgent", "Notification class urgent", None)
            del objs[(15, 1)][P["present-value"]]
        else:                                       # DXR2 room controller
            nn = {2502: "RA01", 2503: "RA02", 2601: "RA03"}.get(instance, f"RA{instance % 100:02d}")
            pre = f"L2'{nn}'"
            add(0, 1, pre + "RTmp", "Room temperature", round(rnd.uniform(20.5, 22.8), 1), 62)
            add(0, 2, pre + "RHuRel", "Room humidity", round(rnd.uniform(38, 52), 1), 29)
            add(0, 3, pre + "RCO2", "Room CO2", round(rnd.uniform(450, 900)), 96)
            add(2, 1, pre + "SpTRCmf", "Comfort setpoint", 21.0, 62, cmd=True, prio={8: 23.5} if instance == 2503 else None, relinquish=21.0)
            add(2, 2, pre + "SpTREco", "Economy setpoint", 18.0, 62, cmd=True)
            add(2, 3, pre + "SpTRShft", "Setpoint shift (room unit)", 0.5, 63, cmd=True)
            add(1, 1, pre + "VlvHtg", "Heating valve", round(rnd.uniform(0, 60)), 98, cmd=True)
            add(1, 2, pre + "VlvClg", "Cooling valve", 0.0, 98, cmd=True, oos=instance == 2502)
            add(4, 1, pre + "Fan", "Fan", {"enum": 1}, cmd=True, texts=("Off", "On"))
            add(3, 1, pre + "PrsDet", "Presence detector", {"enum": 1}, texts=("Unoccupied", "Occupied"))
            add(3, 2, pre + "WndwCnt", "Window contact", {"enum": 1 if instance == 2503 else 0}, texts=("Closed", "Open"),
                flags={"bits": [instance == 2503, False, False, False]}, es=2 if instance == 2503 else 0)
            add(19, 1, pre + "OpMod", "Room operating mode", 1, states=["Comfort", "Precomfort", "Economy", "Protection"], cmd=True)
            add(19, 2, pre + "FanSpd", "Fan speed", 2, states=["Off", "Stage 1", "Stage 2", "Stage 3"], cmd=True,
                prio={8: 4} if instance == 2502 else None, relinquish=2)
            add(5, 1, pre + "OccOvr", "Occupancy override", {"enum": 0}, cmd=True, texts=("Off", "On"))
            for k in range(10, 22):
                add(2, k, f"{pre}Prm{k:02d}", f"Control parameter {k}", round(rnd.uniform(0, 10), 2), 95)
            add(20, 1, f"{pre}TL'RTmp", "Room temperature trend", None)
            del objs[(20, 1)][P["present-value"]]
        objs[(8, instance)] = {P["object-name"]: self._FAKE_DEVICES.get(instance, (f"Device {instance}",))[0]}
        return objs

    def bacnet_objects(self, src_ip, target, instance, cancel, progress):
        from . import bacnet_client as bc
        if target.ip.rsplit(".", 1)[-1] in self.gone:
            raise bc.BacnetError("timeout", "no reply")
        objs = self._fake_objects(instance)
        n = len(objs)
        progress("object list", 0, n)
        time.sleep(0.3)
        out = {}
        for k, (oid, props) in enumerate(objs.items(), start=1):
            if cancel():
                break
            out[oid] = props
            if k % 8 == 0:
                progress("objects", k, n)
                time.sleep(0.04 if n < 100 else 0.02)
        progress("objects", n, n)
        return out

    def bacnet_values(self, src_ip, target, objects):
        import random
        from . import bacnet_client as bc
        inst = next((i for i, (last, route) in self._FAKE_IPS.items()
                     if target.ip.endswith(f".{last}") and (route is None) == (target.dnet is None)), None)
        objs = self._fake_objects(inst) if inst else {}
        out = {}
        for o in objects:
            p = dict(objs.get(o, {}))
            pv = p.get(bc.P["present-value"])
            if isinstance(pv, float) and o[0] in (0,):
                p[bc.P["present-value"]] = round(pv + random.uniform(-0.3, 0.3), 1)
            out[o] = p
        time.sleep(0.1)
        return out

    def bacnet_who_has(self, src_ip, broadcast, name):
        time.sleep(0.6)
        b = self._base(src_ip)
        out = []
        for inst, (last, route) in self._FAKE_IPS.items():
            ip = f"{b}.{last}"
            if not self._here(ip):
                continue
            for oid, props in self._fake_objects(inst).items():
                if props.get(77) == name and oid[0] != 8:
                    from . import bacnet
                    key = bacnet.device_key(ip, route[0] if route else None, route[1] if route else None)
                    out.append({"device": inst, "object": list(oid), "name": name, "ip": ip, "key": key,
                                "route": bacnet.route_text(route[0], route[1]) if route else None})
        return out

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
