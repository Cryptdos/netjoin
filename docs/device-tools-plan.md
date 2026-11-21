# NetJoin – devices in the tree and per-device engineering tools (plan)

Status: **agreed direction, revised with LB's decisions (section 6)**. Nothing here is built yet.

**Ethos: NetJoin reads, observes and connects. It doesn't change devices, and it isn't a commissioning-status or note-taking tool.** Writing to devices stays with the official vendor tools.

## 1. Devices in the Sites list

- Every device shows directly under its network in the Sites list. Networks are collapsible, so a long list is fine (LB). BACnet devices come first, by name (`#2502 RA L2-01`), then the others by IP.
- Devices only load when a network is opened in the tree (no extra scanning, no battery cost).
- The Sites search box also finds devices (by IP, name, instance, MAC) and opens their branch.
- Clicking a device opens a **device page**. The breadcrumb grows one level: `Home › 50 Berkeley Street › Level 2 › FCUs › RA L2-01`.
- Missing devices are greyed in the tree too, so a quick glance down a network shows what's not answering.

## 2. The device page

Top bar: the device's name as the page title, with buttons for the actions below. Facts strip: IP, MAC, BACnet instance, vendor, model, last seen.

Tabs underneath:

| Tab | What's in it |
|---|---|
| Overview | Identity and status, which scans found it, IP history (if its address changed), notes |
| Ping | Live ping |
| BACnet | Device properties and the object list (BACnet devices only) |
| Ports | Open ports with links (web UI, Fox, Modbus, SSH / Telnet) |

- **Notes:** one free-text notes field, the same as networks, sites and areas have (LB). No status, checklist or progress tracking.

## 3. Features, by phase

All read-only.

### Phase A – v0.10: device page and connection checks

| Feature | Why it matters on site | Effort |
|---|---|---|
| **Devices in the tree + device page** | One place for everything about a controller | M |
| **Live ping** – continuous, latency, % lost, flags drop-outs and recovery | Re-terminating a cable, power-cycling a panel, checking a flaky link | S |
| **BACnet device properties** – firmware revision, application software version, protocol revision, location, description, system status, max APDU, segmentation, device time / UTC offset | Firmware checks, confirming the right app is loaded, spotting wrong clocks | S |
| **Notes per device** – same as networks | Context for the next visit | S |

### Phase B – v0.11: see what's inside a controller

| Feature | Why | Effort |
|---|---|---|
| **Object list browser** – every object in the device (AI / AO / AV / BI / BO / BV / MSV / schedules / trend logs / anything else) with present value, units, status flags (in alarm, fault, overridden, out of service) and priority array. Filter by object type, name / description text, and flag (e.g. *only overridden*, *only out of service*, *only in alarm*); refresh button, auto-refresh only while the tab is open | Point checks without a full BMS front end; finding overridden or out-of-service points | M |
| **Export the object list** to CSV (what's shown, filters applied) | Taking a points snapshot away | S |
| **Site health checks** – duplicate BACnet instances, duplicate IPs (MAC changed), factory-default instances (4194303 etc.) or factory IPs, devices on the wrong subnet, firmware outliers (same model, different firmware) | Catches the classic commissioning faults before they bite | S–M |
| **Who-Has search** – find which controller holds an object name (e.g. `L2-FCU-03 SpaceTemp`) | "Which box is this point on?" | S |

### Phase C – later: observe and reach further

| Feature | Why | Effort |
|---|---|---|
| **Watch points live** – subscribe (COV) to a few points and see them update, with a mini trend while the page is open | Seeing a valve respond to a setpoint change made in the vendor tool | M–L |
| **Reach other subnets without changing IP** – register as a foreign device with the site's BBMD | Seeing a whole site's BACnet from one address | M |
| **Modbus TCP read** – read holding / input registers from meters and drives | Common on BMS sites alongside BACnet | M |
| **MS/TP devices behind routers get device pages too** – already discovered | Field controllers on MS/TP trunks | S (after A) |
| **Snapshot and compare** – save a device's properties / object values, compare later | Proving what changed after work done in the vendor tool | M |

## 4. Not doing (LB decisions)

- **Commissioning status, progress roll-ups and commissioning reports.** NetJoin isn't a status or notation tool.
- **Writing to devices:** point overrides / relinquish, time sync, restart (ReinitializeDevice), DeviceCommunicationControl. That stays in the official tools (ABT, Desigo, etc.).

## 5. Suggested order

1. **v0.10** – devices in the tree, device page, live ping, BACnet device properties, notes.
2. **v0.11** – object list browser with filters and CSV, site health checks, Who-Has.
3. **Later** – live watch / mini trend, BBMD foreign device, Modbus read, MS/TP device pages, snapshot / compare.

## 6. Supervisor Decisions
- Devices in the tree also
- Listing all BACnet objects and providing filtering. Let me know if this doesn't answer what you were asking.
- No commissioning status, I don't want this tool to get lost in the weeds of providing some status or notation function, that's not it's intention.
- Writing from NetJoin not really ideal no. It could be useful but I feel like writing should be maintained via official tools. Reading, observing, connection etc. that's the main functional ethos of this tool - not active commisioning tool.
