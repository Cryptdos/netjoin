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
        self.assertEqual((bms["confidence"], bms["bacnet_count"], bms["gateway_hint"]), ("high", 3, "10.20.5.1"))
        self.assertEqual(bms["saved_as"][0]["id"], 3)
        self.assertEqual(bms["range_start"], "10.20.5.201")        # .200 is in use, so the range starts above it
        self.assertEqual(by["192.168.50.0/24"]["confidence"], "guess")
        self.assertEqual(res[0]["subnet"], "10.20.5.0/24")          # most BACnet first

    def test_ignores_own_and_junk_addresses(self):
        obs = {"hosts": {"169.254.1.1": "x", "0.0.0.0": "x", "224.0.0.1": "x", "10.0.0.5": "x"},
               "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
        self.assertEqual(d.analyse(obs, "10.0.0.5"), [])


if __name__ == "__main__":
    unittest.main()
