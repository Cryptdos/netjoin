"""The things NetJoin does: Join, revert to DHCP, rejoin, internet checks."""

from __future__ import annotations

import ipaddress

import random
import threading
from typing import Any

from . import inet, ipcalc
from .eventlog import EventLog
from .netcfg import Adapter, Backend, NetError
from .store import ALLOC_ORDERS, Store

MAX_ATTEMPTS = 10


class JoinError(RuntimeError):
    pass


class Engine:
    def __init__(self, store: Store, backend: Backend, log: EventLog):
        self.store, self.backend, self.log = store, backend, log
        self._net_lock = threading.Lock()   # one adapter change at a time
        self.internet_cache: dict[str, Any] = {}

    # ---------- adapters ----------
    @staticmethod
    def _norm_mac(mac: str | None) -> str:
        return (mac or "").replace(":", "-").upper()

    def _pick(self) -> tuple[Adapter | None, dict | None]:
        """The selected adapter, followed by MAC so a USB dongle is found whatever Windows calls it.

        → (adapter, missing) – missing = {'name', 'mac'} when the chosen adapter is unplugged.
        """
        adapters = self.backend.list_adapters()
        name, mac = self.store.get_setting("adapter"), self._norm_mac(self.store.get_setting("adapter_mac"))
        if mac:
            for a in adapters:
                if self._norm_mac(a.mac) == mac:
                    if a.name != name:      # same dongle, new name (different USB port)
                        self.store.set_setting("adapter", a.name)
                        self._renamed = f"USB adapter reconnected as {a.name}" + (f" (was {name})" if name else "")
                        self.log.add(self._renamed)
                    return a, None
            return None, {"name": name, "mac": mac}
        if name:
            for a in adapters:
                if a.name == name:
                    if a.mac:               # upgrade from 0.5: remember its MAC from now on
                        self.store.set_setting("adapter_mac", a.mac)
                    return a, None
        physical = [a for a in adapters if not a.virtual]
        wired = [a for a in physical if "wi-fi" not in a.name.lower() and "wireless" not in a.description.lower()]
        pick = (wired or physical or [None])[0]
        return pick, None

    def select_adapter(self, name: str) -> Adapter:
        a = self.backend.get_adapter(name)
        if not a:
            raise JoinError("Unknown adapter")
        self.store.set_setting("adapter", a.name)
        self.store.set_setting("adapter_mac", a.mac or None)
        return a

    def selected_adapter_name(self) -> str | None:
        a, _ = self._pick()
        return a.name if a else None

    def adapter(self, name: str | None = None) -> Adapter:
        if name:
            a = self.backend.get_adapter(name)
            if a:
                return a
        a, missing = self._pick()
        if missing:
            raise JoinError(f"{missing['name'] or 'The chosen adapter'} is unplugged – plug the adapter back in, "
                            "or pick another one at the top")
        if not a:
            raise JoinError("Pick a network adapter first")
        return a

    def status(self) -> dict:
        a, missing = self._pick()
        name = a.name if a else None
        applied = self.store.get_setting("applied")
        last = self.store.get_setting("last_network")
        current = None
        if a and applied and applied.get("adapter") == a.name and a.primary and a.primary.ip == applied.get("ip"):
            current = applied
        renamed, self._renamed = getattr(self, "_renamed", None), None
        return {
            "backend": self.backend.kind,
            "admin": self.backend.is_admin(),
            "adapter": a.to_dict() if a else None,
            "adapter_missing": missing,
            "adapter_renamed": renamed,
            "primary": a.primary.__dict__ if a and a.primary else None,
            "joined": current,
            "last_network": last,
            "internet": self.internet_cache.get(name) if name else None,
        }

    # ---------- join ----------
    def candidates(self, network: dict, explicit_ip: str | None = None) -> list[str]:
        """Addresses Join will try, in order (address mode + allocation order, plan items 5 and 6)."""
        net = ipcalc.parse_subnet(network["subnet"])
        if explicit_ip:
            if not ipcalc.in_subnet(explicit_ip, net):
                raise JoinError(f"{explicit_ip} isn't a usable address in {net}")
            return [explicit_ip]
        if (network.get("addr_mode") or "range") == "fixed" and network.get("fixed_ip"):
            return [network["fixed_ip"]]
        allowed = list(ipcalc.iter_range(network["range_start"], network["range_end"], net))
        alloc = network.get("alloc") or "first"
        if alloc == "last":
            return allowed[::-1]
        if alloc == "random":
            random.shuffle(allowed)
            return allowed
        if alloc == "sticky":
            last = self.store.last_worked_ip(network["id"])
            if last and last in allowed:      # last good address first, if it's still in the range
                return [last] + [ip for ip in allowed if ip != last]
        return allowed

    def next_ip(self, network: dict) -> dict:
        """What the 'Join IP' fact shows: the address Join tries first and how it was picked: {'ip', 'how'}."""
        mode = network.get("addr_mode") or "range"
        if mode == "fixed":
            return {"ip": network.get("fixed_ip"), "how": "static IP"}
        alloc = network.get("alloc") or "first"
        if alloc == "random":
            return {"ip": None, "how": "random from the range"}
        try:
            c = self.candidates(network)
        except Exception:
            return {"ip": None, "how": ""}
        how = ALLOC_ORDERS.get(alloc, alloc)
        if alloc == "sticky":
            how = "last used" if c and c[0] == self.store.last_worked_ip(network["id"]) else "first free"
        return {"ip": c[0] if c else None, "how": how}

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
                chk = self.backend.check_internet(cand, gwdns["dns"])
                internet = chk["code"]
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
                             f"{' (' + '; '.join(extra) + ')' if extra else ''} ✓ · internet: {inet.describe(internet)}")
                if internet == inet.IP_ONLY:
                    self.log.add(f"{chk['detail']}. To browse on this network, tick Override DNS on it "
                                 f"(e.g. use the gateway {gwdns['gateway']})" if gwdns["gateway"] else chk["detail"], "warn")
                return {"ok": True, "ip": cand, "prefix": net.prefixlen, "internet": internet, "attempts": tried}
            # Nothing worked: put the adapter back how it was.
            fixed = not ip and (network.get("addr_mode") or "range") == "fixed"
            if fixed:
                fip = network["fixed_ip"]
                what, msg = (f"Static IP {fip} is in use by another device",
                             f"Static IP {fip} is in use by another device. NetJoin doesn't pick "
                             "another address for a static IP – check the address, or use the allowed range.")
            elif ip:
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

    # ---------- edited settings vs. what's on the adapter now ----------
    def config_drift(self) -> dict | None:
        """If the laptop is on a network NetJoin joined, compare that network's saved settings with what
        the adapter actually has. None when not joined (or the adapter has moved on)."""
        applied = self.store.get_setting("applied")
        if not applied:
            return None
        n = self.store.node(applied.get("network_id", -1))
        a = self.backend.get_adapter(applied.get("adapter", ""))
        if not n or not a or a.dhcp or not a.primary or a.primary.ip != applied.get("ip"):
            return None
        ip = applied["ip"]
        rejoin = None
        try:
            net = ipcalc.parse_subnet(n["subnet"])
            addr = ipaddress.ip_address(ip)
            if addr not in net or a.primary.prefix != net.prefixlen:
                rejoin = f"the network is now {net}, and {ip}/{a.primary.prefix} isn't on it"
            elif (n.get("addr_mode") or "range") == "fixed" and n.get("fixed_ip") and n["fixed_ip"] != ip:
                rejoin = f"the static IP is now {n['fixed_ip']}"
            elif (n.get("addr_mode") or "range") != "fixed" and n.get("range_start") and n.get("range_end") and not (
                    ipaddress.ip_address(n["range_start"]) <= addr <= ipaddress.ip_address(n["range_end"])):
                rejoin = f"{ip} is outside the new range {n['range_start']} – {n['range_end']}"
        except ValueError:
            pass
        want = self.store.resolve_gateway_dns(n["id"])
        cur_gw = a.gateways[0] if a.gateways else None
        return {"network_id": n["id"], "network": n["name"], "adapter": a.name, "ip": ip, "prefix": a.primary.prefix,
                "rejoin": rejoin,
                "gateway": {"from": cur_gw, "to": want["gateway"]} if (want["gateway"] or None) != cur_gw else None,
                "dns": {"from": list(a.dns), "to": list(want["dns"])} if list(want["dns"]) != list(a.dns) else None}

    def apply_config_changes(self, confirm: bool = False) -> dict:
        """After an edit: compare the saved settings with the adapter of the network the laptop is on.

        - Purely additive changes (DNS or a gateway where there was none) go on straight away: the
          address isn't touched and nothing that's working can stop.
        - Anything that changes or removes a gateway / DNS that's in use asks first (ask='confirm'),
          and nothing from that save is applied until confirmed – "Later" means later for all of it.
        - Anything that needs a new address asks to rejoin (ask='rejoin'); nothing is applied."""
        if not self.backend.is_admin():
            return {"changed": False}
        with self._net_lock:
            d = self.config_drift()
            if not d or not (d["rejoin"] or d["gateway"] or d["dns"]):
                return {"changed": False}
            out = {"changed": True, "network_id": d["network_id"], "network": d["network"], "adapter": d["adapter"],
                   "ip": d["ip"], "applied": [], "ask": None, "reason": None, "changes": [],
                   "gateway": d["gateway"], "dns": d["dns"]}
            if d["gateway"]:
                g = d["gateway"]
                out["changes"].append(f"gateway {g['from'] or 'none'} → {g['to'] or 'none (removed)'}")
            if d["dns"]:
                n = d["dns"]
                out["changes"].append(f"DNS {', '.join(n['from']) or 'none'} → {', '.join(n['to']) or 'none (removed)'}")
            if d["rejoin"]:
                out.update(ask="rejoin", reason=d["rejoin"])
                return out
            risky = bool((d["gateway"] and d["gateway"]["from"]) or (d["dns"] and d["dns"]["from"]))
            if risky and not confirm:
                out.update(ask="confirm", reason="; ".join(out["changes"]))
                return out
            try:
                if d["dns"]:
                    self.backend.set_dns(d["adapter"], d["dns"]["to"])
                    out["applied"].append("DNS " + (", ".join(d["dns"]["to"]) or "removed"))
                if d["gateway"]:
                    self.backend.set_gateway(d["adapter"], d["ip"], d["prefix"], d["gateway"]["to"])
                    out["applied"].append(f"gateway {d['gateway']['to']}" if d["gateway"]["to"] else "gateway removed")
            except NetError as e:
                self.log.add(f"Couldn't update {d['adapter']}: {e}", "error")
                raise JoinError(str(e))
            self.log.add(f"Updated {d['adapter']} for {d['network']}: {'; '.join(out['applied'])} – address {d['ip']} unchanged")
            chk = self.backend.check_internet(d["ip"], self.store.resolve_gateway_dns(d["network_id"])["dns"])
            self.internet_cache[d["adapter"]] = out["internet"] = chk["code"]
            return out

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
        chk = self.backend.check_internet(p.ip, list(a.dns))
        code = chk["code"]
        self.internet_cache[a.name] = code
        applied = self.store.get_setting("applied")
        if applied and applied.get("adapter") == a.name and applied.get("ip") == p.ip:
            self.store.set_last_internet(applied["network_id"], p.ip, code)
        self.log.add(f"Internet check on {a.name} ({p.ip}): {inet.describe(code)}"
                     + (f" – {chk['detail']}" if code == inet.IP_ONLY else ""), "warn" if code == inet.IP_ONLY else "info")
        return {"internet": code, "detail": chk["detail"]}
