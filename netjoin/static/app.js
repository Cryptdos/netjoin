"use strict";
// NetJoin front end: plain JS, no build step.

const $ = (s) => document.querySelector(s);
const state = { tree: [], selected: null, detail: null, status: null, expanded: new Set(), logAfter: 0, busy: false };

try { JSON.parse(localStorage.getItem("nj.expanded") || "[]").forEach((id) => state.expanded.add(id)); } catch (e) {}
try { state.selected = JSON.parse(localStorage.getItem("nj.selected") || "null"); } catch (e) {}
const saveUi = () => { try {
  localStorage.setItem("nj.expanded", JSON.stringify([...state.expanded]));
  localStorage.setItem("nj.selected", JSON.stringify(state.selected));
} catch (e) {} };

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

function flash(msg, level = "info") {
  const b = $("#banner");
  b.textContent = msg; b.hidden = false;
  b.style.background = level === "error" ? "var(--err-bg)" : "var(--accent-bg)";
  b.style.color = level === "error" ? "var(--err)" : "var(--accent-d)";
  clearTimeout(flash.t); flash.t = setTimeout(() => (b.hidden = true), 7000);
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
      el("span", { class: "small muted" }, (s.adapter.dhcp ? "DHCP" : "Static") + (s.joined ? ` · ${s.joined.network}` : "")));
    if (s.internet !== null && s.internet !== undefined) {
      cur.append(el("span", { class: "inet" + (s.internet ? " yes" : "") }, globe(!s.internet), s.internet ? "Internet" : "No internet"));
    }
    cur.append(el("button", { type: "button", class: "btn small", onclick: (e) => withBusy(e.currentTarget, async () => { await api("/internet", { method: "POST" }); await refreshStatus(); }) }, "Check internet"));
  }
  const rj = $("#rejoinBtn");
  if (s.last_network && !s.joined) { rj.hidden = false; rj.textContent = `Rejoin ${s.last_network.network}`; }
  else rj.hidden = true;
  if (!s.admin) { const b = $("#banner"); b.hidden = false; b.textContent = "NetJoin isn't running as administrator, so Join and DHCP won't work. Close it and start it again, accepting the Windows prompt."; }
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
    : el("span", { class: "pill" + (d.last_internet ? " yes" : "") }, globe(!d.last_internet), d.last_internet ? "Had internet at last join" : "No internet at last join");
  const joinBtn = el("button", { type: "button", class: "btn btn-primary", style: "padding:0 24px", onclick: (e) => doJoin(e.currentTarget) }, "Join");
  const r = d.resolved;
  const tile = (k, v, from) => el("div", { class: "tile" }, el("span", { class: "k" }, k), el("span", { class: "v" }, v), from ? el("span", { class: "from" }, from) : null);
  box.replaceChildren(
    el("div", { class: "dhead" },
      el("div", { class: "row", style: "flex-direction:column;align-items:flex-start;gap:4px" }, crumbs, el("h1", {}, d.name)),
      el("div", { style: "margin-top:22px" }, inetPill),
      el("div", { class: "spacer" }),
      el("div", { class: "row" }, editBtn, el("button", { type: "button", class: "btn", disabled: true, title: "Arrives in Phase 2" }, "Scan"), joinBtn)),
    el("div", { class: "tiles" },
      tile("Subnet", d.subnet),
      tile("Allowed laptop range", `.${d.range_start.split(".").pop()} – .${d.range_end.split(".").pop()}`, `${d.range_start} – ${d.range_end}`),
      tile("Gateway / DNS", `${r.gateway || "—"} / ${r.dns.length ? r.dns.join(", ") : "—"}`,
        r.gateway_from || r.dns_from ? `from ${[r.gateway_from, r.dns_from].filter(Boolean).join(" · ")}` : "none (default)"),
      tile("Next IP on Join", d.next_ip || "—", d.next_ip && d.next_ip === d.last_worked ? "last worked" : "first in range")),
    ...(d.notes ? [el("p", { class: "muted", style: "margin:12px 0 0;white-space:pre-line" }, d.notes)] : []));
  renderHistory();
}

function renderHistory() {
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
    flash(`Joined ${d.name} as ${r.ip}/${r.prefix}${r.attempts > 1 ? ` (after ${r.attempts - 1} clash${r.attempts > 2 ? "es" : ""})` : ""} · internet: ${r.internet ? "yes" : "no"}`);
  });
  await Promise.all([refreshStatus(), refreshTree()]); await refreshDetail();
}

// ---------- node dialog ----------
function openNode({ node, parent, kind }) {
  const f = $("#nodeForm"); f.reset(); $("#nodeErr").textContent = "";
  const k = node ? node.kind : kind || (parent ? "area" : "site");
  f.id.value = node ? node.id : ""; f.kind.value = k; f.parent_id.value = node ? (node.parent_id ?? "") : parent ? parent.id : "";
  $("#nodeTitle").textContent = node ? `Edit ${k}` : `Add ${k}${parent ? " to " + parent.name : ""}`;
  $("#deleteNodeBtn").hidden = !node;
  f.querySelector(".net-only").hidden = k !== "network";
  f.subnet.required = k === "network";
  if (!node && parent && !kind) {   // let the user choose area or network
    $("#nodeTitle").replaceChildren(`Add to ${parent.name}: `,
      el("select", { id: "kindPick", onchange: (e) => { f.kind.value = e.target.value; f.querySelector(".net-only").hidden = e.target.value !== "network"; f.subnet.required = e.target.value === "network"; } },
        el("option", { value: "area" }, "Area"), el("option", { value: "network" }, "Network")));
  }
  if (node) {
    for (const key of ["name", "subnet", "range_start", "range_end", "gateway", "dns", "notes"]) if (f[key]) f[key].value = node[key] || "";
    f.gw_override.checked = !!node.gw_override; f.dns_override.checked = !!node.dns_override;
  }
  syncOverrides(f);
  $("#nodeDlg").showModal(); f.name.focus();
}

// Gateway / DNS inputs are only editable while their override box is ticked.
function syncOverrides(f) {
  f.gateway.disabled = !f.gw_override.checked;
  f.dns.disabled = !f.dns_override.checked;
  const sg = f.querySelector("#suggestGw"); if (sg) sg.disabled = !f.gw_override.checked;
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
  if (f.kind.value === "network") Object.assign(body, { subnet: f.subnet.value.trim(), range_start: f.range_start.value.trim(), range_end: f.range_end.value.trim() });
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
$("#suggestGw").addEventListener("click", () => {
  const f = $("#nodeForm");
  let sub = f.subnet.value.trim();
  if (!sub && state.detail && state.detail.subnet) sub = state.detail.subnet;
  const m = sub.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)/);
  if (!m) { $("#nodeErr").textContent = "Enter the subnet first (gateway suggestions only work on networks)."; return; }
  f.gateway.value = `${m[1]}.${m[2]}.${m[3]}.1`;
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
    ["Npcap", (s.npcap && s.npcap.installed) ? "Installed" : "Not detected – passive listening and raw ARP sweep will be greyed out (Phase 2)"],
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
    for (const it of items) { box.prepend(el("div", { class: it.level }, `${it.ts}  ${it.msg}`)); state.logAfter = it.id; }
    while (box.childElementCount > 200) box.lastElementChild.remove();
  } catch (e) {}
}

// ---------- wiring ----------
$("#adapter").addEventListener("change", async (e) => {
  try { await api("/adapter", { method: "POST", body: { name: e.target.value } }); } catch (err) { flash(err.message, "error"); }
  await refreshStatus(); refreshLog();
});
$("#showAll").addEventListener("change", refreshAdapters);
$("#search").addEventListener("input", renderTree);
$("#addSiteBtn").addEventListener("click", () => openNode({ kind: "site" }));
$("#dhcpBtn").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  await api("/dhcp", { method: "POST" }); flash("Adapter set back to DHCP");
  await refreshStatus(); }));
$("#rejoinBtn").addEventListener("click", (e) => withBusy(e.currentTarget, async () => {
  const r = await api("/rejoin", { method: "POST" }); flash(`Rejoined as ${r.ip}/${r.prefix}`);
  await refreshStatus(); await refreshTree(); await refreshDetail(); }));

(async function init() {
  await refreshStatus(); await refreshAdapters(); await refreshTree(); await refreshDetail(); await refreshLog();
  setInterval(refreshStatus, 5000); setInterval(refreshLog, 3000);
  setInterval(() => { if (document.visibilityState === "visible" && !state.busy && document.activeElement !== $("#adapter")) refreshAdapters(); }, 15000);
})();
