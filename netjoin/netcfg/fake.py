"""In-memory back-end for development and demos on any OS (``--fake``).

Pretends that any address ending in .200 or .230 is already taken, so the
clash-and-retry path can be seen working. Override with NETJOIN_FAKE_TAKEN=
"10.20.5.200,10.20.5.231".
"""

from __future__ import annotations

import os
import threading

from .base import Adapter, Address, Backend, NetError


class FakeBackend(Backend):
    kind = "fake"

    def __init__(self):
        self._lock = threading.Lock()
        self._adapters = {
            "Ethernet 2": Adapter("Ethernet 2", "Realtek USB GbE Family Controller", "00-E0-4C-12-34-56", True,
                                  False, True, [Address("169.254.20.7", 16, "preferred", "dhcp")]),
            "Wi-Fi": Adapter("Wi-Fi", "Intel(R) Wi-Fi 6 AX201", "A4-B1-C1-00-11-22", True, False, True,
                             [Address("192.168.1.50", 24, "preferred", "dhcp")], ["192.168.1.1"], ["192.168.1.1"]),
            "vEthernet (WSL)": Adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", "00-15-5D-00-00-01",
                                       True, True, False, [Address("172.22.96.1", 20, "preferred", "manual")]),
        }
        env = os.environ.get("NETJOIN_FAKE_TAKEN")
        self._taken = {x.strip() for x in env.split(",")} if env else None

    def unplug(self, name: str):
        """Demo/test helper: remove an adapter as if a USB dongle was pulled out."""
        with self._lock:
            return self._adapters.pop(name, None)

    def plug(self, adapter, new_name: str | None = None):
        with self._lock:
            if new_name:
                adapter.name = new_name
            self._adapters[adapter.name] = adapter

    def _is_taken(self, ip: str) -> bool:
        if self._taken is not None:
            return ip in self._taken
        return ip.endswith(".200") or ip.endswith(".230")

    def list_adapters(self):
        with self._lock:
            return [Adapter(**{**a.__dict__, "addresses": [Address(**x.__dict__) for x in a.addresses],
                               "gateways": list(a.gateways), "dns": list(a.dns)})
                    for a in self._adapters.values()]

    def set_static(self, name, ip, prefix, gateway, dns):
        with self._lock:
            a = self._adapters.get(name)
            if not a:
                raise NetError(f"No adapter called {name}")
            a.dhcp = False
            a.addresses = [Address(ip, prefix, "duplicate" if self._is_taken(ip) else "preferred", "manual")]
            a.gateways = [gateway] if gateway else []
            a.dns = list(dns)

    def set_gateway(self, name, ip, prefix, gateway):
        with self._lock:
            a = self._adapters.get(name)
            if not a:
                raise NetError(f"No adapter called {name}")
            a.gateways = [gateway] if gateway else []

    def set_dns(self, name, dns):
        with self._lock:
            a = self._adapters.get(name)
            if not a:
                raise NetError(f"No adapter called {name}")
            a.dns = list(dns)

    def set_dhcp(self, name):
        with self._lock:
            a = self._adapters.get(name)
            if not a:
                raise NetError(f"No adapter called {name}")
            a.dhcp = True
            a.addresses = [Address("169.254.20.7", 16, "preferred", "dhcp")] if name != "Wi-Fi" else \
                [Address("192.168.1.50", 24, "preferred", "dhcp")]
            a.gateways, a.dns = ([], []) if name != "Wi-Fi" else (["192.168.1.1"], ["192.168.1.1"])

    def dad_state(self, name, ip):
        a = self._adapters.get(name)
        for x in (a.addresses if a else []):
            if x.ip == ip:
                return x.dad
        return None

    def wait_dad(self, name, ip, timeout=4.0):
        return self.dad_state(name, ip)

    def check_internet(self, src_ip, dns):
        from ..inet import INTERNET, IP_ONLY, NONE
        if not src_ip.startswith("192.168.1."):
            return {"code": NONE, "detail": "no route to the internet from this adapter"}
        if not dns:
            return {"code": IP_ONLY, "detail": "internet reachable by IP, but no DNS servers are set – web pages won't load"}
        return {"code": INTERNET, "detail": "route and DNS both working"}
