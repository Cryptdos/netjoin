"""Internet check through one specific adapter (by binding to its IP)."""

from __future__ import annotations

import socket

PROBE_HOST = "www.msftconnecttest.com"
PROBE_PATH = "/connecttest.txt"
PROBE_TEXT = b"Microsoft Connect Test"
FALLBACK = ("1.1.1.1", 443)


def check_internet(src_ip: str, timeout: float = 2.5) -> bool:
    """True if the internet is reachable *from src_ip* (i.e. out of that adapter).

    Windows uses the strong host model, so binding the socket to the adapter's
    address keeps the traffic on that adapter even if Wi-Fi has internet.
    """
    try:
        target = socket.getaddrinfo(PROBE_HOST, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.bind((src_ip, 0))
            s.connect((target, 80))
            s.sendall(f"GET {PROBE_PATH} HTTP/1.0\r\nHost: {PROBE_HOST}\r\n\r\n".encode())
            data = s.recv(2048)
            if PROBE_TEXT in data:
                return True
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.bind((src_ip, 0))
            s.connect(FALLBACK)
            return True
    except OSError:
        return False
