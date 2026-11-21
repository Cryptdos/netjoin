import unittest

from netjoin.scan import bacnet_client as bc
from netjoin.scan.bacnet_client import P, Target
from tests.bacnet_sim import SimDevice, demo_objects


class BacnetClientTests(unittest.TestCase):
    def sim(self, **kw):
        d = SimDevice(2502, demo_objects(2502, kw.pop("extra", 0)), **kw)
        self.addCleanup(d.close)
        return d, Target("127.0.0.1", d.port)

    def client(self):
        c = bc.Client("127.0.0.1", timeout=0.4, retries=1)
        self.addCleanup(c.close)
        return c

    def test_target_from_key(self):
        self.assertEqual(bc.target_from_key("10.0.0.5"), Target("10.0.0.5"))
        self.assertEqual(bc.target_from_key("10.0.0.5|2:05"), Target("10.0.0.5", 47808, 2, b"\x05"))

    def test_device_properties_rpm(self):
        d, t = self.sim()
        props = bc.read_device_properties(self.client(), t, 2502)
        self.assertEqual(props["firmware-revision"], "FW=02.20.010")
        self.assertEqual(props["application-software-version"], "Hvac_01.31")
        self.assertEqual(props["system-status"], {"enum": 0})
        self.assertEqual(props["local-date"], {"date": "2026-11-21"})
        self.assertEqual(props["local-time"], {"time": "13:45:02"})
        self.assertEqual(props["utc-offset"], 0)
        self.assertEqual(props["serial-number"], {"error": "unknown property"})
        self.assertEqual(d.requests, 1)                       # one ReadPropertyMultiple

    def test_device_properties_without_rpm(self):
        d, t = self.sim(rpm=False)
        props = bc.read_device_properties(self.client(), t, 2502)
        self.assertEqual((props["model-name"], props["location"]), ("DXR2.E18-102A", "Level 2 east"))

    def test_object_list_whole_and_by_index(self):
        d, t = self.sim()
        objs = bc.read_object_list(self.client(), t, 2502)
        self.assertEqual(len(objs), 5)
        self.assertIn((2, 3), objs)
        # A long list that doesn't fit one reply → read element by element
        d2, t2 = self.sim(extra=150, max_reply=480)
        objs2 = bc.read_object_list(self.client(), t2, 2502)
        self.assertEqual(len(objs2), 155)

    def test_read_objects_values_and_fallbacks(self):
        for kw in ({}, {"max_reply": 300}, {"rpm": False}):
            d, t = self.sim(**kw)
            c = self.client()
            objs = bc.read_object_list(c, t, 2502)
            got = bc.read_objects(c, t, [o for o in objs if o[0] != 8])
            temp = got[(0, 1)]
            self.assertAlmostEqual(temp[P["present-value"]], 21.5)
            self.assertEqual(temp[P["units"]], {"enum": 62})
            sp = got[(2, 3)]
            self.assertEqual(sp[P["status-flags"]], {"bits": [False, False, True, False]})
            pa = sp[P["priority-array"]]
            self.assertEqual(len(pa), 16)
            self.assertAlmostEqual(pa[7], 23.5)
            fan = got[(4, 1)]
            self.assertEqual(bc.fmt_value(fan[P["present-value"]], 4, fan), "On")
            self.assertIs(fan[P["out-of-service"]], True)
            mode = got[(19, 2)]
            self.assertEqual(mode[P["state-text"]], ["Comfort", "Economy", "Protection"])
            self.assertIsInstance(temp.get(P["priority-array"], bc.BacnetError("error", "x")), bc.BacnetError)

    def test_timeout(self):
        c = bc.Client("127.0.0.1", timeout=0.2, retries=0)
        self.addCleanup(c.close)
        with self.assertRaises(bc.BacnetError) as e:
            c.read(Target("127.0.0.1", 9), 8, 1, P["object-name"])
        self.assertEqual(e.exception.kind, "timeout")

    def test_who_has(self):
        d, t = self.sim()
        res = bc.who_has("127.0.0.1", "127.0.0.1", "R'SpTmp", listen=0.8, port=d.port)
        self.assertEqual([(r["device"], r["object"], r["name"]) for r in res], [(2502, [2, 3], "R'SpTmp")])

    def test_fmt(self):
        self.assertEqual(bc.fmt_value(21.50), "21.5")
        self.assertEqual(bc.fmt_value({"oid": [2, 3]}), "AV 3")
        self.assertEqual(bc.fmt_value(bc.BacnetError("error", "unknown property")), "(unknown property)")


if __name__ == "__main__":
    unittest.main()
