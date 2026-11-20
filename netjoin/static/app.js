"use strict";
// NetJoin front end: plain JS, no build step.

const $ = (s) => document.querySelector(s);
const state = { tree: [], selected: null, detail: null, status: null, expanded: new Set(), logAfter: 0, busy: false,
  scan: { running: false, options: [] }, devices: [], filter: "all", scanTimer: null };

try { JSON.parse(localStorage.getItem("nj.expanded") || "[]").forEach((id) => state.expanded.add(id)); } catch (e) {}
try { state.selected = JSON.parse(localStorage.getItem("nj.selected") || "null"); } catch (e) {}
// Internet codes from the server: 1 internet, 2 by IP only (no working DNS), 0 none.
const INET = { 1: { cls: " yes", text: "Internet", off: false },
  2: { cls: " warn", text: "Internet by IP only – no DNS", off: false },
  0: { cls: "", text: "No internet", off: true } };
const inetInfo = (c) => INET[c] || INET[0];
const saveUi = () => { try {
  localStorage.setItem("nj.expanded", JSON.stringify([...state.expanded]));
  localStorage.setItem("nj.selected", JSON.stringify(state.selected));
} catch (e) {} };

// ---------- IPv4 maths (for the network editor's live preview) ----------
const ip4 = {
  toInt(s) {
    const p = String(s || "").trim().split(".");
    if (p.length !== 4 || p.some((x) => !/^\d{1,3}$/.test(x) || +x > 255)) return null;
    return (((+p[0]) << 24) >>> 0) + ((+p[1]) << 16) + ((+p[2]) << 8) + (+p[3]);
  },
  str: (n) => [24, 16, 8, 0].map((b) => (n >>> b) & 255).join("."),
  mask: (p) => (p === 0 ? 0 : ((0xFFFFFFFF << (32 - p)) >>> 0)),
};
function netOf(ipText, prefix) {
  const n = ip4.toInt(ipText); if (n == null || !(prefix >= 8 && prefix <= 30)) return null;
  const m = ip4.mask(prefix); const net = (n & m) >>> 0; const bc = (net | (~m >>> 0)) >>> 0;
  return { net, bc, prefix, first: net + 1, last: bc - 1, count: bc - net - 1, mask: ip4.str(m) };
}
function netFromCidr(cidr) { const [a, p] = String(cidr || "").split("/"); return netOf(a, Number(p)); }
// '35' / '.35' → x.y.z.35, '3.35' → x.y.3.35; full addresses unchanged; null if not an address
function expandShort(text, nw) {
  const t = String(text || "").trim().replace(/^\./, "");
  if (!t) return "";
  if (ip4.toInt(t) != null) return t;
  const parts = t.split(".");
  if (!nw || parts.length > 3 || parts.some((x) => !/^\d{1,3}$/.test(x) || +x > 255)) return null;
  const base = ip4.str(nw.net).split(".");
  return [...base.slice(0, 4 - parts.length), ...parts.map(Number)].join(".");
}
function defaultRange(nw) {
  return nw.prefix === 24 ? [nw.net + 200, nw.bc - 1] : [Math.max(nw.net + 1, nw.bc - 55), nw.bc - 1];
}
const maskLabel = (p) => `${ip4.str(ip4.mask(p))}  (/${p})`;
// The part of an address that changes inside a network: ".35" on a /24, "3.35" on a /22, "5.3.35" on a /14.
function shortIp(ip, prefix) { const parts = String(ip).split("."); const keep = prefix >= 24 ? 1 : prefix >= 16 ? 2 : 3; return "." + parts.slice(4 - keep).join("."); }

// ---------- helpers ----------
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : String(c));
  return n;
}
const globe = (off) => {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("width", "14"); s.setAttribute("height", "14"); s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>' + (off ? '<path d="M4 4l16 16"/>' : "");
  return s;
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") init.headers["X-NetJoin"] = "1";
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch("/api" + path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function withBusy(btn, fn) {
  if (state.busy) return;
  state.busy = true; btn && btn.classList.add("busy"); btn && (btn.disabled = true);
  try { return await fn(); }
  catch (e) { flash(e.message, "error"); }
  finally { state.busy = false; btn && btn.classList.remove("busy"); btn && (btn.disabled = false); refreshLog(); }
}

// Toast notifications: float over the page (top right), so nothing shifts.
// A bar along the bottom counts down; hovering pauses it (CSS animation-play-state).
const TOAST_MS = { info: 2000, warn: 5000, error: 5000 };
function flash(msg, level = "info", opts = {}) {
  const box = $("#toasts");
  const key = opts.key || null;
  if (key) { const old = box.querySelector(`[data-key="${key}"]`); if (old) old.remove(); }
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 200); };
  const bar = opts.sticky ? null : el("div", { class: "timer", "aria-hidden": "true" });
  const t = el("div", { class: `toast ${level}`, role: level === "error" ? "alert" : "status", "data-key": key },
    el("span", { class: "msg" }, msg),
    el("button", { type: "button", class: "x", "aria-label": "Dismiss", onclick: close }, "✕"), bar);
  if (bar) {
    bar.style.animationDuration = `${TOAST_MS[level] || TOAST_MS.info}ms`;
    bar.addEventListener("animationend", close);
  }
  box.append(t);
  while (box.childElementCount > 4) box.firstElementChild.remove();
}

// ---------- status bar ----------
async function refreshStatus() {
  try { state.status = await api("/status"); } catch (e) { return; }
  const s = state.status;
  $("#ver").textContent = s.backend === "fake" ? "DEMO MODE – no real changes" : "";
  const cur = $("#current"); cur.replaceChildren();
  if (!s.adapter) { cur.append(el("span", { class: "muted" }, "No adapter selected")); }
  else {
    const p = s.primary;
    cur.append(el("span", { class: "small muted" }, "Current"),
      el("span", { class: "mono" }, p ? `${p.ip}/${p.prefix}` : "no IPv4"),
      el("span", { class: "small muted cur-mode", title: (s.adapter.dhcp ? "DHCP" : "Static") + (s.joined ? ` · ${s.joined.path || s.joined.network}` : "") }, (s.adapter.dhcp ? "DHCP" : "Static") + (s.joined ? ` · ${s.joined.network}` : "")));
    if (s.internet !== null && s.internet !== undefined) {
      const i = inetInfo(s.internet);
      cur.append(el("span", { class: "inet" + i.cls, title: s.internet === 2 ? "Reaches the internet by IP, but no DNS server answers, so web pages won't load. Set DNS on the network (Edit → Override DNS)." : null }, globe(i.off), i.text));
    }
    cur.append(el("button", { type: "button", class: "btn small", onclick: (e) => withBusy(e.currentTarget, async () => {
      const r = await api("/internet", { method: "POST" }); if (r.internet === 2) flash(r.detail, "warn"); await refreshStatus(); }) }, "Check internet"));
  }
  // Revert to DHCP only makes sense when the adapter isn't already on DHCP.
  const dh = $("#dhcpBtn");
  if (!dh.classList.contains("busy")) {
    const already = !!(s.adapter && s.adapter.dhcp === true);
    dh.disabled = !s.adapter || already;
    dh.title = !s.adapter ? "Pick an adapter first" : already ? `${s.adapter.name} is already on DHCP` : `Set ${s.adapter.name} back to DHCP`;
  }
  const rj = $("#rejoinBtn");
  if (s.last_network && !s.joined) { rj.hidden = false; rj.textContent = `Rejoin ${s.last_network.network}`; }
  else rj.hidden = true;
  if (!s.admin && !refreshStatus.adminWarned) {
    refreshStatus.adminWarned = true;
    flash("NetJoin isn't running as administrator, so Join and DHCP won't work. Quit it and start it again, accepting the Windows prompt.", "error", { sticky: true, key: "admin" });
  }
}

async function refreshAdapters() {
  const list = await api("/adapters" + ($("#showAll").checked ? "?all=1" : ""));
  const sel = $("#adapter"); const current = state.status && state.status.adapter && state.status.adapter.name;
  sel.replaceChildren(...list.map((a) => el("option", { value: a.name, selected: a.name === current },
    `${a.name}${a.description ? " – " + a.description : ""}${a.up ? "" : " (disconnected)"}`)));
  if (current && !list.some((a) => a.name === current)) sel.prepend(el("option", { value: current, selected: true }, current));
}

// ---------- tree ----------
async function refreshTree() {
  state.tree = await api("/tree");
  renderTree();
}

function matches(n, q) {
  if (!q) return true;
  const hay = `${n.name} ${n.subnet || ""}`.toLowerCase();
  return hay.includes(q) || (n.children || []).some((c) => matches(c, q));
}

function renderTree() {
  const q = $("#search").value.trim().toLowerCase();
  const build = (nodes) => nodes.filter((n) => matches(n, q)).map((n) => {
    const kids = n.children || [];
    const open = q || state.expanded.has(n.id);
    const item = el("div", { class: "item" + (state.selected === n.id ? " sel" : "") },
      el("button", { type: "button", class: "tog", "aria-label": open ? "Collapse" : "Expand", "aria-expanded": kids.length ? String(!!open) : null,
        onclick: () => { open ? state.expanded.delete(n.id) : state.expanded.add(n.id); saveUi(); renderTree(); } }, kids.length ? (open ? "▾" : "▸") : ""),
      el("button", { type: "button", class: "lbl", onclick: () => select(n) },
        n.kind !== "network" ? el("span", { class: "kind" }, n.kind === "site" ? "Site" : "Area") : null,
        el("span", { class: "nm" }, n.name),
        n.kind === "network" && n.last_internet === 1 ? el("span", { class: "inet yes", title: "Had internet at last join" }, globe(false)) : null,
        n.kind === "network" ? el("span", { class: "sub" }, n.subnet || "") : null),
      n.kind !== "network" ? el("button", { type: "button", class: "add", title: `Add inside ${n.name}`, "aria-label": `Add inside ${n.name}`,
        onclick: () => openNode({ parent: n }) }, "+ add") : null);
    return el("li", {}, item, kids.length && open ? el("ul", {}, build(kids)) : null);
  });
  const nodes = build(state.tree);
  $("#tree").replaceChildren(...(nodes.length ? nodes : [el("li", { class: "empty" }, q ? "Nothing matches." : "No sites yet.")]));
}

function findNode(id, nodes = state.tree) {
  for (const n of nodes) { if (n.id === id) return n; const f = findNode(id, n.children || []); if (f) return f; }
  return null;
}

async function select(n) {
  state.selected = n.id; state.expanded.add(n.id); saveUi(); renderTree();
  await refreshDetail();
}

// ---------- detail ----------
async function refreshDetail() {
  const box = $("#detail");
  if (state.selected == null || !findNode(state.selected)) {
    state.detail = null;
    box.replaceChildren(el("div", { class: "empty" }, "Pick a network on the left, or add a site to get started."));
    renderHistory(); return;
  }
  const d = state.detail = await api(`/nodes/${state.selected}`);
  const crumbs = el("span", { class: "crumbs" }, d.path.slice(0, -1).join(" › ") || (d.kind === "site" ? "Site" : ""));
  const editBtn = el("button", { type: "button", class: "btn", onclick: () => openNode({ node: d }) }, "Edit");
  if (d.kind !== "network") {
    box.replaceChildren(el("div", { class: "dhead" },
      el("div", { class: "row", style: "flex-direction:column;align-items:flex-start;gap:4px" }, crumbs, el("h1", {}, d.name)),
      el("div", { class: "spacer" }),
      el("div", { class: "row" }, editBtn,
        el("button", { type: "button", class: "btn", onclick: () => openNode({ parent: d, kind: "area" }) }, "+ Area"),
        el("button", { type: "button", class: "btn btn-primary", onclick: () => openNode({ parent: d, kind: "network" }) }, "+ Network"))),
      el("p", { class: "muted" }, `${d.kind === "site" ? "Site" : "Area"} · pick a network inside it to join.`));
    renderHistory(); return;
  }
  const inetPill = d.last_internet == null ? el("span", { class: "pill" }, "Internet: not checked yet")
    : el("span", { class: "pill" + inetInfo(d.last_internet).cls }, globe(inetInfo(d.last_internet).off),
        { 1: "Had internet at last join", 2: "Internet by IP only (no DNS) at last join", 0: "No internet at last join" }[d.last_internet] || "No internet at last join");
  const joinBtn = el("button", { type: "button", class: "btn btn-primary", style: "padding:0 24px", onclick: (e) => doJoin(e.currentTarget) }, "Join");
  const r = d.resolved;
  const tile = (k, v, from) => el("div", { class: "tile" }, el("span", { class: "k" }, k), el("span", { class: "v" }, v), from ? el("span", { class: "from" }, from) : null);
  box.replaceChildren(
    el("div", { class: "dhead" },
      el("div", { class: "row", style: "flex-direction:column;align-items:flex-start;gap:4px" }, crumbs, el("h1", {}, d.name)),
      el("div", { style: "margin-top:22px" }, inetPill),
      el("div", { class: "spacer" }),
      el("div", { class: "row" }, editBtn,
        el("button", { type: "button", class: "btn", disabled: !d.on_subnet || state.scan.running,
          title: d.on_subnet ? "Quick scan: ARP sweep + BACnet Who-Is" : "Join this network first, then scan",
          onclick: () => startScan("quick") }, "Scan"), joinBtn)),
    el("div", { class: "tiles" },
      tile("Network", netText(d.subnet), netFromCidr(d.subnet) ? `usable ${ip4.str(netFromCidr(d.subnet).first)} – ${ip4.str(netFromCidr(d.subnet).last)}` : null),
      d.addr_mode === "fixed"
        ? tile("Laptop address", d.fixed_ip || "—", "fixed IP")
        : tile("Laptop address", `${shortIp(d.range_start, Number(d.subnet.split("/")[1]))} – ${shortIp(d.range_end, Number(d.subnet.split("/")[1]))}`, `${d.range_start} – ${d.range_end} · ${ALLOC_TEXT[d.alloc] || "first free"}`),
      tile("Gateway / DNS", `${r.gateway || "—"} / ${r.dns.length ? r.dns.join(", ") : "—"}`,
        r.gateway_from || r.dns_from ? `from ${[r.gateway_from, r.dns_from].filter(Boolean).join(" · ")}` : "none (default)"),
      tile("Next IP on Join", d.next_ip || (d.next_how === "random from the range" ? "random" : "—"), d.next_how || null)),
    ...(d.notes ? [el("p", { class: "muted", style: "margin:12px 0 0;white-space:pre-line" }, d.notes)] : []));
  renderHistory();
}

const ALLOC_TEXT = { first: "first free", last: "last free", random: "random", sticky: "last used, then first" };
function netText(cidr) { const nw = netFromCidr(cidr); return nw ? `${ip4.str(nw.net)} · ${nw.mask} (/${nw.prefix})` : cidr; }

function renderHistory() {
  renderScanMenu(); refreshDevices();
  const ul = $("#history");
  const d = state.detail;
  if (!d || d.kind !== "network") { ul.replaceChildren(el("li", { class: "empty" }, "Select a network.")); return; }
  if (!d.history.length) { ul.replaceChildren(el("li", { class: "empty" }, "No joins yet.")); return; }
  ul.replaceChildren(...d.history.map((h) => el("li", {},
    el("button", { type: "button", title: `Join with ${h.ip}`, onclick: (e) => doJoin(e.currentTarget, h.ip) },
      el("span", { class: "ip" }, h.ip),
      h.result === "worked" && h.internet === 1 ? el("span", { class: "inet yes", title: "Had internet" }, globe(false)) : null,
      el("span", { class: "tag " + h.result }, h.result === "worked" ? "✓ Worked" : h.result === "clashed" ? "! Clashed" : "× Failed"),
      el("span", { class: "small muted" }, when(h.ts))))));
}

function when(ts) {
  const d = new Date(ts); const today = new Date();
  if (d.toDateString() === today.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString();
}

async function doJoin(btn, ip) {
  const d = state.detail; if (!d) return;
  await withBusy(btn, async () => {
    const r = await api("/join", { method: "POST", body: { network_id: d.id, ip } });
    flash(`Joined ${d.name} as ${r.ip}/${r.prefix}${r.attempts > 1 ? ` (after ${r.attempts - 1} clash${r.attempts > 2 ? "es" : ""})` : ""} · ${inetInfo(r.internet).text}`, r.internet === 2 ? "warn" : "info");
  });
  await Promise.all([refreshStatus(), refreshTree()]); await refreshDetail();
}

// ---------- scanning ----------
function renderScanMenu() {
  const d = state.detail; const box = $("#scanOptions"); const menu = $("#scanMenu");
  const usable = d && d.kind === "network" && d.on_subnet && !state.scan.running;
  menu.setAttribute("aria-disabled", usable ? "false" : "true");
  const kids = [];
  if (d && d.kind === "network" && !d.on_subnet) kids.push(el("div", { class: "note" }, "Join this network first – scans run from the laptop's current subnet."));
  if (state.scan.running) kids.push(el("div", { class: "note" }, "A scan is already running."));
  const desc = { quick: "Finds hosts and BACnet controllers. Recommended.", arp: "Every address on the subnet; devices answer even if they block ping.",
    ping: "ICMP echo to every address; shows response time.", bacnet: "Broadcast Who-Is, then reads each device's name and model.",
    ports: "Checks web UI, Modbus, Niagara, SSH/Telnet on known hosts.", passive: "Sends nothing; listens to ARP and BACnet traffic.",
    full: "ARP + ping + BACnet + ports. Slowest." };
  for (const o of state.scan.options) {
    kids.push(el("button", { type: "button", role: "menuitem", disabled: !usable || !o.available,
      onclick: () => { menu.open = false; startScan(o.kind); } },
      el("span", {}, o.label), el("span", { class: "d" }, o.available ? desc[o.kind] : `${o.note} – see Settings`)));
  }
  box.replaceChildren(...kids);
}
document.addEventListener("click", (e) => { const m = $("#scanMenu"); if (m.open && !m.contains(e.target)) m.open = false; });
// Keep the open menu inside the visible area (dashboard layout: above the Activity bar).
$("#scanMenu").addEventListener("toggle", (e) => {
  if (!e.target.open) return;
  const list = $("#scanOptions"); const top = $("#scanMenuBtn").getBoundingClientRect().bottom;
  const limit = Math.min(window.innerHeight, $("#logBox").getBoundingClientRect().top) - top - 12;
  list.style.maxHeight = `${Math.max(160, limit)}px`;
});

async function startScan(kind) {
  const d = state.detail; if (!d) return;
  try { state.scan = { ...state.scan, ...(await api("/scan", { method: "POST", body: { kind, network_id: d.id } })) }; }
  catch (e) { flash(e.message, "error"); return; }
  pollScan();
}

async function pollScan() {
  clearTimeout(state.scanTimer);
  let st;
  try { st = await api("/scan"); } catch (e) { return; }
  const wasRunning = state.scan.running;
  state.scan = st;
  const box = $("#scanProgress");
  if (st.running) {
    box.hidden = false;
    $("#scanPhase").textContent = `${st.label}: ${st.phase}${st.total > 1 ? ` – ${st.done}/${st.total}` : ""}${st.found ? ` · ${st.found} found` : ""}`;
    $("#scanBar").style.width = st.total ? `${Math.round((st.done / st.total) * 100)}%` : "8%";
    if (!pollScan.n || ++pollScan.n % 3 === 0) refreshDevices();
    pollScan.n = pollScan.n || 1;
    state.scanTimer = setTimeout(pollScan, 700);
  } else {
    box.hidden = true; pollScan.n = 0;
    if (wasRunning) { refreshDevices(); if (st.error) flash(st.error, "error"); }
  }
  renderScanMenu(); refreshLog();
  if (wasRunning !== st.running) refreshDetailButtonsOnly();
}
function refreshDetailButtonsOnly() { if (state.detail && state.detail.kind === "network") refreshDetail(); }
$("#scanCancel").addEventListener("click", () => api("/scan/cancel", { method: "POST" }).catch(() => {}));

async function refreshDevices() {
  const d = state.detail; const box = $("#devicesBox");
  if (!d || d.kind !== "network") { box.replaceChildren(el("div", { class: "empty" }, "Select a network.")); $("#devCount").textContent = ""; $("#clearDevices").hidden = true; $("#lastScan").textContent = ""; return; }
  let r; try { r = await api(`/devices?subnet=${encodeURIComponent(d.subnet)}`); } catch (e) { return; }
  if (!state.detail || state.detail.subnet !== r.subnet) return;
  state.devices = r.devices;
  $("#lastScan").textContent = r.last_scan ? `Last: ${r.last_scan.kind}, ${when(r.last_scan.ts)}` : "";
  renderDevices();
}

// BACnet vendor-name (from the device itself) wins; otherwise the MAC manufacturer.
function vendorCell(x) {
  const bac = x.vendor || (x.bacnet_vendor_id != null ? `Vendor ID ${x.bacnet_vendor_id}` : null);
  if (bac) return [bac, x.mac_vendor && x.mac_vendor !== bac ? el("span", { class: "sub2", title: "Network chip maker, from the MAC address" }, `NIC: ${x.mac_vendor}`) : null];
  if (x.mac_vendor) return [el("span", { class: x.mac_vendor.startsWith("Randomised") ? "muted" : null }, x.mac_vendor),
    el("span", { class: "sub2" }, "from MAC")];
  return "—";
}

function renderDevices() {
  const d = state.detail; const box = $("#devicesBox");
  const all = state.devices;
  const rows = all.filter((x) => state.filter === "all" || (state.filter === "bacnet" ? x.bacnet_instance != null : x.bacnet_instance == null));
  $("#clearDevices").hidden = all.length === 0;
  const others = all.filter((x) => x.sources !== "self").length;
  $("#devCount").textContent = all.length ? `${others} device(s)${rows.length !== all.length ? `, ${rows.length} shown` : ""}` : "";
  if (!all.length) {
    box.replaceChildren(el("div", { class: "empty" }, d.on_subnet ? "No scan results yet – use Scan to find hosts and controllers." : "No scan results yet. Join this network, then Scan."));
    return;
  }
  const portLabel = { 80: "HTTP", 443: "HTTPS", 8080: "HTTP-alt", 502: "Modbus", 1911: "Fox", 4911: "Fox TLS", 22: "SSH", 23: "Telnet" };
  const webUrl = (ip, p) => p === 443 ? `https://${ip}/` : p === 8080 ? `http://${ip}:8080/` : `http://${ip}/`;
  const tr = (x) => {
    const self = x.sources === "self" || (x.sources || "").split(",").includes("self");
    const ports = (x.ports || "").split(",").filter(Boolean).map(Number);
    const seen = [(x.sources || "").split(",").filter((s) => s && s !== "self").join(" · ") || "—",
      el("span", { class: "sub2" }, [x.ping_ms != null ? `${x.ping_ms} ms` : null, when(x.last_seen)].filter(Boolean).join(" · "))];
    return el("tr", { class: self ? "self" : null },
      el("td", { class: "mono ipcell", "data-label": "IP" }, x.ip),
      el("td", { class: "mono", "data-label": "MAC" }, x.mac || "—"),
      el("td", { "data-label": "Vendor" }, self ? [el("strong", {}, "This PC"), x.name ? el("span", { class: "sub2" }, x.name) : null] : vendorCell(x)),
      el("td", { "data-label": "BACnet" }, x.bacnet_instance != null ? [
        el("span", { class: "mono" }, `#${x.bacnet_instance}`), x.name ? ` · ${x.name}` : "",
        x.model ? el("span", { class: "sub2" }, x.model) : null,
        x.route ? el("span", { class: "sub2" }, `behind ${x.ip} (${x.route})`) : null] : "—"),
      el("td", { "data-label": "Ports" }, ports.length ? ports.map((p) => [80, 443, 8080].includes(p)
        ? el("a", { class: "port", href: webUrl(x.ip, p), target: "_blank", rel: "noopener noreferrer" }, portLabel[p] || p)
        : el("span", { class: "port" }, portLabel[p] || p)) : "—"),
      el("td", { class: "small", "data-label": "Seen by" }, seen));
  };
  box.replaceChildren(el("table", { class: "dev" },
    el("thead", {}, el("tr", {}, ["IP", "MAC", "Vendor", "BACnet device", "Open ports", "Seen by"].map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map(tr))));
}

for (const c of document.querySelectorAll(".chip")) c.addEventListener("click", () => {
  state.filter = c.dataset.filter;
  for (const o of document.querySelectorAll(".chip")) { o.classList.toggle("on", o === c); o.setAttribute("aria-pressed", String(o === c)); }
  renderDevices();
});
$("#clearDevices").addEventListener("click", async () => {
  const d = state.detail; if (!d || !confirm(`Clear the device list for ${d.subnet}?`)) return;
  await api(`/devices?subnet=${encodeURIComponent(d.subnet)}`, { method: "DELETE" }); refreshDevices(); refreshLog();
});

// ---------- discover ----------
const disc = { timer: null };
function discShow(view) { for (const v of ["discSetup", "discRunning", "discResults"]) $("#" + v).hidden = v !== view; }

$("#discoverBtn").addEventListener("click", async () => {
  const r = await api("/discover").catch(() => null);
  const s = state.status || {};
  $("#discAdapter").textContent = s.adapter ? `Runs from ${s.adapter.name}${s.primary ? ` (currently ${s.primary.ip}/${s.primary.prefix})` : ""}. Change the adapter at the top if that's not the BMS cable.` : "Pick an adapter at the top first.";
  const note = $("#discNpcapNote");
  if (r && !r.npcap && !r.demo) {
    note.hidden = false;
    note.textContent = "Npcap isn't installed, so NetJoin can only use the BACnet Who-Is. Replies from devices on a different subnet to the laptop may not get through. Install Npcap (npcap.com) for full listening.";
  } else note.hidden = true;
  $("#discoverDlg").showModal();
  if (r && r.state && r.state.kind === "discover" && r.state.running) { discShow("discRunning"); discPoll(); }
  else if (r && r.result) { discShow("discResults"); renderDiscover(r.result); }
  else discShow("discSetup");
});
$("#discoverClose").addEventListener("click", () => $("#discoverDlg").close());
$("#discAgain").addEventListener("click", () => discShow("discSetup"));
$("#discCancel").addEventListener("click", () => api("/scan/cancel", { method: "POST" }).catch(() => {}));
$("#discStart").addEventListener("click", async (e) => {
  try {
    await api("/discover", { method: "POST", body: { seconds: Number($("#discSeconds").value), whois: $("#discWhois").checked } });
  } catch (err) { flash(err.message, "error"); return; }
  discShow("discRunning"); discPoll();
});

async function discPoll() {
  clearTimeout(disc.timer);
  const r = await api("/discover").catch(() => null);
  if (!r) return;
  const st = r.state;
  if (st.running) {
    $("#discPhase").textContent = `${st.phase} – ${st.done}/${st.total} s`;
    $("#discBar").style.width = `${Math.round((st.done / Math.max(1, st.total)) * 100)}%`;
    disc.timer = setTimeout(discPoll, 600);
    return;
  }
  refreshLog();
  if (st.error) { flash(st.error, "error"); discShow("discSetup"); return; }
  discShow("discResults"); renderDiscover(r.result);
}

function flatAreas(site) {
  const out = [];
  const walk = (nodes, depth, path) => { for (const n of nodes) if (n.kind === "area") {
    out.push({ id: n.id, label: "  ".repeat(depth) + n.name, path: [...path, n.name] }); walk(n.children || [], depth + 1, [...path, n.name]); } };
  walk(site ? site.children || [] : [], 0, []);
  return out;
}

function renderDiscover(res) {
  const list = $("#discList");
  if (!res) { list.replaceChildren(); return; }
  $("#discSummary").textContent = `Finished ${res.finished} from ${res.adapter} (${res.src_ip})` +
    (res.npcap && !res.npcap_error ? " · listened with Npcap" : res.npcap_error ? " · Npcap failed" : " · no Npcap") +
    (res.whois && res.whois_to && res.whois_to.length ? ` · Who-Is to ${res.whois_to.join(" and ")}` : "");
  const notes = [];
  if (res.npcap_error) notes.push(el("p", { class: "note-warn" }, `Npcap couldn't listen on this adapter, so only the BACnet Who-Is ran. ${res.npcap_error}`));
  if (!res.subnets.length) {
    const tip = res.npcap && !res.npcap_error
      ? "Nothing heard. Check the cable and the adapter, or try a longer listen."
      : res.npcap_error ? "Nothing answered the Who-Is either."
      : "Nothing heard. Check the cable and the adapter, try a longer listen, or install Npcap so NetJoin can hear traffic that isn't BACnet.";
    list.replaceChildren(...notes, el("div", { class: "empty" }, tip));
    return;
  }
  list.replaceChildren(...notes, ...res.subnets.map((r) => discCard(r)));
}

function discCard(r) {
  const card = el("div", { class: "dcard" });
  const pill = r.confidence === "high" ? el("span", { class: "pill yes" }, "mask from broadcasts") : el("span", { class: "pill guess" }, "guessed /24");
  const counts = `${r.host_count} device(s)${r.bacnet_count ? ` · ${r.bacnet_count} BACnet` : ""}`;
  const hostTable = el("table", {}, el("thead", {}, el("tr", {}, ["IP", "MAC", "Maker", "BACnet"].map((h) => el("th", {}, h)))),
    el("tbody", {}, r.hosts.map((h) => el("tr", {}, el("td", { class: "mono" }, h.ip), el("td", { class: "mono" }, h.mac || "—"),
      el("td", {}, h.mac_vendor || "—"),
      el("td", {}, h.bacnet.length ? h.bacnet.map((b) => `#${b.instance}${b.vendor ? " " + b.vendor : b.vendor_id != null ? " vendor " + b.vendor_id : ""}${b.route ? " (" + b.route + ")" : ""}`).join(", ") : "—")))));
  const actions = el("div", { class: "row" });
  card.append(
    el("div", { class: "row" }, el("h3", {}, r.subnet), pill, el("span", { class: "small muted" }, counts), el("div", { class: "spacer" }), actions),
    el("div", { class: "meta" }, r.evidence.join(" · ") +
      (r.gateway_hint ? ` · devices ARP for ${r.gateway_hint}, probably the gateway (NetJoin leaves the gateway off unless you set it)` : "")),
    el("div", { class: "meta" }, `Suggested laptop range ${r.range_start} – ${r.range_end}` +
      (r.range_clashes ? ` (${r.range_clashes} device(s) seen inside it – Join will skip them)` : " (no devices seen there)")),
    ...(!r.contains_me ? [el("div", { class: "meta hint" }, "Join and scan to identify BACnet devices – from a different subnet, controllers that reply directly to the laptop can't be heard.")] : []),
    el("details", {}, el("summary", {}, `Show ${r.host_count} device(s)`), hostTable));
  if (r.saved_as && r.saved_as.length) {
    const n = r.saved_as[0];
    actions.append(el("span", { class: "small muted" }, `Saved as ${n.path}`),
      el("button", { type: "button", class: "btn small", onclick: async () => { $("#discoverDlg").close(); await openNetwork(n.id); } }, "Open"),
      el("button", { type: "button", class: "btn small btn-primary", title: "Join, then run a Quick scan", onclick: async () => { $("#discoverDlg").close(); await openNetwork(n.id); await joinAndScan(); } }, "Join & scan"));
  } else {
    actions.append(el("button", { type: "button", class: "btn small btn-primary", onclick: () => { actions.hidden = true; card.append(saveForm(r, card)); } }, "Save as network…"));
  }
  return card;
}

function saveForm(r, card) {
  const sites = state.tree.filter((n) => n.kind === "site");
  const siteSel = el("select", {}, ...sites.map((s) => el("option", { value: s.id }, s.name)), el("option", { value: "new" }, "New site…"));
  if (!sites.length) siteSel.value = "new";
  const newSite = el("input", { placeholder: "Site name" });
  const areaSel = el("select", {});
  const newArea = el("input", { placeholder: "Area name" });
  const syncSite = () => {
    newSite.parentElement.hidden = siteSel.value !== "new";
    const site = sites.find((s) => String(s.id) === siteSel.value);
    areaSel.replaceChildren(el("option", { value: "" }, "(directly under the site)"),
      ...flatAreas(site).map((a) => el("option", { value: a.id }, a.label)), el("option", { value: "new" }, "New area…"));
    syncArea();
  };
  const syncArea = () => { newArea.parentElement.hidden = areaSel.value !== "new"; };
  const name = el("input", { value: r.bacnet_count ? `BMS ${r.subnet.split("/")[0].split(".").slice(0, 3).join(".")}.x` : `Network ${r.subnet}` });
  const subnet = el("input", { class: "mono", value: r.subnet });
  const rs = el("input", { class: "mono", value: r.range_start }); const re = el("input", { class: "mono", value: r.range_end });
  const err = el("p", { class: "error full" });
  const save = async (join) => {
    err.textContent = "";
    try {
      let parent;
      if (siteSel.value === "new") {
        if (!newSite.value.trim()) throw new Error("Give the new site a name");
        parent = (await api("/nodes", { method: "POST", body: { kind: "site", name: newSite.value.trim(), parent_id: null } })).id;
      } else parent = Number(siteSel.value);
      if (areaSel.value === "new") {
        if (!newArea.value.trim()) throw new Error("Give the new area a name");
        parent = (await api("/nodes", { method: "POST", body: { kind: "area", name: newArea.value.trim(), parent_id: parent } })).id;
      } else if (areaSel.value) parent = Number(areaSel.value);
      const n = await api("/nodes", { method: "POST", body: { kind: "network", parent_id: parent, name: name.value,
        subnet: subnet.value.trim(), range_start: rs.value.trim(), range_end: re.value.trim() } });
      $("#discoverDlg").close();
      await openNetwork(n.id);
      flash(`Saved ${n.name} (${n.subnet})`);
      if (join) await joinAndScan();
    } catch (e) { err.textContent = e.message; }
  };
  const lab = (t, input, cls) => el("label", { class: cls || null }, t, input);
  const form = el("div", { class: "saveform" },
    lab("Site", siteSel), lab("New site name", newSite),
    lab("Area", areaSel), lab("New area name", newArea),
    lab("Network name", name, "full"), lab("Subnet", subnet, "full"),
    lab("Laptop range from", rs), lab("to", re), err,
    el("div", { class: "row full" }, el("div", { class: "spacer" }),
      el("button", { type: "button", class: "btn", onclick: () => save(false) }, "Save"),
      el("button", { type: "button", class: "btn btn-primary", title: "Save, join, then run a Quick scan", onclick: () => save(true) }, "Save, Join & scan")));
  queueMicrotask(syncSite);
  siteSel.addEventListener("change", syncSite); areaSel.addEventListener("change", syncArea);
  return form;
}

async function joinAndScan() {
  await doJoin(null);
  if (state.detail && state.detail.on_subnet) await startScan("quick");
}

async function openNetwork(id) {
  await refreshTree();
  let n = findNode(id);
  // expand every ancestor so the new network is visible
  const chain = []; const walk = (nodes, path) => { for (const x of nodes) { if (x.id === id) { chain.push(...path); return true; } if (walk(x.children || [], [...path, x.id])) return true; } return false; };
  walk(state.tree, []); chain.forEach((a) => state.expanded.add(a));
  if (n) await select(n);
}

// ---------- node dialog ----------
function openNode({ node, parent, kind }) {
  const f = $("#nodeForm"); f.reset(); $("#nodeErr").textContent = "";
  const k = node ? node.kind : kind || (parent ? "area" : "site");
  f.id.value = node ? node.id : ""; f.kind.value = k; f.parent_id.value = node ? (node.parent_id ?? "") : parent ? parent.id : "";
  $("#nodeTitle").textContent = node ? `Edit ${k}` : `Add ${k}${parent ? " to " + parent.name : ""}`;
  $("#deleteNodeBtn").hidden = !node;
  const setKind = (kk) => { f.kind.value = kk; f.querySelector(".net-only").hidden = kk !== "network"; f.net_ip.required = kk === "network";
    $("#suggestGw").hidden = kk !== "network"; };
  setKind(k);
  if (!node && parent && !kind) {   // let the user choose area or network
    $("#nodeTitle").replaceChildren(`Add to ${parent.name}: `,
      el("select", { id: "kindPick", onchange: (e) => { setKind(e.target.value); netChanged(); } },
        el("option", { value: "area" }, "Area"), el("option", { value: "network" }, "Network")));
  }
  f.dataset.rangeTouched = "";
  f.net_mask.value = "24";
  if (node) {
    for (const key of ["name", "range_start", "range_end", "fixed_ip", "gateway", "dns", "notes"]) if (f[key]) f[key].value = node[key] || "";
    f.gw_override.checked = !!node.gw_override; f.dns_override.checked = !!node.dns_override;
    if (node.subnet) { const [a, p] = node.subnet.split("/"); f.net_ip.value = a; f.net_mask.value = p; f.dataset.rangeTouched = "1"; }
    f.alloc.value = node.alloc || "first";
    for (const r of f.addr_mode) r.checked = r.value === (node.addr_mode || "range");
  }
  syncOverrides(f); syncAddrMode(); netChanged();
  $("#nodeDlg").showModal(); f.name.focus();
}

// Mask list: the common ones first, then every size, each shown as netmask and CIDR.
(function fillMasks() {
  const sel = $("#nodeForm").net_mask;
  const common = [24, 16, 22, 23, 25, 26, 27, 28];
  sel.append(el("optgroup", { label: "Common" }, common.map((p) => el("option", { value: p }, maskLabel(p)))),
    el("optgroup", { label: "All" }, Array.from({ length: 23 }, (_, i) => 30 - i).map((p) => el("option", { value: p }, maskLabel(p)))));
  // the same value appears twice; select the first one
})();

function currentNet() { const f = $("#nodeForm"); return netOf(f.net_ip.value, Number(f.net_mask.value)); }

function netChanged() {
  const f = $("#nodeForm");
  if (f.kind.value !== "network") return;
  const pv = $("#netPreview");
  const nw = currentNet();
  if (!f.net_ip.value.trim()) {
    pv.className = "small preview"; pv.textContent = "Type any IP on the network (e.g. from the drawings) and pick the mask. Pasting 10.20.5.0/24 works too.";
  } else if (!nw) {
    pv.className = "small preview bad"; pv.textContent = "That isn't an IPv4 address.";
  } else {
    pv.className = "small preview ok";
    pv.textContent = `→ Network ${ip4.str(nw.net)}/${nw.prefix} · usable ${ip4.str(nw.first)} – ${ip4.str(nw.last)} (${nw.count.toLocaleString()} addresses)`;
    // auto-fill the laptop range unless the user set one that still fits
    const rs = ip4.toInt(expandShort(f.range_start.value, nw)), re = ip4.toInt(expandShort(f.range_end.value, nw));
    const fits = rs != null && re != null && rs >= nw.first && re <= nw.last;
    if (!f.dataset.rangeTouched || !fits) { const [a, b] = defaultRange(nw); f.range_start.value = ip4.str(a); f.range_end.value = ip4.str(b); }
    $("#suggestGw").title = `Use ${ip4.str(nw.first)} (the network's first address)`;
  }
  rangeChanged();
}

function rangeChanged() {
  const f = $("#nodeForm"); const nw = currentNet(); const rp = $("#rangePreview");
  const base = "Addresses NetJoin may give this laptop – pick a block the site doesn't use. Type 35 or .35 for short.";
  if (!nw || !f.range_start.value.trim()) { rp.className = "small muted"; rp.textContent = base; return; }
  const a = ip4.toInt(expandShort(f.range_start.value, nw)), b = ip4.toInt(expandShort(f.range_end.value, nw));
  if (a == null || b == null) { rp.className = "small bad"; rp.textContent = "From / To must be addresses (or short forms like .35)."; return; }
  if (a < nw.first || b > nw.last || a > nw.last || b < nw.first) { rp.className = "small bad"; rp.textContent = `Outside the network (${ip4.str(nw.first)} – ${ip4.str(nw.last)}).`; return; }
  rp.className = "small muted"; rp.textContent = `${Math.abs(b - a) + 1} address(es). ${base}`;
}

function syncAddrMode() {
  const f = $("#nodeForm"); const mode = [...f.addr_mode].find((r) => r.checked)?.value || "range";
  for (const b of f.querySelectorAll(".mode-block")) {
    const on = b.dataset.mode === mode; b.setAttribute("aria-disabled", on ? "false" : "true");
    for (const i of b.querySelectorAll("input,select")) i.disabled = !on;
  }
}

{
  const f = $("#nodeForm");
  f.net_ip.addEventListener("input", () => {
    // pasted "10.20.5.0/24" or "10.20.5.0 255.255.255.0": split into IP + mask
    const v = f.net_ip.value.trim(); const m = v.match(/^(\d+\.\d+\.\d+\.\d+)\s*(?:\/\s*(\d+)|\s+(\d+\.\d+\.\d+\.\d+))$/);
    if (m) {
      let p = m[2] ? Number(m[2]) : null;
      if (m[3]) { const mi = ip4.toInt(m[3]); for (let i = 8; i <= 30; i++) if (ip4.mask(i) === mi) p = i; }
      if (p >= 8 && p <= 30) { f.net_ip.value = m[1]; f.net_mask.value = String(p); }
    }
    netChanged();
  });
  f.net_mask.addEventListener("change", netChanged);
  for (const n of ["range_start", "range_end"]) {
    f[n].addEventListener("input", () => { f.dataset.rangeTouched = "1"; rangeChanged(); });
    f[n].addEventListener("blur", () => { const x = expandShort(f[n].value, currentNet()); if (x) f[n].value = x; rangeChanged(); });
  }
  f.fixed_ip.addEventListener("blur", () => { const x = expandShort(f.fixed_ip.value, currentNet()); if (x) f.fixed_ip.value = x; });
  for (const r of f.addr_mode) r.addEventListener("change", syncAddrMode);
}

// Gateway / DNS inputs are only editable while their override box is ticked.
function syncOverrides(f) {
  f.gateway.disabled = !f.gw_override.checked;
  f.dns.disabled = !f.dns_override.checked;
  const sg = f.querySelector("#suggestGw"); if (sg) sg.disabled = !f.gw_override.checked;
  const dg = f.querySelector("#dnsFromGw"); if (dg) dg.disabled = !f.dns_override.checked;
}
for (const id of ["#nodeForm", "#settingsForm"]) {
  const f = $(id);
  f.gw_override.addEventListener("change", () => syncOverrides(f));
  f.dns_override.addEventListener("change", () => syncOverrides(f));
}

$("#nodeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target; const id = f.id.value;
  const body = { name: f.name.value, gw_override: f.gw_override.checked, gateway: f.gateway.value.trim(),
    dns_override: f.dns_override.checked, dns: f.dns.value.trim(), notes: f.notes.value };
  if (f.kind.value === "network") {
    const nw = currentNet();
    if (!nw) { $("#nodeErr").textContent = "Enter an IP on the network and pick the mask."; return; }
    Object.assign(body, { subnet: `${ip4.str(nw.net)}/${nw.prefix}`, range_start: f.range_start.value.trim(), range_end: f.range_end.value.trim(),
      addr_mode: [...f.addr_mode].find((r) => r.checked)?.value || "range", fixed_ip: f.fixed_ip.value.trim(), alloc: f.alloc.value });
  }
  try {
    let n;
    if (id) n = await api(`/nodes/${id}`, { method: "PUT", body });
    else { n = await api("/nodes", { method: "POST", body: { ...body, kind: f.kind.value, parent_id: f.parent_id.value === "" ? null : Number(f.parent_id.value) } });
      if (n.parent_id != null) state.expanded.add(n.parent_id); state.selected = n.id; saveUi(); }
    $("#nodeDlg").close();
    await refreshTree(); await refreshDetail(); refreshLog();
  } catch (err) { $("#nodeErr").textContent = err.message; }
});
$("#cancelNode").addEventListener("click", () => $("#nodeDlg").close());
$("#deleteNodeBtn").addEventListener("click", async () => {
  const f = $("#nodeForm"); const n = findNode(Number(f.id.value));
  if (!n) return;
  const count = JSON.stringify(n).split('"kind"').length - 2;
  if (!confirm(`Delete ${n.kind} '${n.name}'${count > 0 ? ` and the ${count} item(s) inside it` : ""}? Its join history goes too.`)) return;
  try { await api(`/nodes/${n.id}`, { method: "DELETE" }); $("#nodeDlg").close();
    if (state.selected === n.id) state.selected = n.parent_id; saveUi(); await refreshTree(); await refreshDetail(); refreshLog(); }
  catch (err) { $("#nodeErr").textContent = err.message; }
});
$("#dnsFromGw").addEventListener("click", () => {
  const f = $("#nodeForm");
  const gw = f.gateway.value.trim() || (state.detail && state.detail.resolved && state.detail.resolved.gateway) || "";
  if (!gw) { $("#nodeErr").textContent = "Set a gateway first (or type the DNS server in)."; return; }
  f.dns.value = gw;
});
$("#suggestGw").addEventListener("click", () => {
  const f = $("#nodeForm"); const nw = currentNet();
  if (!nw) { $("#nodeErr").textContent = "Enter the network's IP and mask first."; return; }
  f.gateway.value = ip4.str(nw.first);      // network + 1, e.g. 192.168.0.1 on a /22
});

// ---------- settings ----------
$("#settingsBtn").addEventListener("click", async () => {
  const f = $("#settingsForm"); $("#settingsErr").textContent = "";
  const g = await api("/settings/global");
  f.gw_override.checked = !!g.gw_override; f.gateway.value = g.gateway || "";
  f.dns_override.checked = !!g.dns_override; f.dns.value = g.dns || "";
  syncOverrides(f);
  const s = state.status || {};
  const rows = [["Back-end", s.backend === "fake" ? "Demo (fake) – no real changes" : "Windows (netsh)"],
    ["Administrator", s.admin ? "Yes" : "No – Join/DHCP unavailable"],
    ["Npcap", s.npcap ? s.npcap.note : "—"],
    ["Data folder", (s.data_dir || "") + (s.portable ? " (portable mode)" : "")]];
  $("#sysInfo").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  $("#settingsDlg").showModal();
});
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault(); const f = e.target;
  try { await api("/settings/global", { method: "PUT", body: { gw_override: f.gw_override.checked, gateway: f.gateway.value, dns_override: f.dns_override.checked, dns: f.dns.value } });
    $("#settingsDlg").close(); await refreshDetail(); refreshLog(); }
  catch (err) { $("#settingsErr").textContent = err.message; }
});
$("#cancelSettings").addEventListener("click", () => $("#settingsDlg").close());
$("#importFile").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  try { const payload = JSON.parse(await file.text()); const r = await api("/import", { method: "POST", body: payload });
    $("#settingsErr").textContent = ""; flash(`Imported ${r.imported} items`); await refreshTree(); refreshLog(); }
  catch (err) { $("#settingsErr").textContent = err.message; }
  e.target.value = "";
});

// ---------- log ----------
async function refreshLog() {
  try {
    const items = await api(`/log?after=${state.logAfter}`);
    const box = $("#log");
    for (const it of items) {
      if (it.id <= state.logAfter) continue;   // two refreshes can overlap; never show a line twice
      box.prepend(el("div", { class: it.level }, el("span", { class: "t" }, it.ts), el("span", {}, it.msg))); state.logAfter = it.id;
      const last = $("#logLast"); last.textContent = `${it.ts}  ${it.msg}`; last.className = "log-last " + (it.level || "");
    }
    while (box.childElementCount > 200) box.lastElementChild.remove();
  } catch (e) {}
}

// ---------- activity log open / closed ----------
(function logToggle() {
  let open = window.innerWidth > 600;
  try { const saved = localStorage.getItem("nj.logOpen"); if (saved !== null) open = saved === "1"; } catch (e) {}
  const apply = () => { $("#logBox").classList.toggle("open", open); $("#logToggle").setAttribute("aria-expanded", String(open)); };
  $("#logToggle").addEventListener("click", () => { open = !open; apply(); try { localStorage.setItem("nj.logOpen", open ? "1" : "0"); } catch (e) {} });
  apply();
})();

// ---------- wiring ----------
$("#adapter").addEventListener("change", async (e) => {
  try { await api("/adapter", { method: "POST", body: { name: e.target.value } }); } catch (err) { flash(err.message, "error"); }
  await refreshStatus(); refreshLog();
});
$("#showAll").addEventListener("change", refreshAdapters);
$("#search").addEventListener("input", renderTree);
$("#addSiteBtn").addEventListener("click", () => openNode({ kind: "site" }));
$("#dhcpBtn").addEventListener("click", async (e) => {
  await withBusy(e.currentTarget, async () => { await api("/dhcp", { method: "POST" }); flash("Adapter set back to DHCP"); });
  await refreshStatus();   // after withBusy re-enables the button, so the DHCP state decides
  if (state.detail) refreshDetail();
});
$("#rejoinBtn").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const r = await api("/rejoin", { method: "POST" }); flash(`Rejoined as ${r.ip}/${r.prefix}`);
  await refreshStatus(); await refreshTree(); await refreshDetail(); }));

(async function init() {
  await refreshStatus(); await refreshAdapters(); await refreshTree();
  try { state.scan = await api("/scan"); } catch (e) {}
  await refreshDetail(); await refreshLog(); if (state.scan.running) pollScan();
  setInterval(refreshStatus, 5000); setInterval(refreshLog, 3000);
  setInterval(() => { if (document.visibilityState === "visible" && !state.busy && document.activeElement !== $("#adapter")) refreshAdapters(); }, 15000);
})();
