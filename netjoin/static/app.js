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
// The host part only: 10.110.102.200 on a /24 → "200"; on a /16 → "102.200"
function shortIp(ip, prefix) { const parts = String(ip).split("."); const keep = prefix >= 24 ? 1 : prefix >= 16 ? 2 : 3; return parts.slice(4 - keep).join("."); }

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
  if (!r.ok) { const err = new Error(data.error || `HTTP ${r.status}`); err.status = r.status; err.data = data; throw err; }
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
  if (s.adapter_renamed) { flash(s.adapter_renamed); refreshAdapters(); }
  const wasMissing = refreshStatus.missing; refreshStatus.missing = !!s.adapter_missing;
  if (wasMissing !== undefined && wasMissing !== refreshStatus.missing) refreshAdapters();   // unplugged / back
  $("#ver").hidden = s.backend !== "fake";
  // Connection chip: [● adapter ▾] ip/prefix · DHCP|Static · network  🌐 internet
  const cur = $("#current"); cur.replaceChildren();
  const a = s.adapter; const p = s.primary;
  $("#connName").textContent = s.adapter_missing ? `${s.adapter_missing.name || "Adapter"} – unplugged` : a ? a.name : "Pick an adapter";
  const dot = $("#connDot"); dot.className = "dot " + (s.adapter_missing ? "warn" : !a ? "off" : a.up === false ? "off" : "up");
  dot.title = s.adapter_missing ? "Unplugged" : !a ? "No adapter" : a.up === false ? "Cable / link down" : "Link up";
  if (s.adapter_missing) cur.append(el("span", { class: "inet warn" }, "plug it back in"));
  else if (!a) cur.append(el("span", { class: "muted" }, "none selected"));
  else {
    cur.append(el("span", { class: "mono" }, p ? `${p.ip}/${p.prefix}` : "no IPv4"),
      el("span", { class: "small muted cur-mode", title: (a.dhcp ? "DHCP" : "Static") + (s.joined ? ` · ${s.joined.path || s.joined.network}` : "") },
        a.dhcp ? "DHCP" : "Static", ...(s.joined ? [" · ", joinedLink(s.joined)] : [])));
    if (s.internet !== null && s.internet !== undefined) {
      const i = inetInfo(s.internet);
      cur.append(el("span", { class: "inet" + i.cls, title: s.internet === 2 ? "Reaches the internet by IP, but no DNS server answers, so web pages won't load. Set DNS on the network (Edit → Override DNS)." : i.text }, globe(i.off), el("span", { class: "inet-t" }, i.text)));
    }
  }
  const kv = (k, v) => [el("dt", {}, k), el("dd", {}, v)];
  $("#connInfo").replaceChildren(
    ...kv("Device", a ? (a.description || a.name) : "—"), ...kv("MAC", a && a.mac ? a.mac : "—"),
    ...kv("Address", a && p ? `${p.ip}/${p.prefix} · ${a.dhcp ? "DHCP" : "Static"}` : "—"),
    ...kv("Internet", s.internet == null ? "not checked" : inetInfo(s.internet).text),
    ...(s.backend === "fake" ? kv("Mode", "Demo – nothing on this PC is changed") : []));
  $("#checkInetBtn").disabled = !a;
  const jb = document.querySelector("#tbActions .join-btn");
  if (jb && state.detail && state.detail.kind === "network") {
    const here = !!(s.joined && s.joined.network_id === state.detail.id);
    if (here !== jb.classList.contains("joined") && !jb.classList.contains("busy")) refreshDetail();
  }
  // Revert to DHCP only makes sense when the adapter isn't already on DHCP.
  const dh = $("#dhcpBtn");
  if (!dh.classList.contains("busy")) {
    const already = !!(s.adapter && s.adapter.dhcp === true);
    dh.hidden = !s.adapter || already;        // nothing to revert: keep the bar clear
    dh.disabled = !s.adapter || already;
    dh.title = !s.adapter ? "Pick an adapter first" : already ? `${s.adapter.name} is already on DHCP` : `Set ${s.adapter.name} back to DHCP`;
  }
  if (!s.admin && !refreshStatus.adminWarned) {
    refreshStatus.adminWarned = true;
    flash("NetJoin isn't running as administrator, so Join and DHCP won't work. Quit it and start it again, accepting the Windows prompt.", "error", { sticky: true, key: "admin" });
  }
}

async function refreshAdapters() {
  const list = await api("/adapters" + ($("#showAll").checked ? "?all=1" : ""));
  const sel = $("#adapter"); const st = state.status || {};
  const current = st.adapter && st.adapter.name; const missing = st.adapter_missing;
  sel.replaceChildren(...list.map((a) => el("option", { value: a.name, selected: !missing && a.name === current },
    `${a.name}${a.description ? " – " + a.description : ""}${a.up ? "" : " (disconnected)"}`)));
  if (missing) sel.prepend(el("option", { value: "", selected: true, disabled: true }, `${missing.name || "Chosen adapter"} (unplugged)`));
  else if (current && !list.some((a) => a.name === current)) sel.prepend(el("option", { value: current, selected: true }, current));
}

// ---------- tree ----------
async function refreshTree() {
  state.tree = await api("/tree");
  renderTree();
}

function matches(n, q) {
  if (!q) return true;
  const hay = `${n.name} ${n.subnet || ""}`.toLowerCase();
  return hay.includes(q) || (n.kind === "network" && searchHits(n.id).length > 0) || (n.children || []).some((c) => matches(c, q));
}

// ---------- devices in the Sites list (v0.10) ----------
// A network's devices load when its branch is opened; nothing is scanned for this.
state.netDevices = {};          // network id → device rows (null while loading)
state.devSearch = [];           // devices matching the Sites search box
const isDevSel = () => typeof state.selected === "string" && state.selected.startsWith("dev:");
const devSel = () => { const [, id, net] = state.selected.split(":"); return { id: Number(id), net: Number(net) }; };
const devKey = (id, net) => `dev:${id}:${net}`;
const searchHits = (netId) => state.devSearch.filter((x) => (x.network_ids || []).includes(netId));
const devLabel = (x) => x.bacnet_instance != null ? (x.name || `Device ${x.bacnet_instance}`) : x.ip;
function treeDeviceOrder(rows) {
  return rows.filter((x) => !x.is_self).sort((a, b) => {
    const ab = a.bacnet_instance != null, bb = b.bacnet_instance != null;
    if (ab !== bb) return ab ? -1 : 1;                                  // BACnet first
    if (ab) return devLabel(a).localeCompare(devLabel(b), undefined, { numeric: true }) || a.bacnet_instance - b.bacnet_instance;
    return ipKey(a.ip) < ipKey(b.ip) ? -1 : ipKey(a.ip) > ipKey(b.ip) ? 1 : 0;
  });
}
async function loadNetDevices(id) {
  state.netDevices[id] = null;
  try { state.netDevices[id] = (await api(`/devices?node_id=${id}`)).devices; } catch (e) { state.netDevices[id] = []; }
  renderTree();
}
function treeDeviceItems(n, q) {
  if (q) return searchHits(n.id).map((x) => treeDeviceItem(x, n.id));
  const rows = state.netDevices[n.id];
  if (rows === undefined) { loadNetDevices(n.id); return [el("li", { class: "empty small" }, "Loading…")]; }
  if (rows === null) return [el("li", { class: "empty small" }, "Loading…")];
  const list = treeDeviceOrder(rows);
  return list.length ? list.map((x) => treeDeviceItem(x, n.id)) : [el("li", { class: "empty small" }, "No devices yet – Join and Scan")];
}
function treeDeviceItem(x, netId) {
  const key = devKey(x.id, netId);
  return el("li", {}, el("div", { class: "item dev" + (state.selected === key ? " sel" : "") + (x.inactive ? " gone" : "") + (x.pending ? " pending" : ""),
      title: x.inactive ? "Missing – didn't answer the latest scan" : null },
    el("span", { class: "tog" }, ""),
    // Two tight lines: name (or IP) [+ instance], then IP / vendor underneath
    el("button", { type: "button", class: "lbl", onclick: () => openDevice(x.id, netId), title: [devLabel(x), x.ip, x.vendor || x.mac_vendor].filter(Boolean).join(" · ") },
      el("span", { class: "dot " + (x.inactive ? "warn" : x.pending ? "off" : "up"), "aria-hidden": "true" }),
      el("span", { class: "dtext" },
        el("span", { class: "nm" }, devLabel(x), x.bacnet_instance != null ? el("span", { class: "inst" }, ` #${x.bacnet_instance}`) : null),
        el("span", { class: "dsub" }, (x.bacnet_instance != null ? [x.ip, x.vendor || x.mac_vendor] : [x.mac_vendor || x.vendor]).filter(Boolean).join(" · ") || null)))));
}
async function openDevice(id, netId) {
  state.selected = devKey(id, netId);
  for (let p = netId; p != null; p = parentOf(p)) state.expanded.add(p);
  saveUi(); renderTree(); await refreshDetail();
}
let devSearchTimer = null;
function searchDevicesSoon() {
  clearTimeout(devSearchTimer);
  const q = $("#search").value.trim();
  if (q.length < 2) { state.devSearch = []; renderTree(); return; }
  devSearchTimer = setTimeout(async () => {
    try { state.devSearch = await api(`/devices/search?q=${encodeURIComponent(q)}`); } catch (e) { state.devSearch = []; }
    if ($("#search").value.trim() === q) renderTree();
  }, 220);
}

function renderTree() {
  const q = $("#search").value.trim().toLowerCase();
  const build = (nodes) => nodes.filter((n) => matches(n, q)).map((n) => {
    const kids = n.children || [];
    const open = q || state.expanded.has(n.id);
    const item = el("div", { class: "item" + (state.selected === n.id ? " sel" : ""), "data-id": n.id, draggable: q ? null : "true",
        title: q ? null : "Drag to reorder, or drop onto a site / area to move it inside" },
      el("button", { type: "button", class: "tog", "aria-label": open ? "Collapse" : "Expand", "aria-expanded": kids.length || n.kind === "network" ? String(!!open) : null,
        onclick: () => { open ? state.expanded.delete(n.id) : state.expanded.add(n.id); saveUi(); renderTree(); } }, kids.length || n.kind === "network" ? (open ? "▾" : "▸") : ""),
      el("button", { type: "button", class: "lbl", onclick: () => select(n) },
        n.kind !== "network" ? el("span", { class: "kind" }, n.kind === "site" ? "Site" : "Area") : null,
        el("span", { class: "nm" }, n.name),
        n.kind === "network" && n.last_internet === 1 ? el("span", { class: "inet yes", title: "Had internet at last join" }, globe(false)) : null,
        n.kind === "network" ? el("span", { class: "sub" }, n.subnet || "") : null),
      n.kind !== "network" ? el("button", { type: "button", class: "add", title: `Add inside ${n.name}`, "aria-label": `Add inside ${n.name}`,
        onclick: () => openNode({ parent: n }) }, "+ add") : null);
    if (n.kind === "network") return el("li", {}, item, open ? el("ul", { class: "devs" }, treeDeviceItems(n, q)) : null);
    return el("li", {}, item, kids.length && open ? el("ul", {}, build(kids)) : null);
  });
  const nodes = build(state.tree);
  const home = q ? null : el("li", {}, el("div", { class: "item home-item" + (state.selected === "home" ? " sel" : "") },
    el("span", { class: "tog" }, ""), el("button", { type: "button", class: "lbl", onclick: showHome }, houseIcon(), el("span", { class: "nm" }, "Home"))));
  $("#tree").replaceChildren(...[home].filter(Boolean), ...(nodes.length ? nodes : [el("li", { class: "empty" }, q ? "Nothing matches." : "No sites yet.")]));
}

// ---------- Drag and drop in the Sites list ----------
// Drop on the top or bottom edge of an item to put it before / after; drop on the middle of a site or
// area to move it inside. Sites only reorder among sites; areas and networks stay within their own site.
const parentOf = (id) => { const n = findNode(id); return n ? n.parent_id ?? null : null; };
const siteOf = (id) => { let n = findNode(id); while (n && n.parent_id != null) n = findNode(n.parent_id); return n ? n.id : null; };
const isInside = (id, ancestorId) => { for (let p = parentOf(id); p != null; p = parentOf(p)) if (p === ancestorId) return true; return false; };
function dropPlan(drag, target, y, h) {
  if (!drag || !target || drag.id === target.id) return null;
  if (target.kind !== "site" && isInside(target.id, drag.id)) return null;          // not inside itself
  const edge = y < h * 0.28 ? "before" : y > h * 0.72 ? "after" : "into";
  if (drag.kind === "site") return target.kind === "site" ? (edge === "into" ? (y < h / 2 ? "before" : "after") : edge) : null;
  if (siteOf(drag.id) !== siteOf(target.id)) return null;                          // same site only
  if (target.kind === "site") return "into";                                       // a site row: inside it
  if (target.kind === "network") return edge === "into" ? (y < h / 2 ? "before" : "after") : edge;
  return edge;                                                                     // area: before / into / after
}
const drag = { node: null, over: null, plan: null, timer: null };
function clearDropMarks() { for (const e of document.querySelectorAll("#tree .drop-before, #tree .drop-after, #tree .drop-into")) e.classList.remove("drop-before", "drop-after", "drop-into"); }
$("#tree").addEventListener("dragstart", (e) => {
  const it = e.target.closest && e.target.closest(".item[data-id]"); if (!it) return;
  drag.node = findNode(Number(it.dataset.id)); if (!drag.node) return;
  e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(drag.node.id));
  requestAnimationFrame(() => it.classList.add("dragging")); document.body.classList.add("tree-dragging");
});
$("#tree").addEventListener("dragover", (e) => {
  if (!drag.node) return;
  const it = e.target.closest && e.target.closest(".item[data-id]");
  const target = it ? findNode(Number(it.dataset.id)) : null;
  const r = it ? it.getBoundingClientRect() : null;
  const plan = target ? dropPlan(drag.node, target, e.clientY - r.top, r.height) : null;
  if (it !== drag.over || plan !== drag.plan) {
    clearDropMarks(); clearTimeout(drag.timer);
    drag.over = it; drag.plan = plan;
    if (plan) it.classList.add("drop-" + plan);
    // Hovering "into" a closed site / area opens it, so you can go deeper
    if (plan === "into" && target.children && target.children.length && !state.expanded.has(target.id))
      drag.timer = setTimeout(() => { state.expanded.add(target.id); renderTree(); }, 650);
  }
  if (plan) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; } else e.dataTransfer.dropEffect = "none";
});
$("#tree").addEventListener("dragleave", (e) => { if (!$("#tree").contains(e.relatedTarget)) { clearDropMarks(); drag.over = drag.plan = null; } });
$("#tree").addEventListener("drop", async (e) => {
  if (!drag.node || !drag.plan || !drag.over) return;
  e.preventDefault();
  const moving = drag.node; const target = findNode(Number(drag.over.dataset.id)); const plan = drag.plan;
  endDrag();
  let parentId, index = null;
  if (plan === "into") parentId = target.id;
  else {
    parentId = target.parent_id ?? null;
    const sibs = (parentId == null ? state.tree : (findNode(parentId).children || [])).filter((x) => x.id !== moving.id);
    index = sibs.findIndex((x) => x.id === target.id) + (plan === "after" ? 1 : 0);
  }
  try {
    const r = await api(`/nodes/${moving.id}/move`, { method: "POST", body: { parent_id: parentId, index } });
    if (plan === "into") state.expanded.add(target.id);
    saveUi(); await refreshTree();
    if (state.selected !== "home") await refreshDetail();        // breadcrumbs / inherited gateway may have changed
    refreshLog();
    if (r.live) await handleLive(r.live);
  } catch (err) { flash(err.message, "error"); }
});
function endDrag() {
  clearTimeout(drag.timer); clearDropMarks(); document.body.classList.remove("tree-dragging");
  for (const x of document.querySelectorAll("#tree .dragging")) x.classList.remove("dragging");
  drag.node = drag.over = drag.plan = null;
}
document.addEventListener("dragend", endDrag);
// If the list redrew mid-drag (opening a branch), the dragged row is gone and its dragend never
// arrives – tidy up on the next drop anywhere or the next click.
document.addEventListener("drop", () => { if (drag.node) setTimeout(endDrag, 0); });
document.addEventListener("pointerdown", () => { if (drag.node) endDrag(); });

function findNode(id, nodes = state.tree) {
  for (const n of nodes) { if (n.id === id) return n; const f = findNode(id, n.children || []); if (f) return f; }
  return null;
}

async function select(n) {
  if (state.selected !== n.id) { state.vendors.clear(); $("#devNetwork").value = ""; }
  state.selected = n.id; state.expanded.add(n.id); saveUi(); renderTree();
  await refreshDetail();
}

// The network the laptop is on, as a link to its page (top bar and This PC).
function joinedLink(j) {
  if (!j) return null;
  if (!j.network_id || !findNode(j.network_id)) return el("span", {}, j.network);
  return el("button", { type: "button", class: "netlink", title: `Open ${j.path || j.network}`, onclick: () => goTo(j.network_id) }, j.network);
}

// ---------- edits to the network the laptop is on ----------
// The server applies gateway / DNS changes straight away without touching the address, and asks (via
// this dialog) before anything that could drop traffic: a new address (rejoin) or swapping a gateway in use.
function askApply({ title, msg, risk, now }) {
  const dlg = $("#applyDlg");
  $("#applyTitle").textContent = title; $("#applyMsg").textContent = msg; $("#applyRisk").textContent = risk;
  $("#applyNow").textContent = now;
  return new Promise((resolve) => {
    const done = (v) => { dlg.close(); resolve(v); };
    $("#applyNow").onclick = () => done(true); $("#applyLater").onclick = () => done(false);
    dlg.oncancel = () => resolve(false);
    dlg.showModal(); $("#applyLater").focus();       // the safe choice has focus
  });
}
async function handleLive(live) {
  if (!live || !live.changed) return;
  const where = `${live.network} (${live.adapter}, ${live.ip})`;
  if (live.applied && live.applied.length) {
    flash(`Applied to ${live.adapter} now: ${live.applied.join(" · ")}. Address ${live.ip} unchanged, connections kept` +
      (live.internet != null ? ` · ${inetInfo(live.internet).text}` : ""), live.internet === 2 ? "warn" : "info");
  }
  if (live.ask === "confirm") {
    const ok = await askApply({ title: "Apply these changes now?",
      msg: `You're on ${where}. This save changes settings that are in use: ${live.changes.join("; ")}.`,
      risk: "The address stays and devices on this network aren't affected. Anything going out through the gateway (an internet download, a remote session) may stop, and web pages and other name lookups use the new DNS. Choose Later to keep everything as it is until you apply or Join again.",
      now: "Apply now" });
    if (ok) {
      try { const r = await api("/apply-changes", { method: "POST", body: { confirm: true } }); await handleLive(r); }
      catch (e) { flash(e.message, "error"); }
    } else flash(`Saved. Nothing on ${live.adapter} has changed – apply it from the note under the bar, or Join ${live.network} again.`);
  } else if (live.ask === "rejoin") {
    const ok = await askApply({ title: "Rejoin with the new settings?",
      msg: `You're on ${where}, and ${live.reason}, so the laptop needs a new address.`,
      risk: "Rejoining drops the connection for a few seconds – a download, sync or upload to a controller may fail. Choose Later to keep the current address until you're ready, then press Join.",
      now: "Rejoin now" });
    if (ok) await joinNetwork(live.network_id, null, null);
    else flash(`Saved. ${live.adapter} stays on ${live.ip} until you Join ${live.network} again.`, "warn");
  }
  await refreshStatus(); refreshLog();
  if (state.selected === live.network_id || state.selected === "home") refreshDetail();
}

// ---------- device page (v0.10): read / observe / connect – nothing is written to the device ----------
state.devTab = "overview";
state.devProps = {};            // device id → last BACnet properties read
const PROP_LABEL = { "object-name": "Name", "vendor-name": "Vendor", "vendor-identifier": "Vendor ID", "model-name": "Model",
  "firmware-revision": "Firmware", "application-software-version": "Application", "protocol-version": "BACnet protocol version",
  "protocol-revision": "BACnet protocol revision", location: "Location", description: "Description", "system-status": "System status",
  "max-apdu-length-accepted": "Max APDU", "segmentation-supported": "Segmentation", "database-revision": "Database revision",
  "local-date": "Device date", "local-time": "Device time", "utc-offset": "UTC offset", "serial-number": "Serial number" };
const webUrlFor = (ip, p) => p === 443 ? `https://${ip}/` : p === 8080 ? `http://${ip}:8080/` : `http://${ip}/`;
const devPorts = (x) => (x.ports || "").split(",").filter(Boolean).map(Number);
function devStatus(x) {
  if (x.pending) return { text: "On the imported list – not checked yet", cls: "muted" };
  if (x.inactive) return { text: x.last_seen ? goneText(x.last_seen) : "Missing – on the list, never seen", cls: "gone" };
  return { text: x.last_seen ? `Online – seen ${when(x.last_seen)}` : "Unknown", cls: "ok" };
}

async function renderDevicePage() {
  const { id, net } = devSel();
  let r;
  try { r = await api(`/device/${id}?network_id=${net}`); }
  catch (e) { flash(e.message, "warn"); state.selected = findNode(net) ? net : "home"; saveUi(); renderTree(); return refreshDetail(); }
  if (!isDevSel() || devSel().id !== id) return;                 // moved on meanwhile
  state.device = r; const d = r.device;
  const title = devLabel(d);
  const crumbs = crumbBar({ crumbs: r.crumbs, id: state.selected, name: title, kind: "device" });
  const web = devPorts(d).find((p) => [443, 80, 8080].includes(p));
  setHead(crumbs, title, [
    web ? el("a", { class: "btn", href: webUrlFor(d.ip, web), target: "_blank", rel: "noopener noreferrer", title: `Open ${webUrlFor(d.ip, web)}` }, "Web UI ↗") : null,
    el("button", { type: "button", class: "btn", onclick: () => { state.devTab = "ping"; renderDeviceTabs(); if (!state.ping || !state.ping.running) startPing(); } }, "Ping"),
  ]);
  const st = devStatus(d);
  const tile = (k, v, from) => el("div", { class: "tile" }, el("span", { class: "k" }, k), el("span", { class: "v" }, v), from ? el("span", { class: "from" }, from) : null);
  $("#detail").replaceChildren(el("div", { class: "tiles" },
    tile("IP", d.ip, d.route ? `behind router · ${d.route}` : null),
    tile("MAC", d.mac || "—", d.mac_vendor || null),
    d.bacnet_instance != null ? tile("BACnet", `#${d.bacnet_instance}`, d.vendor || null) : tile("Vendor", d.vendor || d.mac_vendor || "—"),
    d.model ? tile("Model", d.model) : null,
    tile("Status", el("span", { class: st.cls }, d.inactive ? "Missing" : d.pending ? "Not checked" : "Online"),
      d.inactive ? (d.last_seen ? `last seen ${when(d.last_seen)}` : "never seen") : d.pending ? "on the imported list" : d.last_seen ? `seen ${when(d.last_seen)}` : null)));
  renderDeviceTabs();
}

function renderDeviceTabs() {
  const r = state.device; if (!r) return;
  const d = r.device;
  const tabs = [["overview", "Overview"], ["ping", "Ping"], ...(d.bacnet_instance != null ? [["bacnet", "BACnet"], ["objects", "Objects"]] : []), ["ports", "Ports"]];
  if (!tabs.some(([k]) => k === state.devTab)) state.devTab = "overview";
  const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Device" }, tabs.map(([k, label]) =>
    el("button", { type: "button", role: "tab", class: "tab", id: `tab-${k}`, "aria-selected": String(state.devTab === k), "aria-controls": "devPanel",
      onclick: () => { state.devTab = k; renderDeviceTabs(); } }, label,
      k === "ping" && state.ping && state.ping.running ? el("span", { class: "live-dot", title: "Pinging" }) : null)));
  const panel = el("div", { class: "tab-panel", id: "devPanel", role: "tabpanel", "aria-labelledby": `tab-${state.devTab}` });
  $("#deviceView").replaceChildren(bar, panel);
  ({ overview: renderOverviewTab, ping: renderPingTab, bacnet: renderBacnetTab, objects: renderObjectsTab, ports: renderPortsTab })[state.devTab](panel, r);
}

function renderOverviewTab(panel, r) {
  const d = r.device; const st = devStatus(d);
  const kv = (k, v) => v == null || v === "" ? [] : [el("dt", {}, k), el("dd", {}, v)];
  const netLinks = r.networks.map((n, i) => [i ? " / " : null, el("button", { type: "button", class: "netlink", onclick: () => goTo(n.id) }, n.path)]).flat();
  const notes = el("textarea", { rows: 4, placeholder: "Anything worth knowing next time – where it is, quirks, passwords to ask for…", "aria-label": "Notes" });
  notes.value = d.notes || "";
  const save = el("button", { type: "button", class: "btn small", disabled: true, onclick: async (e) => {
    await withBusy(e.currentTarget, async () => { await api(`/device/${d.id}/notes`, { method: "PUT", body: { notes: notes.value } }); d.notes = notes.value.trim(); flash("Notes saved"); });
    save.disabled = true; } }, "Save notes");
  notes.addEventListener("input", () => { save.disabled = notes.value.trim() === (d.notes || ""); });
  panel.append(el("div", { class: "ov-grid" },
    el("dl", { class: "kv" },
      ...kv("Status", el("span", { class: st.cls }, st.text)),
      ...kv("Found by", (d.sources || "").split(",").filter((s) => s && s !== "self").join(" · ") || null),
      ...kv("Response", d.ping_ms != null ? `${d.ping_ms} ms (last scan)` : null),
      ...kv("First seen", d.first_seen ? when(d.first_seen) : null),
      ...kv("Last seen", d.last_seen ? when(d.last_seen) : null),
      ...kv("Network", netLinks.length ? netLinks : d.subnet),
      ...kv("Behind router", d.route ? `${d.ip} · ${d.route}` : null),
      ...kv("Firmware", d.firmware ? `${d.firmware}${d.app_version ? ` · app ${d.app_version}` : ""}` : null),
      ...kv("On imported list", d.expected ? "Yes" : null),
      ...kv("Also seen at", r.also_seen.length ? r.also_seen.map((a, i) => [i ? ", " : null,
        a.network_id ? el("button", { type: "button", class: "netlink", onclick: () => openDevice(a.id, a.network_id) }, `${a.ip} (${a.network})`) : `${a.ip} (${a.network})`]).flat() : null)),
    el("div", { class: "notes-box" }, el("label", { class: "small muted" }, "Notes"), notes, el("div", { class: "row" }, el("div", { class: "spacer" }), save))));
}

// ---- live ping: runs while this device's page is open (stops when you leave, or the window is hidden a while)
function stopPing() {
  const p = state.ping; if (!p) return;
  clearTimeout(p.timer); p.running = false;
  if (p.session) api(`/ping/${p.session}/stop`, { method: "POST" }).catch(() => {});
  state.ping = null;
}
async function startPing() {
  stopPing();
  const d = state.device && state.device.device; if (!d) return;
  const p = state.ping = { deviceId: d.id, ip: d.ip, session: null, results: [], running: true, timer: null, beep: !!state.pingBeep, down: false, events: [] };
  try { p.session = (await api("/ping", { method: "POST", body: { device_id: d.id } })).session; }
  catch (e) { flash(e.message, "error"); state.ping = null; renderDeviceTabs(); return; }
  renderDeviceTabs(); pollPing();
}
async function pollPing() {
  const p = state.ping; if (!p || !p.running) return;
  if (document.visibilityState === "visible") {
    try {
      const r = await api(`/ping/${p.session}?after=${p.results.length}`);
      if (state.ping !== p) return;
      for (const x of r.results) pingSample(p, x);
      if (!r.running) { p.running = false; p.stopped = r.stopped; }
    } catch (e) { p.running = false; p.stopped = "finished"; }
    if (state.devTab === "ping") updatePingPanel(); else if (!p.running) renderDeviceTabs();
  }
  if (p.running) p.timer = setTimeout(pollPing, 1000);
}
// Two lost replies in a row = dropped out; the first reply after that = back
function pingSample(p, x) {
  p.results.push(x);
  const n = p.results.length;
  if (!p.down && x.ms == null && n >= 2 && p.results[n - 2].ms == null) {
    p.down = true; p.events.push({ t: p.results[n - 2].t, kind: "down" }); if (p.beep) beep(330);
  } else if (p.down && x.ms != null) {
    const since = p.events.length ? p.events[p.events.length - 1].t : x.t;
    p.down = false; p.events.push({ t: x.t, kind: "up", after: Math.round((x.t - since) / 1000) }); if (p.beep) beep(880);
  }
}
function beep(freq) {
  try {
    const ctx = state.audio || (state.audio = new (window.AudioContext || window.webkitAudioContext)());
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = freq; g.gain.value = 0.08; o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.18);
  } catch (e) {}
}
function renderPingTab(panel, r) {
  const d = r.device; const p = state.ping && state.ping.deviceId === d.id ? state.ping : null;
  const startStop = el("button", { type: "button", class: "btn " + (p && p.running ? "" : "btn-primary"), id: "pingBtn",
    onclick: () => { if (state.ping && state.ping.running) { stopPing(); renderDeviceTabs(); } else startPing(); } }, p && p.running ? "Stop" : "Start ping");
  const beepBox = el("label", { class: "check small" }, el("input", { type: "checkbox", checked: !!state.pingBeep,
    onchange: (e) => { state.pingBeep = e.target.checked; if (state.ping) state.ping.beep = e.target.checked; if (e.target.checked) beep(660); } }),
    " Beep when it drops out or comes back");
  panel.append(
    el("div", { class: "row ping-ctl" }, startStop, beepBox, el("div", { class: "spacer" }),
      el("span", { class: "small muted" }, `Pings ${d.ip} every second${r.on_subnet ? "" : " (the laptop isn't on this subnet – replies come through the gateway, if at all)"}`)),
    el("div", { id: "pingStats", class: "ping-stats" }),
    el("div", { id: "pingChart", class: "ping-chart" }),
    el("div", { id: "pingEvents", class: "ping-events" }));
  updatePingPanel();
}
function updatePingPanel() {
  const box = $("#pingStats"); if (!box) return;
  const p = state.ping && state.device && state.ping.deviceId === state.device.device.id ? state.ping : null;
  const btn = $("#pingBtn");
  if (btn) { btn.textContent = p && p.running ? "Stop" : "Start ping"; btn.className = "btn " + (p && p.running ? "" : "btn-primary"); }
  const tabDot = document.querySelector("#tab-ping .live-dot");
  if (!!tabDot !== !!(p && p.running)) { renderDeviceTabs(); return; }
  if (!p || !p.results.length) {
    box.replaceChildren(el("p", { class: "muted" }, p && p.running ? "Starting…" : p && p.stopped ? `Stopped (${p.stopped}).` : "Not running."));
    $("#pingChart").replaceChildren(); $("#pingEvents").replaceChildren(); return;
  }
  const res = p.results; const ok = res.filter((x) => x.ms != null).map((x) => x.ms);
  const last = res[res.length - 1];
  const lost = res.length - ok.length;
  const stat = (k, v, cls) => el("div", { class: "pstat" }, el("span", { class: "k" }, k), el("span", { class: "v " + (cls || "") }, v));
  const downFor = p.down && p.events.length ? Math.round((Date.now() - p.events[p.events.length - 1].t) / 1000) : 0;
  box.replaceChildren(
    stat("Now", p.down ? `Not replying (${downFor} s)` : last.ms == null ? "No reply" : "Replying", p.down || last.ms == null ? "gone" : "ok"),
    stat("Last", last.ms == null ? "—" : `${last.ms} ms`),
    stat("Min / avg / max", ok.length ? `${Math.min(...ok)} / ${(ok.reduce((a, b) => a + b, 0) / ok.length).toFixed(1)} / ${Math.max(...ok)} ms` : "—"),
    stat("Sent", String(res.length)),
    stat("Lost", `${lost} (${Math.round((lost / res.length) * 100)}%)`, lost ? "gone" : ""),
    ...(p.running ? [] : [stat("State", `Stopped${p.stopped ? ` – ${p.stopped}` : ""}`)]));
  // Last two minutes: one bar per ping, height = reply time; lost pings shown as a full-height marker
  const win = res.slice(-120); const W = 600, H = 64; const max = Math.max(5, ...win.map((x) => x.ms || 0)); const bw = W / 120;
  const ns = "http://www.w3.org/2000/svg"; const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Reply times for the last ${win.length} pings, up to ${max} ms`);
  win.forEach((x, i) => {
    const r = document.createElementNS(ns, "rect"); const xpos = (120 - win.length + i) * bw;
    const h = x.ms == null ? H : Math.max(2, (x.ms / max) * (H - 4));
    r.setAttribute("x", (xpos + 0.5).toFixed(1)); r.setAttribute("width", Math.max(1, bw - 1).toFixed(1));
    r.setAttribute("y", (H - h).toFixed(1)); r.setAttribute("height", h.toFixed(1)); r.setAttribute("rx", "1");
    r.setAttribute("class", x.ms == null ? "lost" : "rtt");
    const t = document.createElementNS(ns, "title"); t.textContent = `${new Date(x.t).toLocaleTimeString()} – ${x.ms == null ? "no reply" : x.ms + " ms"}`; r.append(t);
    svg.append(r);
  });
  $("#pingChart").replaceChildren(svg, el("div", { class: "small muted chart-cap" }, el("span", {}, "Last 2 minutes"), el("span", {}, `scale 0–${max} ms · red = no reply`)));
  $("#pingEvents").replaceChildren(...(p.events.length ? [el("h3", {}, "Drop-outs")] : []), ...p.events.slice(-12).reverse().map((e) =>
    el("div", { class: "pev " + e.kind }, el("span", { class: "mono" }, new Date(e.t).toLocaleTimeString()),
      e.kind === "down" ? " Stopped replying" : ` Replying again (down ${e.after} s)`)));
}

// ---- BACnet device properties (read when the tab is opened; nothing is written)
function renderBacnetTab(panel, r) {
  const d = r.device; const cached = state.devProps[d.id];
  const read = el("button", { type: "button", class: "btn small", onclick: (e) => readDeviceProps(e.currentTarget) }, cached ? "Read again" : "Read properties");
  panel.append(el("div", { class: "row" }, el("span", { class: "small muted" },
    `Device #${d.bacnet_instance} at ${d.ip}${d.route ? ` (${d.route})` : ""}` + (cached ? ` · read ${when(cached.read_at)}` : "")),
    el("div", { class: "spacer" }), read), el("div", { id: "bacProps" }));
  if (!r.on_subnet) {
    $("#bacProps").replaceChildren(el("p", { class: "muted" }, `The laptop isn't on ${d.subnet}. `,
      r.network ? el("button", { type: "button", class: "netlink", onclick: () => goTo(r.network.id) }, `Join ${r.network.name}`) : null, " to read from this device."));
    read.disabled = true; return;
  }
  if (cached) showDeviceProps(cached); else readDeviceProps(read);
}
async function readDeviceProps(btn) {
  const d = state.device.device; const box = $("#bacProps");
  if (box) box.replaceChildren(el("p", { class: "muted" }, "Reading…"));
  await withBusy(btn, async () => {
    const r = await api(`/device/${d.id}/bacnet`, { method: "POST" });
    state.devProps[d.id] = r;
    if (state.device && state.device.device.id === d.id && state.devTab === "bacnet") { renderDeviceTabs(); }
  });
  if (!state.devProps[d.id] && box) box.replaceChildren(el("p", { class: "gone" }, "No answer – see the Activity log."));
}
function showDeviceProps(p) {
  const box = $("#bacProps"); if (!box) return;
  const c = p.clock;
  const clock = c ? el("p", { class: Math.abs(c.diff_s) > 120 ? "pending-note" : "small muted" },
    Math.abs(c.diff_s) <= 120 ? `Device clock matches this laptop (${c.diff_s >= 0 ? "+" : ""}${c.diff_s} s).`
      : `Device clock is ${fmtDur(Math.abs(c.diff_s))} ${c.diff_s > 0 ? "ahead of" : "behind"} this laptop (device ${c.device}, laptop ${c.laptop}).`) : null;
  box.replaceChildren(...(clock ? [clock] : []), el("table", { class: "props" }, el("tbody", {}, p.rows.map((x) => el("tr", {},
    el("th", { scope: "row" }, PROP_LABEL[x.prop] || x.prop),
    el("td", { class: x.value == null ? "muted" : null }, x.value == null ? `— (${x.note})` : x.value))))));
}
function fmtDur(s) { const m = Math.floor(s / 60), r = s % 60; return m ? `${m} min${r ? ` ${r} s` : ""}` : `${r} s`; }

// ---- object list (v0.11): every object with value, units, flags, priority – filter, refresh, CSV. Read-only.
state.objects = {};           // device id → { read_at, objects, partial, seconds }
state.objJob = null;
state.objView = { q: "", types: new Set(), flag: "all", sort: { key: "obj", dir: 1 }, auto: false };
const OBJ_FLAGS = [["all", "All"], ["commanded", "Commanded"], ["overridden", "Overridden"], ["oos", "Out of service"], ["alarm", "In alarm"], ["fault", "Fault"]];
const objFlag = (o, f) => f === "all" || (f === "commanded" ? o.commanded : f === "fault" ? (o.flags.fault || !!o.reliability) : o.flags[f]);
function renderObjectsTab(panel, r) {
  const d = r.device; const cached = state.objects[d.id];
  panel.append(el("div", { id: "objBox", class: "obj-box" }));
  if (!r.on_subnet) {
    $("#objBox").replaceChildren(el("p", { class: "muted" }, `The laptop isn't on ${d.subnet}. `,
      r.network ? el("button", { type: "button", class: "netlink", onclick: () => goTo(r.network.id) }, `Join ${r.network.name}`) : null, " to read this device's objects."));
    return;
  }
  if (cached) return drawObjects();
  if (state.objJob && state.objJob.device_id === d.id && !state.objJob.finished) return drawObjJob();
  // Maybe the server already has a list from earlier this session
  api(`/device/${d.id}/objects`).then((res) => {
    if (res && res.objects) { state.objects[d.id] = res; if (state.devTab === "objects" && state.device && state.device.device.id === d.id) drawObjects(); }
    else drawObjStart();
  }).catch(drawObjStart);
}
function drawObjStart() {
  const box = $("#objBox"); if (!box) return;
  box.replaceChildren(el("p", {}, "Reads every object in this controller – name, present value, units, status flags and priority array. Nothing is written."),
    el("p", { class: "small muted" }, "A room controller takes a second or two; a big automation station can take a minute."),
    el("div", {}, el("button", { type: "button", class: "btn btn-primary", onclick: startObjects }, "Read object list")));
}
async function startObjects() {
  const d = state.device.device;
  try { state.objJob = await api(`/device/${d.id}/objects`, { method: "POST" }); } catch (e) { flash(e.message, "error"); return; }
  drawObjJob(); pollObjJob();
}
async function pollObjJob() {
  const j = state.objJob; if (!j || j.finished) return;
  try { Object.assign(j, await api(`/jobs/${j.job}`)); } catch (e) { j.finished = true; j.error = e.message; }
  if (j.finished) {
    if (!j.error) { const res = await api(`/device/${j.device_id}/objects`); if (res.objects) state.objects[j.device_id] = res; }
    else flash(j.error, "error");
    if (state.device && state.device.device.id === j.device_id && state.devTab === "objects") { j.error ? drawObjStart() : drawObjects(); }
    state.objJob = null; refreshLog(); return;
  }
  if (state.device && state.device.device.id === j.device_id && state.devTab === "objects") drawObjJob();
  setTimeout(pollObjJob, 400);
}
function drawObjJob() {
  const box = $("#objBox"); const j = state.objJob; if (!box || !j) return;
  const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
  box.replaceChildren(el("div", { class: "progress" },
    el("div", { class: "row" }, el("span", { class: "small" }, j.phase === "object list" ? `Reading the object list… ${j.total ? `${j.done} / ${j.total}` : ""}`
      : `Reading objects… ${j.done} / ${j.total}`), el("div", { class: "spacer" }),
      el("button", { type: "button", class: "btn small", onclick: () => api(`/jobs/${j.job}/cancel`, { method: "POST" }) }, "Cancel")),
    el("div", { class: "bar" }, el("div", { style: `width:${pct}%` }))));
}
function visibleObjects(list) {
  const v = state.objView; const q = v.q.trim().toLowerCase();
  const rows = list.filter((o) => (!v.types.size || v.types.has(o.short)) && objFlag(o, v.flag) && (!q ||
    [o.name, o.description, `${o.short} ${o.inst}`, `${o.short}${o.inst}`, o.type_name, o.value].some((t) => t && String(t).toLowerCase().includes(q))));
  const k = v.sort.key, dir = v.sort.dir;
  const key = (o) => k === "name" ? [o.name.toLowerCase()] : k === "value" ? [isNaN(parseFloat(o.value)) ? 1 : 0, parseFloat(o.value) || 0, o.value] : [o.type, o.inst];
  return rows.sort((a, b) => { const x = key(a), y = key(b); for (let i = 0; i < x.length; i++) { if (x[i] < y[i]) return -dir; if (x[i] > y[i]) return dir; } return 0; });
}
function drawObjects() {
  const box = $("#objBox"); const d = state.device && state.device.device; if (!box || !d) return;
  const res = state.objects[d.id]; const all = res.objects; const v = state.objView;
  const types = {}; for (const o of all) types[o.short] = (types[o.short] || 0) + 1;
  for (const t of [...v.types]) if (!types[t]) v.types.delete(t);
  const search = el("input", { type: "search", placeholder: "Search name, description, AV 3…", "aria-label": "Search objects", value: v.q, class: "obj-search" });
  search.addEventListener("input", () => { v.q = search.value; drawObjTable(); });
  const typeMenu = el("details", { class: "menu vendor-menu" + (v.types.size ? " active" : ""), id: "objTypes" },
    el("summary", { class: "btn small" }, v.types.size ? [...v.types].join(", ") : "All types", " ▾"),
    el("div", { class: "menu-list vendor-list" }, el("div", { class: "row vendor-acts" },
      el("button", { type: "button", class: "linkish", onclick: () => { v.types.clear(); drawObjects(); } }, "All types")),
      ...Object.keys(types).sort((a, b) => all.find((o) => o.short === a).type - all.find((o) => o.short === b).type).map((t) =>
        el("label", { class: "check vend" }, el("input", { type: "checkbox", checked: v.types.has(t),
          onchange: (e) => { e.target.checked ? v.types.add(t) : v.types.delete(t); drawObjects(); } }),
          el("span", { class: "vn" }, `${t} – ${all.find((o) => o.short === t).type_name}`), el("span", { class: "n" }, String(types[t]))))));
  const chips = el("div", { class: "chips" }, OBJ_FLAGS.map(([k, label]) => {
    const n = k === "all" ? all.length : all.filter((o) => objFlag(o, k)).length;
    if (k !== "all" && !n && v.flag !== k) return null;
    return el("button", { type: "button", class: "chip" + (v.flag === k ? " on" : "") + (k !== "all" && n ? " flagged" : ""), "aria-pressed": String(v.flag === k),
      onclick: () => { v.flag = k; drawObjects(); } }, label, k === "all" ? "" : el("span", { class: "n" }, String(n)));
  }));
  const auto = el("label", { class: "check small" }, el("input", { type: "checkbox", checked: v.auto, onchange: (e) => { v.auto = e.target.checked; if (v.auto) autoRefreshObjects(); } }), " Auto-refresh (10 s)");
  box.replaceChildren(
    el("div", { class: "obj-tools" }, search, typeMenu, chips, el("div", { class: "spacer" }), auto,
      el("button", { type: "button", class: "btn small", title: "Re-read present values and flags of the objects shown", onclick: (e) => refreshObjects(e.currentTarget) }, "Refresh values"),
      el("button", { type: "button", class: "btn small", onclick: exportObjects }, "Export CSV"),
      el("button", { type: "button", class: "btn small", title: "Read the whole object list again", onclick: () => { delete state.objects[d.id]; startObjects(); } }, "Read again")),
    el("div", { class: "table-wrap obj-wrap", id: "objTable" }),
    el("div", { class: "row small muted", id: "objFoot" }));
  drawObjTable();
}
function drawObjTable() {
  const d = state.device && state.device.device; const res = d && state.objects[d.id]; const box = $("#objTable"); if (!box || !res) return;
  const rows = visibleObjects(res.objects); const v = state.objView;
  const th = (label, key) => el("th", { "aria-sort": key && v.sort.key === key ? (v.sort.dir > 0 ? "ascending" : "descending") : null },
    key ? el("button", { type: "button", onclick: () => { v.sort = v.sort.key === key ? { key, dir: -v.sort.dir } : { key, dir: 1 }; drawObjTable(); } }, label) : label);
  const badge = (cls, text, title) => el("span", { class: "obadge " + cls, title }, text);
  const tr = (o) => {
    const top = o.priority[0];
    return el("tr", { class: (o.flags.oos ? "oos " : "") + (o.flags.alarm || o.flags.fault || o.reliability ? "alarm" : "") },
      el("td", { class: "mono", "data-label": "Object", title: o.type_name }, `${o.short} ${o.inst}`),
      el("td", { class: "mono oname", "data-label": "Name" }, o.name || (o.error ? `(${o.error})` : "")),
      el("td", { class: "small", "data-label": "Description" }, o.description),
      el("td", { class: "oval", "data-label": "Value" }, o.value, o.units ? el("span", { class: "unit" }, ` ${o.units}`) : null),
      el("td", { "data-label": "Flags" },
        o.flags.alarm ? badge("alarm", "Alarm", o.event_state ? `Event state: ${o.event_state}` : null) : null,
        o.flags.fault || o.reliability ? badge("fault", "Fault", o.reliability ? `Reliability: ${o.reliability}` : null) : null,
        o.flags.overridden ? badge("ovr", "Overridden", "Overridden locally (e.g. a hand switch)") : null,
        o.flags.oos ? badge("oos", "Out of service", null) : null),
      el("td", { class: "small", "data-label": "Priority", title: o.priority.length ? o.priority.map((p) => `${p.level}: ${p.value}`).join("\n") + (o.relinquish_default != null ? `\nRelinquish default: ${o.relinquish_default}` : "") : (o.relinquish_default != null ? `Relinquish default: ${o.relinquish_default}` : null) },
        top ? [el("span", { class: "mono prio" + (top.level <= 8 ? " manual" : "") }, `@${top.level}`), ` ${top.value}`, o.priority.length > 1 ? el("span", { class: "sub2" }, `+${o.priority.length - 1} more`) : null] : ""));
  };
  box.replaceChildren(rows.length ? el("table", { class: "dev objs" }, el("thead", {}, el("tr", {},
    th("Object", "obj"), th("Name", "name"), th("Description"), th("Value", "value"), th("Flags"), th("Priority"))), el("tbody", {}, rows.map(tr)))
    : el("div", { class: "empty" }, "No objects match the search / filters."));
  $("#objFoot").replaceChildren(el("span", {}, rows.length !== res.objects.length ? `Showing ${rows.length} of ${res.objects.length} objects` : `${res.objects.length} objects`),
    el("span", {}, `read ${when(res.read_at)}${res.seconds != null ? ` in ${res.seconds} s` : ""}${res.refreshed_at ? ` · values ${when(res.refreshed_at)}` : ""}${res.partial ? " · cancelled part-way" : ""}`));
}
async function refreshObjects(btn) {
  const d = state.device && state.device.device; const res = d && state.objects[d.id]; if (!res) return;
  const which = visibleObjects(res.objects).slice(0, 400).map((o) => [o.type, o.inst]);
  const go = async () => {
    const r = await api(`/device/${d.id}/objects/refresh`, { method: "POST", body: { which } });
    const by = Object.fromEntries(r.objects.map((o) => [`${o.type}:${o.inst}`, o]));
    res.objects = res.objects.map((o) => by[`${o.type}:${o.inst}`] || o); res.refreshed_at = r.refreshed_at;
    if (state.devTab === "objects" && state.device && state.device.device.id === d.id) drawObjTable();
  };
  try { btn ? await withBusy(btn, go) : await go(); } catch (e) { state.objView.auto = false; flash(e.message, "error"); }
}
// Auto-refresh only while the Objects tab of this device is open and the window is visible
function autoRefreshObjects() {
  clearTimeout(state.objAutoTimer);
  const v = state.objView; if (!v.auto) return;
  state.objAutoTimer = setTimeout(async () => {
    const on = state.devTab === "objects" && isDevSel() && state.device && state.objects[state.device.device.id];
    if (!on) { v.auto = false; return; }
    if (document.visibilityState === "visible") await refreshObjects(null);
    autoRefreshObjects();
  }, 10000);
}
function exportObjects() {
  const d = state.device.device; const rows = visibleObjects(state.objects[d.id].objects);
  const esc = (v) => { const t = v == null ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const head = ["Device", "Instance", "Object type", "Object", "Name", "Description", "Value", "Units", "In alarm", "Fault", "Overridden", "Out of service", "Reliability", "Priority array", "Relinquish default"];
  const lines = [head.join(",")].concat(rows.map((o) => [devLabel(d), d.bacnet_instance, o.type_name, `${o.short} ${o.inst}`, o.name, o.description, o.value, o.units,
    o.flags.alarm ? "yes" : "", o.flags.fault ? "yes" : "", o.flags.overridden ? "yes" : "", o.flags.oos ? "yes" : "", o.reliability || "",
    o.priority.map((p) => `${p.level}:${p.value}`).join(" "), o.relinquish_default ?? ""].map(esc).join(",")));
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `NetJoin-objects-${devLabel(d).replace(/[^\w.-]+/g, "_")}-${new Date().toISOString().slice(0, 10)}.csv` });
  document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  flash(`Exported ${rows.length} object(s) to CSV`);
}

// ---- ports
function renderPortsTab(panel, r) {
  const d = r.device; const ports = devPorts(d);
  const kind = { 80: "web page", 443: "web page (secure)", 8080: "web page", 502: "Modbus TCP", 1911: "Niagara Fox (Workbench)", 4911: "Niagara Fox TLS (Workbench)", 22: "SSH", 23: "Telnet" };
  const copy = (text) => el("button", { type: "button", class: "btn small", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(text); flash(`Copied: ${text}`); } }, "Copy");
  const rows = ports.map((p) => el("tr", {}, el("td", { class: "mono" }, String(p)), el("td", {}, portLabel[p] || p), el("td", {}, kind[p] || ""),
    el("td", {}, [80, 443, 8080].includes(p) ? el("a", { href: webUrlFor(d.ip, p), target: "_blank", rel: "noopener noreferrer" }, `${webUrlFor(d.ip, p)} ↗`)
      : p === 22 ? [el("span", { class: "mono" }, `ssh ${d.ip}`), " ", copy(`ssh ${d.ip}`)]
      : p === 23 ? [el("span", { class: "mono" }, `telnet ${d.ip}`), " ", copy(`telnet ${d.ip}`)]
      : el("span", { class: "mono" }, `${d.ip}:${p}`))));
  panel.append(el("div", { class: "row" }, el("span", { class: "small muted" }, "Checks the common BMS ports: 80, 443, 8080, 502, 1911, 4911, 22, 23."),
    el("div", { class: "spacer" }),
    el("button", { type: "button", class: "btn small", disabled: !r.on_subnet, title: r.on_subnet ? null : "Join this network first",
      onclick: (e) => withBusy(e.currentTarget, async () => { const res = await api(`/device/${d.id}/ports`, { method: "POST" });
        d.ports = res.ports.join(","); flash(`${res.ports.length} open port(s)`); renderDevicePage(); }) }, "Check ports now")),
    ports.length ? el("table", { class: "props" }, el("tbody", {}, rows)) : el("p", { class: "muted" }, "No open ports found (or not checked yet)."));
}

// ---------- page head: crumbs, title and page buttons live in the top bar ----------
function setHead(crumbs, title, actions) {
  $("#tbCrumbs").replaceChildren(crumbs);       // the last crumb is the page title
  // Revert to DHCP lives at the end of the page buttons (same element every time, so its handler stays)
  $("#tbActions").replaceChildren(...actions.filter(Boolean), $("#dhcpBtn"));
  document.title = title === "Home" ? "NetJoin" : `${title} – NetJoin`;
  fitCrumbs();
}
// Keep the trail on one line: fold levels from the top (Home, then the site…) into "…" until it fits.
// The current page is never folded; if it still doesn't fit, its name is cut short with an ellipsis.
function fitCrumbs() {
  const nav = $("#tbCrumbs .crumbs"); if (!nav) return;
  const groups = [...nav.querySelectorAll(":scope > .cgrp")]; const more = nav.querySelector(":scope > .crumb-more");
  const bar = $("#topbar"); bar.classList.remove("tight");     // try the one-line bar first
  const fold = () => {
    groups.forEach((g) => { g.hidden = false; }); more.hidden = true;
    nav.classList.add("measuring");             // title at full width while deciding what to fold
    for (let i = 0; i < groups.length - 1 && nav.scrollWidth > nav.clientWidth + 1; i++) { groups[i].hidden = true; more.hidden = false; }
    nav.classList.remove("measuring");
  };
  fold();
  // Still no room for the page title itself (lots of page buttons on a narrow screen)? Give the
  // breadcrumbs their own row rather than cutting the title down to a couple of letters.
  const here = nav.querySelector(".crumb-here");
  if (here && here.scrollWidth > here.clientWidth + 1 && here.clientWidth < 160) { bar.classList.add("tight"); fold(); }
}
if (window.ResizeObserver) new ResizeObserver(() => fitCrumbs()).observe($("#tbCrumbs"));

// ---------- detail ----------
async function refreshDetail() {
  const box = $("#detail");
  if (!isDevSel() || (state.ping && state.ping.deviceId !== devSel().id)) stopPing();
  if (!isDevSel() || (state.device && state.device.device.id !== devSel().id)) { state.objView.auto = false; clearTimeout(state.objAutoTimer); }
  if (isDevSel()) {
    document.body.classList.remove("at-home"); document.body.classList.add("at-device");
    state.detail = null;
    await renderDevicePage(); return;
  }
  document.body.classList.remove("at-device");
  if (state.selected === "home" || state.selected == null || !findNode(state.selected)) {
    state.selected = "home"; state.detail = null;
    document.body.classList.add("at-home");
    await renderHome(); return;
  }
  document.body.classList.remove("at-home");
  const d = state.detail = await api(`/nodes/${state.selected}`);
  const crumbs = crumbBar(d);
  const editBtn = el("button", { type: "button", class: "btn", onclick: () => openNode({ node: d }) }, "Edit");
  if (d.kind !== "network") {
    setHead(crumbs, d.name, [d.kind === "site" ? exportMenu(d) : null, editBtn,
      el("button", { type: "button", class: "btn", onclick: () => openNode({ parent: d, kind: "area" }) }, "+ Area"),
      el("button", { type: "button", class: "btn btn-primary", onclick: () => openNode({ parent: d, kind: "network" }) }, "+ Network")]);
    // Counts line, then a compact Contents strip.
    box.replaceChildren(
      el("p", { class: "roll-stats", id: "rollStats" }, `${d.kind === "site" ? "Site" : "Area"} · pick a network inside it to join.`),
      el("div", { class: "contents", id: "contents", "aria-label": `Inside ${d.name}` }));
    renderContents(); renderHistory(); return;
  }
  const inetText = d.last_internet == null ? "Internet not checked yet"
    : { 1: "Had internet at last join", 2: "Internet by IP only (no DNS) at last join", 0: "No internet at last join" }[d.last_internet] || "No internet at last join";
  const inetPill = el("span", { class: "pill inet-pill" + (d.last_internet == null ? "" : inetInfo(d.last_internet).cls), title: inetText },
    globe(d.last_internet == null ? true : inetInfo(d.last_internet).off), el("span", { class: "pill-t" }, inetText));
  // Join shows where you are: blue "Join" when the laptop isn't on this network, outlined "Rejoin" once it is.
  const st0 = state.status || {};
  const here = !!(st0.joined && st0.joined.network_id === d.id);
  const joinBtn = el("button", { type: "button", class: here ? "btn join-btn joined" : "btn btn-primary join-btn",
    title: here ? `On ${d.name} as ${st0.joined.ip}/${st0.joined.prefix}. Rejoin picks the Join IP again.` : `Set ${st0.adapter ? st0.adapter.name : "the adapter"} to an address on ${d.name}`,
    onclick: (e) => doJoin(e.currentTarget) }, here ? [refreshIcon(), "Rejoin"] : "Join");
  const r = d.resolved;
  // Compact facts for the strip under the bar; the longer detail is in each one's tooltip.
  const tile = (k, v, from, tip) => el("div", { class: "tile", title: tip || null }, el("span", { class: "k" }, k), el("span", { class: "v" }, v), from ? el("span", { class: "from" }, from) : null);
  const nw = netFromCidr(d.subnet); const pfx = Number(d.subnet.split("/")[1]);
  setHead(crumbs, d.name, [el("div", { class: "pill-slot" }, inetPill), editBtn, joinBtn]);
  box.replaceChildren(
    el("div", { class: "tiles" },
      tile("Network", nw ? `${ip4.str(nw.net)}/${nw.prefix}` : d.subnet, nw ? nw.mask : null, nw ? `${netText(d.subnet)} · usable ${ip4.str(nw.first)} – ${ip4.str(nw.last)}` : null),
      tile("Gateway / DNS", `${r.gateway || "—"} / ${r.dns.length ? r.dns.join(", ") : "—"}`,
        r.gateway_from || r.dns_from ? `from ${[...new Set([r.gateway_from, r.dns_from].filter(Boolean))].join(" · ")}` : "none set"),
      // The address Join will try first, and how it was picked; the allowed range is in the tooltip.
      tile("Join IP", d.next_ip || (d.next_how === "random from the range" ? "random" : "none free"),
        d.addr_mode === "fixed" ? "static IP" : (d.next_how || null),
        d.addr_mode === "fixed" ? "Static IP – Join always uses this address"
          : `Join tries this address first. Allowed range ${shortIp(d.range_start, pfx)} – ${shortIp(d.range_end, pfx)} (${d.range_start} – ${d.range_end}), ${ALLOC_TEXT[d.alloc] || "first free"}.`)),
    ...(d.pending ? [el("div", { class: "pending" },
      el("span", {}, "⚠ Saved changes aren't on the adapter yet – " + (d.pending.rejoin ? `${d.pending.rejoin}; Join to use them.`
        : [d.pending.gateway ? `gateway ${d.pending.gateway.from || "none"} → ${d.pending.gateway.to || "none"}` : null,
           d.pending.dns ? `DNS ${d.pending.dns.from.join(", ") || "none"} → ${d.pending.dns.to.join(", ") || "none"}` : null].filter(Boolean).join(" · ") + ".")),
      el("button", { type: "button", class: "btn small", onclick: async () => {
        try { await handleLive(await api("/apply-changes", { method: "POST", body: {} })); } catch (e) { flash(e.message, "error"); } } }, "Apply…"))] : []),
    ...(d.notes ? [el("p", { class: "notes", title: d.notes }, "Notes: ", d.notes.replace(/\s*\n\s*/g, " · "))] : []));
  renderHistory();
}

const ALLOC_TEXT = { first: "first free", last: "last free", random: "random", sticky: "last used, then first" };
function netText(cidr) { const nw = netFromCidr(cidr); return nw ? `${ip4.str(nw.net)} · ${nw.mask} (/${nw.prefix})` : cidr; }

// History (top bar, every page): recent joins across all networks, newest first. Loaded when opened – no polling.
async function loadHistory() {
  const ul = $("#history"); let rows;
  try { rows = await api("/history?limit=20"); } catch (e) { ul.replaceChildren(el("li", { class: "empty" }, e.message)); return; }
  if (!rows.length) { ul.replaceChildren(el("li", { class: "empty" }, "No joins yet.")); return; }
  const here = state.detail && state.detail.kind === "network" ? state.detail.id : null;
  ul.replaceChildren(...rows.map((h) => el("li", { class: h.network_id === here ? "here" : null },
    el("button", { type: "button", title: `Join ${h.where ? h.where + " › " : ""}${h.network} with ${h.ip}`,
      onclick: (e) => { $("#histMenu").open = false; joinNetwork(h.network_id, h.ip, null); } },
      el("span", { class: "hnet" }, el("span", { class: "nm" }, h.network), h.where ? el("span", { class: "where" }, h.where) : null),
      el("span", { class: "ip" }, h.ip),
      h.result === "worked" && h.internet === 1 ? el("span", { class: "inet yes", title: "Had internet" }, globe(false)) : el("span", { class: "inet-gap" }),
      el("span", { class: "tag " + h.result }, h.result === "worked" ? "✓ Worked" : h.result === "clashed" ? "! Clashed" : "× Failed"),
      el("span", { class: "small muted when" }, when(h.ts))))));
}
$("#histMenu").addEventListener("toggle", (e) => { if (e.target.open) loadHistory(); });
document.addEventListener("click", (e) => { const m = $("#histMenu"); if (m && m.open && !m.contains(e.target)) m.open = false; });

// Join any network (History, Home), then show its page.
async function joinNetwork(id, ip, btn) {
  const name = (findNode(id) || {}).name || "network";
  const ok = await withBusy(btn, async () => {
    const r = await api("/join", { method: "POST", body: { network_id: id, ip } });
    flash(`Joined ${name} as ${r.ip}/${r.prefix}${r.attempts > 1 ? ` (after ${r.attempts - 1} clash${r.attempts > 2 ? "es" : ""})` : ""} · ${inetInfo(r.internet).text}`, r.internet === 2 ? "warn" : "info");
    return true;
  });
  await refreshStatus();
  if (ok && findNode(id)) { if (state.selected === id) await refreshDetail(); else goTo(id); }
  else await refreshDetail();
}

function goTo(id) {
  if (id === "home") return showHome();
  if (typeof id === "string" && id.startsWith("dev:")) { const [, d, net] = id.split(":"); return openDevice(Number(d), Number(net)); }
  const n = findNode(id); if (n) select(n);
}
async function showHome() { state.selected = "home"; saveUi(); renderTree(); await refreshDetail(); }

// ---------- Breadcrumbs: Home › Site ▾ › Area ▾ › Network ›▾ (Explorer style) ----------
// Each ▾ lists what's inside that level, so you can go sideways or forward without the Sites list.
const KIND_LABEL = { site: "Site", area: "Area", network: "Network" };
function crumbBar(d) {
  const nav = el("nav", { class: "crumbs", "aria-label": "Breadcrumb" });
  const chain = d ? [...d.crumbs, { id: d.id, name: d.name, kind: d.kind }] : [];
  const parts = [{ id: "home", name: "Home", kind: "home" }, ...chain];
  // "…" stands in for the first levels when the trail doesn't fit on one line (see fitCrumbs)
  const more = el("button", { type: "button", class: "crumb-more", hidden: true, "aria-label": "Show the hidden levels", title: "Hidden levels",
    "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => openCrumbMenu(e.currentTarget, parts.filter((c, i) => nav.children[i + 1] && nav.children[i + 1].hidden)
      .map((c) => c.id === "home" ? { id: "home", name: "Home", kind: "", children: state.tree } : (findNode(c.id) || c)), null) }, "…");
  nav.append(more, ...parts.map(() => el("span", { class: "cgrp" })));
  parts.forEach((c, i) => {
    const grp = nav.children[i + 1];
    const last = i === parts.length - 1;
    const kids = c.id === "home" ? state.tree : (findNode(c.id) || {}).children || [];
    const label = c.id === "home" ? [houseIcon(), "Home"] : c.name;
    grp.append(last
      ? el("h1", { class: "crumb crumb-here", "aria-current": "page" }, label)
      : el("button", { type: "button", class: "crumb", title: c.id === "home" ? "Go to Home" : `Go to ${c.kind} ${c.name}`,
          onclick: () => goTo(c.id) }, label));
    if (kids.length) {
      const next = parts[i + 1];
      grp.append(el("button", { type: "button", class: "crumb-menu" + (last ? " forward" : ""),
        "aria-haspopup": "menu", "aria-label": last ? `Go into ${c.name}` : `Other items in ${c.name}`,
        title: last ? `What's inside ${c.name}` : `Everything in ${c.id === "home" ? "Home" : c.name}`,
        "aria-expanded": "false",
        onclick: (e) => openCrumbMenu(e.currentTarget, kids, next ? next.id : null) }, el("span", { class: "chev", "aria-hidden": "true" }, "›")));
    } else if (!last) {
      grp.append(el("span", { class: "sep", "aria-hidden": "true" }, "›"));
    }
  });
  return nav;
}
function refreshIcon() {
  const ns = "http://www.w3.org/2000/svg"; const sv = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ width: 15, height: 15, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) sv.setAttribute(k, v);
  sv.innerHTML = '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>';
  return sv;
}
function houseIcon() {
  const ns = "http://www.w3.org/2000/svg"; const sv = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ width: 13, height: 13, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2.2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) sv.setAttribute(k, v);
  sv.innerHTML = '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>';
  return sv;
}
function openCrumbMenu(btn, items, currentId) {
  if (crumbMenuBtn === btn) { closeCrumbMenu(); return; }   // second click closes it
  closeCrumbMenu();
  const pop = el("div", { class: "crumb-pop", id: "crumbPop", role: "menu" },
    items.map((n) => el("button", { type: "button", role: "menuitem", class: n.id === currentId ? "here" : null,
      onclick: () => { closeCrumbMenu(); goTo(n.id); } },
      el("span", { class: "k" }, KIND_LABEL[n.kind] || ""), el("span", { class: "nm" }, n.name),
      n.kind === "network" ? el("span", { class: "sub mono" }, n.subnet || "") :
        el("span", { class: "sub" }, `${(n.children || []).length} inside`))));
  document.body.append(pop);
  const r = btn.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8))}px`;
  pop.style.top = `${r.bottom + 4}px`;
  pop.style.maxHeight = `${Math.max(160, window.innerHeight - r.bottom - 16)}px`;
  btn.setAttribute("aria-expanded", "true"); pop.dataset.for = "1"; crumbMenuBtn = btn;
  (pop.querySelector(".here") || pop.querySelector("button")).focus();
}
let crumbMenuBtn = null;
function closeCrumbMenu() { const p = $("#crumbPop"); if (p) p.remove(); if (crumbMenuBtn) crumbMenuBtn.setAttribute("aria-expanded", "false"); crumbMenuBtn = null; }
document.addEventListener("click", (e) => { const p = $("#crumbPop"); if (p && !p.contains(e.target) && !(crumbMenuBtn && crumbMenuBtn.contains(e.target))) closeCrumbMenu(); });
document.addEventListener("keydown", (e) => {
  const p = $("#crumbPop"); if (!p) return;
  const items = [...p.querySelectorAll("button")]; const i = items.indexOf(document.activeElement);
  if (e.key === "Escape") { const b = crumbMenuBtn; closeCrumbMenu(); if (b) b.focus(); e.preventDefault(); }
  else if (e.key === "ArrowDown") { items[(i + 1) % items.length].focus(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); e.preventDefault(); }
});
window.addEventListener("resize", closeCrumbMenu);

// ---------- Home ----------
function ago(ts) {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return days < 31 ? `${days} day${days === 1 ? "" : "s"} ago` : new Date(ts).toLocaleDateString();
}
const card = (title, ...body) => el("section", { class: "panel hcard" }, el("h2", {}, title), ...body);
const nodePath = (id) => { const out = []; const walk = (ns, p) => { for (const n of ns) { if (n.id === id) { out.push(...p, n.name); return true; } if (walk(n.children || [], [...p, n.name])) return true; } return false; }; walk(state.tree, []); return out; };

async function renderHome() {
  const box = $("#detail"); const hv = $("#homeView");
  let h; try { h = await api("/home"); } catch (e) { return; }
  if (state.selected !== "home") return;
  state.home = h;
  const st = state.status || {};
  box.replaceChildren();
  setHead(crumbBar(null), "Home", [
      el("button", { type: "button", class: "btn", onclick: () => $("#discoverBtn").click() }, "Discover networks…"),
      el("button", { type: "button", class: "btn", onclick: () => $("#importSiteFile").click() }, "Import site…"),
      el("button", { type: "button", class: "btn btn-primary", onclick: () => openNode({ kind: "site" }) }, "+ Add site")]);

  // Recent networks: one click to join again
  const recent = h.recent.length ? el("ul", { class: "hlist" }, h.recent.map((n) => el("li", {},
    el("div", { class: "grow" },
      el("button", { type: "button", class: "linkish", onclick: () => goTo(n.id), title: "Open this network" }, n.name),
      el("span", { class: "sub2" }, n.path.split(" › ").slice(0, -1).join(" › "))),
    el("div", { class: "small muted right" }, el("span", { class: "mono" }, n.ip), el("span", { class: "sub2" }, ago(n.ts))),
    n.internet === 1 ? el("span", { class: "inet yes", title: "Had internet at last join" }, globe(false)) : el("span", { class: "inet-spacer" }),
    el("button", { type: "button", class: "btn small btn-primary", onclick: (e) => joinFromHome(n.id, e.currentTarget) }, "Join"))))
    : el("p", { class: "muted" }, "Networks you join show up here, with one-click Join.");

  // This PC
  const a = st.adapter; const p = st.primary;
  const kv = (k, v) => [el("dt", {}, k), el("dd", {}, v)];
  const pc = el("dl", { class: "kv" },
    ...kv("Adapter", st.adapter_missing ? `${st.adapter_missing.name} (unplugged)` : a ? `${a.name}${a.description ? " – " + a.description : ""}` : "none picked"),
    ...kv("Address", p ? [`${p.ip}/${p.prefix} · ${a && a.dhcp ? "DHCP" : "Static"}`, ...(st.joined ? [" · ", joinedLink(st.joined)] : [])] : "—"),
    ...kv("MAC", a && a.mac ? a.mac : "—"),
    ...kv("Internet", st.internet == null ? "not checked" : inetInfo(st.internet).text),
    ...kv("Npcap", st.npcap ? (st.npcap.available ? "installed" : st.npcap.note) : "—"),
    ...kv("NetJoin", `${st.version || ""}${st.backend === "fake" ? " (demo mode)" : ""}`));

  // Sites
  const sites = h.sites.length ? el("div", { class: "site-cards" }, h.sites.map((x) => el("button", { type: "button", class: "ccard", onclick: () => goTo(x.id) },
    el("span", { class: "k" }, "Site"), el("span", { class: "nm" }, x.name),
    el("span", { class: "meta" }, `${x.networks} network${x.networks === 1 ? "" : "s"} · ${x.devices} device${x.devices === 1 ? "" : "s"} · ${x.bacnet} BACnet`),
    el("span", { class: "meta" }, x.last_join ? `Last joined ${ago(x.last_join)}` : "Not joined yet"),
    x.inactive ? el("span", { class: "gone small" }, `${x.inactive} inactive`) : null)))
    : el("p", { class: "muted" }, "No sites yet – add one, import one, or run Discover.");

  // Needs attention
  const at = h.attention; const att = [];
  for (const x of at.inactive) att.push(el("li", {}, el("span", { class: "gone" }, "●"), el("div", { class: "grow" },
    el("button", { type: "button", class: "linkish", onclick: () => openMissing(x.id) }, x.path),
    el("span", { class: "sub2" }, `${x.count} device${x.count === 1 ? "" : "s"} not seen in the latest scan`))));
  const attention = att.length ? el("ul", { class: "hlist attn" }, att) : el("p", { class: "muted" }, "Nothing needs attention – every device was seen in its latest scan.");

  // Stats
  const S = h.stats;
  const tile = (n, label, title) => el("div", { class: "stat", title: title || null }, el("span", { class: "n" }, n.toLocaleString()), el("span", { class: "l" }, label));
  const maxV = Math.max(1, ...S.vendors.map((v) => v.count));
  const vendors = S.vendors.length ? el("ul", { class: "bars", "aria-label": "Devices by vendor" }, S.vendors.map((v) =>
    el("li", { title: `${v.name}: ${v.count} device${v.count === 1 ? "" : "s"}` },
      el("span", { class: "bl" }, v.name),
      el("span", { class: "bt" }, el("span", { class: "bf", style: `width:${Math.max(2, (v.count / maxV) * 100)}%` })),
      el("span", { class: "bv" }, String(v.count))))) : el("p", { class: "muted small" }, "Vendors appear after your first scan.");
  const facts = el("dl", { class: "kv" },
    ...kv("Most visited site", S.most_visited ? `${S.most_visited.name} (${S.most_visited.joins} join${S.most_visited.joins === 1 ? "" : "s"})` : "—"),
    ...kv("Busiest network", S.busiest ? `${S.busiest.path} (${S.busiest.devices} devices)` : "—"),
    ...kv("Saved", `${S.areas} area${S.areas === 1 ? "" : "s"} · ${S.subnets} subnet${S.subnets === 1 ? "" : "s"}`),
    ...kv("Joins all time", String(S.joins_total)),
    ...kv("Using NetJoin since", S.since ? new Date(S.since).toLocaleDateString() : "—"));
  const stats = [el("div", { class: "stats" },
    tile(S.sites, "sites"), tile(S.networks, "networks"), tile(S.devices, "devices found"), tile(S.bacnet, "BACnet controllers"),
    tile(S.joins_week, "joins this week"), tile(S.clashes_dodged, "clashes dodged", "Times NetJoin found an address in use and moved on")),
    el("div", { class: "split" }, el("div", {}, el("h3", {}, "Devices by vendor"), vendors), el("div", {}, el("h3", {}, "Facts"), facts))];

  hv.replaceChildren(
    el("div", { class: "home-top" }, card("Recent networks", recent), card("Needs attention", attention), el("section", { class: "panel hcard" },
      el("div", { class: "hcard-head" }, el("h2", {}, "This PC"),
        el("button", { type: "button", class: "btn icon small refresh", "aria-label": "Refresh This PC", title: "Read the adapter again (NetJoin doesn't poll, to save battery)",
          onclick: (e) => withBusy(e.currentTarget, async () => { await refreshStatus(); await refreshAdapters(); await renderHome(); }) }, refreshIcon())), pc)),
    el("section", { class: "panel hcard wide" }, el("h2", {}, "Sites"), sites),
    el("section", { class: "panel hcard wide2" }, el("h2", {}, "Stats"), ...stats));
}

function joinFromHome(id, btn) { return joinNetwork(id, null, btn); }

function openMissing(id) {
  state.filter = "missing";
  for (const o of document.querySelectorAll(".dev-tools .chip")) { const on = o.dataset.filter === "missing"; o.classList.toggle("on", on); o.setAttribute("aria-pressed", String(on)); }
  goTo(id);
}

// ---------- Contents strip on site / area pages: a card for each thing inside ----------
function renderContents() {
  const box = $("#contents"); const d = state.detail;
  if (!box || !d || d.kind === "network") return;
  const node = findNode(d.id); const kids = (node && node.children) || [];
  const devs = (state.devData && state.devData.devices) || [];
  const netIdsUnder = (n) => n.kind === "network" ? [n.id] : (n.children || []).flatMap(netIdsUnder);
  const count = (ids, pred = () => true) => devs.filter((x) => !x.is_self && pred(x) && (x.network_ids || []).some((i) => ids.includes(i))).length;
  if (!kids.length) { box.replaceChildren(el("p", { class: "small muted" }, `Nothing in this ${d.kind} yet – add an area or a network.`)); return; }
  box.replaceChildren(...kids.map((n) => {
    const ids = netIdsUnder(n); const total = count(ids); const gone = count(ids, (x) => x.inactive);
    const meta = n.kind === "network"
      ? [el("span", { class: "mono" }, n.subnet || ""), ` · ${total} device${total === 1 ? "" : "s"}`]
      : [`${ids.length} network${ids.length === 1 ? "" : "s"} · ${total} device${total === 1 ? "" : "s"}`];
    return el("button", { type: "button", class: "ccard", onclick: () => goTo(n.id), title: `Open ${KIND_LABEL[n.kind].toLowerCase()} ${n.name}` },
      el("span", { class: "nm" }, n.name),
      el("span", { class: "meta" }, meta), gone ? el("span", { class: "gone small" }, `· ${gone} inactive`) : null);
  }));
}

// Site page: Export ▾ (plan item 1)
function exportMenu(d) {
  return el("details", { class: "menu", id: "exportMenu" },
    el("summary", { class: "btn", title: "Save this site to a file, e.g. to move it to another laptop" }, "Export ▾"),
    el("div", { class: "menu-list", role: "menu" },
      el("a", { class: "mi", role: "menuitem", href: `/api/export?node_id=${d.id}`, download: "" },
        el("span", {}, "Export site"), el("span", { class: "d" }, "Areas and networks")),
      el("a", { class: "mi", role: "menuitem", href: `/api/export?node_id=${d.id}&devices=1`, download: "" },
        el("span", {}, "Export site with device lists"), el("span", { class: "d" }, "Also every network's scan results"))));
}
document.addEventListener("click", (e) => { for (const id of ["#exportMenu", "#addMenu"]) { const m = $(id); if (m && m.open && !m.contains(e.target)) m.open = false; } });

// Import from a file; if a site with the same name exists, ask Replace / Keep both / Cancel.
async function importFromFile(file) {
  let payload;
  try { payload = JSON.parse(await file.text()); } catch (e) { flash("That file isn't a NetJoin export (not valid JSON)", "error"); return; }
  const send = (mode) => api(`/import?on_conflict=${mode}`, { method: "POST", body: payload });
  let r;
  try { r = await send("ask"); }
  catch (e) {
    if (e.status !== 409) { flash(e.message, "error"); return; }
    const choice = await askImportConflict(e.data.conflicts || []);
    if (choice === "cancel") return;
    try { r = await send(choice); } catch (e2) { flash(e2.message, "error"); return; }
  }
  flash(`Imported ${r.imported} item(s)${r.devices ? ` and ${r.devices} device(s)` : ""}` +
    (r.replaced.length ? ` – replaced ${r.replaced.join(", ")}` : "") + (r.renamed.length ? ` – kept both copies of ${r.renamed.join(", ")}` : ""));
  await refreshTree(); await refreshDetail(); refreshLog();
}
function askImportConflict(names) {
  return new Promise((resolve) => {
    const dlg = $("#importDlg");
    $("#importMsg").textContent = names.length === 1 ? "You already have a site with this name:" : "You already have sites with these names:";
    $("#importNames").replaceChildren(...names.map((n) => el("li", {}, n)));
    const done = (c) => { dlg.close(); resolve(c); };
    for (const b of dlg.querySelectorAll("[data-choice]")) b.onclick = () => done(b.dataset.choice);
    dlg.oncancel = (e) => { e.preventDefault(); done("cancel"); };
    dlg.showModal();
  });
}

function renderHistory() { renderScanMenu(); refreshDevices(); }

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
// ---------- Checks (v0.11): duplicate instances, MAC changes, wrong subnets, odd firmware ----------
async function updateHealthBtn() {
  const d = state.detail; const btn = $("#healthBtn"); const wh = $("#whoHasBtn");
  btn.hidden = !d; wh.hidden = !d || d.kind !== "network";
  if (!d) return;
  try {
    const h = state.health = await api(`/health?node_id=${d.id}`);
    if (!state.detail || state.detail.id !== d.id) return;
    const warn = h.issues.filter((i) => i.severity === "warn").length;
    btn.textContent = h.issues.length ? `Checks (${h.issues.length})` : "Checks";
    btn.classList.toggle("warn-btn", warn > 0);
  } catch (e) {}
}
$("#healthBtn").addEventListener("click", async () => { await updateHealthBtn(); drawHealth(); $("#healthDlg").showModal(); });
$("#healthClose").addEventListener("click", () => $("#healthDlg").close());
function drawHealth() {
  const h = state.health; const d = state.detail; if (!h || !d) return;
  $("#healthTitle").textContent = `Checks – ${d.name}`;
  const fwBtn = el("button", { type: "button", class: "btn small", onclick: (e) => readFirmware(e.currentTarget) }, "Read firmware now");
  $("#healthSummary").replaceChildren(`${h.checked} device(s) checked · ${h.bacnet} BACnet · firmware known for ${h.firmware_known} of ${h.bacnet}. `,
    h.bacnet > h.firmware_known ? fwBtn : "", el("div", { id: "fwProgress" }));
  const sev = { warn: "⚠", info: "ℹ" };
  $("#healthBody").replaceChildren(...(h.issues.length ? h.issues.map((i) => el("div", { class: "hissue " + i.severity },
    el("div", { class: "htitle" }, el("span", { class: "sev", "aria-hidden": "true" }, sev[i.severity]), i.title),
    el("p", { class: "small muted" }, i.detail),
    el("ul", { class: "hdevs" }, i.devices.map((x) => el("li", {},
      x.network_id ? el("button", { type: "button", class: "netlink", onclick: () => { $("#healthDlg").close(); openDevice(x.id, x.network_id); } }, x.label) : x.label,
      el("span", { class: "small muted" }, ` ${x.ip}${x.instance != null ? ` · #${x.instance}` : ""}${x.network ? ` · ${x.network}` : ""}${x.note ? ` · ${x.note}` : ""}`))))))
    : [el("p", { class: "ok" }, "Nothing found. "), el("p", { class: "small muted" }, "Checks: duplicate BACnet instances across the site, the 'not configured' instance 4194303, an address answering with a different MAC, one MAC at several addresses, devices answering from outside the subnet, and firmware that differs from others of the same model (read firmware first).")]));
}
async function readFirmware(btn) {
  const d = state.detail; if (!d) return;
  let j; try { j = await api(`/health/firmware?node_id=${d.id}`, { method: "POST" }); } catch (e) { flash(e.message, "error"); return; }
  btn.disabled = true;
  while (!j.finished) {
    const p = $("#fwProgress");
    if (p) p.replaceChildren(el("div", { class: "progress" }, el("span", { class: "small" }, `Reading firmware ${j.done} / ${j.total}…`),
      el("div", { class: "bar" }, el("div", { style: `width:${j.total ? Math.round(j.done / j.total * 100) : 0}%` }))));
    await new Promise((r) => setTimeout(r, 400));
    try { j = await api(`/jobs/${j.job}`); } catch (e) { break; }
  }
  if (j.summary) flash(j.summary); if (j.error) flash(j.error, "error");
  await updateHealthBtn(); drawHealth(); refreshLog();
}
$("#whoHasBtn").addEventListener("click", () => { $("#whoHasResults").replaceChildren(); $("#whoHasDlg").showModal(); $("#whoHasName").focus(); });
$("#whoHasClose").addEventListener("click", () => $("#whoHasDlg").close());
$("#whoHasForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#whoHasName").value.trim(); if (!name) return;
  const out = $("#whoHasResults"); out.replaceChildren(el("p", { class: "muted" }, "Asking…"));
  await withBusy($("#whoHasGo"), async () => {
    const r = await api("/who-has", { method: "POST", body: { name } });
    out.replaceChildren(r.results.length ? el("table", { class: "props" }, el("tbody", {}, r.results.map((x) => el("tr", {},
      el("td", {}, x.device_id ? el("button", { type: "button", class: "netlink", onclick: () => { $("#whoHasDlg").close(); openDevice(x.device_id, x.network_id || state.detail.id); } },
        x.device_name || `Device ${x.device}`) : `Device ${x.device}`, el("span", { class: "small muted" }, ` #${x.device} · ${x.ip}${x.route ? ` (${x.route})` : ""}`)),
      el("td", { class: "mono" }, x.object), el("td", { class: "mono" }, x.name))))) :
      el("p", { class: "muted" }, `No controller on ${r.subnet} has an object called “${r.name}”. Check the exact name (case matters).`));
  });
});

function renderScanMenu() {
  const d = state.detail; const box = $("#scanOptions"); const menu = $("#scanMenu");
  menu.hidden = !d;
  if (d && d.kind !== "network") {          // site / area: scans run per network, so offer a way to get there
    menu.setAttribute("aria-disabled", "false");
    $("#scanMenuBtn").textContent = "Scan a network ▾";
    const nets = (state.devData && state.devData.networks) || [];
    box.replaceChildren(el("div", { class: "note" }, "Scans run per network, from the laptop's current subnet. Pick one:"),
      ...(nets.length ? nets.map((n) => el("button", { type: "button", role: "menuitem", onclick: () => { menu.open = false; goTo(n.id); } },
        el("span", {}, n.path.split(" › ").slice(-2).join(" › ")), el("span", { class: "d mono" }, n.subnet)))
        : [el("div", { class: "d", style: "padding:8px 10px" }, "No networks here yet.")]));
    return;
  }
  $("#scanMenuBtn").textContent = "Scan ▾";
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
  if (!d) {
    state.devData = null;
    box.replaceChildren(el("div", { class: "empty" }, "Select a site, area or network."));
    for (const id of ["#devCount", "#lastScan"]) $(id).textContent = "";
    $("#clearDevices").hidden = $("#clearInactive").hidden = true; return;
  }
  let r; try { r = await api(`/devices?node_id=${d.id}`); } catch (e) { return; }
  if (!state.detail || state.detail.id !== r.node_id) return;      // the user moved on meanwhile
  state.devData = r; state.devices = r.devices;
  if (d.kind === "network" && state.netDevices[d.id] !== undefined) { state.netDevices[d.id] = r.devices; renderTree(); }
  $("#devTitle").textContent = d.kind === "network" ? "Devices on this network" : `Devices in this ${d.kind}`;
  $("#lastScan").textContent = r.last_scan ? `Last: ${r.last_scan.kind}, ${when(r.last_scan.ts)}` : "";
  fillDeviceFilters(r);
  if (d.kind !== "network") { renderScanMenu(); renderRollStats(r); renderContents(); }
  renderDevices(); updateHealthBtn();
}

// Site / area header: "3 networks · 42 devices · 18 BACnet · last scan 09:02"
function renderRollStats(r) {
  const box = $("#rollStats"); if (!box) return;
  const devs = r.devices.filter((x) => !x.is_self);
  const parts = [`${r.networks.length} network${r.networks.length === 1 ? "" : "s"}`, `${devs.length} device${devs.length === 1 ? "" : "s"}`,
    `${devs.filter((x) => x.bacnet_instance != null).length} BACnet`];
  const gone = devs.filter((x) => x.inactive).length; if (gone) parts.push(`${gone} inactive`);
  if (r.last_scan) parts.push(`last scan ${when(r.last_scan.ts)}`);
  box.textContent = parts.join(" · ");
}

// Vendor filter: tick any number (e.g. "Siemens" and "Siemens Building Technologies"). None ticked = all.
state.vendors = new Set();
function vendorLabel() {
  const n = state.vendors.size;
  $("#devVendorLabel").textContent = !n ? "All vendors" : n === 1 ? [...state.vendors][0] : `${n} vendors`;
  $("#devVendor").classList.toggle("active", n > 0);
}
function fillDeviceFilters(r) {
  const nSel = $("#devNetwork"); const keepN = nSel.value;
  const counts = {};
  for (const x of r.devices) if (!x.is_self) { const v = vendorName(x); if (v) counts[v] = (counts[v] || 0) + 1; }
  const vendors = Object.keys(counts).sort((a, b) => a.localeCompare(b));
  for (const v of [...state.vendors]) if (!counts[v]) state.vendors.delete(v);
  $("#vendorChecks").replaceChildren(...(vendors.length ? vendors.map((v) => el("label", { class: "check vend" },
    el("input", { type: "checkbox", value: v, checked: state.vendors.has(v),
      onchange: (e) => { e.target.checked ? state.vendors.add(v) : state.vendors.delete(v); vendorLabel(); renderDevices(); } }),
    el("span", { class: "vn" }, v), el("span", { class: "n" }, String(counts[v])))) : [el("p", { class: "small muted" }, "No vendors yet.")]));
  vendorLabel();
  const rollup = state.detail && state.detail.kind !== "network";
  nSel.hidden = !rollup || r.networks.length < 2;
  const nets = [...new Set(r.devices.map((x) => x.network).filter(Boolean))].sort();
  nSel.replaceChildren(el("option", { value: "" }, "All networks"), ...nets.map((n) => el("option", { value: n }, n)));
  nSel.value = nets.includes(keepN) ? keepN : "";
  for (const o of $("#devSort").options) if (o.value === "network") o.hidden = !rollup;
}

// BACnet vendor-name (from the device itself) wins; otherwise the MAC manufacturer.
function vendorCell(x) {
  const bac = x.vendor || (x.bacnet_vendor_id != null ? `Vendor ID ${x.bacnet_vendor_id}` : null);
  if (bac) return [bac, x.mac_vendor && x.mac_vendor !== bac ? el("span", { class: "sub2", title: "Network chip maker, from the MAC address" }, `NIC: ${x.mac_vendor}`) : null];
  if (x.mac_vendor) return [el("span", { class: x.mac_vendor.startsWith("Randomised") ? "muted" : null }, x.mac_vendor),
    el("span", { class: "sub2" }, "from MAC")];
  return "—";
}

const vendorName = (x) => x.vendor || (x.bacnet_vendor_id != null ? `Vendor ID ${x.bacnet_vendor_id}` : null) || x.mac_vendor || "";
const hasWeb = (x) => (x.ports || "").split(",").some((p) => ["80", "443", "8080"].includes(p));
const ipKey = (ip) => String(ip).split(".").reduce((a, o) => a * 256 + (+o || 0), 0);
const DEV_FILTERS = {
  all: () => true, bacnet: (x) => x.bacnet_instance != null, other: (x) => x.bacnet_instance == null,
  web: hasWeb, missing: (x) => x.inactive, new: (x) => x.new, listed: (x) => x.expected,
};
// Sort keys: the first value decides; null = blank, always sorted last whichever direction.
const DEV_SORTS = {
  ip: (x) => [ipKey(x.ip), x.key], name: (x) => [(x.name || "").toLowerCase() || null, ipKey(x.ip)],
  vendor: (x) => [(x.is_self ? "this pc" : vendorName(x).toLowerCase()) || null, ipKey(x.ip)], bacnet: (x) => [x.bacnet_instance ?? null, ipKey(x.ip)],
  mac: (x) => [x.mac || null, ipKey(x.ip)], ports: (x) => [(x.ports || "").split(",").filter(Boolean).length || null, ipKey(x.ip)],
  seen: (x) => [x.last_seen || null, ipKey(x.ip)], network: (x) => [x.network || null, ipKey(x.ip)],
};
state.sort = { key: "ip", dir: 1 };
const portLabel = { 80: "HTTP", 443: "HTTPS", 8080: "HTTP-alt", 502: "Modbus", 1911: "Fox", 4911: "Fox TLS", 22: "SSH", 23: "Telnet" };

function visibleDevices() {
  const q = $("#devSearch").value.trim().toLowerCase(); const vs = state.vendors; const n = $("#devNetwork").value;
  const f = DEV_FILTERS[state.filter] || DEV_FILTERS.all;
  const rows = (state.devices || []).filter((x) => f(x) && (!vs.size || vs.has(vendorName(x))) && (!n || x.network === n) && (!q ||
    [x.ip, x.mac, x.vendor, x.mac_vendor, x.name, x.model, x.route, x.network, x.bacnet_instance != null ? `#${x.bacnet_instance} ${x.bacnet_instance}` : ""]
      .some((t) => t && String(t).toLowerCase().includes(q))));
  const key = DEV_SORTS[state.sort.key] || DEV_SORTS.ip; const dir = state.sort.dir;
  const cmp = (a, b) => { const ka = key(a), kb = key(b);
    if ((ka[0] == null) !== (kb[0] == null)) return ka[0] == null ? 1 : -1;      // blanks last
    for (let i = 0; i < ka.length; i++) { if (ka[i] < kb[i]) return -dir; if (ka[i] > kb[i]) return dir; } return 0; };
  // "This PC" sorts like any other row (it keeps its highlight, so it's easy to spot)
  return rows.sort(cmp);
}

function goneText(ts) {
  if (!ts) return "Not seen in the latest scan";
  const d = new Date(ts); const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (d.toDateString() === new Date().toDateString()) return `Not seen since ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  return days >= 1 ? `Not seen for ${days} day${days === 1 ? "" : "s"}` : `Not seen since ${when(ts)}`;
}

function renderDevices() {
  const d = state.detail; const box = $("#devicesBox");
  const all = state.devices || [];
  const rollup = d && d.kind !== "network";
  const rows = visibleDevices();
  // chip counts
  for (const c of document.querySelectorAll(".dev-tools .chip")) {
    const k = c.dataset.filter; const n = all.filter((x) => !x.is_self && DEV_FILTERS[k](x)).length;
    c.replaceChildren(c.dataset.label || (c.dataset.label = c.textContent), k === "all" || !n ? "" : el("span", { class: "n" }, String(n)));
    if (k === "missing" || k === "new" || k === "listed") c.hidden = n === 0 && state.filter !== k;
  }
  const inactive = all.filter((x) => x.inactive).length;
  $("#clearDevices").hidden = all.length === 0;
  $("#clearInactive").hidden = inactive === 0;
  $("#clearInactive").textContent = `Clear inactive (${inactive})`;
  // Footer: "Showing 5 of 11 devices · 2 missing · 3 on the list not checked yet"
  const devs = all.filter((x) => !x.is_self); const shown = rows.filter((x) => !x.is_self).length;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const missing = devs.filter((x) => x.inactive).length, pending = devs.filter((x) => x.pending).length, listed = devs.filter((x) => x.expected).length;
  $("#devCount").replaceChildren(...(devs.length ? [
    el("span", { class: "cnt" }, shown !== devs.length ? `Showing ${shown} of ${plural(devs.length, "device")}` : plural(devs.length, "device")),
    missing ? el("span", { class: "cnt gone" }, `${missing} missing`) : null,
    listed ? el("span", { class: "cnt" }, `${listed} on the imported list`) : null,
    pending ? el("span", { class: "cnt" }, `${pending} not checked yet`) : null].filter(Boolean) : []));
  $("#clearExpected").hidden = listed === 0;
  if (!all.length) {
    box.replaceChildren(el("div", { class: "empty" }, rollup ? "No scan results in this " + d.kind + " yet – open one of its networks, Join and Scan."
      : d.on_subnet ? "No scan results yet – use Scan to find hosts and controllers." : "No scan results yet. Join this network, then Scan."));
    return;
  }
  const webUrl = (ip, p) => p === 443 ? `https://${ip}/` : p === 8080 ? `http://${ip}:8080/` : `http://${ip}/`;
  const tr = (x) => {
    const ports = (x.ports || "").split(",").filter(Boolean).map(Number);
    const seen = x.pending ? [el("span", { class: "muted" }, "On the list – not checked yet"), el("span", { class: "sub2" }, "Scan to check")]
      : x.inactive && x.expected && !x.last_seen ? [el("span", { class: "gone" }, "Missing – on the list, never seen"), el("span", { class: "sub2" }, "imported list")]
      : x.inactive
      ? [el("span", { class: "gone" }, goneText(x.last_seen)), el("span", { class: "sub2" }, (x.sources || "").split(",").filter((s) => s && s !== "self").join(" · "))]
      : [(x.sources || "").split(",").filter((s) => s && s !== "self").join(" · ") || "—",
        el("span", { class: "sub2" }, [x.ping_ms != null ? `${x.ping_ms} ms` : null, when(x.last_seen)].filter(Boolean).join(" · "))];
    return el("tr", { class: [x.is_self ? "self" : "", x.inactive ? "inactive" : "", x.pending ? "pending" : ""].join(" ").trim() || null,
        title: x.inactive ? goneText(x.last_seen) : null },
      el("td", { class: "mono ipcell", "data-label": "IP" },
        x.is_self ? x.ip : el("button", { type: "button", class: "devlink", title: `Open ${devLabel(x)}`,
          onclick: () => openDevice(x.id, (x.network_ids && x.network_ids[0]) || d.id) }, x.ip),
        x.new ? el("span", { class: "badge-new" }, "NEW") : null,
        x.expected ? el("span", { class: "badge-list", title: "On the imported device list" }, "LIST") : null),
      el("td", { class: "mono", "data-label": "MAC" }, x.mac || "—"),
      el("td", { "data-label": "Vendor" }, x.is_self ? [el("strong", {}, "This PC"), x.name ? el("span", { class: "sub2" }, x.name) : null] : vendorCell(x)),
      el("td", { "data-label": "BACnet" }, x.bacnet_instance != null ? [
        el("span", { class: "mono" }, `#${x.bacnet_instance}`), x.name ? ` · ${x.name}` : "",
        x.model ? el("span", { class: "sub2" }, x.model) : null,
        x.route ? el("span", { class: "sub2" }, `behind ${x.ip} (${x.route})`) : null] : "—"),
      el("td", { "data-label": "Ports" }, ports.length ? ports.map((p) => [80, 443, 8080].includes(p)
        ? el("a", { class: "port", href: webUrl(x.ip, p), target: "_blank", rel: "noopener noreferrer" }, portLabel[p] || p)
        : el("span", { class: "port" }, portLabel[p] || p)) : "—"),
      el("td", { class: "small", "data-label": "Seen by" }, seen),
      rollup ? el("td", { class: "small netcell", "data-label": "Network" }, (x.network_links || []).length
        ? x.network_links.flatMap((l, i) => [i ? " / " : null, el("button", { type: "button", class: "netlink", title: `Open ${l.path}`, onclick: () => goTo(l.id) }, l.path)])
        : x.network || "—") : null);
  };
  const cols = [["IP", "ip"], ["MAC", "mac"], ["Vendor", "vendor"], ["BACnet device", "bacnet"], ["Open ports", "ports"], ["Seen by", "seen"]];
  if (rollup) cols.push(["Network", "network"]);
  const th = ([label, key]) => el("th", { "aria-sort": state.sort.key === key ? (state.sort.dir > 0 ? "ascending" : "descending") : null },
    el("button", { type: "button", title: `Sort by ${label}`, onclick: () => setSort(key) }, label));
  box.replaceChildren(rows.length ? el("table", { class: "dev" }, el("thead", {}, el("tr", {}, cols.map(th))), el("tbody", {}, rows.map(tr)))
    : el("div", { class: "empty" }, "No devices match the search / filters."));
}

function setSort(key) {
  state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: key === "seen" || key === "ports" ? -1 : 1 };
  $("#devSort").value = key; renderDevices();
}

for (const c of document.querySelectorAll(".dev-tools .chip")) c.addEventListener("click", () => {
  state.filter = c.dataset.filter;
  for (const o of document.querySelectorAll(".dev-tools .chip")) { o.classList.toggle("on", o === c); o.setAttribute("aria-pressed", String(o === c)); }
  renderDevices();
});
$("#devSearch").addEventListener("input", renderDevices);
$("#vendAll").addEventListener("click", () => { for (const c of $("#vendorChecks").querySelectorAll("input")) { c.checked = true; state.vendors.add(c.value); } vendorLabel(); renderDevices(); });
$("#vendNone").addEventListener("click", () => { for (const c of $("#vendorChecks").querySelectorAll("input")) c.checked = false; state.vendors.clear(); vendorLabel(); renderDevices(); });
document.addEventListener("click", (e) => { const m = $("#devVendor"); if (m.open && !m.contains(e.target)) m.open = false; });
$("#devNetwork").addEventListener("change", renderDevices);
$("#devSort").addEventListener("change", (e) => { state.sort = { key: e.target.value, dir: 1 }; renderDevices(); });

$("#clearDevices").addEventListener("click", async () => {
  const d = state.detail; if (!d) return;
  const what = d.kind === "network" ? `for ${d.subnet}` : `for every network in ${d.name}`;
  if (!confirm(`Clear the whole device list ${what}?`)) return;
  await api(`/devices?node_id=${d.id}`, { method: "DELETE" }); refreshDevices(); refreshLog();
});
$("#clearInactive").addEventListener("click", async () => {
  const d = state.detail; if (!d) return;
  const gone = (state.devices || []).filter((x) => x.inactive);
  const oldest = gone.map((x) => x.last_seen).sort()[0];
  if (!confirm(`Remove ${gone.length} inactive device${gone.length === 1 ? "" : "s"} (${goneText(oldest).toLowerCase()})?`)) return;
  const r = await api(`/devices?node_id=${d.id}&inactive=1`, { method: "DELETE" });
  flash(`Removed ${r.cleared} inactive device(s)`); refreshDevices(); refreshLog();
});

// CSV of exactly what's shown (search, filters and sort applied).
$("#devCsv").addEventListener("click", () => {
  const d = state.detail; if (!d) return;
  const rows = visibleDevices();
  const head = ["Network", "IP", "MAC", "Vendor", "NIC maker", "BACnet instance", "Name", "Model", "Behind router", "Open ports", "Seen by", "Last seen", "Status", "On list"];
  const esc = (v) => { const t = v == null ? "" : String(v); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const lines = [head.join(",")].concat(rows.map((x) => [x.network || d.name, x.ip, x.mac, x.is_self ? "This PC" : x.vendor || "", x.mac_vendor,
    x.bacnet_instance, x.name, x.model, x.route, (x.ports || "").split(",").filter(Boolean).map((p) => portLabel[p] || p).join(" "),
    (x.sources || "").split(",").filter((s) => s && s !== "self").join(" "), x.last_seen,
    x.pending ? "not checked" : x.inactive ? (x.expected && !x.last_seen ? "missing" : "inactive") : x.new ? "new" : "active", x.expected ? "yes" : ""].map(esc).join(",")));
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `NetJoin-devices-${d.name.replace(/[^\w.-]+/g, "_")}-${new Date().toISOString().slice(0, 10)}.csv` });
  document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  flash(`Exported ${rows.length} device(s) to CSV`);
});

// Import a list of devices that should be here: a scan then shows which are online and which are missing.
$("#devCsvIn").addEventListener("click", () => { if (state.detail) { $("#devCsvFile").value = ""; $("#devCsvFile").click(); } });
$("#devCsvFile").addEventListener("change", async (e) => {
  const file = e.target.files[0]; const d = state.detail; if (!file || !d) return;
  if (file.size > 5 * 1024 * 1024) { flash("That file is over 5 MB – is it the right one?", "error"); return; }
  try {
    const r = await api(`/devices/import?node_id=${d.id}`, { method: "POST", body: { csv: await file.text() } });
    const skip = r.skipped.length ? ` · ${r.skipped.length} skipped (${[...new Set(r.skipped.map((x) => x.why))].join(", ")}; first: line ${r.skipped[0].line}${r.skipped[0].ip ? " " + r.skipped[0].ip : ""})` : "";
    flash(`Imported ${file.name}: ${r.added} added, ${r.updated} already known${skip}. Scan to check which are online.`, r.skipped.length ? "warn" : "info");
    state.filter = "listed";
    for (const o of document.querySelectorAll(".dev-tools .chip")) { o.classList.toggle("on", o.dataset.filter === "listed"); o.setAttribute("aria-pressed", String(o.dataset.filter === "listed")); }
    await refreshDevices(); refreshLog();
  } catch (err) { flash(err.message, "error"); }
});
$("#clearExpected").addEventListener("click", async () => {
  const d = state.detail; if (!d) return;
  if (!confirm("Remove the imported device list? Devices never seen are removed; the rest stay as ordinary devices.")) return;
  try { await api(`/devices/expected?node_id=${d.id}`, { method: "DELETE" }); flash("Imported list removed"); await refreshDevices(); refreshLog(); }
  catch (err) { flash(err.message, "error"); }
});

// ---------- discover ----------
const disc = { timer: null };
function discShow(view) { for (const v of ["discSetup", "discRunning", "discResults"]) $("#" + v).hidden = v !== view; }

$("#discoverBtn").addEventListener("click", async () => {
  const r = await api("/discover").catch(() => null);
  await fillDiscAdapters();
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

const isWifi = (a) => /wi-?fi|wireless|wlan|802\.11/i.test(`${a.name} ${a.description}`);
async function fillDiscAdapters() {
  const list = await api("/adapters").catch(() => []);
  disc.adapters = list;
  const sel = $("#discAdapterSel"); const st = state.status || {};
  const top = st.adapter && st.adapter.name;
  // default: the adapter chosen at the top, unless it's Wi-Fi and a wired one is available
  const wired = list.filter((a) => !isWifi(a));
  const pick = list.find((a) => a.name === top && !isWifi(a)) || wired.find((a) => a.up) || wired[0] || list[0];
  sel.replaceChildren(...list.map((a) => el("option", { value: a.name, selected: pick && a.name === pick.name },
    `${a.name}${a.description ? " – " + a.description : ""}${a.up ? "" : " (disconnected)"}`)));
  discAdapterChanged();
}
function discAdapterChanged() {
  const a = (disc.adapters || []).find((x) => x.name === $("#discAdapterSel").value);
  $("#discWifiNote").hidden = !(a && isWifi(a));
  const p = a && a.addresses && (a.addresses.find((x) => !x.ip.startsWith("169.254.")) || a.addresses[0]);
  $("#discAdapter").textContent = a ? `Currently ${p ? `${p.ip}/${p.prefix}` : "no IPv4 address"}${a.dhcp ? " (DHCP)" : ""}.` : "";
}
$("#discAdapterSel").addEventListener("change", discAdapterChanged);
$("#discAgain").addEventListener("click", () => discShow("discSetup"));
$("#discCancel").addEventListener("click", () => api("/scan/cancel", { method: "POST" }).catch(() => {}));
$("#discStart").addEventListener("click", async (e) => {
  try {
    await api("/discover", { method: "POST", body: { seconds: Number($("#discSeconds").value), whois: $("#discWhois").checked,
      adapter: $("#discAdapterSel").value } });
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
    (res.npcap_error ? " · Npcap failed" : res.npcap ? "" : " · no Npcap") +
    (res.methods && res.methods.length ? ` · ${res.methods.join(" · ")}` : "");
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
  const pill = el("span", { class: "pill " + (r.confidence === "broadcast" || r.confidence === "adapter" ? "yes" : "guess") },
    r.confidence_label || r.confidence);
  const counts = `${r.host_count} device(s)${r.bacnet_count ? ` · ${r.bacnet_count} BACnet` : ""}`;
  const hostTable = el("table", {}, el("thead", {}, el("tr", {}, ["IP", "MAC", "Maker", "BACnet"].map((h) => el("th", {}, h)))),
    el("tbody", {}, r.hosts.map((h) => el("tr", {}, el("td", { class: "mono" }, h.ip), el("td", { class: "mono" }, h.mac || "—"),
      el("td", {}, h.mac_vendor || "—"),
      el("td", {}, h.bacnet.length ? h.bacnet.map((b) => `#${b.instance}${b.vendor ? " " + b.vendor : b.vendor_id != null ? " vendor " + b.vendor_id : ""}${b.route ? " (" + b.route + ")" : ""}`).join(", ") : "—")))));
  const actions = el("div", { class: "row" });
  card.append(
    el("div", { class: "row" }, el("h3", {}, r.subnet), pill, el("span", { class: "small muted" }, counts), el("div", { class: "spacer" }), actions),
    el("div", { class: "meta" }, r.evidence.join(" · ") +
      (r.gateway_hint ? ` · likely gateway ${r.gateway_hint} (${r.gateway_from || "seen"}; NetJoin leaves the gateway off unless you set it)` : "")),
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
  // Editing an area or network: where it lives, within the same site
  const mv = f.querySelector(".move-only"); mv.hidden = !node || node.kind === "site";
  if (node && node.kind !== "site") {
    const site = findNode(siteOf(node.id)); const opts = [];
    const walk = (n, depth) => {
      if (n.id === node.id || n.kind === "network") return;            // not itself, not inside itself
      opts.push(el("option", { value: n.id, selected: n.id === node.parent_id }, `${"\u2003".repeat(depth)}${n.kind === "site" ? "Site" : "Area"}: ${n.name}`));
      for (const c of n.children || []) walk(c, depth + 1);
    };
    if (site) walk(site, 0);
    f.move_parent.replaceChildren(...opts);
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
    pv.className = "small preview"; pv.textContent = "Type any address on the network and pick the mask. Pasting 10.20.5.0/24 works too.";
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
    if (!nw) { $("#nodeErr").textContent = "Enter the IP Network and pick the mask."; return; }
    Object.assign(body, { subnet: `${ip4.str(nw.net)}/${nw.prefix}`, range_start: f.range_start.value.trim(), range_end: f.range_end.value.trim(),
      addr_mode: [...f.addr_mode].find((r) => r.checked)?.value || "range", fixed_ip: f.fixed_ip.value.trim(), alloc: f.alloc.value });
  }
  try {
    let n;
    if (id) {
      const mv = f.querySelector(".move-only");
      if (!mv.hidden && f.move_parent.value && Number(f.move_parent.value) !== parentOf(Number(id))) {
        await api(`/nodes/${id}/move`, { method: "POST", body: { parent_id: Number(f.move_parent.value) } });
        state.expanded.add(Number(f.move_parent.value));
      }
      n = await api(`/nodes/${id}`, { method: "PUT", body });      // its 'live' reflects the move as well
    }
    else { n = await api("/nodes", { method: "POST", body: { ...body, kind: f.kind.value, parent_id: f.parent_id.value === "" ? null : Number(f.parent_id.value) } });
      if (n.parent_id != null) state.expanded.add(n.parent_id); state.selected = n.id; saveUi(); }
    $("#nodeDlg").close();
    await refreshTree(); await refreshDetail(); refreshLog();
    if (n && n.live) await handleLive(n.live);
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
  if (!nw) { $("#nodeErr").textContent = "Enter the IP Network and mask first."; return; }
  f.gateway.value = ip4.str(nw.first);      // network + 1, e.g. 192.168.0.1 on a /22
});

// ---------- theme: system / light / dark, saved in NetJoin's settings and cached for a flash-free start ----------
const darkMq = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
let themeChoice = "system";
function applyTheme(choice) {
  themeChoice = choice || "system";
  const dark = themeChoice === "dark" || (themeChoice === "system" && darkMq && darkMq.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try { localStorage.setItem("netjoin-theme", themeChoice); } catch (e) {}
  for (const r of document.querySelectorAll('#themeSeg input')) r.checked = r.value === themeChoice;
}
if (darkMq) darkMq.addEventListener("change", () => { if (themeChoice === "system") applyTheme("system"); });
$("#themeSeg").addEventListener("change", async (e) => {
  applyTheme(e.target.value);
  try { await api("/settings/ui", { method: "PUT", body: { theme: themeChoice } }); }
  catch (err) { $("#settingsErr").textContent = err.message; }
});

// ---------- settings ----------
$("#settingsBtn").addEventListener("click", async () => {
  const f = $("#settingsForm"); $("#settingsErr").textContent = "";
  applyTheme(themeChoice);
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
  try { const g = await api("/settings/global", { method: "PUT", body: { gw_override: f.gw_override.checked, gateway: f.gateway.value, dns_override: f.dns_override.checked, dns: f.dns.value } });
    $("#settingsDlg").close(); await refreshDetail(); refreshLog();
    if (g.live) await handleLive(g.live); }
  catch (err) { $("#settingsErr").textContent = err.message; }
});
$("#cancelSettings").addEventListener("click", () => $("#settingsDlg").close());
$("#importFile").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (file) { $("#settingsDlg").close(); await importFromFile(file); } e.target.value = "";
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

// ---------- Sites sidebar: show / hide (remembered) ----------
(function navToggle() {
  let open = true;
  try { open = localStorage.getItem("nj.navOpen") !== "0"; } catch (e) {}
  const apply = () => { document.body.classList.toggle("nav-collapsed", !open); $("#navToggle").setAttribute("aria-expanded", String(open)); };
  const set = (v) => { open = v; apply(); try { localStorage.setItem("nj.navOpen", open ? "1" : "0"); } catch (e) {} };
  $("#navToggle").addEventListener("click", () => set(!open));
  // "/" anywhere (not while typing): open Sites and jump to its search box
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.querySelector("dialog[open]")) return;
    e.preventDefault(); set(true); $("#search").focus();
  });
  apply();
})();

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
function setConnPop(open) {
  $("#connPop").hidden = !open; $("#connBtn").setAttribute("aria-expanded", String(open));
  if (open) { refreshAdapters(); $("#adapter").focus(); }
}
$("#connBtn").addEventListener("click", () => setConnPop($("#connPop").hidden));
document.addEventListener("click", (e) => { if (!$("#connPop").hidden && !$("#conn").contains(e.target)) setConnPop(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#connPop").hidden) { setConnPop(false); $("#connBtn").focus(); } });
$("#checkInetBtn").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const r = await api("/internet", { method: "POST" });
  if (r.internet === 2) flash(r.detail, "warn"); else flash(`Internet check: ${inetInfo(r.internet).text}`);
  await refreshStatus(); }));
$("#search").addEventListener("input", () => { renderTree(); searchDevicesSoon(); });
$("#newSiteBtn").addEventListener("click", () => { $("#addMenu").open = false; openNode({ kind: "site" }); });
$("#importSiteFile").addEventListener("change", async (e) => {
  const file = e.target.files[0]; $("#addMenu").open = false; if (file) await importFromFile(file); e.target.value = "";
});
$("#dhcpBtn").addEventListener("click", async (e) => {
  await withBusy(e.currentTarget, async () => { await api("/dhcp", { method: "POST" }); flash("Adapter set back to DHCP"); });
  await refreshStatus();   // after withBusy re-enables the button, so the DHCP state decides
  refreshDetail();
});
// Toasts sit just under the top bar, whatever height it wraps to.
if (window.ResizeObserver) {
  const ro = new ResizeObserver(() => document.documentElement.style.setProperty("--tb-h",
    `${Math.round(Math.max($("#topbar").getBoundingClientRect().bottom, $("#detail").getBoundingClientRect().bottom))}px`));
  ro.observe($("#topbar")); ro.observe($("#detail"));
}

(async function init() {
  try { applyTheme((await api("/settings/ui")).theme); } catch (e) { try { applyTheme(localStorage.getItem("netjoin-theme")); } catch (e2) {} }
  await refreshStatus(); await refreshAdapters(); await refreshTree();
  try { state.scan = await api("/scan"); } catch (e) {}
  await refreshDetail(); await refreshLog(); if (state.scan.running) pollScan();
  // Battery: no background polling of the adapter (each read runs netsh). It's read again after every
  // action, when the window comes back into focus, and from the ↻ on This PC. The Activity log (cheap,
  // in memory) is only polled while the window is visible.
  setInterval(() => { if (document.visibilityState === "visible") refreshLog(); }, 3000);
  let lastFocusRead = Date.now();
  const onBack = () => {
    if (document.visibilityState !== "visible" || state.busy || Date.now() - lastFocusRead < 3000) return;
    lastFocusRead = Date.now();
    refreshStatus().then(() => { if (state.selected === "home") renderHome(); });
    refreshLog();
  };
  window.addEventListener("focus", onBack); document.addEventListener("visibilitychange", onBack);
})();
