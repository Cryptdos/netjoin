# NetJoin – devices in the tree and per-device engineering tools (plan)

Status: **proposal for discussion** – nothing here is built yet. Edit freely: strike what you don't want, move things between phases, add what's missing.

## 1. Devices in the Sites list

A busy network can have 50–200 devices, so listing them all under every network would bury the sites and areas. Proposal:

- Under each network, a collapsed **Devices (24)** row. Open it to see the devices, BACnet ones first, by name (`#2502 RA L2-01`), then the others by IP.
- Devices only load when the network is opened in the tree (no extra scanning, no battery cost).
- The Sites search box also finds devices (by IP, name, instance, MAC) and opens their branch.
- Clicking a device opens a **device page**. The breadcrumb grows one level: `Home › 50 Berkeley Street › Level 2 › FCUs › RA L2-01`.
- Missing devices are greyed in the tree too, so a quick glance down a network shows what's not answering.

> Alternative if the tree gets too long: devices only on the device page / list, and the tree shows just the count. Worth trying the first way before deciding.

## 2. The device page

Top bar: the device's name as the page title, with buttons for the actions below. Facts strip: IP, MAC, BACnet instance, vendor, model, last seen.

Tabs underneath:

| Tab | What's in it |
|---|---|
| Overview | Identity and status, which scans found it, IP history (if its address changed), notes |
| Ping | Live ping |
| BACnet | Device properties and the object list (BACnet devices only) |
| Ports | Open ports with links (web UI, Fox, Modbus, SSH / Telnet) |
| Notes | Your commissioning notes and a status: *not started / in progress / commissioned / issue* |

## 3. Features, by phase

### Phase A – read-only (safe to use on a live site)

| Feature | Why it matters on site | Effort |
|---|---|---|
| **Device page + tree entries** | One place for everything about a controller | M |
| **Live ping** – continuous, latency, % lost, beeps / flashes on drop-out and recovery | Re-terminating a cable, power-cycling a panel, checking a flaky link | S |
| **BACnet device properties** – firmware revision, application software version, protocol revision, location, description, system status, max APDU, segmentation, device time / UTC offset | Firmware checks, confirming the right app is loaded, spotting wrong clocks | S |
| **Object list browser** – every object (AI / AO / AV / BI / BO / BV / MSV / schedules / trend logs) with present value, units, status flags (in alarm, fault, overridden, out of service) and priority array; refresh button or auto-refresh while open | Point-to-point checks without a full BMS front end; finding overridden or out-of-service points | M |
| **Site health checks** – duplicate BACnet instances, duplicate IPs (MAC changed), factory-default instances (4194303 etc.) or factory IPs, devices on the wrong subnet, firmware outliers (same model, different firmware) | Catches the classic commissioning faults before they bite | S–M |
| **Who-Has search** – find which controller holds an object name (e.g. `L2-FCU-03 SpaceTemp`) | "Which box is this point on?" | S |
| **Commissioning status + notes per device**, rolled up per network / area / site (e.g. 18 of 24 commissioned) | Tracks progress across visits; works with the imported list | S |
| **Commissioning report export** – per network / site: expected vs found, firmware, status, notes (CSV and printable page) | Handover evidence | M |

### Phase B – writes (affect live plant – opt-in and guarded)

| Feature | Why | Effort |
|---|---|---|
| **Override a point** – write a present value at a chosen priority (default 8, Manual Operator), and **relinquish** (write NULL) | Driving outputs / setpoints for point checks | M |
| **"Overrides I've left in"** list – everything NetJoin wrote and hasn't relinquished, per site, with **Relinquish all** | Stops overrides being left in when you leave site | S |
| **Time sync** – send the laptop's time to a device or a whole network | Wrong controller clocks break schedules and trends | S |
| **Restart device** (ReinitializeDevice warm / cold, with password) | After a download or a lock-up | S |
| **Enable / disable comms** (DeviceCommunicationControl) | Isolating a device during work | S |

Safety rules for phase B:
- Writes are **off by default**, enabled per site (Settings or the site's Edit).
- Every write shows what will change and asks to confirm; the safe choice is focused.
- Every write is logged (who / what / old value / new value / priority / when) and kept in the site's history.
- Restarts and comms control need the device password where it has one, and say plainly what will stop.

### Phase C – later / bigger

| Feature | Why | Effort |
|---|---|---|
| **COV subscriptions + mini trend** – watch a few points update live while you're there | Seeing a valve respond to a setpoint change | M–L |
| **Reach other subnets without changing IP** – register as a foreign device with the site's BBMD | Seeing a whole site's BACnet from one address | M |
| **Modbus TCP read** – read holding / input registers from meters and drives | Common on BMS sites alongside BACnet | M |
| **MS/TP devices behind routers on their own page** – already discovered; give them device pages too | Field controllers on MS/TP trunks | S (after A) |
| **Backup / compare device properties** – snapshot before work, compare after | Proving what changed | M |

## 4. Suggested order

1. **v0.10** – Device page, devices in the tree, live ping, BACnet device properties, commissioning status + notes.
2. **v0.11** – Object list browser, site health checks, Who-Has.
3. **v0.12** – Commissioning report export.
4. **v0.13+** – Phase B writes, with the safety rules above.

## 5. Questions for you

- Tree: devices under every network (collapsed), or only on the device list?
- Which BACnet objects matter most on your sites (e.g. Siemens DXR2 / PXC: AVs and BVs for setpoints, MSVs for modes)?
- Commissioning status: are the four states above right, or does BES use its own (e.g. *pre-commissioned / point-checked / witnessed*)?
- Phase B: is writing from NetJoin something you'd use, or is that always done from the vendor tool (ABT, Desigo, etc.)?
