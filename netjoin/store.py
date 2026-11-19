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
    updated TEXT
);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY,
    network_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    adapter TEXT,
    ip TEXT,
    prefix INTEGER,
    result TEXT NOT NULL,          -- worked | clashed | failed
    internet INTEGER,              -- 1 / 0 / NULL (not checked)
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
    vendor TEXT,
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
"""

ALLOWED_PARENTS = {"site": {None}, "area": {"site", "area"}, "network": {"site", "area"}}
NODE_FIELDS = ("name", "subnet", "range_start", "range_end", "gw_override", "gateway",
               "dns_override", "dns", "notes", "sort")


class StoreError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

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
            raise StoreError("Subnet must look like 10.20.5.0/24")
        rec["subnet"] = str(net)
        if not rec.get("range_start") or not rec.get("range_end"):
            rec["range_start"], rec["range_end"] = ipcalc.default_range(net)
        for k in ("range_start", "range_end"):
            if not ipcalc.in_subnet(rec[k], net):
                raise StoreError(f"{k.replace('_', ' ').capitalize()} {rec[k]} isn't a usable address in {net}")
        if rec.get("gateway") and not ipcalc.in_subnet(rec["gateway"], net):
            raise StoreError("Gateway must be inside the network's subnet")
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
            if "subnet" in clean and clean.get("subnet") != cur["subnet"] and \
                    "range_start" not in data:
                merged["range_start"] = merged["range_end"] = None
            merged = self._validate_network(merged)
            for k in ("subnet", "range_start", "range_end"):
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

    def set_last_internet(self, network_id: int, ip: str, internet: bool) -> None:
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

    def devices(self, subnet: str) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM devices WHERE subnet=?", (subnet,))]

        def sort_key(r):
            try:
                return (tuple(int(x) for x in r["ip"].split(".")), r["key"])
            except ValueError:
                return ((999,), r["key"])
        return sorted(rows, key=sort_key)

    def device_ips(self, subnet: str) -> list[str]:
        return sorted({r["ip"] for r in self.db.execute("SELECT ip FROM devices WHERE subnet=?", (subnet,))},
                      key=lambda ip: tuple(int(x) for x in ip.split(".")))

    def clear_devices(self, subnet: str) -> int:
        with self._lock:
            n = self.db.execute("DELETE FROM devices WHERE subnet=?", (subnet,)).rowcount
            self.db.commit()
        return n

    # ---------- import / export ----------
    def export(self) -> dict:
        def strip(n):
            out = {k: n[k] for k in ("kind", "name", "subnet", "range_start", "range_end", "gw_override",
                                     "gateway", "dns_override", "dns", "notes") if n.get(k) not in (None, "")}
            if n["children"]:
                out["children"] = [strip(c) for c in n["children"]]
            return out
        return {"app": "NetJoin", "format": 1, "exported": now(), "sites": [strip(s) for s in self.tree()]}

    def import_(self, payload: dict) -> int:
        if payload.get("app") != "NetJoin" or not isinstance(payload.get("sites"), list):
            raise StoreError("Not a NetJoin export file")
        count = 0

        def add(n, parent_id):
            nonlocal count
            created = self.create_node(n.get("kind", "site"), parent_id, n)
            count += 1
            for c in n.get("children", []):
                add(c, created["id"])
        for s in payload["sites"]:
            add(s, None)
        return count
