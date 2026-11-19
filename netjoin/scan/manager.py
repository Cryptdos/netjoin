"""Runs one scan at a time in the background and reports progress to the UI."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import npcap, oui
from .probes import BMS_PORTS, Prober

MAX_SWEEP_HOSTS = 1024
WORKERS = 64

KINDS = {
    "quick":   {"label": "Quick scan (ARP + BACnet)", "steps": ["arp", "bacnet"]},
    "arp":     {"label": "ARP sweep", "steps": ["arp"]},
    "ping":    {"label": "Ping sweep", "steps": ["ping"]},
    "bacnet":  {"label": "BACnet Who-Is", "steps": ["bacnet"]},
    "ports":   {"label": "BMS port check", "steps": ["ports"]},
    "passive": {"label": "Passive listen (30 s)", "steps": ["passive"], "npcap": True},
    "full":    {"label": "Full scan", "steps": ["arp", "ping", "bacnet", "ports"]},
}


class ScanError(RuntimeError):
    pass


class ScanManager:
    def __init__(self, engine, prober: Prober):
        self.engine, self.prober = engine, prober
        self.store, self.log = engine.store, engine.log
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.state: dict = {"running": False}

    # ---------- public ----------
    def options(self) -> list[dict]:
        np = npcap.status() if self.prober.kind == "windows" else {"available": False}
        out = []
        for k, v in KINDS.items():
            needs = v.get("npcap", False)
            out.append({"kind": k, "label": v["label"], "available": (not needs) or np["available"],
                        "note": "Needs Npcap" if needs and not np["available"] else None})
        return out

    def status(self) -> dict:
        return dict(self.state)

    def cancel(self) -> None:
        self._cancel.set()

    def start(self, kind: str, network_id: int | None = None) -> dict:
        if kind not in KINDS:
            raise ScanError("Unknown scan type")
        if KINDS[kind].get("npcap") and not next(o for o in self.options() if o["kind"] == kind)["available"]:
            raise ScanError("That scan needs Npcap, which isn't installed")
        a = self.engine.adapter()
        p = a.primary
        if not p:
            raise ScanError(f"{a.name} has no IPv4 address")
        here = ipaddress.IPv4Network(f"{p.ip}/{p.prefix}", strict=False)
        if network_id is not None:
            n = self.store.node(network_id)
            if not n or n["kind"] != "network":
                raise ScanError("That isn't a network")
            target = ipaddress.IPv4Network(n["subnet"])
            if ipaddress.IPv4Address(p.ip) not in target:
                raise ScanError(f"{a.name} isn't on {target} – Join this network first, then scan")
        with self._lock:
            if self.state.get("running"):
                raise ScanError("A scan is already running")
            self._cancel.clear()
            self.state = {"running": True, "kind": kind, "label": KINDS[kind]["label"], "subnet": str(here),
                          "adapter": a.name, "phase": "starting", "done": 0, "total": 0, "found": 0,
                          "started": datetime.now().strftime("%H:%M:%S"), "error": None}
        threading.Thread(target=self._run, args=(kind, a.name, p.ip, here), daemon=True).start()
        return self.status()

    # ---------- worker ----------
    def _set(self, **kw):
        self.state.update(kw)

    def _run(self, kind, adapter, src_ip, net: ipaddress.IPv4Network):
        subnet = str(net)
        t0 = time.monotonic()
        self.log.add(f"{KINDS[kind]['label']} on {subnet} from {adapter} ({src_ip})")
        count = lambda: sum(1 for d in self.store.devices(subnet) if d["key"] != src_ip)  # noqa: E731
        before = count()
        try:
            try:
                my_mac = self.engine.adapter(adapter).mac or None
            except Exception:
                my_mac = None
            self.store.upsert_device(subnet, src_ip, src_ip, "self", name=socket.gethostname(), mac=my_mac)
            for step in KINDS[kind]["steps"]:
                if self._cancel.is_set():
                    break
                getattr(self, f"_step_{step}")(adapter, src_ip, net)
            if not self._cancel.is_set():
                self._enrich(src_ip, net)
            self.store.set_setting(f"lastscan:{subnet}", {"ts": datetime.now().isoformat(timespec="seconds"),
                                                          "kind": KINDS[kind]["label"]})
            found = count()
            took = time.monotonic() - t0
            if self._cancel.is_set():
                self.log.add(f"Scan cancelled after {took:.0f} s", "warn")
            else:
                new = found - before
                self.log.add(f"{KINDS[kind]['label']} done in {took:.1f} s – {found} device(s) known on {subnet}"
                             f"{f' ({new} new)' if new > 0 else ''}")
            self._set(running=False, phase="done", found=found)
        except Exception as e:   # report, never kill the server
            self.log.add(f"Scan failed: {e}", "error")
            self._set(running=False, phase="failed", error=str(e))

    def _enrich(self, src_ip, net):
        """Fill in missing MACs (Windows ARP lookup, only for hosts we found) and MAC manufacturers."""
        subnet = str(net)
        devs = self.store.devices(subnet)
        need_mac = sorted({d["ip"] for d in devs if not d["mac"] and "|" not in d["key"] and d["ip"] != src_ip})
        got = 0
        if need_mac:
            for ip, mac in self._sweep("Looking up MAC addresses", need_mac, lambda ip: self.prober.arp(ip, src_ip)):
                self.store.upsert_device(subnet, ip, ip, "arp", mac=mac)
                got += 1
        named = 0
        self._set(phase="Looking up manufacturers", done=0, total=1)
        for d in self.store.devices(subnet):
            if d["mac"] and not d["mac_vendor"]:
                v = oui.lookup(d["mac"])
                if v:
                    self.store.upsert_device(subnet, d["key"], d["ip"], d["sources"].split(",")[0] or "arp",
                                             mac_vendor=v)
                    named += 1
        if got or named:
            self.log.add(f"Filled in {got} MAC address(es) and {named} manufacturer(s)"
                         f"{'' if oui.available() else ' (manufacturer list unavailable – scapy missing)'}")

    def _hosts(self, src_ip, net):
        if net.num_addresses - 2 > MAX_SWEEP_HOSTS:
            raise ScanError(f"{net} has {net.num_addresses - 2} addresses – sweeps are limited to "
                            f"{MAX_SWEEP_HOSTS}. BACnet Who-Is still works on big subnets.")
        return [str(h) for h in net.hosts() if str(h) != src_ip]

    def _sweep(self, phase, items, fn):
        """Run fn over items with a thread pool, updating progress. Yields (item, result) for hits."""
        self._set(phase=phase, done=0, total=len(items))
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(fn, it): it for it in items}
            for i, f in enumerate(as_completed(futures), 1):
                if self._cancel.is_set():
                    for other in futures:
                        other.cancel()
                    break
                self._set(done=i)
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r:
                    yield futures[f], r

    def _step_arp(self, adapter, src_ip, net):
        subnet = str(net)
        np = npcap.status() if self.prober.kind == "windows" else {"available": False}
        if np["available"]:
            self._set(phase="ARP sweep (Npcap)", done=0, total=1)
            try:
                hits = npcap.arp_sweep(adapter, src_ip, subnet)
                for ip, mac in hits.items():
                    self.store.upsert_device(subnet, ip, ip, "arp", mac=mac)
                self._set(done=1, found=len(hits))
                self.log.add(f"ARP sweep (Npcap): {len(hits)} host(s) answered")
                return
            except Exception as e:
                self.log.add(f"Npcap ARP sweep failed ({e}); falling back to Windows ARP", "warn")
        hosts = self._hosts(src_ip, net)
        n = 0
        for ip, mac in self._sweep("ARP sweep", hosts, lambda ip: self.prober.arp(ip, src_ip)):
            self.store.upsert_device(subnet, ip, ip, "arp", mac=mac)
            n += 1
            self._set(found=n)
        self.log.add(f"ARP sweep: {n} host(s) answered")

    def _step_ping(self, adapter, src_ip, net):
        subnet = str(net)
        n = 0
        for ip, ms in self._sweep("Ping sweep", self._hosts(src_ip, net), lambda ip: self.prober.ping(ip, src_ip)):
            self.store.upsert_device(subnet, ip, ip, "ping", ping_ms=ms)
            n += 1
            self._set(found=n)
        self.log.add(f"Ping sweep: {n} host(s) replied")

    def _step_bacnet(self, adapter, src_ip, net):
        subnet = str(net)
        self._set(phase="BACnet Who-Is (listening 3 s, then reading names)", done=0, total=1)
        devs = self.prober.who_is(src_ip, net.prefixlen, self._cancel.is_set, self.log.add)
        for d in devs:
            self.store.upsert_device(subnet, d["key"], d["ip"], "bacnet", bacnet_instance=d["instance"],
                                     bacnet_vendor_id=d["vendor_id"], vendor=d.get("vendor"), name=d.get("name"),
                                     model=d.get("model"), route=d.get("route"))
        routed = sum(1 for d in devs if d.get("route"))
        self._set(done=1, found=len(devs))
        self.log.add(f"BACnet Who-Is: {len(devs)} device(s) answered"
                     f"{f' ({routed} behind routers)' if routed else ''}")

    def _step_ports(self, adapter, src_ip, net):
        subnet = str(net)
        hosts = [ip for ip in self.store.device_ips(subnet) if ip != src_ip]
        if not hosts:
            self.log.add("Port check: no known hosts yet – running an ARP sweep first")
            self._step_arp(adapter, src_ip, net)
            hosts = [ip for ip in self.store.device_ips(subnet) if ip != src_ip]
        jobs = [(ip, port) for ip in hosts for port in BMS_PORTS]
        open_by_ip: dict[str, set] = {ip: set() for ip in hosts}
        for (ip, port), ok in self._sweep("BMS port check", jobs, lambda j: self.prober.tcp(j[0], j[1], src_ip)):
            open_by_ip[ip].add(port)
        for ip, ports in open_by_ip.items():
            self.store.upsert_device(subnet, ip, ip, "ports", ports=ports)
        n = sum(1 for p in open_by_ip.values() if p)
        self.log.add(f"Port check: {n} of {len(hosts)} host(s) have BMS ports open")

    def _step_passive(self, adapter, src_ip, net):
        subnet = str(net)
        secs = 30
        self._set(phase=f"Passive listen ({secs} s, sending nothing)", done=0, total=secs)
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(
            npcap.passive_listen(adapter, src_ip, secs, self._cancel.is_set)), daemon=True)
        t0 = time.monotonic()
        t.start()
        while t.is_alive():
            t.join(0.5)
            self._set(done=min(secs, int(time.monotonic() - t0)))
        for ip, mac in result.get("hosts", {}).items():
            try:
                inside = ipaddress.IPv4Address(ip) in net
            except ValueError:
                inside = False
            if inside:
                self.store.upsert_device(subnet, ip, ip, "passive", mac=mac)
        for d in result.get("bacnet", []):
            self.store.upsert_device(subnet, d["key"], d["ip"], "passive", bacnet_instance=d["instance"],
                                     bacnet_vendor_id=d["vendor_id"], vendor=d.get("vendor"), route=d.get("route"))
        others = [ip for ip in result.get("hosts", {}) if ip and _outside(ip, net)]
        msg = f"Passive listen: heard {len(result.get('hosts', {}))} host(s), {len(result.get('bacnet', []))} BACnet I-Am"
        if others:
            msg += f"; {len(others)} on other subnets (e.g. {others[0]}) – Discover (Phase 3) will use these"
        self.log.add(msg)


def _outside(ip, net) -> bool:
    try:
        return ipaddress.IPv4Address(ip) not in net
    except ValueError:
        return False
