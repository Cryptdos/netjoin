"""A tiny simulated BACnet/IP device for tests: answers ReadProperty, ReadPropertyMultiple and Who-Has
over real UDP on 127.0.0.1, so the client's encoding, decoding and fall-backs are exercised end to end."""

from __future__ import annotations

import socket
import struct
import threading

from netjoin.scan import bacnet
from netjoin.scan.bacnet_client import P, SVC_READ_PROPERTY, SVC_READ_PROPERTY_MULTIPLE, SVC_WHO_HAS


def _app(tag: int, data: bytes) -> bytes:
    n = len(data)
    if n <= 4:
        return bytes([(tag << 4) | n]) + data
    if n < 254:
        return bytes([(tag << 4) | 5, n]) + data
    return bytes([(tag << 4) | 5, 254]) + n.to_bytes(2, "big") + data


def enc(v) -> bytes:
    """Python value → application-tagged BACnet encoding (enough for tests)."""
    if v is None:
        return b"\x00"
    if isinstance(v, bool):
        return bytes([0x10 | int(v)])
    if isinstance(v, tuple):
        kind = v[0]
        if kind == "enum":
            n = v[1]
            return _app(9, n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big"))
        if kind == "oid":
            return _app(12, ((v[1] << 22) | v[2]).to_bytes(4, "big"))
        if kind == "bits":
            bits = v[1]
            nbytes = (len(bits) + 7) // 8
            out = bytearray(nbytes)
            for k, b in enumerate(bits):
                if b:
                    out[k // 8] |= 0x80 >> (k % 8)
            return _app(8, bytes([nbytes * 8 - len(bits)]) + bytes(out))
        if kind == "date":
            y, m, d = v[1]
            return _app(10, bytes([y - 1900, m, d, 255]))
        if kind == "time":
            h, mi, s = v[1]
            return _app(11, bytes([h, mi, s, 0]))
        if kind == "signed":
            return _app(3, v[1].to_bytes(4, "big", signed=True))
    if isinstance(v, int):
        return _app(2, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))
    if isinstance(v, float):
        return _app(4, struct.pack(">f", v))
    if isinstance(v, str):
        return _app(7, b"\x00" + v.encode())
    if isinstance(v, list):
        return b"".join(enc(x) for x in v)
    raise TypeError(v)


class SimDevice:
    def __init__(self, instance: int, objects: dict, max_reply: int = 1400, rpm: bool = True, name_index: dict | None = None):
        self.instance, self.objects, self.max_reply, self.rpm = instance, objects, max_reply, rpm
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def close(self):
        self._stop.set()
        self.sock.close()

    def _send(self, addr, apdu: bytes):
        body = bytes([0x01, 0x00]) + apdu
        self.sock.sendto(bytes([0x81, 0x0A]) + (len(body) + 4).to_bytes(2, "big") + body, addr)

    def _value(self, oid, prop, index):
        obj = self.objects.get(oid)
        if obj is None:
            return None, (1, 31)                  # object / unknown-object
        if prop not in obj:
            return None, (2, 32)                  # property / unknown-property
        v = obj[prop]
        if index is not None:
            if not isinstance(v, list):
                return None, (2, 50)
            if index == 0:
                return len(v), None
            if index > len(v):
                return None, (2, 42)
            return v[index - 1], None
        return v, None

    def _run(self):
        while not self._stop.is_set():
            self.sock.settimeout(0.2)
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            b = bacnet.parse_bvlc(data)
            n = bacnet.parse_npdu(b[2]) if b else None
            if not n:
                continue
            apdu = n[2]
            self.requests += 1
            if apdu[0] == 0x10 and apdu[1] == SVC_WHO_HAS:
                self._who_has(apdu, addr)
                continue
            if apdu[0] >> 4 != 0:
                continue
            inv, svc, body = apdu[2], apdu[3], apdu[4:]
            if svc == SVC_READ_PROPERTY:
                self._rp(inv, body, addr)
            elif svc == SVC_READ_PROPERTY_MULTIPLE and self.rpm:
                self._rpm(inv, body, addr)
            else:
                self._send(addr, bytes([0x60, inv, 9]))          # reject: unrecognized service

    def _reply(self, addr, inv, svc, payload):
        apdu = bytes([0x30, inv, svc]) + payload
        if len(apdu) > self.max_reply:
            self._send(addr, bytes([0x71, inv, 4]))              # abort: segmentation not supported
        else:
            self._send(addr, apdu)

    def _rp(self, inv, body, addr):
        i, oid, prop, index = 0, None, None, None
        while i < len(body):
            tag, ctx, length, j, kind = bacnet.read_tag(body, i)
            v = int.from_bytes(body[j:j + length], "big")
            if tag == 0:
                oid = (v >> 22, v & 0x3FFFFF)
            elif tag == 1:
                prop = v
            elif tag == 2:
                index = v
            i = j + length
        val, err = self._value(oid, prop, index)
        if err:
            self._send(addr, bytes([0x50, inv, SVC_READ_PROPERTY]) + enc(("enum", err[0])) + enc(("enum", err[1])))
            return
        payload = bytes([0x0C]) + ((oid[0] << 22) | oid[1]).to_bytes(4, "big") + bytes([0x19 if prop < 256 else 0x1A]) + \
            prop.to_bytes(1 if prop < 256 else 2, "big")
        if index is not None:
            payload += bytes([0x29, index]) if index < 256 else bytes([0x2A]) + index.to_bytes(2, "big")
        payload += b"\x3e" + enc(val) + b"\x3f"
        self._reply(addr, inv, SVC_READ_PROPERTY, payload)

    def _rpm(self, inv, body, addr):
        out, i, oid = b"", 0, None
        while i < len(body):
            tag, ctx, length, j, kind = bacnet.read_tag(body, i)
            if kind == "data" and tag == 0 and length == 4:
                v = int.from_bytes(body[j:j + 4], "big")
                oid = (v >> 22, v & 0x3FFFFF)
                out += bytes([0x0C]) + v.to_bytes(4, "big")
                i = j + 4
                continue
            if kind == "open" and tag == 1:
                out += b"\x1e"
                i = j
                while True:
                    t2, c2, l2, j2, k2 = bacnet.read_tag(body, i)
                    if k2 == "close":
                        i = j2
                        break
                    prop = int.from_bytes(body[j2:j2 + l2], "big")
                    i = j2 + l2
                    out += (bytes([0x29, prop]) if prop < 256 else bytes([0x2A]) + prop.to_bytes(2, "big"))
                    val, err = self._value(oid, prop, None)
                    if err:
                        out += b"\x5e" + enc(("enum", err[0])) + enc(("enum", err[1])) + b"\x5f"
                    else:
                        out += b"\x4e" + enc(val) + b"\x4f"
                out += b"\x1f"
                continue
            i = j + length
        self._reply(addr, inv, SVC_READ_PROPERTY_MULTIPLE, out)

    def _who_has(self, apdu, addr):
        vals = apdu[2:]
        tag, ctx, length, j, kind = bacnet.read_tag(vals, 0)
        if tag != 3:
            return
        name = bacnet.decode_string(vals[j:j + length])
        for oid, props in self.objects.items():
            if props.get(P["object-name"]) == name:
                i_have = bytes([0x10, 0x01]) + enc(("oid", 8, self.instance)) + enc(("oid", oid[0], oid[1])) + enc(name)
                body = bytes([0x01, 0x00]) + i_have
                self.sock.sendto(bytes([0x81, 0x0A]) + (len(body) + 4).to_bytes(2, "big") + body, addr)


def demo_objects(instance: int = 2502, n_extra: int = 0) -> dict:
    """A small DXR2-like device."""
    objs = {
        (8, instance): {P["object-name"]: "RA L2-01", P["vendor-name"]: "Siemens Schweiz AG", P["vendor-identifier"]: 7,
                        P["model-name"]: "DXR2.E18-102A", P["firmware-revision"]: "FW=02.20.010",
                        P["application-software-version"]: "Hvac_01.31", P["protocol-version"]: 1,
                        P["protocol-revision"]: 14, P["location"]: "Level 2 east", P["description"]: "Room automation",
                        P["system-status"]: ("enum", 0), P["max-apdu-length-accepted"]: 480,
                        P["segmentation-supported"]: ("enum", 3), P["database-revision"]: 12,
                        P["local-date"]: ("date", (2026, 11, 21)), P["local-time"]: ("time", (13, 45, 2)),
                        P["utc-offset"]: ("signed", 0)},
        (0, 1): {P["object-name"]: "R'Temp", P["present-value"]: 21.5, P["units"]: ("enum", 62),
                 P["status-flags"]: ("bits", [False, False, False, False]), P["out-of-service"]: False,
                 P["description"]: "Room temperature", P["event-state"]: ("enum", 0), P["reliability"]: ("enum", 0)},
        (2, 3): {P["object-name"]: "R'SpTmp", P["present-value"]: 22.0, P["units"]: ("enum", 62),
                 P["status-flags"]: ("bits", [False, False, True, False]), P["out-of-service"]: False,
                 P["priority-array"]: [None] * 7 + [23.5] + [None] * 8, P["relinquish-default"]: 21.0,
                 P["event-state"]: ("enum", 0), P["reliability"]: ("enum", 0)},
        (4, 1): {P["object-name"]: "Fan", P["present-value"]: ("enum", 1), P["active-text"]: "On", P["inactive-text"]: "Off",
                 P["status-flags"]: ("bits", [False, False, False, True]), P["out-of-service"]: True,
                 P["priority-array"]: [None] * 16, P["relinquish-default"]: ("enum", 0),
                 P["event-state"]: ("enum", 0), P["reliability"]: ("enum", 0)},
        (19, 2): {P["object-name"]: "Mode", P["present-value"]: 2, P["state-text"]: ["Comfort", "Economy", "Protection"],
                  P["number-of-states"]: 3, P["status-flags"]: ("bits", [True, False, False, False]),
                  P["out-of-service"]: False, P["event-state"]: ("enum", 2), P["reliability"]: ("enum", 0)},
    }
    for k in range(n_extra):
        objs[(2, 100 + k)] = {P["object-name"]: f"Spare AV {k}", P["present-value"]: float(k), P["units"]: ("enum", 98),
                              P["status-flags"]: ("bits", [False] * 4), P["out-of-service"]: False}
    objs[(8, instance)][P["object-list"]] = [("oid", t, i) for t, i in objs]
    return objs
