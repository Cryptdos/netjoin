"""Per-device tools: live ping, BACnet device properties, a port check. All read-only.

Nothing here runs in the background on its own: a live ping only runs while its page keeps asking
for results, and stops by itself a short while after the page goes away (battery).
"""

from __future__ import annotations

import ipaddress
import itertools
import threading
import time
from datetime import datetime, timezone

from . import bacnet_client as bc
from .probes import BMS_PORTS, Prober

PING_INTERVAL = 1.0
PING_IDLE_STOP = 20.0          # no one asking for results for this long → stop
PING_MAX = 60 * 60             # an hour at most
MAX_SESSIONS = 4


class DeviceToolError(RuntimeError):
    pass


class PingSession:
    _ids = itertools.count(1)

    def __init__(self, prober: Prober, ip: str, src_ip: str):
        self.id = next(self._ids)
        self.ip, self.src_ip, self.prober = ip, src_ip, prober
        self.results: list[tuple[int, float | None]] = []      # (unix ms, rtt ms or None)
        self.started = time.time()
        self.last_poll = time.monotonic()
        self.stopped_why: str | None = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"ping-{ip}")
        self.thread.start()

    def _run(self):
        t0 = time.monotonic()
        while not self._stop.is_set():
            tick = time.monotonic()
            rtt = self.prober.ping(self.ip, self.src_ip, timeout_ms=900)
            self.results.append((int(time.time() * 1000), rtt))
            if len(self.results) > 3600:
                del self.results[:600]
            if time.monotonic() - self.last_poll > PING_IDLE_STOP:
                self.stopped_why = "page closed"
                break
            if time.monotonic() - t0 > PING_MAX:
                self.stopped_why = "an hour is the limit"
                break
            self._stop.wait(max(0.0, PING_INTERVAL - (time.monotonic() - tick)))
        self._stop.set()

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def stop(self, why: str = "stopped"):
        if self.running:
            self.stopped_why = why
        self._stop.set()


class DeviceTools:
    def __init__(self, engine, prober: Prober):
        self.engine, self.prober, self.store, self.log = engine, prober, engine.store, engine.log
        self.sessions: dict[int, PingSession] = {}
        self._lock = threading.Lock()

    # ---------- helpers
    def _src_ip(self) -> str:
        a = self.engine.adapter()
        if not a or not a.primary:
            raise DeviceToolError("The adapter has no IPv4 address – Join a network first")
        return a.primary.ip

    def device(self, device_id: int) -> dict:
        d = self.store.device(device_id)
        if not d:
            raise DeviceToolError("That device is no longer in the list")
        return d

    def on_subnet(self, d: dict) -> bool:
        try:
            a = self.engine.adapter()
            return bool(a and a.primary and ipaddress.ip_address(a.primary.ip) in ipaddress.ip_network(d["subnet"]))
        except ValueError:
            return False

    def _need_subnet(self, d: dict):
        if not self.on_subnet(d):
            raise DeviceToolError(f"The laptop isn't on {d['subnet']} – Join that network to talk to this device")

    # ---------- live ping
    def ping_start(self, device_id: int) -> dict:
        d = self.device(device_id)
        src = self._src_ip()
        with self._lock:
            for s in list(self.sessions.values()):
                if not s.running:
                    self.sessions.pop(s.id, None)
            for s in self.sessions.values():           # one at a time per device: reuse it
                if s.ip == d["ip"] and s.running:
                    s.last_poll = time.monotonic()
                    return {"session": s.id, "ip": s.ip}
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.started)
                oldest.stop("too many live pings")
                self.sessions.pop(oldest.id, None)
            s = PingSession(self.prober, d["ip"], src)
            self.sessions[s.id] = s
        self.log.add(f"Live ping to {d['ip']} started")
        return {"session": s.id, "ip": s.ip}

    def ping_poll(self, sid: int, after: int = 0) -> dict:
        s = self.sessions.get(sid)
        if not s:
            raise DeviceToolError("That ping has finished")
        s.last_poll = time.monotonic()
        res = s.results[after:] if after <= len(s.results) else []
        return {"session": sid, "ip": s.ip, "running": s.running, "stopped": s.stopped_why,
                "count": len(s.results), "results": [{"t": t, "ms": ms} for t, ms in res]}

    def ping_stop(self, sid: int) -> dict:
        s = self.sessions.pop(sid, None)
        if s:
            s.stop()
            ok = sum(1 for _t, ms in s.results if ms is not None)
            self.log.add(f"Live ping to {s.ip} stopped – {ok} of {len(s.results)} replied")
        return {"ok": True}

    def stop_all(self):
        for s in list(self.sessions.values()):
            s.stop("NetJoin closing")

    # ---------- BACnet device properties
    def bacnet_properties(self, device_id: int) -> dict:
        d = self.device(device_id)
        if d.get("bacnet_instance") is None:
            raise DeviceToolError("This isn't a BACnet device (no instance number)")
        self._need_subnet(d)
        target = bc.target_from_key(d["key"])
        try:
            props = self.prober.bacnet_device_props(self._src_ip(), target, d["bacnet_instance"])
        except bc.BacnetError as e:
            raise DeviceToolError(f"No answer from device {d['bacnet_instance']} at {d['ip']} ({e.detail})")
        val = lambda k: props.get(k) if isinstance(props.get(k), str) else None
        self.store.set_device_props(device_id, val("firmware-revision"), val("application-software-version"),
                                    name=val("object-name"), model=val("model-name"), vendor=val("vendor-name"))
        rows = []
        for name in bc.DEVICE_PROPS:
            if name not in props:
                continue
            v = props[name]
            if isinstance(v, dict) and "error" in v:
                rows.append({"prop": name, "value": None, "note": v["error"]})
                continue
            text = bc.fmt_value(v)
            if name == "system-status" and isinstance(v, dict):
                text = bc.SYSTEM_STATUS.get(v.get("enum"), text)
            elif name == "segmentation-supported" and isinstance(v, dict):
                text = bc.SEGMENTATION.get(v.get("enum"), text)
            elif name == "utc-offset" and isinstance(v, int):
                text = f"{v} min" + (" (UTC)" if v == 0 else "")
            rows.append({"prop": name, "value": text})
        clock = self._clock_check(props)
        self.log.add(f"Read BACnet properties of device {d['bacnet_instance']} ({d['ip']})")
        return {"device_id": device_id, "rows": rows, "clock": clock, "read_at": datetime.now(timezone.utc).isoformat()}

    @staticmethod
    def _clock_check(props: dict) -> dict | None:
        """Compare the device's local date / time with the laptop's (catches wrong controller clocks)."""
        d, t = props.get("local-date"), props.get("local-time")
        if not (isinstance(d, dict) and isinstance(t, dict) and "date" in d and "time" in t) or "*" in d["date"] + t["time"]:
            return None
        try:
            dev = datetime.fromisoformat(f"{d['date']}T{t['time'].split('.')[0]}")
        except ValueError:
            return None
        diff = (dev - datetime.now()).total_seconds()
        return {"device": dev.isoformat(sep=" "), "laptop": datetime.now().replace(microsecond=0).isoformat(sep=" "),
                "diff_s": round(diff)}

    # ---------- ports
    def check_ports(self, device_id: int) -> dict:
        d = self.device(device_id)
        self._need_subnet(d)
        src = self._src_ip()
        open_ports = [p for p in BMS_PORTS if self.prober.tcp(d["ip"], p, src)]
        self.store.set_device_fields(d["subnet"], d["key"], ports=open_ports)
        self.log.add(f"Port check on {d['ip']}: {', '.join(BMS_PORTS[p] for p in open_ports) or 'none open'}")
        return {"ports": open_ports}
