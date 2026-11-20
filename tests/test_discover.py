import ipaddress
import unittest

from netjoin.scan import discover as d
from netjoin.scan.probes import FakeProber


class Infer(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(d.infer_prefix("192.168.1.255", ["192.168.1.200"]), 24)        # /24 preferred when it fits
        self.assertEqual(d.infer_prefix("10.20.255.255", ["10.20.5.7", "10.20.9.1"]), 16)  # senders span a /16
        self.assertEqual(d.infer_prefix("10.0.0.127", ["10.0.0.5"]), 25)                  # .127 can't be a /24 broadcast
        self.assertIsNone(d.infer_prefix("10.0.0.255", ["10.1.0.5"]))

    def test_suggest_range(self):
        net = ipaddress.IPv4Network("10.20.5.0/24")
        self.assertEqual(d.suggest_range(net, {"10.20.5.10"}), ("10.20.5.200", "10.20.5.254", 0))
        self.assertEqual(d.suggest_range(net, {"10.20.5.210", "10.20.5.240"})[:2], ("10.20.5.211", "10.20.5.239"))
        crowded = {f"10.20.5.{i}" for i in range(1, 255, 10)}
        self.assertEqual(d.suggest_range(net, crowded)[2], 6)                            # no free run: default + warning


class Analyse(unittest.TestCase):
    def test_demo_observations(self):
        res = d.analyse(FakeProber().discover_extra(), "169.254.20.7", {"10.20.5.0/24": [{"id": 3, "name": "FCU"}]})
        by = {r["subnet"]: r for r in res}
        bms = by["10.20.5.0/24"]
        self.assertEqual((bms["confidence"], bms["bacnet_count"], bms["gateway_hint"]), ("broadcast", 3, "10.20.5.1"))
        self.assertEqual(bms["saved_as"][0]["id"], 3)
        self.assertEqual(bms["range_start"], "10.20.5.201")        # .200 is in use, so the range starts above it
        self.assertEqual(by["192.168.50.0/24"]["confidence"], "guess")
        self.assertEqual(res[0]["subnet"], "10.20.5.0/24")          # most BACnet first

    def test_dhcp_lease_22_beats_24_guess(self):
        # On site: laptop DHCP'd 192.168.3.95/22, one device heard at 192.168.0.20, no broadcasts.
        obs = {"hosts": {"192.168.0.20": "00-11-22-33-44-55"}, "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
        own = {"net": "192.168.3.95/22", "source": "DHCP lease", "gateway": "192.168.0.1"}
        r = d.analyse(obs, "192.168.3.95", own=own)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["subnet"], r[0]["confidence"], r[0]["gateway_hint"]), ("192.168.0.0/22", "adapter", "192.168.0.1"))
        self.assertIn("mask from your DHCP lease", r[0]["evidence"])
        self.assertEqual(d.analyse(obs, "192.168.3.95")[0]["subnet"], "192.168.0.0/24")    # without it: old /24 guess

    def test_neighbouring_24s_merge_into_one_guess(self):
        obs = {"hosts": {"10.9.4.10": None, "10.9.5.10": None, "10.9.7.3": None, "172.16.0.5": None},
               "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
        by = {r["subnet"]: r["confidence"] for r in d.analyse(obs, "169.254.1.1")}
        self.assertEqual(by, {"10.9.4.0/22": "cover", "172.16.0.0/24": "guess"})

    def test_apipa_adapter_is_not_evidence(self):
        obs = {"hosts": {"10.1.1.5": None}, "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
        r = d.analyse(obs, "169.254.20.7", own={"net": "169.254.20.7/16", "source": "adapter settings"})
        self.assertEqual((r[0]["subnet"], r[0]["confidence"]), ("10.1.1.0/24", "guess"))

    def test_ignores_own_and_junk_addresses(self):
        obs = {"hosts": {"169.254.1.1": "x", "0.0.0.0": "x", "224.0.0.1": "x", "10.0.0.5": "x"},
               "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
        self.assertEqual(d.analyse(obs, "10.0.0.5"), [])


if __name__ == "__main__":
    unittest.main()


class NpcapInterfaceMatching(unittest.TestCase):
    """scapy isn't installed in CI; a stand-in for scapy.conf.ifaces checks the matching rules."""

    def _sc(self, stale, fresh):
        class Ifaces(dict):
            def reload(self):
                self.clear()
                self.update(fresh)

        class Conf:
            ifaces = Ifaces(stale)

        class SC:
            conf = Conf()
        return SC()

    def _iface(self, name, ip, mac):
        class I:
            pass
        i = I()
        i.name, i.ip, i.ips, i.mac = name, ip, {4: [ip]}, mac
        return i

    def test_reload_finds_adapter_plugged_in_later(self):
        from netjoin.scan.npcap import _iface
        wifi = self._iface("WiFi", "192.168.3.95", "aa:bb:cc:00:00:01")
        eth3 = self._iface("Ethernet 3", "10.110.102.200", "00:e0:4c:54:68:40")
        sc = self._sc({"wifi": wifi}, {"wifi": wifi, "eth3": eth3})
        self.assertIs(_iface(sc, "Ethernet 3", "10.110.102.200"), eth3)

    def test_match_by_mac_when_name_and_ip_differ(self):
        from netjoin.scan.npcap import _iface
        odd = self._iface("\\\\Device\\\\NPF_{GUID}", "", "00:e0:4c:54:68:40")
        sc = self._sc({}, {"x": odd})
        self.assertIs(_iface(sc, "Ethernet 3", "10.110.102.39", mac="00-E0-4C-54-68-40"), odd)

    def test_error_lists_what_npcap_sees(self):
        from netjoin.scan.npcap import _iface
        sc = self._sc({}, {"w": self._iface("WiFi", "192.168.3.95", "aa:bb")})
        with self.assertRaisesRegex(RuntimeError, "Adapters Npcap can see: WiFi"):
            _iface(sc, "Ethernet 3", "10.110.102.39")
