"""The things NetJoin does: Join, revert to DHCP, rejoin, internet checks."""

from __future__ import annotations

import threading
from typing import Any

from . import ipcalc
from .eventlog import EventLog
from .netcfg import Adapter, Backend, NetError
from .store import Store

MAX_ATTEMPTS = 10


class JoinError(RuntimeError):
    pass


class Engine:
    def __init__(self, store: Store, backend: Backend, log: EventLog):
        self.store, self.backend, self.log = store, backend, log
        self._net_lock = threading.Lock()   # one adapter change at a time
        self.internet_cache: dict[str, Any] = {}

    # ---------- adapters ----------
    def selected_adapter_name(self) -> str | None:
        name = self.store.get_setting("adapter")
        names = [a.name for a in self.backend.list_adapters()]
        if name in names:
            return name
        physical = [a for a in self.backend.list_adapters() if not a.virtual]
        wired = [a for a in physical if "wi-fi" not in a.name.lower() and "wireless" not in a.description.lower()]
        pick = (wired or physical or [None])[0]
        return pick.name if pick else None

    def adapter(self, name: str | None = None) -> Adapter:
        name = name or self.selected_adapter_name()
        a = self.backend.get_adapter(name) if name else None
        if not a:
            raise JoinError("Pick a network adapter first")
        return a

    def status(self) -> dict:
        name = self.selected_adapter_name()
        a = self.backend.get_adapter(name) if name else None
        applied = self.store.get_setting("applied")
        last = self.store.get_setting("last_network")
        current = None
        if a and applied and applied.get("adapter") == a.name and a.primary and a.primary.ip == applied.get("ip"):
            current = applied
        return {
            "backend": self.backend.kind,
            "admin": self.backend.is_admin(),
            "adapter": a.to_dict() if a else None,
            "primary": a.primary.__dict__ if a and a.primary else None,
            "joined": current,
            "last_network": last,
            "internet": self.internet_cache.get(name) if name else None,
        }

    # ---------- join ----------
    def candidates(self, network: dict, explicit_ip: str | None = None) -> list[str]:
        net = ipcalc.parse_subnet(network["subnet"])
        if explicit_ip:
            if not ipcalc.in_subnet(explicit_ip, net):
                raise JoinError(f"{explicit_ip} isn't a usable address in {net}")
            return [explicit_ip]
        order = []
        last = self.store.last_worked_ip(network["id"])
        if last and ipcalc.in_subnet(last, net):
            order.append(last)
        for ip in ipcalc.iter_range(network["range_start"], network["range_end"], net):
            if ip not in order:
                order.append(ip)
        return order

    def next_ip(self, network: dict) -> str | None:
        try:
            c = self.candidates(network)
        except Exception:
            return None
        return c[0] if c else None

    def _snapshot(self, a: Adapter) -> dict:
        p = a.primary
        return {"adapter": a.name, "dhcp": bool(a.dhcp), "ip": p.ip if p else None,
                "prefix": p.prefix if p else None, "gateway": a.gateways[0] if a.gateways else None,
                "dns": list(a.dns)}

    def _restore(self, snap: dict) -> None:
        if snap.get("dhcp") or not snap.get("ip"):
            self.backend.set_dhcp(snap["adapter"])
        else:
            self.backend.set_static(snap["adapter"], snap["ip"], snap["prefix"], snap.get("gateway"), snap.get("dns") or [])

    def join(self, network_id: int, ip: str | None = None, adapter_name: str | None = None) -> dict:
        network = self.store.node(network_id)
        if not network or network["kind"] != "network":
            raise JoinError("That isn't a network")
        if not self.backend.is_admin():
            raise JoinError("NetJoin isn't running as administrator, so it can't change adapter settings")
        with self._net_lock:
            a = self.adapter(adapter_name)
            net = ipcalc.parse_subnet(network["subnet"])
            gwdns = self.store.resolve_gateway_dns(network_id)
            label = " › ".join(self.store.path_names(network_id))
            snap = self._snapshot(a)
            self.log.add(f"Join {label} on {a.name} ({net})")
            tried = 0
            for cand in self.candidates(network, ip):
                if tried >= MAX_ATTEMPTS:
                    break
                tried += 1
                try:
                    self.backend.set_static(a.name, cand, net.prefixlen, gwdns["gateway"], gwdns["dns"])
                except NetError as e:
                    self.log.add(f"Failed to apply {cand}: {e}", "error")
                    self.store.add_history(network_id, a.name, cand, net.prefixlen, "failed", detail=str(e))
                    raise JoinError(str(e))
                state = self.backend.wait_dad(a.name, cand)
                if state == "duplicate":
                    self.log.add(f"{cand} clashed (another device answered ARP), trying next", "warn")
                    self.store.add_history(network_id, a.name, cand, net.prefixlen, "clashed")
                    continue
                if state not in ("preferred", None):
                    self.log.add(f"{cand} state is '{state}' after waiting; carrying on", "warn")
                internet = self.backend.check_internet(cand)
                self.internet_cache[a.name] = internet
                self.store.add_history(network_id, a.name, cand, net.prefixlen, "worked", internet)
                applied = {"adapter": a.name, "ip": cand, "prefix": net.prefixlen, "network_id": network_id,
                           "network": network["name"], "path": label}
                self.store.set_setting("applied", applied)
                self.store.set_setting("last_network", applied)
                extra = []
                if gwdns["gateway"]:
                    extra.append(f"gw {gwdns['gateway']}")
                if gwdns["dns"]:
                    extra.append(f"dns {', '.join(gwdns['dns'])}")
                self.log.add(f"Applied {cand}/{net.prefixlen} to {a.name}"
                             f"{' (' + '; '.join(extra) + ')' if extra else ''} ✓ · internet: {'yes' if internet else 'no'}")
                return {"ok": True, "ip": cand, "prefix": net.prefixlen, "internet": internet, "attempts": tried}
            # Nothing worked: put the adapter back how it was.
            if ip:
                what, msg = f"{ip} is in use by another device", f"{ip} is in use by another device. Try Join to pick a free address."
            else:
                what, msg = f"No free address found after {tried} tries", f"Every address tried clashed ({tried} tries). Check the allowed range."
            try:
                self._restore(snap)
                self.log.add(f"{what}; restored previous settings on {a.name}", "error")
            except NetError as e:
                self.log.add(f"{what}, and restoring previous settings failed: {e}", "error")
            raise JoinError(msg)

    def dhcp(self, adapter_name: str | None = None) -> dict:
        if not self.backend.is_admin():
            raise JoinError("NetJoin isn't running as administrator, so it can't change adapter settings")
        with self._net_lock:
            a = self.adapter(adapter_name)
            try:
                self.backend.set_dhcp(a.name)
            except NetError as e:
                self.log.add(f"Revert to DHCP failed on {a.name}: {e}", "error")
                raise JoinError(str(e))
            self.store.set_setting("applied", None)
            self.internet_cache.pop(a.name, None)
            self.log.add(f"{a.name} set back to DHCP")
            return {"ok": True}

    def still_applied(self) -> dict | None:
        """The static IP NetJoin last applied, if that adapter still has it (used when quitting)."""
        applied = self.store.get_setting("applied")
        if not applied:
            return None
        a = self.backend.get_adapter(applied.get("adapter", ""))
        if a and any(x.ip == applied.get("ip") for x in a.addresses):
            return applied
        return None

    def rejoin(self) -> dict:
        last = self.store.get_setting("last_network")
        if not last or not self.store.node(last.get("network_id", -1)):
            raise JoinError("No previous network to rejoin")
        return self.join(last["network_id"], adapter_name=self.selected_adapter_name())

    def internet_check(self) -> dict:
        a = self.adapter()
        p = a.primary
        if not p:
            raise JoinError(f"{a.name} has no IPv4 address")
        ok = self.backend.check_internet(p.ip)
        self.internet_cache[a.name] = ok
        applied = self.store.get_setting("applied")
        if applied and applied.get("adapter") == a.name and applied.get("ip") == p.ip:
            self.store.set_last_internet(applied["network_id"], p.ip, ok)
        self.log.add(f"Internet check on {a.name} ({p.ip}): {'yes' if ok else 'no'}")
        return {"internet": ok}
