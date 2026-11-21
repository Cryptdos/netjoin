# NetJoin

Quickly put a Windows laptop's network adapter onto site BMS subnets.
Pick a network from your site tree, click **Join**, and NetJoin picks a free IP from that network's allowed range, checks for clashes, and applies it. **Revert to DHCP** is always one click away.

Runs as a small local web app on `http://127.0.0.1:8765` (local only).

Design notes: [docs/network-join-tool.md](docs/network-join-tool.md)

## Status

**Phase 1 (this version):** site / area / network tree, Join with clash check and retry, Revert to DHCP, Rejoin last network, IP history (worked / clashed), internet check per adapter, gateway / DNS overrides (none by default), SQLite storage with automatic backups, import / export.

**Phase 2 (v0.2.0):** Scan menu – ARP sweep, ping sweep, BACnet Who-Is (reads device name, model and vendor; finds devices behind MS/TP routers), BMS port check with clickable web UI links, passive listen (Npcap), and a device list per network with BACnet / Other filters.

**Phase 3 (v0.3.0):** **Discover networks on the wire** – plug into an unknown BMS network, listen and broadcast a BACnet Who-Is, and NetJoin works out the subnet(s), the likely gateway and a free laptop range, then saves the network and joins it. Build script now produces a ready-to-share zip.

**v0.6.0 (branch `v0.6`, being tested):** network editor with IP Network + mask, allowed range or static IP, allocation order (first / last / random / last used); Discover fixes for quiet BACnet-only networks and DHCP masks; adapter followed by MAC (USB dongles); device roll-up on sites and areas with search, sorting, filters, inactive devices and CSV export; per-site export / import; clickable breadcrumbs, hideable Sites list, IP history drop-down. Details: [docs/v0.6-plan.md](docs/v0.6-plan.md).

**v0.10.0 (branch `v0.10`):** devices listed under each network in the Sites list; a device page with Overview + notes, **live ping**, **BACnet device properties** (firmware, app version, clock check) and ports. Read-only. Details: [docs/v0.10-device-pages.md](docs/v0.10-device-pages.md).

**v0.9.1 (branch `v0.9.1`, being tested):** device list – network column shows `Level 2 › FCUs` as a link, multi-vendor filter, shown / total / missing counts, **Import CSV** of devices that should be there (missing ones show after a scan). Details: [docs/v0.9.1-device-list.md](docs/v0.9.1-device-list.md). Next ideas: [docs/device-tools-plan.md](docs/device-tools-plan.md).

**v0.9.0 (branch `v0.9`, being tested):** drag to reorder the Sites list and drop networks / areas onto an area to nest them – always within the same site; Edit → Inside does the same without a mouse. Details: [docs/v0.9-arrange-sites.md](docs/v0.9-arrange-sites.md).

**v0.8.0 (branch `v0.8`, first build for feedback):** one top bar – breadcrumbs, page title and page buttons merged with the app bar; adapter, address, network and internet in a connection chip with a drop-down panel. Details: [docs/v0.8-top-bar.md](docs/v0.8-top-bar.md).

**v0.7.0 (branch `v0.7`, being tested):** opens on a **Home** page – recent networks with one-click Join, needs attention (networks with inactive devices), This PC, site cards and stats. Breadcrumbs now go forward and back (`⌂ Home › Site › Area ›`, each `›` opens a menu) so the Sites list can stay closed; site and area pages show a Contents strip. Dark theme (Settings → Appearance: System / Light / Dark). Details: [docs/v0.7-home-and-breadcrumbs.md](docs/v0.7-home-and-breadcrumbs.md).

**Phase 4 (v0.4.0):** works in narrow windows – split-screen, small browser windows, down to phone width. Device list switches to cards when its panel is narrow. Discover offers **Join & scan** for networks found from another subnet.

## Quick start (from source)

1. Install **Python 3.12+** from python.org (tick *Add python.exe to PATH*).
2. Clone and run:
   ```bat
   git clone https://github.com/Cryptdos/netjoin
   cd netjoin
   run.bat --fake
   ```
   `--fake` is **demo mode**: a pretend set of adapters, nothing on your PC changes. Addresses ending .200 and .230 act as "already taken" so you can see the clash handling. Set `NETJOIN_FAKE_GONE=11` before starting to make the demo controller at .11 "unplugged" and see inactive devices.
3. For real use: `run.bat` (Windows asks for admin, which is needed to change IP settings).

Other options: `--port 8766`, `--no-browser`, `--no-elevate`, `--console` (run in a console window instead of the taskbar icon).

## Taskbar icon

NetJoin runs in the background with an icon in the notification area (bottom right; it may be under the ^ arrow):

- **Left-click** opens NetJoin in your browser.
- **Right-click** → Open NetJoin · Open data folder · Quit.
- **Quit** asks whether to switch the adapter back to DHCP if NetJoin left it on a site IP (Yes / No / Cancel).
- Starting NetJoin again while it's running just opens the browser.
- If it can't start, a pop-up shows the error; details are in `netjoin-error.log` in the data folder.

Running it a second time while it's already running just opens the browser again.

## Build the portable exe

```bat
build.bat
```
Produces `dist\NetJoin\NetJoin.exe` with the NetJoin icon (plus its support files in `_internal`) and `dist\NetJoin-<version>.zip`. Unzip anywhere and run `NetJoin.exe`; there's no installer and Python isn't needed. The exe asks for admin when it starts.

## Npcap (optional)

Install [Npcap](https://npcap.com) to unlock passive listening and a faster raw ARP sweep. Without it those options are greyed out and everything else works. Settings shows whether it's detected.

## Where data lives

- `%APPDATA%\NetJoin\netjoin.db` (SQLite), plus `backups\` (last 10, one per start) and `netjoin.log`.
- **Portable mode:** create a folder called `data` next to `NetJoin.exe` and everything is stored there instead (handy on a USB stick).

## Testing safely

- Join changes the selected adapter's IP. **Don't Join on the adapter that gives you internet** unless you mean to: use a USB-Ethernet adapter, or press *Revert to DHCP* afterwards.
- Gateway and DNS default to **none**, so joining a BMS network on Ethernet won't steal your Wi-Fi internet.

## How it works (short)

- Adapter changes: `netsh interface ipv4 set address / set dnsservers`.
- Clash check: after applying a candidate IP, NetJoin reads Windows' Duplicate Address Detection state. If another device already has the address, it's logged as **Clashed** and the next one is tried (up to 10). If they all clash, the previous settings are restored.
- Candidate order: the last IP that worked on that network, then the allowed range upwards (default .200–.254 on a /24).
- Internet check: an HTTP request to Microsoft's connectivity-test URL, sent *from the adapter's own IP*.
- The local API only answers to `127.0.0.1` / `localhost` and requires a custom header for changes, so other websites can't drive it.

## Tests

```bat
python -m unittest discover tests
```

## Icon

`tools/make_icon.py` draws the icon and writes `netjoin/static/netjoin.ico`, `favicon.png` and `icon-256.png` (needs Pillow).
