"""SQLite store: the site tree, join history and settings."""

from __future__ import annotations

import json
import ipaddress
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
    expected INTEGER NOT NULL DEFAULT 0,   -- on an imported device list: should be there
    expected_since TEXT,
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
        if "expected" not in cols:     # v0.9.1: imported device lists
            self.db.execute("ALTER TABLE devices ADD COLUMN expected INTEGER NOT NULL DEFAULT 0")
            self.db.execute("ALTER TABLE devices ADD COLUMN expected_since TEXT")
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
        if "sort" not in clean:      # new items go to the end of their parent's list
            clean["sort"] = self._next_sort(parent_id)
        cols = ",".join(clean)
        with self._lock:
            cur = self.db.execute(f"INSERT INTO nodes({cols}) VALUES({','.join('?' * len(clean))})",
                                  tuple(clean.values()))
            self.db.commit()
        return self.node(cur.lastrowid)

    def _siblings(self, parent_id: int | None) -> list[dict]:
        q = "SELECT id, sort, name FROM nodes WHERE parent_id IS ? ORDER BY sort, name COLLATE NOCASE"
        return [dict(r) for r in self.db.execute(q, (parent_id,))]

    def _next_sort(self, parent_id: int | None) -> int:
        sib = self._siblings(parent_id)
        return (max(r["sort"] for r in sib) + 1) if sib else 0

    def move_node(self, node_id: int, parent_id: int | None, index: int | None = None) -> dict:
        """Move a node to a new parent (or the same one) at a position among its siblings.
        Sites stay at the top level; areas and networks go inside a site or an area; nothing can go
        inside itself or something inside it."""
        n = self.node(node_id)
        if not n:
            raise StoreError("Not found")
        parent = self.node(parent_id) if parent_id is not None else None
        if parent_id is not None and not parent:
            raise StoreError("That place no longer exists")
        if (parent["kind"] if parent else None) not in ALLOWED_PARENTS[n["kind"]]:
            where = f"a {parent['kind']}" if parent else "the top level"
            raise StoreError(f"A {n['kind']} can't go inside {where}" if parent else f"A {n['kind']} has to be inside a site or an area")
        if parent_id is not None and any(a["id"] == node_id for a in self.ancestors(parent_id)):
            raise StoreError(f"Can't move '{n['name']}' inside itself")
        if n["kind"] != "site":                       # areas and networks stay in their own site
            site_of = lambda nid: self.ancestors(nid)[-1]["id"]
            if site_of(node_id) != site_of(parent_id):
                raise StoreError(f"'{n['name']}' can only move within its own site")
        with self._lock:
            sib = [r["id"] for r in self._siblings(parent_id) if r["id"] != node_id]
            index = len(sib) if index is None else max(0, min(int(index), len(sib)))
            sib.insert(index, node_id)
            # Renumber the whole list so the order is explicit from now on
            self.db.executemany("UPDATE nodes SET sort=? WHERE id=?", [(i, nid) for i, nid in enumerate(sib)])
            self.db.execute("UPDATE nodes SET parent_id=?, updated=? WHERE id=?", (parent_id, now(), node_id))
            if n["parent_id"] != parent_id:     # close the gap it left
                rest = [r["id"] for r in self._siblings(n["parent_id"]) if r["id"] != node_id]
                self.db.executemany("UPDATE nodes SET sort=? WHERE id=?", [(i, nid) for i, nid in enumerate(rest)])
            self.db.commit()
        return self.node(node_id)

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

    def recent_history(self, limit: int = 20) -> list[dict]:
        """Across every network: one row per network + IP (its latest outcome), newest first,
        with the network's name and path so a single History list can say where each was."""
        nodes = {r["id"]: dict(r) for r in self.db.execute("SELECT id, parent_id, name, kind, subnet FROM nodes")}

        def path(nid):
            out = []
            while nid is not None and nid in nodes:
                out.append(nodes[nid]["name"])
                nid = nodes[nid]["parent_id"]
            return " › ".join(reversed(out))
        rows = self.db.execute(
            "SELECT h.* FROM history h JOIN (SELECT network_id, ip, MAX(id) mid FROM history "
            "WHERE network_id IS NOT NULL GROUP BY network_id, ip) x ON h.id=x.mid ORDER BY h.id DESC LIMIT ?",
            (limit,)).fetchall()
        out = []
        for r in rows:
            n = nodes.get(r["network_id"])
            if not n:
                continue
            parent = path(n["parent_id"]) if n["parent_id"] is not None else ""
            out.append({**dict(r), "network": n["name"], "where": parent, "subnet": n["subnet"]})
        return out

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
                if not row["first_seen"]:         # an imported (expected) device turning up for the first time
                    upd["first_seen"] = ts
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
            r["expected"] = bool(r.get("expected"))
            relevant = srcs & ACTIVE_METHODS
            if r["expected"] and not relevant:
                # On an imported list but never seen: any ARP / ping / BACnet scan could have found it
                relevant = {"arp", "ping"} | ({"bacnet"} if r["bacnet_instance"] is not None else set())
            judge = next((sc for sc in scans if relevant & set(sc["methods"].split(","))), None)
            if r["expected"] and not r["last_seen"] and judge and judge["started"] < (r["expected_since"] or ""):
                judge = None                     # only scans after the import count for a never-seen device
            r["inactive"] = bool(judge and not r["is_self"] and (r["last_seen"] or "") < judge["started"])
            r["pending"] = bool(r["expected"] and not r["last_seen"] and not judge)    # not checked yet
            r["new"] = bool(latest and len(scans) > 1 and not r["is_self"] and not r["inactive"] and not r["expected"]
                            and (r["first_seen"] or "") >= latest["started"])

        def sort_key(r):
            try:
                return (tuple(int(x) for x in r["ip"].split(".")), r["key"])
            except ValueError:
                return ((999,), r["key"])
        return sorted(rows, key=sort_key)

    # ---------- imported device lists (CSV) ----------
    CSV_ALIASES = {
        "ip": ("ip", "ip address", "ipaddress", "address", "ipv4", "ip addr"),
        "mac": ("mac", "mac address", "macaddress", "physical address", "hardware address"),
        "name": ("name", "device name", "object name", "description", "label", "tag"),
        "bacnet_instance": ("bacnet instance", "instance", "device instance", "device id", "bacnet id", "object instance", "instance number"),
        "vendor": ("vendor", "manufacturer", "make", "brand"),
        "model": ("model", "model name", "type", "product"),
    }

    def import_device_list(self, node_id: int, text: str) -> dict:
        """Mark devices from a CSV as expected, so a scan shows which are there and which are missing.
        Each row goes to the network (under node_id) whose subnet holds its IP. Rows already known are
        flagged and their blank details filled in; scanned details are never overwritten."""
        import csv, io
        nets = self.networks_under(node_id)
        if not nets:
            raise StoreError("There are no networks here to import into")
        parsed = []
        for n in nets:
            try:
                parsed.append((ipcalc.parse_subnet(n["subnet"]), n))
            except ValueError:
                pass
        text = text.lstrip("\ufeff")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rd = csv.reader(io.StringIO(text), dialect)
        rows = [r for r in rd if any(c.strip() for c in r)]
        if not rows:
            raise StoreError("The file is empty")
        head = [h.strip().lower().replace("_", " ") for h in rows[0]]
        col = {}
        for field, names in self.CSV_ALIASES.items():
            for i, h in enumerate(head):
                if h in names and field not in col:
                    col[field] = i
        if "ip" not in col:
            raise StoreError("No IP column found – the first row needs headings, e.g. IP, Name, MAC, BACnet instance")
        ts = now()
        added = updated = 0
        skipped, per_net = [], {}
        seen_keys = set()
        for line_no, r in enumerate(rows[1:], start=2):
            get = lambda f: (r[col[f]].strip() if f in col and col[f] < len(r) else "") or None
            ip = get("ip")
            if not ip or not ipcalc.valid_ip(ip):
                skipped.append({"line": line_no, "ip": ip or "", "why": "not a valid IP address"})
                continue
            addr = ipaddress.ip_address(ip)
            hit = next((n for net, n in parsed if addr in net), None)
            if not hit:
                skipped.append({"line": line_no, "ip": ip, "why": "not on any network here"})
                continue
            if (hit["subnet"], ip) in seen_keys:
                skipped.append({"line": line_no, "ip": ip, "why": "listed twice"})
                continue
            seen_keys.add((hit["subnet"], ip))
            inst = get("bacnet_instance")
            try:
                inst = int(str(inst).lstrip("#")) if inst is not None else None
            except ValueError:
                inst = None
            mac = get("mac")
            if mac:
                mac = mac.upper().replace(":", "-").replace(".", "")
            info = {"mac": mac, "name": get("name"), "bacnet_instance": inst, "vendor": get("vendor"), "model": get("model")}
            with self._lock:
                row = self.db.execute("SELECT * FROM devices WHERE subnet=? AND key=?", (hit["subnet"], ip)).fetchone()
                if row:
                    upd = {"expected": 1}
                    if not row["expected_since"]:
                        upd["expected_since"] = ts
                    upd.update({k: v for k, v in info.items() if v is not None and row[k] in (None, "")})
                    self.db.execute(f"UPDATE devices SET {','.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), row["id"]))
                    updated += 1
                else:
                    cols = {"subnet": hit["subnet"], "key": ip, "ip": ip, "sources": "", "expected": 1, "expected_since": ts,
                            **{k: v for k, v in info.items() if v is not None}}
                    self.db.execute(f"INSERT INTO devices({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
                    added += 1
                self.db.commit()
            per_net[hit["name"]] = per_net.get(hit["name"], 0) + 1
        return {"added": added, "updated": updated, "skipped": skipped, "networks": per_net,
                "columns": sorted(col)}

    def clear_expected(self, subnets: list[str]) -> int:
        """Forget an imported list: never-seen rows go, seen ones stay as ordinary devices."""
        n = 0
        with self._lock:
            for sn in subnets:
                n += self.db.execute("DELETE FROM devices WHERE subnet=? AND expected=1 AND last_seen IS NULL", (sn,)).rowcount
                n += self.db.execute("UPDATE devices SET expected=0, expected_since=NULL WHERE subnet=? AND expected=1", (sn,)).rowcount
            self.db.commit()
        return n

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
        depth = len(self.path_names(node_id))
        for subnet, ns in by_subnet.items():
            # Where each network is, from the page you're on: on a site "Level 2 › FCUs", on Level 2 "FCUs"
            links = [{"id": n["id"], "path": " › ".join(self.path_names(n["id"])[depth:] or [n["name"]])} for n in ns]
            for r in self.devices(subnet):
                r["network"] = " / ".join(x["path"] for x in links)
                r["network_ids"] = [n["id"] for n in ns]
                r["network_links"] = links
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
            ids = [r["id"] for r in self.devices(subnet) if r["inactive"] and not r["expected"]]   # keep the imported list
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

    # ---------- home page ----------
    def home_summary(self, recent_limit: int = 6) -> dict:
        """Everything the Home page shows, worked out in one go from data NetJoin already keeps."""
        from collections import Counter
        from datetime import datetime, timedelta, timezone

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="milliseconds")
        nodes = {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM nodes")}
        networks = [n for n in nodes.values() if n["kind"] == "network"]
        site_of: dict[int, int] = {}

        def root(nid):
            while nodes[nid]["parent_id"] is not None:
                nid = nodes[nid]["parent_id"]
            return nid
        for n in nodes.values():
            site_of[n["id"]] = root(n["id"])

        def path(nid):
            out = []
            while nid is not None:
                out.append(nodes[nid]["name"])
                nid = nodes[nid]["parent_id"]
            return " › ".join(reversed(out))

        # devices per subnet (inactive flags need the scan list, so use devices())
        subnets = sorted({n["subnet"] for n in networks if n["subnet"]})
        devs_by_subnet = {sn: [d for d in self.devices(sn) if not d["is_self"]] for sn in subnets}
        last_scan = {sn: (self.scans(sn, 1) or [{}])[0].get("started") for sn in subnets}
        hist = [dict(r) for r in self.db.execute("SELECT * FROM history ORDER BY id DESC")]
        worked = [h for h in hist if h["result"] == "worked"]

        # recent networks: the last few distinct networks joined successfully
        recent, seen = [], set()
        for h in worked:
            nid = h["network_id"]
            if nid in seen or nid not in nodes:
                continue
            seen.add(nid)
            n = nodes[nid]
            recent.append({"id": nid, "name": n["name"], "path": path(nid), "subnet": n["subnet"], "ip": h["ip"],
                           "ts": h["ts"], "internet": h["internet"]})
            if len(recent) >= recent_limit:
                break

        # site cards
        sites = []
        for sid, s in nodes.items():
            if s["kind"] != "site":
                continue
            nets = [n for n in networks if site_of[n["id"]] == sid]
            sns = {n["subnet"] for n in nets if n["subnet"]}
            ds = [d for sn in sns for d in devs_by_subnet.get(sn, [])]
            nids = {n["id"] for n in nets}
            last_join = next((h["ts"] for h in worked if h["network_id"] in nids), None)
            scans = [last_scan[sn] for sn in sns if last_scan.get(sn)]
            sites.append({"id": sid, "name": s["name"], "networks": len(nets),
                          "areas": sum(1 for n in nodes.values() if n["kind"] == "area" and site_of[n["id"]] == sid),
                          "devices": len(ds), "bacnet": sum(1 for d in ds if d["bacnet_instance"] is not None),
                          "inactive": sum(1 for d in ds if d["inactive"]), "last_join": last_join,
                          "last_scan": max(scans) if scans else None})
        sites.sort(key=lambda x: (x["last_join"] or "", x["name"]), reverse=True)

        # needs attention
        inactive = []
        for n in networks:
            k = sum(1 for d in devs_by_subnet.get(n["subnet"], []) if d["inactive"])
            if k:
                inactive.append({"id": n["id"], "path": path(n["id"]), "count": k})

        # stats
        all_devs = [d for ds in devs_by_subnet.values() for d in ds]
        vend = Counter((d["vendor"] or d["mac_vendor"] or "Unknown") for d in all_devs)
        top = vend.most_common(6)
        other = sum(vend.values()) - sum(c for _, c in top)
        site_joins = Counter(site_of[h["network_id"]] for h in worked if h["network_id"] in nodes)
        busiest = max(networks, key=lambda n: len(devs_by_subnet.get(n["subnet"], [])), default=None)
        firsts = [n["created"] for n in nodes.values() if n.get("created")] + [h["ts"] for h in hist[-1:]]
        stats = {
            "sites": sum(1 for n in nodes.values() if n["kind"] == "site"),
            "areas": sum(1 for n in nodes.values() if n["kind"] == "area"),
            "networks": len(networks), "subnets": len(subnets),
            "devices": len(all_devs), "bacnet": sum(1 for d in all_devs if d["bacnet_instance"] is not None),
            "vendors": [{"name": k, "count": c} for k, c in top] + ([{"name": "Other", "count": other}] if other else []),
            "joins_week": sum(1 for h in worked if h["ts"] >= week_ago), "joins_total": len(worked),
            "clashes_dodged": sum(1 for h in hist if h["result"] == "clashed"),
            "most_visited": ({"id": site_joins.most_common(1)[0][0], "name": nodes[site_joins.most_common(1)[0][0]]["name"],
                              "joins": site_joins.most_common(1)[0][1]} if site_joins else None),
            "busiest": ({"id": busiest["id"], "path": path(busiest["id"]), "devices": len(devs_by_subnet.get(busiest["subnet"], []))}
                        if busiest and devs_by_subnet.get(busiest["subnet"]) else None),
            "since": min(firsts) if firsts else None,
        }
        return {"recent": recent, "sites": sites,
                "attention": {"inactive": inactive}, "stats": stats}

    # ---------- import / export ----------
    EXPORT_FIELDS = ("kind", "name", "subnet", "range_start", "range_end", "gw_override", "gateway",
                     "dns_override", "dns", "notes", "addr_mode", "fixed_ip", "alloc")
    DEVICE_FIELDS = ("key", "ip", "mac", "ping_ms", "bacnet_instance", "bacnet_vendor_id", "vendor", "mac_vendor",
                     "name", "model", "route", "ports", "sources", "first_seen", "last_seen", "expected", "expected_since")

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
        if not cols["sources"] and not cols.get("expected"):
            return 0                       # someone else's "This PC" row: not useful here
        cols["expected"] = 1 if cols.get("expected") else 0
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
