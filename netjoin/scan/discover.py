"""Discover: find the subnets on the wire without knowing them first.

1. Listen (Npcap): ARP and broadcast traffic reveal device IPs, the broadcast
   addresses they use (→ subnet mask) and the IPs they ARP for (→ likely gateway).
2. Broadcast a BACnet Who-Is to 255.255.255.255 (reaches every device on the
   cable whatever subnet it is on) and collect the I-Am replies.
3. Group everything into candidate subnets and suggest a free laptop range.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict

from .. import ipcalc
from . import bacnet, oui

MIN_FREE_RUN = 20


def _usable(ip: str, own_ip: str) -> bool:
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (ip == own_ip or a.is_multicast or a.is_loopback or a.is_unspecified or a.is_link_local
                or ip == "255.255.255.255")


def infer_prefix(broadcast: str, sources: list[str]) -> int | None:
    """Prefix for which `broadcast` is the broadcast address and every source is inside.

    Several prefixes usually fit (x.y.1.255 is the broadcast of a /24, /25, /26…). /24 is
    by far the most common on BMS networks, so it wins when it fits; otherwise the
    shortest prefix that fits (the largest network containing all the senders).
    """
    try:
        b = ipaddress.IPv4Address(broadcast)
        srcs = [ipaddress.IPv4Address(s) for s in sources]
    except ValueError:
        return None
    fits = []
    for p in range(8, 31):
        net = ipaddress.IPv4Network(f"{b}/{p}", strict=False)
        if net.broadcast_address == b and all(s in net and s != b for s in srcs):
            fits.append(p)
    if not fits:
        return None
    return 24 if 24 in fits else min(fits)


def suggest_range(net: ipaddress.IPv4Network, used: set[str]) -> tuple[str, str, int]:
    """Default .200–.254 if it's clear; else the highest run of ≥20 free addresses.

    → (start, end, number of seen devices inside the returned range)
    """
    start, end = ipcalc.default_range(net)
    inside = [u for u in used if ipcalc.in_subnet(u, net) and
              int(ipaddress.IPv4Address(start)) <= int(ipaddress.IPv4Address(u)) <= int(ipaddress.IPv4Address(end))]
    if not inside:
        return start, end, 0
    used_ints = {int(ipaddress.IPv4Address(u)) for u in used}
    hi = int(net.broadcast_address) - 1
    lo = int(net.network_address) + 1
    run_end = None
    best = None
    for n in range(hi, lo - 1, -1):
        if n in used_ints:
            if run_end is not None and run_end - n >= MIN_FREE_RUN:
                best = (max(n + 1, run_end - 54), run_end)
                break
            run_end = None
        elif run_end is None:
            run_end = n
    else:
        if run_end is not None and run_end - lo + 1 >= MIN_FREE_RUN:
            best = (max(lo, run_end - 54), run_end)
    if best:
        return str(ipaddress.IPv4Address(best[0])), str(ipaddress.IPv4Address(best[1])), 0
    return start, end, len(inside)


def analyse(obs: dict, own_ip: str, known_subnets: dict[str, list[dict]] | None = None) -> list[dict]:
    """Turn raw observations into candidate subnets.

    obs = {'hosts': {ip: mac}, 'bacnet': [dev], 'bcast_sources': {bcast: [src]}, 'arp_asks': {target: [askers]}}
    known_subnets = {'10.20.5.0/24': [{'id':…, 'name':…, 'path':…}]} for networks already saved.
    """
    known_subnets = known_subnets or {}
    hosts = {ip: mac for ip, mac in obs.get("hosts", {}).items() if _usable(ip, own_ip)}
    bac = [d for d in obs.get("bacnet", []) if _usable(d["ip"], own_ip)]
    for d in bac:
        hosts.setdefault(d["ip"], None)

    # 1) subnets with evidence from directed broadcasts
    nets: dict[str, dict] = {}
    for bcast, srcs in obs.get("bcast_sources", {}).items():
        if bcast == "255.255.255.255":
            continue
        srcs = [s for s in srcs if _usable(s, own_ip)]
        if not srcs:
            continue
        p = infer_prefix(bcast, srcs)
        if p is None:
            continue
        net = ipaddress.IPv4Network(f"{bcast}/{p}", strict=False)
        e = nets.setdefault(str(net), {"net": net, "evidence": set()})
        e["evidence"].add(f"broadcasts to {bcast}")

    # 2) every host goes into the most specific evidenced subnet, else a /24 guess
    members: dict[str, list[str]] = defaultdict(list)
    for ip in hosts:
        a = ipaddress.IPv4Address(ip)
        fit = [e["net"] for e in nets.values() if a in e["net"]]
        if fit:
            net = max(fit, key=lambda n: n.prefixlen)
        else:
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            nets.setdefault(str(net), {"net": net, "evidence": set()})
        members[str(net)].append(ip)

    out = []
    asks = obs.get("arp_asks", {})
    for key, e in nets.items():
        ips = sorted(set(members.get(key, [])), key=lambda x: tuple(int(p) for p in x.split(".")))
        if not ips:
            continue
        net = e["net"]
        devs = [d for d in bac if ipaddress.IPv4Address(d["ip"]) in net]
        # likely gateway: an address in this subnet that several devices ARP for
        gw = None
        cands = [(len(set(a)), t) for t, a in asks.items() if _usable(t, own_ip) and ipaddress.IPv4Address(t) in net]
        cands.sort(reverse=True)
        if cands and cands[0][0] >= 2:
            gw = cands[0][1]
        used = set(ips) | ({gw} if gw else set())
        start, end, clash = suggest_range(net, used)
        host_rows = []
        for ip in ips:
            mac = hosts.get(ip)
            dv = [d for d in devs if d["ip"] == ip]
            host_rows.append({"ip": ip, "mac": mac, "mac_vendor": oui.lookup(mac) if mac else None,
                              "bacnet": [{"instance": d["instance"], "vendor_id": d["vendor_id"],
                                          "vendor": d.get("vendor"), "route": d.get("route")} for d in dv]})
        out.append({
            "subnet": key, "prefix": net.prefixlen,
            "confidence": "high" if e["evidence"] else "guess",
            "evidence": sorted(e["evidence"]) or ["no broadcasts heard – assumed /24"],
            "hosts": host_rows, "host_count": len(ips), "bacnet_count": len(devs),
            "gateway_hint": gw, "range_start": start, "range_end": end, "range_clashes": clash,
            "saved_as": known_subnets.get(key, []),
            "contains_me": ipaddress.IPv4Address(own_ip) in net if _usable(own_ip, "") else False,
        })
    out.sort(key=lambda r: (-r["bacnet_count"], -r["host_count"], r["subnet"]))
    return out


def run(prober, adapter: str, src_ip: str, seconds: float, send_whois: bool, use_npcap: bool,
        cancel, log, progress, prefix: int | None = None, mac: str | None = None) -> dict:
    """Collect observations for `seconds`. Returns the raw observation dict."""
    from . import npcap
    if prober.kind == "fake":
        for i in range(int(seconds)):
            if cancel():
                break
            time.sleep(min(0.15, seconds / 40))
            progress(i + 1)
        return prober.discover_extra()
    obs: dict = {"hosts": {}, "bacnet": [], "bcast_sources": {}, "arp_asks": {}}
    sniff_result: dict = {}
    t = None
    if use_npcap:
        def listen():
            try:
                sniff_result.update(npcap.passive_listen(adapter, src_ip, seconds, cancel, broad=True, mac=mac))
            except Exception as e:      # report it rather than silently hearing nothing
                sniff_result["error"] = str(e)
        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(1.0)    # let the capture start before we send anything
    t0 = time.monotonic()
    if send_whois and not cancel():
        targets = ["255.255.255.255"]
        if prefix and not src_ip.startswith("169.254."):
            # Windows may send 255.255.255.255 out of a different adapter (Wi-Fi, Hyper-V, VPN).
            # The adapter's own subnet broadcast is always routed out of this adapter.
            targets.insert(0, str(ipaddress.IPv4Network(f"{src_ip}/{prefix}", strict=False).broadcast_address))
        obs["whois_to"] = targets
        obs["bacnet"].extend(_who_is_any(src_ip, min(seconds, 5.0), cancel, log, targets))
        progress(min(seconds, time.monotonic() - t0))
    while t is not None and t.is_alive():
        t.join(0.5)
        progress(min(seconds, time.monotonic() - t0 + 1))
    if sniff_result.get("error"):
        obs["npcap_error"] = sniff_result["error"]
    if sniff_result:
        obs["hosts"].update(sniff_result.get("hosts", {}))
        seen = {d["key"] for d in obs["bacnet"]}
        obs["bacnet"].extend(d for d in sniff_result.get("bacnet", []) if d["key"] not in seen)
        obs["bcast_sources"] = sniff_result.get("bcast_sources", {})
        obs["arp_asks"] = sniff_result.get("arp_asks", {})
    return obs


def _who_is_any(src_ip: str, listen: float, cancel, log, targets=("255.255.255.255",)) -> list[dict]:
    """Who-Is to the given broadcast addresses (we may not be on the devices' subnet); no name reads."""
    sock, std = bacnet.open_socket(src_ip)
    if not std:
        log(f"UDP {bacnet.BACNET_PORT} is in use by another program; replies may be missed", "warn")
    found: dict[str, dict] = {}
    try:
        for dst in targets:
            try:
                sock.sendto(bacnet.WHO_IS, (dst, bacnet.BACNET_PORT))
            except OSError as e:
                log(f"Who-Is to {dst} failed: {e}", "warn")
        end = time.monotonic() + listen
        while time.monotonic() < end and not cancel():
            sock.settimeout(max(0.05, min(0.3, end - time.monotonic())))
            try:
                data, (ip, port) = sock.recvfrom(1500)
            except OSError:
                continue
            if ip == src_ip:
                continue
            d = bacnet.decode_iam_datagram(data, ip, port)
            if d:
                found.setdefault(d["key"], d)
    finally:
        sock.close()
    return list(found.values())
