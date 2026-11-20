"""Run with:  python -m pytest  (or  python -m unittest discover tests)"""

import tempfile
import unittest
from pathlib import Path

from netjoin import ipcalc
from netjoin.engine import Engine, JoinError
from netjoin.eventlog import EventLog
from netjoin.netcfg.fake import FakeBackend
from netjoin.netcfg.windows import _parse_config, _parse_ipaddresses
from netjoin.store import Store, StoreError

SHOW_CONFIG = """
Configuration for interface "Ethernet 2"
    DHCP enabled:                         No
    IP Address:                           10.20.5.231
    Subnet Prefix:                        10.20.5.0/24 (mask 255.255.255.0)
    InterfaceMetric:                      25
    Statically Configured DNS Servers:    None
    Register with which suffix:           None
    Statically Configured WINS Servers:   None

Configuration for interface "Wi-Fi"
    DHCP enabled:                         Yes
    IP Address:                           192.168.1.50
    Subnet Prefix:                        192.168.1.0/24 (mask 255.255.255.0)
    Default Gateway:                      192.168.1.1
    Gateway Metric:                       0
    InterfaceMetric:                      35
    DNS servers configured through DHCP:  192.168.1.1
                                          8.8.8.8
    Register with which suffix:           Primary only
    WINS servers configured through DHCP: None
"""

SHOW_IPADDRESSES = """
Interface 1: Loopback Pseudo-Interface 1

Addr Type  DAD State   Valid Life Pref. Life Address
---------  ----------- ---------- ---------- ------------------------
Other      Preferred     infinite   infinite 127.0.0.1

Interface 12: Ethernet 2

Addr Type  DAD State   Valid Life Pref. Life Address
---------  ----------- ---------- ---------- ------------------------
Manual     Duplicate     infinite   infinite 10.20.5.230

Interface 9: Wi-Fi

Addr Type  DAD State   Valid Life Pref. Life Address
---------  ----------- ---------- ---------- ------------------------
Dhcp       Preferred   23h59m12s  23h59m12s 192.168.1.50
"""


class NetshParsing(unittest.TestCase):
    def test_config(self):
        c = _parse_config(SHOW_CONFIG)
        self.assertFalse(c["Ethernet 2"]["dhcp"])
        self.assertEqual(c["Ethernet 2"]["gateways"], [])
        self.assertEqual(c["Ethernet 2"]["dns"], [])
        self.assertTrue(c["Wi-Fi"]["dhcp"])
        self.assertEqual(c["Wi-Fi"]["gateways"], ["192.168.1.1"])
        self.assertEqual(c["Wi-Fi"]["dns"], ["192.168.1.1", "8.8.8.8"])

    def test_ipaddresses(self):
        d = _parse_ipaddresses(SHOW_IPADDRESSES)
        self.assertEqual(d["Ethernet 2"], [("manual", "duplicate", "10.20.5.230")])
        self.assertEqual(d["Wi-Fi"][0][1:], ("preferred", "192.168.1.50"))


class Migration(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, parent_id INTEGER, kind TEXT NOT NULL, name TEXT NOT NULL,"
                      " sort INTEGER NOT NULL DEFAULT 0, subnet TEXT, range_start TEXT, range_end TEXT,"
                      " gw_override INTEGER NOT NULL DEFAULT 0, gateway TEXT, dns_override INTEGER NOT NULL DEFAULT 0,"
                      " dns TEXT, notes TEXT, created TEXT, updated TEXT)")
            c.execute("INSERT INTO nodes(kind,name,subnet,range_start,range_end) VALUES('network','N','10.0.0.0/24','10.0.0.200','10.0.0.254')")
            c.commit()
            c.close()
            s = Store(p)
            n = s.node(1)
            self.assertEqual((n["addr_mode"], n["alloc"], n["fixed_ip"]), ("range", "first", None))
            s.db.close()


class IpCalc(unittest.TestCase):
    def test_default_range(self):
        self.assertEqual(ipcalc.default_range(ipcalc.parse_subnet("10.20.5.0/24")), ("10.20.5.200", "10.20.5.254"))
        self.assertEqual(ipcalc.default_range(ipcalc.parse_subnet("10.20.0.0/16"))[1], "10.20.255.254")
        self.assertEqual(str(ipcalc.parse_subnet("192.168.0.7 255.255.255.0")), "192.168.0.0/24")


class StoreAndJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.store, FakeBackend(), EventLog())
        s = self.store.create_node("site", None, {"name": "Site"})
        self.area = self.store.create_node("area", s["id"], {"name": "Area", "gw_override": 1, "gateway": ""})
        self.net = self.store.create_node("network", self.area["id"], {"name": "FCU", "subnet": "10.20.5.0/24"})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_rules(self):
        with self.assertRaises(StoreError):
            self.store.create_node("network", None, {"name": "x", "subnet": "10.0.0.0/24"})
        with self.assertRaises(StoreError):
            self.store.create_node("network", self.area["id"], {"name": "x", "subnet": "10.0.0.0/24",
                                                                  "range_start": "10.0.1.5", "range_end": "10.0.0.9"})

    def test_edit_applies_live_or_asks(self):
        self.engine.join(self.net["id"])                              # on 10.20.5.201, no gateway / DNS
        name = self.engine.adapter().name
        nid = self.net["id"]
        # Adding a gateway + DNS: applied straight away, address untouched
        self.store.update_node(nid, {"gw_override": 1, "gateway": "10.20.5.1", "dns_override": 1, "dns": "10.20.5.1"})
        r = self.engine.apply_config_changes()
        self.assertEqual((r["ask"], r["applied"]), (None, ["DNS 10.20.5.1", "gateway 10.20.5.1"]))
        a = self.engine.backend.get_adapter(name)
        self.assertEqual((a.primary.ip, a.gateways, a.dns), ("10.20.5.201", ["10.20.5.1"], ["10.20.5.1"]))
        self.assertEqual(self.engine.apply_config_changes(), {"changed": False})      # nothing left to do
        # Removing DNS and replacing the gateway (both in use): ask first and change NOTHING until confirmed
        self.store.update_node(nid, {"gateway": "10.20.5.254", "dns_override": 0})
        r = self.engine.apply_config_changes()
        self.assertEqual((r["ask"], r["applied"]), ("confirm", []))
        self.assertEqual(r["changes"], ["gateway 10.20.5.1 → 10.20.5.254", "DNS 10.20.5.1 → none (removed)"])
        a = self.engine.backend.get_adapter(name)
        self.assertEqual((a.gateways, a.dns), (["10.20.5.1"], ["10.20.5.1"]))          # untouched
        r = self.engine.apply_config_changes(confirm=True)
        a = self.engine.backend.get_adapter(name)
        self.assertEqual((r["ask"], a.gateways, a.dns, a.primary.ip), (None, ["10.20.5.254"], [], "10.20.5.201"))
        # A change that needs a new address: ask to rejoin, change nothing
        self.store.update_node(nid, {"range_start": "10.20.5.10", "range_end": "10.20.5.20"})
        r = self.engine.apply_config_changes()
        self.assertEqual(r["ask"], "rejoin")
        self.assertIn("outside the new range", r["reason"])
        self.assertEqual(self.engine.backend.get_adapter(name).primary.ip, "10.20.5.201")
        # Not joined → nothing to do
        self.engine.dhcp()
        self.assertEqual(self.engine.apply_config_changes(), {"changed": False})

    def test_move_and_nest(self):
        site = self.store.tree()[0]["id"]
        a2 = self.store.create_node("area", site, {"name": "B area"})
        net2 = self.store.create_node("network", site, {"name": "Loose", "subnet": "10.30.0.0/24"})
        names = lambda pid: [r["name"] for r in self.store._siblings(pid)]
        self.assertEqual(names(site), ["Area", "B area", "Loose"])          # new items go to the end
        self.store.move_node(net2["id"], site, 0)                             # reorder
        self.assertEqual(names(site), ["Loose", "Area", "B area"])
        self.store.move_node(net2["id"], a2["id"])                            # nest into an area
        self.assertEqual((names(site), names(a2["id"])), (["Area", "B area"], ["Loose"]))
        self.store.move_node(a2["id"], self.area["id"], 0)                    # area inside an area
        self.assertEqual(names(self.area["id"]), ["B area", "FCU"])
        for bad in ((self.area["id"], a2["id"]),          # inside something inside itself
                    (site, self.area["id"]),              # a site inside an area
                    (a2["id"], None)):                    # an area at the top level
            with self.assertRaises(StoreError):
                self.store.move_node(bad[0], bad[1])
        with self.assertRaises(StoreError):               # nothing inside a network
            self.store.move_node(a2["id"], self.net["id"])
        s2 = self.store.create_node("site", None, {"name": "A site"})
        self.store.move_node(s2["id"], None, 0)                               # reorder sites
        self.assertEqual(names(None), ["A site", "Site"])
        with self.assertRaises(StoreError):               # never into another site
            self.store.move_node(net2["id"], s2["id"])
        self.assertEqual(names(a2["id"]), ["Loose"])

    def test_join_skips_clash(self):
        r = self.engine.join(self.net["id"])          # .200 is "taken" in the fake back-end
        self.assertEqual((r["ip"], r["attempts"]), ("10.20.5.201", 2))
        hist = {h["ip"]: h["result"] for h in self.store.ip_history(self.net["id"])}
        self.assertEqual(hist, {"10.20.5.200": "clashed", "10.20.5.201": "worked"})
        self.engine.dhcp()
        self.assertEqual(self.engine.rejoin()["attempts"], 2)   # first free: tries .200 again (v0.6)

    def test_allocation_orders(self):
        nid = self.net["id"]
        self.engine.join(nid)                                    # .201 worked
        n = lambda: self.store.node(nid)                         # noqa: E731
        self.assertEqual(self.engine.candidates(n())[:2], ["10.20.5.200", "10.20.5.201"])     # first (default)
        self.store.update_node(nid, {"alloc": "last"})
        self.assertEqual(self.engine.candidates(n())[:2], ["10.20.5.254", "10.20.5.253"])
        self.store.update_node(nid, {"alloc": "sticky"})
        self.assertEqual(self.engine.candidates(n())[0], "10.20.5.201")                       # last used
        self.assertEqual(self.engine.next_ip(n()), {"ip": "10.20.5.201", "how": "last used"})
        self.store.update_node(nid, {"alloc": "random"})
        c = self.engine.candidates(n())
        self.assertEqual(sorted(c, key=lambda x: int(x.split(".")[-1])), [f"10.20.5.{i}" for i in range(200, 255)])
        self.assertEqual(self.engine.next_ip(n())["how"], "random from the range")
        self.store.update_node(nid, {"addr_mode": "fixed", "fixed_ip": "39"})
        self.assertEqual(self.engine.next_ip(n()), {"ip": "10.20.5.39", "how": "static IP"})
        with self.assertRaises(StoreError):
            self.store.update_node(nid, {"alloc": "sideways"})

    def test_fixed_ip(self):
        nid = self.net["id"]
        with self.assertRaisesRegex(StoreError, "Enter the static IP"):
            self.store.update_node(nid, {"addr_mode": "fixed"})
        with self.assertRaisesRegex(StoreError, "isn't a usable address"):
            self.store.update_node(nid, {"addr_mode": "fixed", "fixed_ip": "10.20.6.39"})
        self.store.update_node(nid, {"addr_mode": "fixed", "fixed_ip": "39"})             # shorthand
        self.assertEqual(self.store.node(nid)["fixed_ip"], "10.20.5.39")
        self.assertEqual(self.engine.join(nid)["ip"], "10.20.5.39")
        self.store.update_node(nid, {"fixed_ip": ".230"})                                   # "taken" in the fake
        with self.assertRaisesRegex(JoinError, "Static IP 10.20.5.230 is in use"):
            self.engine.join(nid)

    def test_shorthand_and_subnet_entry(self):
        n = self.store.create_node("network", self.area["id"], {"name": "Big", "subnet": "192.168.3.95 255.255.252.0",
                                                                  "range_start": "3.35", "range_end": ".3.45"})
        self.assertEqual((n["subnet"], n["range_start"], n["range_end"]),
                         ("192.168.0.0/22", "192.168.3.35", "192.168.3.45"))
        self.store.update_node(n["id"], {"subnet": "192.168.1.7/22"})        # same network typed differently
        self.assertEqual(self.store.node(n["id"])["range_start"], "192.168.3.35")   # range kept
        self.store.update_node(n["id"], {"subnet": "10.1.1.0/24"})           # really different: range re-filled
        self.assertEqual(self.store.node(n["id"])["range_start"], "10.1.1.200")
        with self.assertRaises(StoreError):
            self.store.update_node(n["id"], {"range_start": "abc"})

    def test_narrowed_range_skips_last_worked(self):
        self.engine.join(self.net["id"])                                   # works on .201
        self.store.update_node(self.net["id"], {"range_start": "10.20.5.202", "range_end": "10.20.5.254",
                                                "alloc": "sticky"})
        n = self.store.node(self.net["id"])
        self.assertEqual(self.engine.candidates(n)[0], "10.20.5.202")      # .201 is now outside the range
        self.assertEqual(self.engine.join(self.net["id"])["ip"], "10.20.5.202")
        self.assertEqual(self.engine.candidates(n, "10.20.5.201"), ["10.20.5.201"])   # clicking it in history still works

    def test_adapter_followed_by_mac(self):
        be = self.engine.backend
        self.engine.select_adapter("Ethernet 2")
        dongle = be.unplug("Ethernet 2")
        st = self.engine.status()
        self.assertIsNone(st["adapter"])
        self.assertEqual(st["adapter_missing"]["name"], "Ethernet 2")
        with self.assertRaisesRegex(JoinError, "unplugged"):
            self.engine.join(self.net["id"])
        be.plug(dongle, new_name="Ethernet 4")          # same MAC, new name (different USB port)
        st = self.engine.status()
        self.assertEqual(st["adapter"]["name"], "Ethernet 4")
        self.assertIn("reconnected as Ethernet 4", st["adapter_renamed"])
        self.assertIsNone(self.engine.status()["adapter_renamed"])     # only reported once

    def test_gateway_inheritance(self):
        self.store.set_setting("global", {"gw_override": 1, "gateway": "10.20.5.1", "dns_override": 1, "dns": "1.1.1.1"})
        r = self.store.resolve_gateway_dns(self.net["id"])
        self.assertIsNone(r["gateway"])               # area forces "none"
        self.assertEqual(r["dns"], ["1.1.1.1"])       # DNS falls through to global
        self.store.update_node(self.net["id"], {"gw_override": 1, "gateway": "10.20.5.254"})
        self.assertEqual(self.store.resolve_gateway_dns(self.net["id"])["gateway"], "10.20.5.254")

    def test_explicit_ip_in_use(self):
        with self.assertRaisesRegex(JoinError, "10.20.5.200 is in use"):
            self.engine.join(self.net["id"], ip="10.20.5.200")

    def test_explicit_ip_outside_subnet(self):
        with self.assertRaises(JoinError):
            self.engine.join(self.net["id"], ip="10.20.6.5")

    def test_scan_manager_full_on_fake(self):
        import time
        from netjoin.scan import ScanError, ScanManager
        from netjoin.scan.probes import FakeProber
        scans = ScanManager(self.engine, FakeProber())
        with self.assertRaises(ScanError):            # not joined yet
            scans.start("quick", self.net["id"])
        self.engine.join(self.net["id"])
        scans.start("full", self.net["id"])
        for _ in range(100):
            if not scans.status()["running"]:
                break
            time.sleep(0.05)
        devs = self.store.devices("10.20.5.0/24")
        self.assertEqual(scans.status()["phase"], "done")
        self.assertEqual(sum(1 for d in devs if d["bacnet_instance"]), 4)
        self.assertTrue(any("self" in (d["sources"] or "") for d in devs))
        with self.assertRaises(ScanError):
            scans.start("passive", self.net["id"])     # needs Npcap
        # Join again on another address: the old "This PC" row goes at the next scan
        self.engine.join(self.net["id"], ip="10.20.5.240")
        scans.start("arp", self.net["id"])
        for _ in range(100):
            if not scans.status()["running"]:
                break
            time.sleep(0.05)
        selfs = [d["ip"] for d in self.store.devices("10.20.5.0/24") if "self" in (d["sources"] or "")]
        self.assertEqual(selfs, ["10.20.5.240"])

    def _scan(self, scans, kind):
        import time
        scans.start(kind, self.net["id"])
        for _ in range(200):
            if not scans.status()["running"]:
                return
            time.sleep(0.03)

    def test_inactive_devices(self):
        from netjoin.scan import ScanManager
        from netjoin.scan.probes import FakeProber
        prober = FakeProber()
        scans = ScanManager(self.engine, prober)
        self.engine.join(self.net["id"])
        self._scan(scans, "full")
        sub = "10.20.5.0/24"
        self.assertFalse(any(d["inactive"] for d in self.store.devices(sub)))
        prober.gone = {"11"}                                   # RA L2-01 unplugged
        self._scan(scans, "ping")                              # ping-only scan
        by = {d["key"]: d for d in self.store.devices(sub)}
        self.assertTrue(by["10.20.5.11"]["inactive"])          # was found by ping before, missed now
        self.assertFalse(by["10.20.5.12"]["inactive"])         # .12 never answered ping: not judged by a ping scan
        self.assertFalse(by["10.20.5.10|2:05"]["inactive"])    # MS/TP device only ever seen by BACnet
        self.assertFalse(any(d["inactive"] for d in by.values() if d["is_self"]))
        self._scan(scans, "bacnet")
        self.assertTrue({d["key"]: d for d in self.store.devices(sub)}["10.20.5.11"]["inactive"])
        self.assertEqual(self.store.clear_inactive([sub]), 1)
        self.assertNotIn("10.20.5.11", {d["key"] for d in self.store.devices(sub)})
        prober.gone = set()                                    # plugged back in: it reappears as new
        self._scan(scans, "quick")
        d11 = {d["key"]: d for d in self.store.devices(sub)}["10.20.5.11"]
        self.assertEqual((d11["inactive"], d11["new"]), (False, True))

    def test_home_summary(self):
        self.engine.join(self.net["id"])                                 # .200 clashes, .201 works
        self.store.upsert_device("10.20.5.0/24", "10.20.5.10", "10.20.5.10", "bacnet", bacnet_instance=1, vendor="Siemens")
        self.store.upsert_device("10.20.5.0/24", "10.20.5.11", "10.20.5.11", "arp", mac_vendor="Intel")
        never = self.store.create_node("network", self.area["id"], {"name": "Unscanned", "subnet": "10.20.9.0/24"})
        h = self.store.home_summary()
        self.assertEqual([r["name"] for r in h["recent"]], ["FCU"])
        self.assertEqual(h["recent"][0]["ip"], "10.20.5.201")
        site = h["sites"][0]
        self.assertEqual((site["name"], site["networks"], site["devices"], site["bacnet"]), ("Site", 2, 2, 1))
        st = h["stats"]
        self.assertEqual((st["sites"], st["networks"], st["devices"], st["bacnet"], st["joins_total"], st["clashes_dodged"]),
                         (1, 2, 2, 1, 1, 1))
        self.assertEqual({v["name"] for v in st["vendors"]}, {"Siemens", "Intel"})
        self.assertEqual(st["most_visited"]["name"], "Site")
        self.assertEqual(list(h["attention"]), ["inactive"])            # only inactive devices are flagged
        rh = self.store.recent_history()
        self.assertEqual([(x["network"], x["ip"], x["result"]) for x in rh],
                         [("FCU", "10.20.5.201", "worked"), ("FCU", "10.20.5.200", "clashed")])
        self.assertEqual(rh[0]["where"], "Site › Area")

    def test_rollup_by_node(self):
        other = self.store.create_node("network", self.area["id"], {"name": "AHU", "subnet": "10.20.6.0/24"})
        twin = self.store.create_node("network", self.area["id"], {"name": "FCU twin", "subnet": "10.20.5.0/24"})
        self.store.upsert_device("10.20.5.0/24", "10.20.5.10", "10.20.5.10", "arp")
        self.store.upsert_device("10.20.6.0/24", "10.20.6.10", "10.20.6.10", "arp")
        r = self.store.devices_for_node(self.area["id"])
        self.assertEqual(sorted(r["subnets"]), ["10.20.5.0/24", "10.20.6.0/24"])
        self.assertEqual(len(r["devices"]), 2)                                       # shared subnet listed once
        self.assertIn("FCU twin", [x for x in r["devices"] if x["ip"] == "10.20.5.10"][0]["network"])
        self.assertEqual(len(self.store.devices_for_node(other["id"])["devices"]), 1)
        self.assertEqual({n["id"] for n in self.store.networks_under(self.area["id"])}, {self.net["id"], other["id"], twin["id"]})

    def test_export_import_roundtrip(self):
        from netjoin.store import ImportConflict
        self.store.update_node(self.net["id"], {"addr_mode": "fixed", "fixed_ip": "39", "alloc": "last"})
        self.store.upsert_device("10.20.5.0/24", "10.20.5.10", "10.20.5.10", "bacnet", bacnet_instance=2501, name="AS01")
        site_id = self.area["parent_id"]
        payload = self.store.export(site_id, include_devices=True)
        self.assertEqual((payload["format"], payload["scope"], len(payload["sites"])), (2, "site", 1))
        self.assertIn("10.20.5.0/24", payload["devices"])
        with self.assertRaises(ImportConflict) as cm:                       # same name: ask first
            self.store.import_(payload, on_conflict="ask")
        self.assertEqual(cm.exception.names, ["Site"])
        r = self.store.import_(payload, on_conflict="keep")
        self.assertEqual((r["imported"], r["renamed"]), (3, ["Site"]))
        self.assertIn("Site (2)", self.store.site_names())
        net2 = [n for n in self.store.networks_under([x for x in self.store.tree() if x["name"] == "Site (2)"][0]["id"])][0]
        self.assertEqual((net2["addr_mode"], net2["fixed_ip"], net2["alloc"]), ("fixed", "10.20.5.39", "last"))
        r = self.store.import_(payload, on_conflict="replace")              # replaces "Site", keeps "Site (2)"
        self.assertEqual(sorted(self.store.site_names()), ["Site", "Site (2)"])
        self.assertEqual(len([x for x in self.store.tree() if x["name"] == "Site"]), 1)

    def test_import_old_format_and_devices(self):
        old = {"app": "NetJoin", "format": 1, "sites": [{"kind": "site", "name": "Old", "children": [
            {"kind": "network", "name": "N", "subnet": "10.9.9.0/24"}]}]}
        self.assertEqual(self.store.import_(old, on_conflict="ask")["imported"], 2)
        dev = {"app": "NetJoin", "format": 2, "sites": [], "devices": {"10.9.9.0/24": [
            {"key": "10.9.9.10", "ip": "10.9.9.10", "sources": "arp", "last_seen": "2026-01-01T00:00:00.000+00:00"},
            {"key": "10.9.9.200", "ip": "10.9.9.200", "sources": "self", "last_seen": "2026-01-01T00:00:00.000+00:00"}]}}
        self.assertEqual(self.store.import_(dev)["devices"], 1)              # someone else's This PC is skipped
        with self.assertRaisesRegex(StoreError, "newer NetJoin"):
            self.store.import_({"app": "NetJoin", "format": 3, "sites": []})


if __name__ == "__main__":
    unittest.main()
