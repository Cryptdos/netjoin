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

    def test_join_skips_clash_and_prefers_last_worked(self):
        r = self.engine.join(self.net["id"])          # .200 is "taken" in the fake back-end
        self.assertEqual((r["ip"], r["attempts"]), ("10.20.5.201", 2))
        hist = {h["ip"]: h["result"] for h in self.store.ip_history(self.net["id"])}
        self.assertEqual(hist, {"10.20.5.200": "clashed", "10.20.5.201": "worked"})
        self.engine.dhcp()
        self.assertEqual(self.engine.rejoin()["attempts"], 1)   # goes straight to the last worked IP

    def test_narrowed_range_skips_last_worked(self):
        self.engine.join(self.net["id"])                                   # works on .201
        self.store.update_node(self.net["id"], {"range_start": "10.20.5.202", "range_end": "10.20.5.254"})
        n = self.store.node(self.net["id"])
        self.assertEqual(self.engine.candidates(n)[0], "10.20.5.202")      # .201 is now outside the range
        self.assertEqual(self.engine.join(self.net["id"])["ip"], "10.20.5.202")
        self.assertEqual(self.engine.candidates(n, "10.20.5.201"), ["10.20.5.201"])   # clicking it in history still works

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

    def test_export_import_roundtrip(self):
        payload = self.store.export()
        self.assertEqual(self.store.import_(payload), 3)


if __name__ == "__main__":
    unittest.main()
