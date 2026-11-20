"""SQLite store: the site tree, join history and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ipcalc

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('site','area','network')),
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    subnet TEXT,
    range_start TEXT,
    range_end TEXT,
    gw_override INTEGER NOT NULL DEFAULT 0,
    gateway TEXT,
    dns_override INTEGER NOT NULL DEFAULT 0,
    dns TEXT,
    notes TEXT,
    created TEXT,
    updated TEXT,
    addr_mode TEXT NOT NULL DEFAULT 'range',   -- range | fixed
    fixed_ip TEXT,
    alloc TEXT NOT NULL DEFAULT 'first'        -- first | last | random | sticky
);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY,
    network_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    adapter TEXT,
    ip TEXT,
    prefix INTEGER,
    result TEXT NOT NULL,          -- worked | clashed | failed
    internet INTEGER,              -- 1 internet / 2 by IP only (no DNS) / 0 none / NULL not checked
    detail TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_net ON history(network_id, ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS devices(
    id INTEGER PRIMARY KEY,
    subnet TEXT NOT NULL,          -- e.g. 10.20.5.0/24 (devices belong to a subnet, not a tree node)
    key TEXT NOT NULL,             -- ip, or ip|net:mac for BACnet devices routed behind that ip
    ip TEXT NOT NULL,
    mac TEXT,
    ping_ms REAL,
    bacnet_instance INTEGER,
    bacnet_vendor_id INTEGER,
    vendor TEXT,                   -- from BACnet (the device's own vendor-name)
    mac_vendor TEXT,               -- from the MAC address (network chip maker)
    name TEXT,
    model TEXT,
    route TEXT,
    ports TEXT,                    -- comma-separated open TCP ports
    sources TEXT,                  -- comma-separated: arp, ping, bacnet, ports, passive
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE(subnet, key)
);
CREATE INDEX IF NOT EXISTS devices_subnet ON devices(subnet);
CREATE TABLE IF NOT EXISTS scans(
    id INTEGER PRIMARY KEY,
    subnet TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT NOT NULL,
    label TEXT,
    methods TEXT NOT NULL           -- comma-separated steps that ran: arp, ping, bacnet, ports, passive
);
CREATE INDEX IF NOT EXISTS scans_subnet ON scans(subnet, started);
"""

# A device is only judged "inactive" by scans that used a method that found it before.
# Passive listening and the port check aren't evidence of absence (quiet devices; no open ports).
ACTIVE_METHODS = {"arp", "ping", "bacnet"}

ALLOWED_PARENTS = {"site": {None}, "area": {"site", "area"}, "network": {"site", "area"}}
NODE_FIELDS = ("name", "subnet", "range_start", "range_end", "gw_override", "gateway",
               "dns_override", "dns", "notes", "sort", "addr_mode", "fixed_ip", "alloc")
ADDR_MODES = ("range", "fixed")
ALLOC_ORDERS = {"first": "first free", "last": "last free", "random": "random", "sticky": "last used, then first"}


class StoreError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(devices)")}
        if "mac_vendor" not in cols:
            self.db.execute("ALTER TABLE devices ADD COLUMN mac_vendor TEXT")
        ncols = {r["name"] for r in self.db.execute("PRAGMA table_info(nodes)")}
        for col, ddl in (("addr_mode", "TEXT NOT NULL DEFAULT 'range'"), ("fixed_ip", "TEXT"),
                         ("alloc", "TEXT NOT NULL DEFAULT 'first'")):
            if col not in ncols:      # v0.6: existing networks become "range, first free"
                self.db.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")

    # ---------- backups ----------
    def backup(self, folder: Path, keep: int = 10) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"netjoin-{datetime.now():%Y%m%d-%H%M%S}.db"
        with self._lock:
            dst = sqlite3.connect(str(target))
            self.db.backup(dst)
            dst.close()
        olds = sorted(folder.glob("netjoin-*.db"))
        for old in olds[:-keep]:
            old.unlink(missing_ok=True)
        return target

    # ---------- settings ----------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()

    def global_net_settings(self) -> dict:
        return self.get_setting("global", {"gw_override": 0, "gateway": "", "dns_override": 0, "dns": ""})

    # ---------- nodes ----------
    def node(self, node_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def tree(self) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM nodes ORDER BY sort, name COLLATE NOCASE")]
        last = {r["network_id"]: r for r in self._last_joins()}
        by_parent: dict[Any, list] = {}
        for r in rows:
            if r["kind"] == "network":
                lj = last.get(r["id"])
                r["last_internet"] = lj["internet"] if lj else None
                r["last_result"] = lj["result"] if lj else None
            by_parent.setdefault(r["parent_id"], []).append(r)

        def build(pid):
            out = []
            for r in by_parent.get(pid, []):
                r["children"] = build(r["id"])
                out.append(r)
            return out
        return build(None)

    def _last_joins(self):
        return self.db.execute(
            "SELECT h.* FROM history h JOIN (SELECT network_id, MAX(id) mid FROM history "
            "WHERE result='worked' GROUP BY network_id) x ON h.id=x.mid").fetchall()

    def _validate(self, kind: str, parent_id: int | None, data: dict) -> dict:
        parent = self.node(parent_id) if parent_id is not None else None
        if parent_id is not None and not parent:
            raise StoreError("Parent not found")
        if (parent["kind"] if parent else None) not in ALLOWED_PARENTS[kind]:
            raise StoreError(f"A {kind} can't go under a {parent['kind'] if parent else 'top level'}")
        clean = {k: data.get(k) for k in NODE_FIELDS if k in data}
        if "name" in clean:
            clean["name"] = (clean["name"] or "").strip()
            if not clean["name"]:
                raise StoreError("Name is required")
        for flag in ("gw_override", "dns_override"):
            if flag in clean:
                clean[flag] = 1 if clean[flag] else 0
        if clean.get("gateway"):
            if not ipcalc.valid_ip(clean["gateway"]):
                raise StoreError("Gateway isn't a valid IPv4 address")
        if clean.get("dns"):
            parts = [p.strip() for p in str(clean["dns"]).replace(";", ",").split(",") if p.strip()]
            bad = [p for p in parts if not ipcalc.valid_ip(p)]
            if bad:
                raise StoreError(f"DNS server isn't a valid IPv4 address: {bad[0]}")
            clean["dns"] = ", ".join(parts)
        return clean

    def _validate_network(self, rec: dict) -> dict:
        try:
            net = ipcalc.parse_subnet(rec.get("subnet") or "")
        except ValueError:
            raise StoreError("Enter the IP Network and pick a mask (e.g. 192.168.0.1 and 255.255.255.0)")
        if net.prefixlen > 30:
            raise StoreError("The mask leaves no room for devices – use /30 or bigger")
        rec["subnet"] = str(net)
        for k in ("range_start", "range_end", "fixed_ip"):
            try:
                rec[k] = ipcalc.expand(rec.get(k), net) or None
            except ValueError as e:
                raise StoreError(str(e))
        if not rec.get("range_start") or not rec.get("range_end"):
            rec["range_start"], rec["range_end"] = ipcalc.default_range(net)
        for k in ("range_start", "range_end"):
            if not ipcalc.in_subnet(rec[k], net):
                label = "Range start" if k == "range_start" else "Range end"
                raise StoreError(f"{label} {rec[k]} isn't a usable address in {net}")
        rec["addr_mode"] = rec.get("addr_mode") or "range"
        if rec["addr_mode"] not in ADDR_MODES:
            raise StoreError("Laptop address must be the allowed range or a static IP")
        rec["alloc"] = rec.get("alloc") or "first"
        if rec["alloc"] not in ALLOC_ORDERS:
            raise StoreError("Unknown allocation order")
        if rec["addr_mode"] == "fixed":
            if not rec.get("fixed_ip"):
                raise StoreError("Enter the static IP for this laptop")
            if not ipcalc.in_subnet(rec["fixed_ip"], net):
                raise StoreError(f"Static IP {rec['fixed_ip']} isn't a usable address in {net}")
        elif rec.get("fixed_ip") and not ipcalc.in_subnet(rec["fixed_ip"], net):
            rec["fixed_ip"] = None      # stale static IP from an old subnet; not in use
        if rec.get("gateway") and not ipcalc.in_subnet(rec["gateway"], net):
            raise StoreError("Gateway must be inside the network")
        return rec

    def create_node(self, kind: str, parent_id: int | None, data: dict) -> dict:
        if kind not in ALLOWED_PARENTS:
            raise StoreError("Unknown kind")
        clean = self._validate(kind, parent_id, data)
        if "name" not in clean:
            raise StoreError("Name is required")
        if kind == "network":
            clean = self._validate_network(clean)
        clean.update(kind=kind, parent_id=parent_id, created=now(), updated=now())
        cols = ",".join(clean)
        with self._lock:
            cur = self.db.execute(f"INSERT INTO nodes({cols}) VALUES({','.join('?' * len(clean))})",
                                  tuple(clean.values()))
            self.db.commit()
        return self.node(cur.lastrowid)

    def update_node(self, node_id: int, data: dict) -> dict:
        cur = self.node(node_id)
        if not cur:
            raise StoreError("Not found")
        clean = self._validate(cur["kind"], cur["parent_id"], data)
        if cur["kind"] == "network":
            merged = {**cur, **clean}
            if "subnet" in clean and "range_start" not in data:
                try:
                    changed = str(ipcalc.parse_subnet(clean["subnet"] or "")) != cur["subnet"]
                except ValueError:
                    changed = True
                if changed:              # new network, no range given: auto-fill it again
                    merged["range_start"] = merged["range_end"] = None
            merged = self._validate_network(merged)
            for k in ("subnet", "range_start", "range_end", "addr_mode", "fixed_ip", "alloc"):
                clean[k] = merged[k]
        if not clean:
            return cur
        clean["updated"] = now()
        sets = ",".join(f"{k}=?" for k in clean)
        with self._lock:
            self.db.execute(f"UPDATE nodes SET {sets} WHERE id=?", (*clean.values(), node_id))
            self.db.commit()
        return self.node(node_id)

    def delete_node(self, node_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            self.db.commit()

    def ancestors(self, node_id: int) -> list[dict]:
        """The node and its parents, nearest first."""
        out, nid = [], node_id
        while nid is not None:
            n = self.node(nid)
            if not n:
                break
            out.append(n)
            nid = n["parent_id"]
        return out

    def resolve_gateway_dns(self, network_id: int) -> dict:
        """Nearest level (network › area › site › global) with an override ticked wins."""
        chain = self.ancestors(network_id)
        result = {"gateway": None, "gateway_from": None, "dns": [], "dns_from": None}
        for n in chain:
            if n["gw_override"] and result["gateway_from"] is None:
                result["gateway"] = n["gateway"] or None
                result["gateway_from"] = f"{n['kind']}: {n['name']}"
            if n["dns_override"] and result["dns_from"] is None:
                result["dns"] = [d.strip() for d in (n["dns"] or "").split(",") if d.strip()]
                result["dns_from"] = f"{n['kind']}: {n['name']}"
        g = self.global_net_settings()
        if result["gateway_from"] is None and g.get("gw_override"):
            result["gateway"] = g.get("gateway") or None
            result["gateway_from"] = "global"
        if result["dns_from"] is None and g.get("dns_override"):
            result["dns"] = [d.strip() for d in (g.get("dns") or "").split(",") if d.strip()]
            result["dns_from"] = "global"
        # A global gateway only makes sense if it is inside this network's subnet.
        net = self.node(network_id)
        if result["gateway"] and net and net.get("subnet"):
            if not ipcalc.in_subnet(result["gateway"], ipcalc.parse_subnet(net["subnet"])):
                result["gateway"], result["gateway_from"] = None, (result["gateway_from"] or "") + " (outside subnet, ignored)"
        return result

    def path_names(self, node_id: int) -> list[str]:
        return [n["name"] for n in reversed(self.ancestors(node_id))]

    # ---------- history ----------
    def add_history(self, network_id: int, adapter: str, ip: str, prefix: int, result: str,
                    internet: bool | None = None, detail: str = "") -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO history(network_id,adapter,ip,prefix,result,internet,detail,ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (network_id, adapter, ip, prefix, result,
                 None if internet is None else int(internet), detail, now()))
            self.db.commit()

    def set_last_internet(self, network_id: int, ip: str, internet: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE history SET internet=? WHERE id=(SELECT MAX(id) FROM history "
                "WHERE network_id=? AND ip=? AND result='worked')", (int(internet), network_id, ip))
            self.db.commit()

    def ip_history(self, network_id: int, limit: int = 15) -> list[dict]:
        """One row per IP: its latest outcome."""
        rows = self.db.execute(
            "SELECT h.* FROM history h JOIN (SELECT ip, MAX(id) mid FROM history WHERE network_id=? "
            "GROUP BY ip) x ON h.id=x.mid ORDER BY h.id DESC LIMIT ?", (network_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_worked_ip(self, network_id: int) -> str | None:
        row = self.db.execute("SELECT ip FROM history WHERE network_id=? AND result='worked' "
                              "ORDER BY id DESC LIMIT 1", (network_id,)).fetchone()
        return row["ip"] if row else None

    def last_internet(self, network_id: int) -> int | None:
        row = self.db.execute("SELECT internet FROM history WHERE network_id=? AND result='worked' "
                              "ORDER BY id DESC LIMIT 1", (network_id,)).fetchone()
        return row["internet"] if row else None

    # ---------- devices ----------
    def upsert_device(self, subnet: str, key: str, ip: str, source: str, **fields) -> None:
        """Merge what one scan learned about a device into what we already know."""
        with self._lock:
            row = self.db.execute("SELECT * FROM devices WHERE subnet=? AND key=?", (subnet, key)).fetchone()
            ts = now()
            ports = fields.pop("ports", None)
            if row is None:
                cols = {"subnet": subnet, "key": key, "ip": ip, "sources": source, "first_seen": ts, "last_seen": ts,
                        "ports": ",".join(str(p) for p in sorted(ports)) if ports else None}
                cols.update({k: v for k, v in fields.items() if v is not None})
                self.db.execute(f"INSERT INTO devices({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                tuple(cols.values()))
            else:
                srcs = set(filter(None, (row["sources"] or "").split(","))) | {source}
                upd = {"last_seen": ts, "sources": ",".join(sorted(srcs))}
                upd.update({k: v for k, v in fields.items() if v is not None})
                if ports is not None:   # a port check replaces the old list for this device
                    upd["ports"] = ",".join(str(p) for p in sorted(ports)) or None
                sets = ",".join(f"{k}=?" for k in upd)
                self.db.execute(f"UPDATE devices SET {sets} WHERE id=?", (*upd.values(), row["id"]))
            self.db.commit()

    def remove_old_self(self, subnet: str, current_key: str) -> int:
        """Drop "This PC" rows left from addresses the laptop used before on this subnet."""
        with self._lock:
            n = self.db.execute("DELETE FROM devices WHERE subnet=? AND key<>? AND ','||sources||',' LIKE '%,self,%'",
                                (subnet, current_key)).rowcount
            self.db.commit()
        return n

    def set_device_fields(self, subnet: str, key: str, **fields) -> None:
        """Update details (manufacturer, ports…) without counting the device as seen again."""
        if not fields:
            return
        if "ports" in fields:
            p = fields["ports"]
            fields["ports"] = ",".join(str(x) for x in sorted(p)) if p else None
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE devices SET {sets} WHERE subnet=? AND key=?", (*fields.values(), subnet, key))
            self.db.commit()

    def record_scan(self, subnet: str, started: str, label: str, methods: list[str]) -> None:
        with self._lock:
            self.db.execute("INSERT INTO scans(subnet,started,finished,label,methods) VALUES(?,?,?,?,?)",
                            (subnet, started, now(), label, ",".join(dict.fromkeys(methods))))
            # keep the last 200 scans per subnet
            self.db.execute("DELETE FROM scans WHERE subnet=? AND id NOT IN "
                            "(SELECT id FROM scans WHERE subnet=? ORDER BY id DESC LIMIT 200)", (subnet, subnet))
            self.db.commit()

    def scans(self, subnet: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM scans WHERE subnet=? ORDER BY id DESC LIMIT ?",
                                                 (subnet, limit))]

    def devices(self, subnet: str) -> list[dict]:
        """Devices on a subnet, each flagged 'inactive' (missed the latest scan that could have found it)
        and 'new' (first seen in the latest scan, when there was an earlier one)."""
        rows = [dict(r) for r in self.db.execute("SELECT * FROM devices WHERE subnet=?", (subnet,))]
        scans = self.scans(subnet)
        latest = scans[0] if scans else None
        for r in rows:
            srcs = set(filter(None, (r["sources"] or "").split(",")))
            r["is_self"] = "self" in srcs
            relevant = srcs & ACTIVE_METHODS
            judge = next((sc for sc in scans if relevant & set(sc["methods"].split(","))), None)
            r["inactive"] = bool(judge and not r["is_self"] and (r["last_seen"] or "") < judge["started"])
            r["new"] = bool(latest and len(scans) > 1 and not r["is_self"] and not r["inactive"]
                            and (r["first_seen"] or "") >= latest["started"])

        def sort_key(r):
            try:
                return (tuple(int(x) for x in r["ip"].split(".")), r["key"])
            except ValueError:
                return ((999,), r["key"])
        return sorted(rows, key=sort_key)

    def networks_under(self, node_id: int) -> list[dict]:
        """Every network at or below a node (a network gives just itself)."""
        node = self.node(node_id)
        if not node:
            return []
        if node["kind"] == "network":
            return [node]
        out, todo = [], [node_id]
        while todo:
            pid = todo.pop()
            for r in self.db.execute("SELECT * FROM nodes WHERE parent_id=? ORDER BY sort, name COLLATE NOCASE", (pid,)):
                r = dict(r)
                if r["kind"] == "network":
                    out.append(r)
                else:
                    todo.append(r["id"])
        return out

    def devices_for_node(self, node_id: int) -> dict:
        """Roll-up: devices from every network under a site / area (or one network).
        Networks sharing a subnet show its devices once."""
        nets = self.networks_under(node_id)
        by_subnet: dict[str, list[dict]] = {}
        for n in nets:
            by_subnet.setdefault(n["subnet"], []).append(n)
        rows, last = [], None
        for subnet, ns in by_subnet.items():
            names = [" › ".join(self.path_names(n["id"])[-2:]) if len(nets) > 1 else n["name"] for n in ns]
            for r in self.devices(subnet):
                r["network"] = " / ".join(names)
                r["network_ids"] = [n["id"] for n in ns]
                rows.append(r)
            sc = self.scans(subnet, 1)
            if sc and (last is None or sc[0]["started"] > last["started"]):
                last = sc[0]
        return {"node_id": node_id, "subnets": list(by_subnet), "networks": [{"id": n["id"], "name": n["name"],
                "path": " › ".join(self.path_names(n["id"])), "subnet": n["subnet"]} for n in nets],
                "devices": rows, "last_scan": {"ts": last["started"], "kind": last["label"]} if last else None}

    def clear_inactive(self, subnets: list[str]) -> int:
        n = 0
        for subnet in subnets:
            ids = [r["id"] for r in self.devices(subnet) if r["inactive"]]
            if ids:
                with self._lock:
                    n += self.db.execute(f"DELETE FROM devices WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount
                    self.db.commit()
        return n

    def device_ips(self, subnet: str) -> list[str]:
        return sorted({r["ip"] for r in self.db.execute("SELECT ip FROM devices WHERE subnet=?", (subnet,))},
                      key=lambda ip: tuple(int(x) for x in ip.split(".")))

    def clear_devices(self, subnet: str) -> int:
        with self._lock:
            n = self.db.execute("DELETE FROM devices WHERE subnet=?", (subnet,)).rowcount
            self.db.commit()
        return n

    # ---------- import / export ----------
    EXPORT_FIELDS = ("kind", "name", "subnet", "range_start", "range_end", "gw_override", "gateway",
                     "dns_override", "dns", "notes", "addr_mode", "fixed_ip", "alloc")
    DEVICE_FIELDS = ("key", "ip", "mac", "ping_ms", "bacnet_instance", "bacnet_vendor_id", "vendor", "mac_vendor",
                     "name", "model", "route", "ports", "sources", "first_seen", "last_seen")

    def export(self, node_id: int | None = None, include_devices: bool = False) -> dict:
        """Format 2. node_id = one site (plan item 1); None = everything."""
        def strip(n):
            out = {k: n[k] for k in self.EXPORT_FIELDS if n.get(k) not in (None, "")}
            if n["children"]:
                out["children"] = [strip(c) for c in n["children"]]
            return out
        sites = self.tree()
        if node_id is not None:
            sites = [x for x in sites if x["id"] == node_id]
            if not sites:
                raise StoreError("Only a whole site can be exported")
        payload = {"app": "NetJoin", "format": 2, "exported": now(), "scope": "site" if node_id else "all",
                   "sites": [strip(x) for x in sites]}
        if include_devices:
            subnets = sorted({n["subnet"] for x in sites for n in self.networks_under(x["id"])})
            payload["devices"] = {sn: [{k: d[k] for k in self.DEVICE_FIELDS if d.get(k) is not None}
                                       for d in self.devices(sn)] for sn in subnets}
        return payload

    def site_names(self) -> set[str]:
        return {r["name"] for r in self.db.execute("SELECT name FROM nodes WHERE kind='site'")}

    def import_(self, payload: dict, on_conflict: str = "keep") -> dict:
        """Import sites from a format 1 or 2 file.

        on_conflict when a site with the same name exists:
          'ask'     → raise ImportConflict listing the names (nothing imported)
          'replace' → delete the existing site (and everything under it) first
          'keep'    → keep both; the imported one becomes "Name (2)"
        """
        if payload.get("app") != "NetJoin" or not isinstance(payload.get("sites"), list):
            raise StoreError("Not a NetJoin export file")
        if payload.get("format", 1) > 2:
            raise StoreError("This file is from a newer NetJoin – update NetJoin to import it")
        existing = self.site_names()
        clashes = [x.get("name") for x in payload["sites"] if x.get("name") in existing]
        if clashes and on_conflict == "ask":
            raise ImportConflict(clashes)
        count = 0

        def add(n, parent_id):
            nonlocal count
            created = self.create_node(n.get("kind", "site" if parent_id is None else "area"), parent_id, n)
            count += 1
            for c in n.get("children", []):
                add(c, created["id"])

        for site in payload["sites"]:
            site = dict(site, kind="site")
            if site.get("name") in existing:
                if on_conflict == "replace":
                    for r in self.db.execute("SELECT id FROM nodes WHERE kind='site' AND name=?", (site["name"],)).fetchall():
                        self.delete_node(r["id"])
                else:
                    base, i = site["name"], 2
                    while f"{base} ({i})" in existing:
                        i += 1
                    site["name"] = f"{base} ({i})"
            add(site, None)
            existing = self.site_names()
        devices = 0
        for subnet, rows in (payload.get("devices") or {}).items():
            for d in rows:
                devices += self._import_device(subnet, d)
        return {"imported": count, "devices": devices, "replaced": clashes if on_conflict == "replace" else [],
                "renamed": clashes if on_conflict != "replace" else []}

    def _import_device(self, subnet: str, d: dict) -> int:
        """Add a device from a file; if we already know it, keep whichever was seen more recently."""
        if not d.get("key") or not d.get("ip"):
            return 0
        row = self.db.execute("SELECT last_seen FROM devices WHERE subnet=? AND key=?", (subnet, d["key"])).fetchone()
        if row and (row["last_seen"] or "") >= (d.get("last_seen") or ""):
            return 0
        cols = {k: d.get(k) for k in self.DEVICE_FIELDS}
        cols["subnet"] = subnet
        cols["sources"] = ",".join(x for x in (cols.get("sources") or "").split(",") if x and x != "self") or None
        if not cols["sources"]:
            return 0                       # someone else's "This PC" row: not useful here
        with self._lock:
            self.db.execute(f"INSERT INTO devices({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                            f"ON CONFLICT(subnet,key) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("subnet", "key")),
                            tuple(cols.values()))
            self.db.commit()
        return 1


class ImportConflict(StoreError):
    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(f"A site called {', '.join(repr(n) for n in names)} already exists")
