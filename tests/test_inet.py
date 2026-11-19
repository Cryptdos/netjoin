import socket
import struct
import threading
import unittest
from unittest import mock

from netjoin import inet


def fake_dns_server(answer: bool):
    """One-shot DNS server on 127.0.0.1 that answers (or refuses) the first query."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(3)

    def serve():
        try:
            data, addr = s.recvfrom(512)
        except socket.timeout:
            return
        tid = data[:2]
        if answer:     # NOERROR, 1 question, 1 answer (A 1.2.3.4)
            reply = tid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + data[12:] + \
                bytes.fromhex("c00c000100010000003c000401020304")
        else:          # SERVFAIL
            reply = tid + struct.pack(">HHHHH", 0x8182, 1, 0, 0, 0) + data[12:]
        s.sendto(reply, addr)
    threading.Thread(target=serve, daemon=True).start()
    return s


class DnsQuery(unittest.TestCase):
    def _query(self, answer):
        srv = fake_dns_server(answer)
        port = srv.getsockname()[1]
        real_sendto = socket.socket.sendto

        def redirect(sock, data, addr):          # send "port 53" queries to our test server instead
            return real_sendto(sock, data, (addr[0], port) if addr[1] == 53 else addr)
        with mock.patch.object(socket.socket, "sendto", redirect):
            ok = inet.dns_query("127.0.0.1", "127.0.0.1", timeout=2)
        srv.close()
        return ok

    def test_answer(self):
        self.assertTrue(self._query(True))

    def test_servfail(self):
        self.assertFalse(self._query(False))

    def test_no_server(self):
        self.assertFalse(inet.dns_query("127.0.0.1", "127.0.0.1", timeout=0.3))


class Check(unittest.TestCase):
    def test_states(self):
        with mock.patch.object(inet, "route_ok", return_value=False):
            self.assertEqual(inet.check("10.0.0.5", ["10.0.0.1"])["code"], inet.NONE)
        with mock.patch.object(inet, "route_ok", return_value=True):
            self.assertEqual(inet.check("10.0.0.5", [])["code"], inet.IP_ONLY)          # the bug that was reported
            with mock.patch.object(inet, "dns_query", return_value=False):
                self.assertEqual(inet.check("10.0.0.5", ["10.0.0.1"])["code"], inet.IP_ONLY)
            with mock.patch.object(inet, "dns_query", return_value=True):
                self.assertEqual(inet.check("10.0.0.5", ["10.0.0.1"])["code"], inet.INTERNET)


if __name__ == "__main__":
    unittest.main()
