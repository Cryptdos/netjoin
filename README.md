# NetJoin

Quickly put a Windows laptop's network adapter onto site BMS subnets.
Pick a network from your site tree, click **Join**, and NetJoin picks a free IP from that network's allowed range, checks for clashes, and applies it. **Revert to DHCP** is always one click away.

Runs as a small local web app on `http://127.0.0.1:8765` (local only).

Design notes: [docs/network-join-tool.md](docs/network-join-tool.md)

## Status

**Phase 1 (this version):** site / area / network tree, Join with clash check and retry, Revert to DHCP, Rejoin last network, IP history (worked / clashed), internet check per adapter, gateway / DNS overrides (none by default), SQLite storage with automatic backups, import / export.

Next: Phase 2 scans (ARP, ping, BACnet Who-Is, port check, Npcap features). Phase 3 Discover wizard and polish.

## Quick start (from source)

1. Install **Python 3.12+** from python.org (tick *Add python.exe to PATH*).
2. Clone and run:
   ```bat
   git clone https://github.com/Cryptdos/netjoin
   cd netjoin
   run.bat --fake
   ```
   `--fake` is **demo mode**: a pretend set of adapters, nothing on your PC changes. Addresses ending .200 and .230 act as "already taken" so you can see the clash handling.
3. For real use: `run.bat` (Windows asks for admin, which is needed to change IP settings).

Other options: `--port 8766`, `--no-browser`, `--no-elevate`.

Running it a second time while it's already running just opens the browser again.

## Build the portable exe

```bat
build.bat
```
Produces `dist\NetJoin\NetJoin.exe` (plus its support files). Zip the `dist\NetJoin` folder to share it; there's no installer. The exe asks for admin when it starts.

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
