import socket
import threading
import unittest

from netjoin.scan import bacnet as b


def rp_ack(invoke: int, instance: int, prop: int, text: str) -> bytes:
    """A ReadProperty Complex-ACK carrying a character string, as a device would send it."""
    oid = ((8 << 22) | instance).to_bytes(4, "big")
    s = b"\x00" + text.encode()
    val = bytes([0x75, len(s)]) + s if len(s) > 4 else bytes([0x70 | len(s)]) + s
    apdu = bytes([0x30, invoke, 0x0C, 0x0C]) + oid + bytes([0x19, prop, 0x3E]) + val + bytes([0x3F])
    body = bytes([0x01, 0x00]) + apdu
    return bytes([0x81, 0x0A]) + (len(body) + 4).to_bytes(2, "big") + body


class Decode(unittest.TestCase):
    def test_who_is_bytes(self):
        self.assertEqual(len(b.WHO_IS), int.from_bytes(b.WHO_IS[2:4], "big"))
        self.assertEqual(b.WHO_IS[-2:], b"\x10\x08")

    def test_iam(self):
        d = b.decode_iam_datagram(b.i_am_packet(2501, 7), "10.20.5.10", 47808)
        self.assertEqual((d["instance"], d["vendor_id"], d["key"], d["route"]), (2501, 7, "10.20.5.10", None))
        self.assertEqual(d["vendor"], "Siemens")

    def test_routed_iam(self):
        # I-Am from an MS/TP device (net 2, MAC 5) relayed by a router at 10.20.5.10
        oid = ((8 << 22) | 2601).to_bytes(4, "big")
        apdu = bytes([0x10, 0x00, 0xC4]) + oid + bytes([0x22, 0x01, 0xE0, 0x91, 0x03, 0x21, 0x07])
        npdu = bytes([0x01, 0x08, 0x00, 0x02, 0x01, 0x05]) + apdu
        pkt = bytes([0x81, 0x0B]) + (len(npdu) + 4).to_bytes(2, "big") + npdu
        d = b.decode_iam_datagram(pkt, "10.20.5.10", 47808)
        self.assertEqual((d["instance"], d["key"], d["route"]), (2601, "10.20.5.10|2:05", "net 2, MAC 5"))

    def test_forwarded_npdu_uses_original_sender(self):
        inner = b.i_am_packet(42, 5)[4:]
        pkt = bytes([0x81, 0x04]) + (len(inner) + 10).to_bytes(2, "big") + socket.inet_aton("10.9.9.9") + \
            (47808).to_bytes(2, "big") + inner
        d = b.decode_iam_datagram(pkt, "10.20.5.1", 47808)
        self.assertEqual((d["ip"], d["instance"]), ("10.9.9.9", 42))

    def test_read_property_ack(self):
        pkt = rp_ack(9, 2501, b.PROP_OBJECT_NAME, "AS01 East Panel")
        _, _, npdu = b.parse_bvlc(pkt)
        self.assertEqual(b.parse_read_property_ack(b.parse_npdu(npdu)[2]), (9, 77, "AS01 East Panel"))

    def test_read_property_request_routed(self):
        pkt = b.read_property_request(3, 2601, 77, dnet=2, dadr=b"\x05")
        self.assertEqual(pkt[4:11], bytes([0x01, 0x24, 0x00, 0x02, 0x01, 0x05, 0xFF]))
        self.assertEqual(int.from_bytes(pkt[2:4], "big"), len(pkt))

    def test_garbage_is_ignored(self):
        for junk in (b"", b"\x81", b"\x81\x0b\x00\x05\x01", b"hello world", b"\x81\x0b\x00\x0c\x01\x80\x00\x00"):
            self.assertIsNone(b.decode_iam_datagram(junk, "1.2.3.4", 47808))


class ReadNamesOverLoopback(unittest.TestCase):
    """A tiny fake device on 127.0.0.1 answers ReadProperty; _read_names should fill name/model/vendor."""

    def test_names(self):
        dev = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dev.bind(("127.0.0.1", 0))
        dev.settimeout(3)
        answers = {77: "RA L2-01", 70: "DXR2.E18-102A", 121: "Siemens Schweiz AG"}

        def serve():
            for _ in range(3):
                try:
                    data, addr = dev.recvfrom(1500)
                except socket.timeout:
                    return
                apdu = data[6:]           # BVLC(4) + NPDU(2)
                invoke, prop = apdu[2], apdu[10]
                dev.sendto(rp_ack(invoke, 2502, prop, answers[prop]), addr)
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        d = {"instance": 2502, "ip": "127.0.0.1", "port": dev.getsockname()[1], "snet": None, "sadr": None,
             "name": None, "model": None, "vendor": None}
        b._read_names(client, [d], cancel=lambda: False, wait=2)
        t.join(3)
        client.close()
        dev.close()
        self.assertEqual((d["name"], d["model"], d["vendor"]), ("RA L2-01", "DXR2.E18-102A", "Siemens Schweiz AG"))


if __name__ == "__main__":
    unittest.main()
