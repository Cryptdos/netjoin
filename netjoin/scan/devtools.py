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


class ObjectsJob:
    """A read that takes a while (a device's object list, or firmware across a network), with progress."""
    _ids = itertools.count(1)

    def __init__(self, device: dict | None, kind: str = "objects"):
        self.id = next(self._ids)
        self.kind = kind
        self.device_id = device["id"] if device else None
        self.phase, self.done, self.total = "starting", 0, 0
        self.finished, self.error, self.cancelled = False, None, False
        self._cancel = threading.Event()

    def progress(self, phase: str, done: int, total: int):
        self.phase, self.done, self.total = phase, done, total

    def to_dict(self) -> dict:
        return {"job": self.id, "kind": self.kind, "device_id": self.device_id, "phase": self.phase, "done": self.done,
                "total": self.total, "finished": self.finished, "error": self.error, "cancelled": self.cancelled,
                "summary": getattr(self, "summary", None)}


def _state_or(v, t, props):
    """A multi-state number as its state text where the device gives one."""
    st = props.get(bc.P["state-text"])
    if t in bc.MULTISTATE and isinstance(v, int) and isinstance(st, list) and 0 < v <= len(st) and isinstance(st[v - 1], str):
        return st[v - 1]
    return bc.fmt_value(v, t, props)


def format_object(oid: tuple[int, int], props: dict) -> dict:
    """Raw property values → what the object list shows."""
    t, i = oid
    P = bc.P
    val = lambda pid: props.get(pid)
    good = lambda pid: not isinstance(val(pid), bc.BacnetError) and pid in props
    sf = val(P["status-flags"])
    bits = sf["bits"] if isinstance(sf, dict) and "bits" in sf else []
    flags = {k: bool(bits[n]) if len(bits) > n else False for n, k in enumerate(("alarm", "fault", "overridden", "oos"))}
    if good(P["out-of-service"]) and val(P["out-of-service"]) is True:
        flags["oos"] = True
    pv = val(P["present-value"])
    units = val(P["units"]) if good(P["units"]) else None
    unit_txt = bc.UNITS.get(units["enum"], f"units #{units['enum']}") if isinstance(units, dict) and "enum" in units else ""
    if t in bc.MULTISTATE and isinstance(pv, int) and good(P["state-text"]):
        st = val(P["state-text"])
        st = st if isinstance(st, list) else [st]
        value = st[pv - 1] if 0 < pv <= len(st) and isinstance(st[pv - 1], str) else str(pv)
    elif P["present-value"] in props:
        value = bc.fmt_value(pv, t, props)
    else:
        value = ""
    pa = val(P["priority-array"]) if good(P["priority-array"]) else None
    prio = []
    if isinstance(pa, list):
        for level, v in enumerate(pa, start=1):
            if v is not None and not (isinstance(v, dict) and not v):
                txt = bc.fmt_value(v, t, props)
                if t in bc.MULTISTATE and isinstance(v, int) and good(P["state-text"]):
                    st = val(P["state-text"]); st = st if isinstance(st, list) else [st]
                    txt = st[v - 1] if 0 < v <= len(st) else txt
                prio.append({"level": level, "value": txt})
    es = val(P["event-state"])
    rel = val(P["reliability"])
    name = val(P["object-name"])
    desc = val(P["description"])
    return {"type": t, "inst": i, "short": bc.SHORT_TYPES.get(t, str(t)), "type_name": bc.OBJECT_TYPES.get(t, f"type {t}"),
            "name": name if isinstance(name, str) else "", "description": desc if isinstance(desc, str) else "",
            "value": value, "units": unit_txt, "flags": flags,
            "event_state": bc.EVENT_STATE.get(es["enum"], str(es["enum"])) if isinstance(es, dict) and "enum" in es else None,
            "reliability": bc.RELIABILITY.get(rel["enum"], str(rel["enum"])) if isinstance(rel, dict) and "enum" in rel and rel["enum"] != 0 else None,
            "priority": prio, "commanded": bool(prio),
            "relinquish_default": _state_or(val(P["relinquish-default"]), t, props) if good(P["relinquish-default"]) else None,
            "error": val(P["object-name"]).detail if isinstance(val(P["object-name"]), bc.BacnetError) else None}


class DeviceTools:
    def __init__(self, engine, prober: Prober):
        self.engine, self.prober, self.store, self.log = engine, prober, engine.store, engine.log
        self.sessions: dict[int, PingSession] = {}
        self._lock = threading.Lock()
        self.jobs: dict[int, ObjectsJob] = {}
        self.objects: dict[int, dict] = {}        # device id → {"read_at", "objects": [...]} (kept in memory)

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

    # ---------- object list (v0.11)
    def objects_start(self, device_id: int) -> dict:
        d = self.device(device_id)
        if d.get("bacnet_instance") is None:
            raise DeviceToolError("This isn't a BACnet device")
        self._need_subnet(d)
        for j in self.jobs.values():
            if j.device_id == device_id and not j.finished:
                return j.to_dict()
        job = ObjectsJob(d)
        self.jobs[job.id] = job
        src = self._src_ip()
        threading.Thread(target=self._objects_run, args=(job, d, src), daemon=True, name=f"objects-{device_id}").start()
        return job.to_dict()

    def _objects_run(self, job: ObjectsJob, d: dict, src: str):
        t0 = time.monotonic()
        try:
            raw = self.prober.bacnet_objects(src, bc.target_from_key(d["key"]), d["bacnet_instance"],
                                             cancel=job._cancel.is_set, progress=job.progress)
            objs = [format_object(oid, props) for oid, props in raw.items() if oid[0] != 8]
            objs.sort(key=lambda o: (o["type"], o["inst"]))
            if job._cancel.is_set():
                job.cancelled = True
            self.objects[d["id"]] = {"read_at": datetime.now(timezone.utc).isoformat(), "objects": objs,
                                     "partial": job.cancelled, "seconds": round(time.monotonic() - t0, 1)}
            self.log.add(f"Read {len(objs)} object(s) from device {d['bacnet_instance']} ({d['ip']}) in "
                         f"{time.monotonic() - t0:.1f} s" + (" – cancelled part-way" if job.cancelled else ""))
        except bc.BacnetError as e:
            job.error = f"No answer from device {d['bacnet_instance']} ({e.detail})"
            self.log.add(job.error, "warn")
        except Exception as e:        # never leave the page spinning
            job.error = f"Reading objects failed: {e}"
            self.log.add(job.error, "error")
        finally:
            job.finished = True

    def job(self, jid: int) -> dict:
        return self.objects_job(jid)

    def objects_job(self, jid: int) -> dict:
        j = self.jobs.get(jid)
        if not j:
            raise DeviceToolError("That read has finished")
        return j.to_dict()

    def objects_cancel(self, jid: int) -> dict:
        j = self.jobs.get(jid)
        if j:
            j._cancel.set()
        return {"ok": True}

    def objects_get(self, device_id: int) -> dict | None:
        return self.objects.get(device_id)

    def objects_refresh(self, device_id: int, which: list[list[int]] | None = None) -> dict:
        """Re-read present value + status flags (+ priority array) of objects already listed: quick."""
        d = self.device(device_id)
        self._need_subnet(d)
        cached = self.objects.get(device_id)
        if not cached:
            raise DeviceToolError("Read the object list first")
        want = {tuple(x) for x in which} if which else None
        objs = [o for o in cached["objects"] if want is None or (o["type"], o["inst"]) in want][:400]
        try:
            fresh = self.prober.bacnet_values(self._src_ip(), bc.target_from_key(d["key"]), [(o["type"], o["inst"]) for o in objs])
        except bc.BacnetError as e:
            raise DeviceToolError(f"No answer from device {d['bacnet_instance']} ({e.detail})")
        by = {(o["type"], o["inst"]): o for o in cached["objects"]}
        for oid, props in fresh.items():
            o = by.get(oid)
            if not o:
                continue
            f = format_object(oid, {**props, bc.P["object-name"]: o["name"], bc.P["description"]: o["description"]})
            for k in ("value", "flags", "priority", "commanded", "event_state", "reliability"):
                o[k] = f[k]
        cached["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        return {"read_at": cached["read_at"], "refreshed_at": cached["refreshed_at"],
                "objects": [by[(o["type"], o["inst"])] for o in objs]}

    # ---------- firmware across a network (feeds the health checks)
    def firmware_start(self, node_id: int) -> dict:
        a = self.engine.adapter()
        if not a or not a.primary:
            raise DeviceToolError("The adapter has no IPv4 address – Join a network first")
        here = ipaddress.ip_network(f"{a.primary.ip}/{a.primary.prefix}", strict=False)
        devs = [x for x in self.store.devices_for_node(node_id)["devices"]
                if x.get("bacnet_instance") is not None and not x["inactive"] and ipaddress.ip_network(x["subnet"]) == here]
        if not devs:
            raise DeviceToolError(f"No BACnet devices here on {here} (the subnet the laptop is on) – Join one of the networks and scan first")
        for j in self.jobs.values():
            if j.kind == "firmware" and not j.finished:
                return j.to_dict()
        job = ObjectsJob(None, "firmware")
        self.jobs[job.id] = job
        threading.Thread(target=self._firmware_run, args=(job, devs, a.primary.ip), daemon=True, name="firmware").start()
        return job.to_dict()

    def _firmware_run(self, job: ObjectsJob, devs: list[dict], src: str):
        ok = 0
        try:
            for k, d in enumerate(devs):
                if job._cancel.is_set():
                    job.cancelled = True
                    break
                job.progress("firmware", k, len(devs))
                try:
                    props = self.prober.bacnet_device_props(src, bc.target_from_key(d["key"]), d["bacnet_instance"])
                except bc.BacnetError:
                    continue
                v = lambda n: props.get(n) if isinstance(props.get(n), str) else None
                self.store.set_device_props(d["id"], v("firmware-revision"), v("application-software-version"),
                                            name=v("object-name"), model=v("model-name"), vendor=v("vendor-name"))
                ok += 1
            job.progress("firmware", len(devs), len(devs))
            job.summary = f"Read firmware from {ok} of {len(devs)} device(s)"
            self.log.add(job.summary)
        except Exception as e:
            job.error = f"Reading firmware failed: {e}"
        finally:
            job.finished = True

    # ---------- Who-Has: which controller holds a point called …
    def who_has(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise DeviceToolError("Type an object name")
        a = self.engine.adapter()
        if not a or not a.primary:
            raise DeviceToolError("The adapter has no IPv4 address – Join a network first")
        net = ipaddress.ip_network(f"{a.primary.ip}/{a.primary.prefix}", strict=False)
        found = self.prober.bacnet_who_has(a.primary.ip, str(net.broadcast_address), name)
        subnet = str(net)
        known = {(x["bacnet_instance"], x["key"]): x for x in self.store.devices(subnet) if x.get("bacnet_instance") is not None}
        by_inst = {x["bacnet_instance"]: x for x in known.values()}
        nets = self.store.networks_for_subnet(subnet)
        out = []
        for r in found:
            dev = known.get((r["device"], r["key"])) or by_inst.get(r["device"])
            t, i = r["object"]
            out.append({"device": r["device"], "ip": r["ip"], "route": r.get("route"), "object": f"{bc.SHORT_TYPES.get(t, t)} {i}",
                        "type_name": bc.OBJECT_TYPES.get(t, str(t)), "name": r["name"],
                        "device_id": dev["id"] if dev else None, "device_name": dev["name"] if dev else None,
                        "network_id": nets[0]["id"] if nets and dev else None})
        self.log.add(f"Who-Has '{name}' on {subnet}: {len(out)} answer(s)")
        return {"name": name, "subnet": subnet, "results": out}

    # ---------- ports
    def check_ports(self, device_id: int) -> dict:
        d = self.device(device_id)
        self._need_subnet(d)
        src = self._src_ip()
        open_ports = [p for p in BMS_PORTS if self.prober.tcp(d["ip"], p, src)]
        self.store.set_device_fields(d["subnet"], d["key"], ports=open_ports)
        self.log.add(f"Port check on {d['ip']}: {', '.join(BMS_PORTS[p] for p in open_ports) or 'none open'}")
        return {"ports": open_ports}
