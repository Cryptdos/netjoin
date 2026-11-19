"""Minimal BACnet/IP: Who-Is broadcast, I-Am decoding, ReadProperty for names.

Only what NetJoin needs, no third-party stack. References: ASHRAE 135 Annex J (BVLC),
clause 6 (NPDU), clause 20 (APDU encoding).
"""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Callable

BACNET_PORT = 47808

# BVLC Original-Broadcast-NPDU | NPDU v1, dest present, DNET=0xFFFF (global), DLEN=0, hops=255 | Who-Is
WHO_IS = bytes([0x81, 0x0B, 0x00, 0x0C, 0x01, 0x20, 0xFF, 0xFF, 0x00, 0xFF, 0x10, 0x08])

PROP_OBJECT_NAME = 77
PROP_MODEL_NAME = 70
PROP_VENDOR_NAME = 121
DEVICE_OBJECT_TYPE = 8

# Small fallback table (ASHRAE assignments) used only if a device won't tell us its vendor name.
VENDORS = {
    0: "ASHRAE", 1: "NIST", 2: "Trane", 3: "Daikin Applied", 4: "PolarSoft", 5: "Johnson Controls",
    6: "ABB (American Auto-Matrix)", 7: "Siemens", 8: "Delta Controls", 9: "Siemens", 10: "Schneider Electric",
    11: "TAC", 13: "Teletrol", 14: "Cimetrics", 16: "Carrier", 17: "Honeywell", 18: "Alerton / Honeywell",
    19: "TAC AB",
}


# ---------------------------------------------------------------- decoding
def parse_bvlc(data: bytes):
    """→ (function, original_source or None, npdu) or None."""
    if len(data) < 4 or data[0] != 0x81:
        return None
    fn, length = data[1], int.from_bytes(data[2:4], "big")
    end = min(length, len(data))
    if fn == 0x04 and end >= 10:            # Forwarded-NPDU (via a BBMD): carries the original sender
        return fn, (socket.inet_ntoa(data[4:8]), int.from_bytes(data[8:10], "big")), data[10:end]
    if fn in (0x0A, 0x0B):                  # Original-Unicast / Original-Broadcast
        return fn, None, data[4:end]
    return None


def parse_npdu(npdu: bytes):
    """→ (snet, sadr bytes, apdu) or None for network-layer messages."""
    if len(npdu) < 2 or npdu[0] != 0x01:
        return None
    ctrl, i = npdu[1], 2
    snet = sadr = None
    if ctrl & 0x20:                         # destination present
        dlen = npdu[i + 2]
        i += 3 + dlen
    if ctrl & 0x08:                         # source present (routed from another BACnet network)
        snet = int.from_bytes(npdu[i:i + 2], "big")
        slen = npdu[i + 2]
        sadr = bytes(npdu[i + 3:i + 3 + slen])
        i += 3 + slen
    if ctrl & 0x20:
        i += 1                              # hop count
    if ctrl & 0x80:                         # network-layer message, not an APDU
        return None
    return snet, sadr, npdu[i:]


def read_tag(buf: bytes, i: int):
    """→ (tag_number, is_context, length, data_start, kind) where kind is 'data' | 'open' | 'close'."""
    b = buf[i]
    tag, ctx, lvt = b >> 4, bool(b & 0x08), b & 0x07
    j = i + 1
    if tag == 15:
        tag = buf[j]
        j += 1
    if ctx and lvt == 6:
        return tag, ctx, 0, j, "open"
    if ctx and lvt == 7:
        return tag, ctx, 0, j, "close"
    if lvt == 5:
        length = buf[j]
        j += 1
        if length == 254:
            length = int.from_bytes(buf[j:j + 2], "big")
            j += 2
        elif length == 255:
            length = int.from_bytes(buf[j:j + 4], "big")
            j += 4
    else:
        length = lvt
    return tag, ctx, length, j, "data"


def parse_iam(apdu: bytes) -> dict | None:
    """Unconfirmed I-Am → {'instance', 'max_apdu', 'segmentation', 'vendor_id'}."""
    if len(apdu) < 2 or apdu[0] != 0x10 or apdu[1] != 0x00:
        return None
    try:
        vals = []
        i = 2
        while i < len(apdu) and len(vals) < 4:
            tag, ctx, length, j, kind = read_tag(apdu, i)
            if ctx or kind != "data":
                return None
            vals.append((tag, int.from_bytes(apdu[j:j + length], "big")))
            i = j + length
        if len(vals) < 4 or vals[0][0] != 12:
            return None
        oid = vals[0][1]
        if oid >> 22 != DEVICE_OBJECT_TYPE:
            return None
        return {"instance": oid & 0x3FFFFF, "max_apdu": vals[1][1], "segmentation": vals[2][1],
                "vendor_id": vals[3][1]}
    except IndexError:
        return None


def decode_string(buf: bytes) -> str:
    if not buf:
        return ""
    charset, raw = buf[0], buf[1:]
    if charset == 4:
        return raw.decode("utf-16-be", errors="replace")
    if charset == 5:
        return raw.decode("latin-1", errors="replace")
    return raw.decode("utf-8", errors="replace")


def parse_read_property_ack(apdu: bytes) -> tuple[int, int, str | None] | None:
    """Complex-ACK to ReadProperty → (invoke_id, property, text value or None)."""
    if len(apdu) < 3 or apdu[0] >> 4 != 3 or apdu[2] != 0x0C:
        return None
    invoke, i, prop = apdu[1], 3, None
    try:
        while i < len(apdu):
            tag, ctx, length, j, kind = read_tag(apdu, i)
            if kind == "open" and tag == 3:
                vtag, vctx, vlen, vj, vkind = read_tag(apdu, j)
                if not vctx and vtag == 7:
                    return invoke, prop, decode_string(apdu[vj:vj + vlen]).strip()
                return invoke, prop, None
            if ctx and tag == 1:
                prop = int.from_bytes(apdu[j:j + length], "big")
            i = j + length
    except IndexError:
        pass
    return None


def error_invoke(apdu: bytes) -> int | None:
    """Invoke id of an Error / Reject / Abort PDU, so we stop waiting for it."""
    if len(apdu) >= 2 and apdu[0] >> 4 in (5, 6, 7):
        return apdu[1]
    return None


# ---------------------------------------------------------------- encoding
def read_property_request(invoke: int, instance: int, prop: int,
                          dnet: int | None = None, dadr: bytes | None = None) -> bytes:
    oid = ((DEVICE_OBJECT_TYPE << 22) | instance).to_bytes(4, "big")
    prop_tag = bytes([0x19, prop]) if prop < 256 else bytes([0x1A]) + prop.to_bytes(2, "big")
    apdu = bytes([0x00, 0x05, invoke & 0xFF, 0x0C, 0x0C]) + oid + prop_tag
    if dnet is not None:
        npdu = bytes([0x01, 0x24]) + dnet.to_bytes(2, "big") + bytes([len(dadr or b"")]) + (dadr or b"") + bytes([0xFF])
    else:
        npdu = bytes([0x01, 0x04])
    body = npdu + apdu
    return bytes([0x81, 0x0A]) + (len(body) + 4).to_bytes(2, "big") + body


def i_am_packet(instance: int, vendor_id: int) -> bytes:
    """Build an I-Am (used by tests and the demo back-end)."""
    oid = ((DEVICE_OBJECT_TYPE << 22) | instance).to_bytes(4, "big")
    apdu = bytes([0x10, 0x00, 0xC4]) + oid + bytes([0x22, 0x05, 0xC4, 0x91, 0x00, 0x21, vendor_id & 0xFF])
    body = bytes([0x01, 0x00]) + apdu
    return bytes([0x81, 0x0B]) + (len(body) + 4).to_bytes(2, "big") + body


# ---------------------------------------------------------------- discovery
def device_key(ip: str, snet: int | None, sadr: bytes | None) -> str:
    return ip if snet is None else f"{ip}|{snet}:{(sadr or b'').hex()}"


def route_text(snet: int | None, sadr: bytes | None) -> str | None:
    if snet is None:
        return None
    mac = int.from_bytes(sadr, "big") if sadr and len(sadr) == 1 else (sadr or b"").hex()
    return f"net {snet}, MAC {mac}"


def open_socket(src_ip: str, port: int = BACNET_PORT) -> tuple[socket.socket, bool]:
    """UDP socket on the adapter's IP. Prefers the BACnet port so broadcast I-Am replies arrive."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind((src_ip, port))
        return s, True
    except OSError:
        s.bind((src_ip, 0))
        return s, False


def who_is(src_ip: str, prefix: int, listen: float = 3.0, read_names: bool = True,
           cancel: Callable[[], bool] = lambda: False, log: Callable[[str, str], None] = lambda m, l="info": None
           ) -> list[dict]:
    """Broadcast Who-Is on the adapter's subnet, collect I-Am replies, then ask each device its names."""
    net = ipaddress.IPv4Network(f"{src_ip}/{prefix}", strict=False)
    sock, std_port = open_socket(src_ip)
    if not std_port:
        log(f"UDP {BACNET_PORT} is in use by another program (another BACnet tool?); "
            "devices that reply by broadcast may be missed", "warn")
    devices: dict[str, dict] = {}
    try:
        for dst in {str(net.broadcast_address), "255.255.255.255"}:
            try:
                sock.sendto(WHO_IS, (dst, BACNET_PORT))
            except OSError as e:
                log(f"Who-Is to {dst} failed: {e}", "warn")
        end = time.monotonic() + listen
        while time.monotonic() < end and not cancel():
            sock.settimeout(max(0.05, min(0.3, end - time.monotonic())))
            try:
                data, (ip, port) = sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                continue
            if ip == src_ip:
                continue
            dev = decode_iam_datagram(data, ip, port)
            if dev:
                devices.setdefault(dev["key"], dev)
        if read_names and devices and not cancel():
            _read_names(sock, list(devices.values()), cancel)
    finally:
        sock.close()
    return list(devices.values())


def decode_iam_datagram(data: bytes, ip: str, port: int) -> dict | None:
    b = parse_bvlc(data)
    if not b:
        return None
    _fn, orig, npdu = b
    if orig:
        ip, port = orig
    n = parse_npdu(npdu)
    if not n:
        return None
    snet, sadr, apdu = n
    iam = parse_iam(apdu)
    if not iam:
        return None
    return {**iam, "ip": ip, "port": port, "snet": snet, "sadr": sadr,
            "key": device_key(ip, snet, sadr), "route": route_text(snet, sadr),
            "vendor": VENDORS.get(iam["vendor_id"]), "name": None, "model": None}


def _read_names(sock: socket.socket, devs: list[dict], cancel, batch: int = 80, wait: float = 1.5) -> None:
    props = (PROP_OBJECT_NAME, PROP_MODEL_NAME, PROP_VENDOR_NAME)
    field = {PROP_OBJECT_NAME: "name", PROP_MODEL_NAME: "model", PROP_VENDOR_NAME: "vendor"}
    for start in range(0, len(devs), batch):
        pending: dict[int, tuple[dict, int]] = {}
        invoke = 0
        for d in devs[start:start + batch]:
            for p in props:
                pkt = read_property_request(invoke, d["instance"], p, d["snet"], d["sadr"])
                try:
                    sock.sendto(pkt, (d["ip"], d["port"]))
                    pending[invoke] = (d, p)
                except OSError:
                    pass
                invoke = (invoke + 1) % 256
        end = time.monotonic() + wait
        while pending and time.monotonic() < end and not cancel():
            sock.settimeout(max(0.05, min(0.3, end - time.monotonic())))
            try:
                data, _addr = sock.recvfrom(1500)
            except (socket.timeout, OSError):
                continue
            b = parse_bvlc(data)
            n = parse_npdu(b[2]) if b else None
            if not n:
                continue
            apdu = n[2]
            ack = parse_read_property_ack(apdu)
            if ack and ack[0] in pending:
                d, p = pending.pop(ack[0])
                if ack[2]:
                    d[field[p]] = ack[2]
                continue
            err = error_invoke(apdu)
            if err is not None:
                pending.pop(err, None)
