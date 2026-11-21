"""BACnet/IP client: ReadProperty, ReadPropertyMultiple, Who-Has. Read-only by design.

NetJoin reads and observes; it never writes to devices (no WriteProperty, time sync,
ReinitializeDevice or DeviceCommunicationControl). References: ASHRAE 135 clause 20 (encoding),
21 (ASN.1 productions), 15.5 / 15.7 (ReadProperty / ReadPropertyMultiple), 16.9 (Who-Has).

Segmented responses aren't accepted: when a reply is too big (e.g. a long object-list) the device
aborts and we fall back to reading array elements one at a time, or smaller batches.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Any, Callable, NamedTuple

from . import bacnet

BACNET_PORT = bacnet.BACNET_PORT

# ------------------------------------------------------------------ names
OBJECT_TYPES = {
    0: "analog-input", 1: "analog-output", 2: "analog-value", 3: "binary-input", 4: "binary-output",
    5: "binary-value", 6: "calendar", 7: "command", 8: "device", 9: "event-enrollment", 10: "file",
    11: "group", 12: "loop", 13: "multi-state-input", 14: "multi-state-output", 15: "notification-class",
    16: "program", 17: "schedule", 18: "averaging", 19: "multi-state-value", 20: "trend-log",
    21: "life-safety-point", 22: "life-safety-zone", 23: "accumulator", 24: "pulse-converter",
    25: "event-log", 26: "global-group", 27: "trend-log-multiple", 28: "load-control", 29: "structured-view",
    30: "access-door", 31: "timer", 32: "access-credential", 33: "access-point", 34: "access-rights",
    35: "access-user", 36: "access-zone", 37: "credential-data-input", 38: "network-security",
    39: "bitstring-value", 40: "characterstring-value", 41: "date-pattern-value", 42: "date-value",
    43: "datetime-pattern-value", 44: "datetime-value", 45: "integer-value", 46: "large-analog-value",
    47: "octetstring-value", 48: "positive-integer-value", 49: "time-pattern-value", 50: "time-value",
    51: "notification-forwarder", 52: "alert-enrollment", 53: "channel", 54: "lighting-output",
    55: "binary-lighting-output", 56: "network-port", 57: "elevator-group", 58: "escalator", 59: "lift",
}
SHORT_TYPES = {
    0: "AI", 1: "AO", 2: "AV", 3: "BI", 4: "BO", 5: "BV", 6: "CAL", 7: "CMD", 8: "DEV", 9: "EE", 10: "FILE",
    11: "GRP", 12: "LOOP", 13: "MSI", 14: "MSO", 15: "NC", 16: "PRG", 17: "SCH", 18: "AVG", 19: "MSV",
    20: "TL", 23: "ACC", 25: "EL", 27: "TLM", 29: "SV", 31: "TMR", 39: "BSV", 40: "CSV", 42: "DTV",
    44: "DTMV", 45: "IV", 46: "LAV", 48: "PIV", 50: "TV", 53: "CHN", 54: "LO", 56: "NP",
}
ANALOG = {0, 1, 2, 23, 45, 46, 48}
BINARY = {3, 4, 5}
MULTISTATE = {13, 14, 19}
COMMANDABLE = {1, 2, 4, 5, 14, 19, 45, 46, 48, 54}   # (AV / BV / MSV etc. may be commandable)

P = {
    "acked-transitions": 0, "active-text": 4, "apdu-timeout": 11, "application-software-version": 12,
    "description": 28, "event-state": 36, "firmware-revision": 44, "inactive-text": 46, "local-date": 56,
    "local-time": 57, "location": 58, "max-apdu-length-accepted": 62, "model-name": 70,
    "number-of-apdu-retries": 73, "number-of-states": 74, "object-identifier": 75, "object-list": 76,
    "object-name": 77, "object-type": 79, "out-of-service": 81, "present-value": 85, "priority-array": 87,
    "protocol-version": 98, "reliability": 103, "relinquish-default": 104, "segmentation-supported": 107,
    "state-text": 110, "status-flags": 111, "system-status": 112, "units": 117, "utc-offset": 119,
    "vendor-identifier": 120, "vendor-name": 121, "protocol-revision": 139, "database-revision": 155,
    "serial-number": 372,
}
PROP_NAMES = {v: k for k, v in P.items()}

SYSTEM_STATUS = {0: "operational", 1: "operational, read-only", 2: "download required",
                 3: "download in progress", 4: "non-operational", 5: "backup in progress"}
SEGMENTATION = {0: "both", 1: "transmit only", 2: "receive only", 3: "none"}
EVENT_STATE = {0: "normal", 1: "fault", 2: "offnormal", 3: "high-limit", 4: "low-limit", 5: "life-safety-alarm"}
RELIABILITY = {0: "no fault", 1: "no sensor", 2: "over range", 3: "under range", 4: "open loop",
               5: "shorted loop", 6: "no output", 7: "unreliable (other)", 8: "process error",
               9: "multi-state fault", 10: "configuration error", 12: "communication failure"}

# Engineering units (BACnetEngineeringUnits) → short symbol; unknown ones show as "units #N".
UNITS = {
    0: "m²", 1: "ft²", 2: "mA", 3: "A", 4: "Ω", 5: "V", 6: "kV", 7: "MV", 8: "VA", 9: "kVA", 10: "MVA",
    11: "var", 12: "kvar", 13: "Mvar", 14: "°phase", 15: "PF", 16: "J", 17: "kJ", 18: "Wh", 19: "kWh",
    20: "BTU", 21: "therm", 22: "ton·h", 23: "J/kg", 24: "BTU/lb", 25: "cycles/h", 26: "cycles/min",
    27: "Hz", 28: "g/kg", 29: "%RH", 30: "mm", 31: "m", 32: "in", 33: "ft", 34: "W/ft²", 35: "W/m²",
    36: "lm", 37: "lx", 38: "fc", 39: "kg", 40: "lb", 41: "t", 42: "kg/s", 43: "kg/min", 44: "kg/h",
    45: "lb/min", 46: "lb/h", 47: "W", 48: "kW", 49: "MW", 50: "BTU/h", 51: "hp", 52: "TR", 53: "Pa",
    54: "kPa", 55: "bar", 56: "psi", 57: "cmH₂O", 58: "inH₂O", 59: "mmHg", 60: "cmHg", 61: "inHg",
    62: "°C", 63: "K", 64: "°F", 65: "°C·days", 66: "°F·days", 67: "years", 68: "months", 69: "weeks",
    70: "days", 71: "h", 72: "min", 73: "s", 74: "m/s", 75: "km/h", 76: "ft/s", 77: "ft/min", 78: "mph",
    79: "ft³", 80: "m³", 81: "imp gal", 82: "l", 83: "US gal", 84: "cfm", 85: "m³/s", 86: "imp gal/min",
    87: "l/s", 88: "l/min", 89: "US gal/min", 90: "°", 91: "°C/h", 92: "°C/min", 93: "°F/h", 94: "°F/min",
    95: "", 96: "ppm", 97: "ppb", 98: "%", 99: "%/s", 100: "/min", 101: "/s", 102: "psi/°F", 103: "rad",
    104: "rpm", 115: "in²", 116: "cm²", 118: "cm", 122: "kΩ", 123: "MΩ", 124: "mV", 129: "kHz",
    130: "MHz", 131: "/h", 132: "mW", 133: "hPa", 134: "mbar", 135: "m³/h", 136: "l/h",
}

ERROR_CODES = {31: "unknown object", 32: "unknown property", 27: "read access denied",
               42: "invalid array index", 9: "invalid data type", 2: "configuration in progress",
               25: "operational problem", 45: "optional functionality not supported"}
REJECT_REASONS = {0: "other", 1: "buffer overflow", 2: "inconsistent parameters", 3: "invalid parameter data type",
                  4: "invalid tag", 5: "missing required parameter", 6: "parameter out of range",
                  7: "too many arguments", 8: "undefined enumeration", 9: "unrecognized service"}
ABORT_REASONS = {0: "other", 1: "buffer overflow", 2: "invalid APDU in this state", 3: "preempted by higher priority task",
                 4: "segmentation not supported", 9: "APDU too long", 11: "out of resources"}

SVC_READ_PROPERTY, SVC_READ_PROPERTY_MULTIPLE = 12, 14
SVC_WHO_HAS, SVC_I_HAVE = 7, 1
NULL = None


class BacnetError(Exception):
    """kind: error | reject | abort | timeout"""

    def __init__(self, kind: str, detail: str, code: int | None = None):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.code = kind, detail, code


class Target(NamedTuple):
    ip: str
    port: int = BACNET_PORT
    dnet: int | None = None
    dadr: bytes | None = None


def target_from_key(key: str, port: int = BACNET_PORT) -> Target:
    """Device row key: 'ip' or 'ip|snet:sadrhex' (behind a BACnet router)."""
    if "|" in key:
        ip, route = key.split("|", 1)
        net, adr = route.split(":", 1)
        return Target(ip, port, int(net), bytes.fromhex(adr))
    return Target(key, port)


# ------------------------------------------------------------------ encoding
def _ctx_unsigned(tag: int, value: int) -> bytes:
    n = max(1, (value.bit_length() + 7) // 8)
    return bytes([(tag << 4) | 0x08 | n]) + value.to_bytes(n, "big")


def _ctx_objid(tag: int, otype: int, inst: int) -> bytes:
    return bytes([(tag << 4) | 0x08 | 4]) + ((otype << 22) | inst).to_bytes(4, "big")


def _ctx_charstring(tag: int, text: str) -> bytes:
    raw = b"\x00" + text.encode("utf-8")
    if len(raw) <= 4:
        return bytes([(tag << 4) | 0x08 | len(raw)]) + raw
    if len(raw) < 254:
        return bytes([(tag << 4) | 0x08 | 5, len(raw)]) + raw
    return bytes([(tag << 4) | 0x08 | 5, 254]) + len(raw).to_bytes(2, "big") + raw


def _npdu(target: Target, expect_reply: bool = True) -> bytes:
    ctrl = 0x04 if expect_reply else 0x00
    if target.dnet is not None:
        dadr = target.dadr or b""
        return bytes([0x01, ctrl | 0x20]) + target.dnet.to_bytes(2, "big") + bytes([len(dadr)]) + dadr + b"\xff"
    return bytes([0x01, ctrl])


def _bvlc(body: bytes, broadcast: bool = False) -> bytes:
    return bytes([0x81, 0x0B if broadcast else 0x0A]) + (len(body) + 4).to_bytes(2, "big") + body


def rp_body(otype: int, inst: int, prop: int, index: int | None = None) -> bytes:
    b = _ctx_objid(0, otype, inst) + _ctx_unsigned(1, prop)
    return b + (_ctx_unsigned(2, index) if index is not None else b"")


def rpm_body(specs: list[tuple[int, int, list[int]]]) -> bytes:
    out = b""
    for otype, inst, props in specs:
        out += _ctx_objid(0, otype, inst) + b"\x1e"
        for p in props:
            out += _ctx_unsigned(0, p)
        out += b"\x1f"
    return out


def confirmed_apdu(invoke: int, service: int, body: bytes) -> bytes:
    # 0x00 confirmed request, no segmentation, segmented response NOT accepted; 0x05 = max APDU 1476
    return bytes([0x00, 0x05, invoke & 0xFF, service]) + body


def who_has_packet(name: str | None = None, otype: int | None = None, inst: int | None = None) -> bytes:
    """Unconfirmed Who-Has, global (reaches devices behind routers)."""
    apdu = bytes([0x10, SVC_WHO_HAS]) + (_ctx_objid(2, otype, inst) if name is None else _ctx_charstring(3, name))
    npdu = bytes([0x01, 0x20, 0xFF, 0xFF, 0x00, 0xFF])
    return _bvlc(npdu + apdu, broadcast=True)


# ------------------------------------------------------------------ decoding
def _date(b: bytes) -> str:
    y, m, d, _dow = b[0], b[1], b[2], b[3]
    f = lambda v, w: "*" if v == 255 else str(v).zfill(w)
    return f"{'*' if y == 255 else 1900 + y}-{f(m, 2)}-{f(d, 2)}"


def _time(b: bytes) -> str:
    h, m, s, c = b[0], b[1], b[2], b[3]
    f = lambda v: "*" if v == 255 else str(v).zfill(2)
    return f"{f(h)}:{f(m)}:{f(s)}" + ("" if c in (0, 255) else f".{str(c).zfill(2)}")


def decode_values(buf: bytes, i: int = 0, end: int | None = None, until_close: int | None = None):
    """Decode application-tagged values from buf[i:end]. Stops at a closing tag (returned position is
    after it when it matches until_close). Constructed (context-tagged) values come back as dicts.
    → (values, next_index)"""
    end = len(buf) if end is None else end
    out: list[Any] = []
    while i < end:
        tag, ctx, length, j, kind = bacnet.read_tag(buf, i)
        if kind == "close":
            return out, j
        if kind == "open":
            inner, i = decode_values(buf, j, end, tag)
            out.append({"ctx": tag, "values": inner})
            continue
        if ctx:
            out.append({"ctx": tag, "raw": buf[j:j + length].hex()})
            i = j + length
            continue
        if tag == 1:                              # boolean: the value is in the length bits
            out.append(bool(length))
            i = j
            continue
        data = buf[j:j + length]
        i = j + length
        if tag == 0:
            out.append(None)
        elif tag == 2:
            out.append(int.from_bytes(data, "big"))
        elif tag == 3:
            out.append(int.from_bytes(data, "big", signed=True))
        elif tag == 4:
            out.append(struct.unpack(">f", data)[0])
        elif tag == 5:
            out.append(struct.unpack(">d", data)[0])
        elif tag == 6:
            out.append({"octets": data.hex()})
        elif tag == 7:
            out.append(bacnet.decode_string(data))
        elif tag == 8:
            unused = data[0] if data else 0
            bits = []
            for byte in data[1:]:
                bits.extend(bool(byte & (0x80 >> k)) for k in range(8))
            out.append({"bits": bits[:len(bits) - unused] if unused else bits})
        elif tag == 9:
            out.append({"enum": int.from_bytes(data, "big")})
        elif tag == 10:
            out.append({"date": _date(data)})
        elif tag == 11:
            out.append({"time": _time(data)})
        elif tag == 12:
            v = int.from_bytes(data, "big")
            out.append({"oid": [v >> 22, v & 0x3FFFFF]})
        else:
            out.append({"raw": data.hex()})
    return out, i


def _unwrap(values: list) -> Any:
    return values[0] if len(values) == 1 else values


def parse_rp_ack(payload: bytes) -> tuple[int, int, int | None, Any]:
    """ReadProperty-ACK payload (after the service byte) → (object_type, instance, property, index, value)."""
    i, oid, prop, index = 0, None, None, None
    while i < len(payload):
        tag, ctx, length, j, kind = bacnet.read_tag(payload, i)
        if kind == "open" and tag == 3:
            vals, _ = decode_values(payload, j, None, 3)
            return oid, prop, index, _unwrap(vals)
        v = int.from_bytes(payload[j:j + length], "big")
        if tag == 0:
            oid = (v >> 22, v & 0x3FFFFF)
        elif tag == 1:
            prop = v
        elif tag == 2:
            index = v
        i = j + length
    raise BacnetError("error", "malformed ReadProperty reply")


def parse_rpm_ack(payload: bytes) -> dict[tuple[int, int], dict[int, Any]]:
    """ReadPropertyMultiple-ACK → {(type, instance): {property: value | BacnetError}}."""
    out: dict[tuple[int, int], dict[int, Any]] = {}
    i, oid = 0, None
    while i < len(payload):
        tag, ctx, length, j, kind = bacnet.read_tag(payload, i)
        if kind == "data" and tag == 0:
            v = int.from_bytes(payload[j:j + length], "big")
            oid = (v >> 22, v & 0x3FFFFF)
            out.setdefault(oid, {})
            i = j + length
            continue
        if kind == "open" and tag == 1:          # list of results for this object
            i = j
            prop = None
            while i < len(payload):
                t2, c2, l2, j2, k2 = bacnet.read_tag(payload, i)
                if k2 == "close" and t2 == 1:
                    i = j2
                    break
                if k2 == "data" and t2 == 2:
                    prop = int.from_bytes(payload[j2:j2 + l2], "big")
                    i = j2 + l2
                elif k2 == "data" and t2 == 3:   # array index
                    i = j2 + l2
                elif k2 == "open" and t2 == 4:
                    vals, i = decode_values(payload, j2, None, 4)
                    out[oid][prop] = _unwrap(vals)
                elif k2 == "open" and t2 == 5:
                    vals, i = decode_values(payload, j2, None, 5)
                    code = vals[1]["enum"] if len(vals) > 1 and isinstance(vals[1], dict) else None
                    out[oid][prop] = BacnetError("error", ERROR_CODES.get(code, f"error {code}"), code)
                else:
                    i = j2 + l2
            continue
        i = j + length
    return out


def parse_error(pdu_type: int, apdu: bytes) -> BacnetError:
    if pdu_type == 5:
        vals, _ = decode_values(apdu, 3)
        code = vals[1]["enum"] if len(vals) > 1 and isinstance(vals[1], dict) else None
        return BacnetError("error", ERROR_CODES.get(code, f"error class {vals[0]['enum'] if vals else '?'}, code {code}"), code)
    reason = apdu[2] if len(apdu) > 2 else 0
    if pdu_type == 6:
        return BacnetError("reject", REJECT_REASONS.get(reason, f"reason {reason}"), reason)
    return BacnetError("abort", ABORT_REASONS.get(reason, f"reason {reason}"), reason)


def parse_i_have(apdu: bytes) -> dict | None:
    if len(apdu) < 2 or apdu[0] != 0x10 or apdu[1] != SVC_I_HAVE:
        return None
    vals, _ = decode_values(apdu, 2)
    if len(vals) < 3 or not all(isinstance(v, dict) and "oid" in v for v in vals[:2]) or not isinstance(vals[2], str):
        return None
    return {"device": vals[0]["oid"][1], "object": vals[1]["oid"], "name": vals[2]}


# ------------------------------------------------------------------ client
class Client:
    """One UDP socket on the adapter's address; requests pipelined, matched by invoke id."""

    def __init__(self, src_ip: str, timeout: float = 1.2, retries: int = 2, window: int = 8):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((src_ip, 0))
        self.timeout, self.retries, self.window = timeout, retries, window
        self._invoke = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _next_invoke(self) -> int:
        self._invoke = (self._invoke + 1) % 256
        return self._invoke

    def call_many(self, target: Target, calls: list[tuple[int, bytes]],
                  cancel: Callable[[], bool] = lambda: False, progress: Callable[[int], None] | None = None
                  ) -> list[bytes | BacnetError]:
        """Send confirmed requests (service, body), at most `window` at a time. Returns, in order,
        each Complex-ACK payload (after the service byte) or a BacnetError."""
        results: list[bytes | BacnetError | None] = [None] * len(calls)
        todo = list(range(len(calls)))
        inflight: dict[int, tuple[int, float, int]] = {}     # invoke → (call index, deadline, tries)
        done = 0
        while (todo or inflight) and not cancel():
            while todo and len(inflight) < self.window:
                k = todo.pop(0)
                self._send(target, calls[k], k, 1, inflight)
            try:
                self.sock.settimeout(0.1)
                data, _addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                data = None
            if data:
                b = bacnet.parse_bvlc(data)
                n = bacnet.parse_npdu(b[2]) if b else None
                apdu = n[2] if n else b""
                if len(apdu) >= 3 and apdu[1] in inflight:
                    pdu = apdu[0] >> 4
                    k, _dl, _t = inflight.pop(apdu[1])
                    if pdu == 3 and not apdu[0] & 0x08:
                        results[k] = apdu[3:]
                    elif pdu == 3:
                        results[k] = BacnetError("abort", "segmented reply not accepted", 4)
                    elif pdu == 2:
                        results[k] = b""
                    else:
                        results[k] = parse_error(pdu, apdu)
                    done += 1
                    if progress:
                        progress(done)
            now = time.monotonic()
            for inv, (k, deadline, tries) in list(inflight.items()):
                if now > deadline:
                    inflight.pop(inv)
                    if tries <= self.retries:
                        self._send(target, calls[k], k, tries + 1, inflight)
                    else:
                        results[k] = BacnetError("timeout", "no reply")
                        done += 1
        return [r if r is not None else BacnetError("timeout", "cancelled") for r in results]

    def _send(self, target, call, k, tries, inflight):
        inv = self._next_invoke()
        while inv in inflight:
            inv = self._next_invoke()
        service, body = call
        pkt = _bvlc(_npdu(target) + confirmed_apdu(inv, service, body))
        try:
            self.sock.sendto(pkt, (target.ip, target.port))
        except OSError:
            pass
        inflight[inv] = (k, time.monotonic() + self.timeout, tries)

    def call(self, target: Target, service: int, body: bytes) -> bytes:
        r = self.call_many(target, [(service, body)])[0]
        if isinstance(r, BacnetError):
            raise r
        return r

    # ---- ReadProperty / ReadPropertyMultiple
    def read(self, target: Target, otype: int, inst: int, prop: int, index: int | None = None) -> Any:
        return parse_rp_ack(self.call(target, SVC_READ_PROPERTY, rp_body(otype, inst, prop, index)))[3]

    def read_many(self, target: Target, items: list[tuple[int, int, int, int | None]], **kw) -> list[Any]:
        """Many ReadProperty calls pipelined → value or BacnetError each."""
        res = self.call_many(target, [(SVC_READ_PROPERTY, rp_body(*it)) for it in items], **kw)
        out = []
        for r in res:
            if isinstance(r, BacnetError):
                out.append(r)
            else:
                try:
                    out.append(parse_rp_ack(r)[3])
                except (BacnetError, IndexError, struct.error) as e:
                    out.append(e if isinstance(e, BacnetError) else BacnetError("error", "malformed reply"))
        return out

    def rpm(self, target: Target, specs: list[tuple[int, int, list[int]]]) -> dict:
        return parse_rpm_ack(self.call(target, SVC_READ_PROPERTY_MULTIPLE, rpm_body(specs)))


# ------------------------------------------------------------------ what NetJoin reads
DEVICE_PROPS = ["object-name", "vendor-name", "vendor-identifier", "model-name", "firmware-revision",
                "application-software-version", "protocol-version", "protocol-revision", "location",
                "description", "system-status", "max-apdu-length-accepted", "segmentation-supported",
                "database-revision", "local-date", "local-time", "utc-offset", "serial-number"]


def read_device_properties(client: Client, target: Target, instance: int) -> dict[str, Any]:
    """Device object properties → {name: value or {'error': text}}. One RPM if the device takes it."""
    ids = [P[n] for n in DEVICE_PROPS]
    try:
        got = client.rpm(target, [(8, instance, ids)]).get((8, instance), {})
        if not got:
            raise BacnetError("error", "empty reply")
    except BacnetError as e:
        if e.kind == "timeout":
            raise
        vals = client.read_many(target, [(8, instance, p, None) for p in ids])
        got = dict(zip(ids, vals))
    return {PROP_NAMES[p]: ({"error": v.detail} if isinstance(v, BacnetError) else v) for p, v in got.items()}


def read_object_list(client: Client, target: Target, instance: int,
                     cancel=lambda: False, progress: Callable[[str, int, int], None] | None = None) -> list[tuple[int, int]]:
    """The device's object-list as [(type, instance)]. Whole list in one read if it fits, otherwise
    element by element (index 0 = length)."""
    try:
        v = client.read(target, 8, instance, P["object-list"])
        vals = v if isinstance(v, list) else [v]
        return [tuple(x["oid"]) for x in vals if isinstance(x, dict) and "oid" in x]
    except BacnetError as e:
        if e.kind == "timeout":
            raise
    n = client.read(target, 8, instance, P["object-list"], 0)
    if not isinstance(n, int):
        raise BacnetError("error", "object-list length unreadable")
    if progress:
        progress("object list", 0, n)
    vals = client.read_many(target, [(8, instance, P["object-list"], k) for k in range(1, n + 1)], cancel=cancel,
                            progress=(lambda d: progress("object list", d, n)) if progress else None)
    return [tuple(v["oid"]) for v in vals if isinstance(v, dict) and "oid" in v]


def object_props(otype: int) -> list[int]:
    ps = [P["object-name"], P["description"], P["present-value"], P["status-flags"], P["out-of-service"],
          P["event-state"], P["reliability"]]
    if otype in ANALOG:
        ps.append(P["units"])
    if otype in BINARY:
        ps += [P["active-text"], P["inactive-text"]]
    if otype in MULTISTATE:
        ps += [P["state-text"], P["number-of-states"]]
    if otype in COMMANDABLE:
        ps += [P["priority-array"], P["relinquish-default"]]
    return ps


def read_objects(client: Client, target: Target, objects: list[tuple[int, int]], cancel=lambda: False,
                 progress: Callable[[str, int, int], None] | None = None, batch: int = 8) -> dict:
    """Name, value, units, flags… for each object. ReadPropertyMultiple in batches (smaller if the
    device aborts for size); plain ReadProperty if it doesn't do RPM at all."""
    out: dict[tuple[int, int], dict] = {o: {} for o in objects}
    pending = list(objects)
    use_rpm = True
    done = 0
    while pending and not cancel():
        if use_rpm:
            chunk = pending[:batch]
            try:
                got = client.rpm(target, [(t, i, object_props(t)) for t, i in chunk])
                for o in chunk:
                    out[o].update(got.get(o, {}))
                pending = pending[len(chunk):]
                done += len(chunk)
                if progress:
                    progress("objects", done, len(objects))
                continue
            except BacnetError as e:
                if e.kind == "abort" and batch > 1:
                    batch = max(1, batch // 2)      # reply too big: smaller batches
                    continue
                if e.kind == "timeout" and batch > 1:
                    batch = max(1, batch // 2)
                    continue
                if e.kind in ("reject", "error", "abort", "timeout"):
                    use_rpm = False                 # no RPM here: one property at a time
                    continue
        items = [(t, i, p, None) for t, i in pending for p in object_props(t)]
        vals = client.read_many(target, items, cancel=cancel,
                                progress=(lambda d: progress("objects", done + d * len(pending) // max(1, len(items)), len(objects)))
                                if progress else None)
        for (t, i, p, _), v in zip(items, vals):
            out[(t, i)][p] = v
        pending = []
    return out


def who_has(src_ip: str, broadcast: str, name: str, listen: float = 3.0, port: int = BACNET_PORT,
            cancel=lambda: False) -> list[dict]:
    """Ask every device (including behind routers) which one holds an object called `name`."""
    sock, _std = bacnet.open_socket(src_ip, port)
    found: dict[tuple, dict] = {}
    try:
        pkt = who_has_packet(name=name)
        for dst in {broadcast, "255.255.255.255"}:
            try:
                sock.sendto(pkt, (dst, port))
            except OSError:
                pass
        end = time.monotonic() + listen
        while time.monotonic() < end and not cancel():
            sock.settimeout(max(0.05, min(0.3, end - time.monotonic())))
            try:
                data, (ip, rport) = sock.recvfrom(1500)
            except (socket.timeout, OSError):
                continue
            b = bacnet.parse_bvlc(data)
            if not b:
                continue
            if b[1]:
                ip, rport = b[1]
            n = bacnet.parse_npdu(b[2])
            if not n:
                continue
            ih = parse_i_have(n[2])
            if ih:
                key = bacnet.device_key(ip, n[0], n[1])
                found[(key, tuple(ih["object"]))] = {**ih, "ip": ip, "key": key, "route": bacnet.route_text(n[0], n[1])}
    finally:
        sock.close()
    return list(found.values())


# ------------------------------------------------------------------ display helpers
def fmt_value(v: Any, otype: int | None = None, props: dict | None = None) -> str:
    props = props or {}
    if isinstance(v, BacnetError):
        return f"({v.detail})"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1e7 else f"{v:.4g}"
    if isinstance(v, dict):
        if "enum" in v:
            e = v["enum"]
            if otype in BINARY and e in (0, 1):
                t = props.get(P["active-text"] if e else P["inactive-text"])
                return t if isinstance(t, str) and t else ("active" if e else "inactive")
            return str(e)
        if "oid" in v:
            t, i = v["oid"]
            return f"{SHORT_TYPES.get(t, OBJECT_TYPES.get(t, t))} {i}"
        for k in ("date", "time"):
            if k in v:
                return v[k]
        if "bits" in v:
            return "".join("1" if b else "0" for b in v["bits"])
        if "octets" in v:
            return v["octets"]
        return "…"
    if isinstance(v, list):
        return ", ".join(fmt_value(x, otype, props) for x in v)
    return str(v)
