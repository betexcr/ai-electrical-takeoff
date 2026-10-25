// Ground-truth reviewer (vanilla JS, no build).
//
// Coordinates: annotations are stored in canonical normalized (u, v) page space.
// The served image covers meta.image.covers (normalized) at width_px x height_px.
// View transform: screen = (tx + imgX * s, ty + imgY * s). Markers are drawn in
// screen space (constant size), so every position goes through the functions
// below; window.gt exposes them for the end-to-end tests.
//
// Blind scan: when the sheet phase is "blind_scan" this page fetches only /meta,
// /progress and /blind (the server also refuses the others with 409).

"use strict";

const CATS = ["switch", "receptacle", "light", "ceiling_fan", "fan", "detector", "junction_box", "other_electrical"];
const COLORS = { switch: "#d62728", receptacle: "#1f77b4", light: "#2ca02c", ceiling_fan: "#9467bd", fan: "#8c564b",
  detector: "#e377c2", junction_box: "#ff7f0e", other_electrical: "#7f7f7f" };
const ABBR = { switch: "S", receptacle: "R", light: "L", ceiling_fan: "CF", fan: "F", detector: "D", junction_box: "J",
  other_electrical: "?" };

const st = {
  config: null, sheets: [], sheetId: null, meta: null, progress: null,
  annotations: [], clusters: [], warnings: [], observations: [], reconcile: null,
  view: { s: 1, tx: 0, ty: 0 }, selected: null, placing: false, category: "switch", cellIndex: 0,
  drag: null,
};

// ---------------------------------------------------------------- coordinates

function normToImg(u, v) {
  const m = st.meta.image, c = m.covers;
  return [(u - c.u0) / (c.u1 - c.u0) * m.width_px, (v - c.v0) / (c.v1 - c.v0) * m.height_px];
}
function imgToNorm(x, y) {
  const m = st.meta.image, c = m.covers;
  return [c.u0 + x / m.width_px * (c.u1 - c.u0), c.v0 + y / m.height_px * (c.v1 - c.v0)];
}
function imgToScreen(x, y) { return [st.view.tx + x * st.view.s, st.view.ty + y * st.view.s]; }
function screenToImg(sx, sy) { return [(sx - st.view.tx) / st.view.s, (sy - st.view.ty) / st.view.s]; }
function normToScreen(u, v) { const [x, y] = normToImg(u, v); return imgToScreen(x, y); }
function screenToNorm(sx, sy) { const [x, y] = screenToImg(sx, sy); return imgToNorm(x, y); }
function distPt(a, b) {
  const g = st.meta.geometry;
  return Math.hypot((a.u - b.u) * g.displayed_width_pt, (a.v - b.v) * g.displayed_height_pt);
}
function ptToScreenLen(pt) { return pt * st.meta.image.dpi / 72 * st.view.s; }

// ---------------------------------------------------------------- api

function reviewer() {
  return (document.getElementById("reviewer").value || "").trim() || st.config?.default_reviewer || "reviewer";
}

async function api(method, url, body) {
  const opts = { method, headers: { "X-Reviewer": reviewer() } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    const msg = data ? (data.message + (data.reasons ? " — " + data.reasons.join("; ") : "")) : r.statusText;
    toast(msg, true);
    throw new Error(msg);
  }
  return data;
}

function toast(msg, error = false) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "show" + (error ? " error" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.className = ""; }, error ? 5000 : 1800);
}

// ---------------------------------------------------------------- loading

const phase = () => st.meta?.review?.phase;
const blind = () => phase() === "blind_scan";

async function loadSheet(sheetId, keepView = false) {
  // Fetch everything first, then swap state in one step (no transient empty overlay).
  const meta = await api("GET", `/api/sheets/${sheetId}/meta`);
  const p = meta.review.phase;
  const next = { annotations: [], clusters: [], warnings: [], observations: [], reconcile: null };
  next.progress = await api("GET", `/api/sheets/${sheetId}/progress`);
  if (p === "blind_scan") {
    next.observations = (await api("GET", `/api/sheets/${sheetId}/blind`)).observations;
  } else {
    next.annotations = await api("GET", `/api/sheets/${sheetId}/annotations`);
    next.warnings = await api("GET", `/api/sheets/${sheetId}/warnings`);
    if (p === "pass1") next.clusters = (await api("GET", `/api/sheets/${sheetId}/candidates`)).clusters;
    if (p === "reconcile" || p === "verified") {
      next.reconcile = await api("GET", `/api/sheets/${sheetId}/reconcile`);
      next.observations = next.reconcile.observations;
    }
  }
  Object.assign(st, { sheetId, meta }, next);
  const img = document.getElementById("plan");
  if (img.dataset.sheet !== sheetId) {
    img.dataset.sheet = sheetId;
    img.width = st.meta.image.width_px; img.height = st.meta.image.height_px;
    await new Promise((res) => { img.onload = res; img.src = `/api/sheets/${sheetId}/image`; });
    if (!keepView) fitView();
  }
  if (st.selected && !findSelected()) st.selected = null;
  renderAll();
}

async function refresh() { await loadSheet(st.sheetId, true); }

// ---------------------------------------------------------------- view

function applyView() {
  const { s, tx, ty } = st.view;
  document.getElementById("plan").style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
  renderOverlay();
}
function setView(s, tx, ty) { st.view = { s, tx, ty }; applyView(); }
function fitView() {
  const vp = document.getElementById("viewport").getBoundingClientRect();
  const m = st.meta.image;
  const s = Math.min(vp.width / m.width_px, vp.height / m.height_px) * 0.96;
  setView(s, (vp.width - m.width_px * s) / 2, (vp.height - m.height_px * s) / 2);
}
function minScale() {
  const vp = document.getElementById("viewport").getBoundingClientRect();
  return Math.min(vp.width / st.meta.image.width_px, vp.height / st.meta.image.height_px) * 0.25;
}
function zoomAt(sx, sy, factor) {
  const s = Math.min(8, Math.max(minScale(), st.view.s * factor));
  const [x, y] = screenToImg(sx, sy);
  setView(s, sx - x * s, sy - y * s);
}
function centerOnNorm(u, v, s) {
  const vp = document.getElementById("viewport").getBoundingClientRect();
  const [x, y] = normToImg(u, v);
  s = s || st.view.s;
  setView(s, vp.width / 2 - x * s, vp.height / 2 - y * s);
}
function goToCell(i) {
  const cells = st.meta.cells;
  st.cellIndex = (i + cells.length) % cells.length;
  const b = cells[st.cellIndex].bbox;
  const vp = document.getElementById("viewport").getBoundingClientRect();
  const [x0, y0] = normToImg(b.u0, b.v0), [x1, y1] = normToImg(b.u1, b.v1);
  const margin = 1.25; // show a little of the neighbouring cells
  const s = Math.min(vp.width / ((x1 - x0) * margin), vp.height / ((y1 - y0) * margin));
  setView(s, vp.width / 2 - (x0 + x1) / 2 * s, vp.height / 2 - (y0 + y1) / 2 * s);
  renderPanel();
}

// ---------------------------------------------------------------- rendering

function svg(tag, attrs, parent) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

function renderAll() { renderTopbar(); applyView(); renderPanel(); }

function renderOverlay() {
  const o = document.getElementById("overlay");
  o.innerHTML = "";
  if (!st.meta) return;
  const show = (id) => document.getElementById(id).checked;
  // coverage grid
  if (show("show-grid")) {
    const g = svg("g", { class: "layer-grid" }, o);
    st.meta.cells.forEach((c, i) => {
      const [x0, y0] = normToScreen(c.bbox.u0, c.bbox.v0), [x1, y1] = normToScreen(c.bbox.u1, c.bbox.v1);
      const done = blind() ? c.blind_scan_done : c.pass1_done;
      svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, class: "cell", "data-cell": c.id,
        fill: done ? "rgba(43,122,61,0.07)" : "rgba(200,120,0,0.06)",
        stroke: i === st.cellIndex ? "#1f5fbf" : "rgba(0,0,0,0.18)", "stroke-width": i === st.cellIndex ? 2.5 : 1 }, g);
    });
  }
  if (show("show-rooms")) {
    const g = svg("g", { class: "layer-rooms" }, o);
    for (const r of st.meta.rooms.filter((r) => r.kind === "labeled")) {
      const [x0, y0] = normToScreen(r.bbox.u0, r.bbox.v0), [x1, y1] = normToScreen(r.bbox.u1, r.bbox.v1);
      svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: "none", stroke: "#2b7a3d", "stroke-width": 1,
        "stroke-dasharray": "3 2", class: "room" }, g);
    }
  }
  if (show("show-tokens")) {
    const g = svg("g", { class: "layer-tokens" }, o);
    for (const t of st.meta.tokens) {
      const [x0, y0] = normToScreen(t.bbox.u0, t.bbox.v0), [x1, y1] = normToScreen(t.bbox.u1, t.bbox.v1);
      svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: "none", stroke: t.short ? "#c0c" : "#999",
        "stroke-width": 1, class: "token", "data-token": t.id }, g);
    }
  }
  // candidates (pass 1 only; never fetched during the blind scan)
  if (!blind() && show("show-candidates")) {
    const g = svg("g", { class: "layer-candidates" }, o);
    for (const cl of st.clusters) {
      if (!cl.unreviewed) continue;
      const [x, y] = normToScreen(cl.point.u, cl.point.v);
      const r = Math.max(9, ptToScreenLen(5));
      const el = svg("rect", { x: x - r, y: y - r, width: 2 * r, height: 2 * r, fill: "rgba(255,255,255,0.01)",
        stroke: st.selected?.type === "cluster" && st.selected.id === cl.id ? "#000" : "#ff7f0e",
        "stroke-width": 2, "stroke-dasharray": "4 3", class: "candidate hit", "data-cluster": cl.id,
        "data-cx": x, "data-cy": y }, g);
      el.addEventListener("mousedown", (e) => { if (!st.placing) { e.stopPropagation(); select({ type: "cluster", id: cl.id }); } });
    }
  }
  // annotations (never fetched during the blind scan)
  if (!blind() && show("show-annotations")) {
    const g = svg("g", { class: "layer-annotations" }, o);
    for (const a of st.annotations) {
      const [x, y] = normToScreen(a.point.u, a.point.v);
      const sel = st.selected?.type === "annotation" && st.selected.id === a.id;
      const grp = svg("g", { class: "annotation hit", "data-id": a.id, "data-category": a.category, "data-cx": x, "data-cy": y }, g);
      svg("circle", { cx: x, cy: y, r: sel ? 9 : 7, fill: a.review_state === "verified" ? COLORS[a.category] : "rgba(255,255,255,0.55)",
        stroke: a.review_state === "needs_adjudication" ? "#b35900" : COLORS[a.category], "stroke-width": sel ? 3.5 : 2.5,
        "stroke-dasharray": a.review_state === "needs_adjudication" ? "3 2" : "" }, grp);
      svg("text", { x: x + 9, y: y - 8, "font-size": 11, fill: COLORS[a.category], "font-weight": 700 }, grp).textContent = ABBR[a.category];
      grp.addEventListener("mousedown", (e) => { if (!st.placing) { e.stopPropagation(); select({ type: "annotation", id: a.id }); } });
    }
  }
  // blind observations (blind scan: own marks; reconcile: work items)
  if (blind() || phase() === "reconcile" || phase() === "verified") {
    const g = svg("g", { class: "layer-observations" }, o);
    for (const ob of st.observations) {
      if (ob.deleted) continue;
      const [x, y] = normToScreen(ob.point.u, ob.point.v);
      const sel = st.selected?.type === "observation" && st.selected.id === ob.id;
      const d = sel ? 10 : 8;
      const el = svg("path", { d: `M ${x} ${y - d} L ${x + d} ${y} L ${x} ${y + d} L ${x - d} ${y} Z`,
        fill: ob.reconciliation === "pending" ? "rgba(106,44,145,0.25)" : "rgba(106,44,145,0.05)",
        stroke: "#6a2c91", "stroke-width": sel ? 3 : 2, class: "observation hit", "data-id": ob.id,
        "data-cx": x, "data-cy": y }, g);
      el.addEventListener("mousedown", (e) => { if (!st.placing) { e.stopPropagation(); select({ type: "observation", id: ob.id }); } });
    }
  }
  if (st.selected?.type === "new") {
    const [x, y] = normToScreen(st.selected.point.u, st.selected.point.v);
    svg("circle", { cx: x, cy: y, r: 8, fill: "none", stroke: "#000", "stroke-width": 2, class: "pending-marker" }, o);
  }
}

function renderTopbar() {
  const p = phase();
  const el = document.getElementById("phase");
  el.textContent = { pass1: "Pass 1 — annotate", blind_scan: "Pass 2a — blind scan", reconcile: "Pass 2b — reconcile",
    verified: "Verified" }[p];
  el.className = "phase " + p;
  for (const name of ["candidates", "annotations"]) {
    document.querySelector(`#layers [data-layer=${name}]`).style.display = p === "blind_scan" ? "none" : "";
  }
  const acts = document.getElementById("phase-actions");
  acts.innerHTML = "";
  const btn = (label, fn, primary = false) => {
    const b = document.createElement("button"); b.textContent = label; if (primary) b.className = "primary";
    b.onclick = async () => { try { await fn(); await refresh(); } catch (e) { /* toast shown */ } };
    acts.appendChild(b); return b;
  };
  const sid = st.sheetId;
  if (p === "pass1") {
    btn(st.meta.review.region_confirmed ? "Region confirmed ✓" : "Confirm region", () => api("POST", `/api/sheets/${sid}/region`, { confirmed: true }));
    btn("Flag region", () => { const r = prompt("Why does the region not cover the electrical plan?"); return r && api("POST", `/api/sheets/${sid}/region`, { confirmed: false, reason: r }); });
    btn("Start blind scan", () => confirm("Start the blind scan? Candidates and your annotations will be hidden until every cell is blind-scanned.") && api("POST", `/api/sheets/${sid}/phase/start-blind-scan`), true);
  } else if (p === "blind_scan") {
    btn("Open reconciliation", () => confirm("Close the blind scan for good and reveal annotations?") && api("POST", `/api/sheets/${sid}/phase/open-reconcile`), true);
  } else if (p === "reconcile") {
    btn("Complete sheet", () => api("POST", `/api/sheets/${sid}/phase/complete`), true);
  } else if (p === "verified") {
    btn("Reopen", () => { const r = prompt("Reason for reopening this verified sheet?"); return r && api("POST", `/api/sheets/${sid}/phase/reopen`, { reason: r }); });
  }
  btn("Export", async () => { const r = await api("POST", "/api/export"); toast(`Export ${r.level}: ${r.path}`); });
  document.getElementById("mode-banner").textContent = st.placing
    ? `Placing ${blind() ? "observation" : "annotation"}: ${st.category} — click on the plan (Esc to cancel)`
    : (blind() ? "BLIND SCAN — candidates and annotations hidden" : "");
}

function kv(obj) {
  return `<div class="kv">${Object.entries(obj).map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("")}</div>`;
}

function renderPanel() {
  const p = st.progress, cells = st.meta.cells;
  const cell = cells[st.cellIndex];
  document.getElementById("cell-nav").innerHTML = `<span class="badge" data-testid="cell-nav">cell ${st.cellIndex + 1}/${cells.length} ${cell.id.split(":")[1]}</span>`;
  const prog = { phase: p.phase, cells: blind() ? `${p.cells_blind_scan_done}/${p.cells_total} blind-scanned`
    : `${p.cells_pass1_done}/${p.cells_total} pass 1 · ${p.cells_blind_scan_done}/${p.cells_total} blind` };
  if (!blind()) {
    prog.annotations = p.annotations;
    prog["by state"] = Object.entries(p.annotations_by_state).map(([k, v]) => `${k} ${v}`).join(", ") || "—";
    prog["candidates unreviewed"] = `${p.candidates_unreviewed} (of ${p.candidate_clusters} clusters)`;
    prog["warnings open"] = st.warnings.filter((w) => !w.acknowledged).length;
    prog.observations = `${p.observations} ` + Object.entries(p.observations_by_reconciliation).map(([k, v]) => `${k} ${v}`).join(", ");
  } else {
    prog.observations = p.observations;
  }
  document.getElementById("progress").innerHTML = kv(prog) +
    (!blind() && p.tallies ? `<details><summary>tallies (review progress, not results)</summary>${kv(p.tallies)}</details>` : "");
  renderDetails();
  renderLists();
  document.getElementById("help").textContent = blind()
    ? "C / shift-click: place observation · 1-8: category · N/P: next/prev cell · Space: mark cell blind-scanned · Del: remove selected observation · wheel: zoom · drag: pan · F: fit"
    : phase() === "reconcile"
      ? "G: next pending item · M: match · O: promote · D: dismiss · K: keep unseen · V: save & verify · Esc: leave a field · C / shift-click: place annotation · 1-8: category · Del: delete selected · wheel: zoom · drag: pan · F: fit"
      : "C / shift-click: place annotation · 1-8: category · A: accept candidate · R: reject candidate · N/P: next/prev cell · Space: mark cell done · Del: delete selected · wheel: zoom · drag: pan · F: fit";
}

function findSelected() {
  const s = st.selected;
  if (!s) return null;
  if (s.type === "annotation") return st.annotations.find((a) => a.id === s.id);
  if (s.type === "cluster") return st.clusters.find((c) => c.id === s.id);
  if (s.type === "observation") return st.observations.find((o) => o.id === s.id);
  if (s.type === "new") return s;
  return null;
}

function select(sel) { st.selected = sel; renderOverlay(); renderDetails(); }

function nearbyTokens(point) {
  const r = st.config.tolerances_pt.token_suggest;
  return st.meta.tokens.map((t) => ({ t, d: distPt(point, t.center) })).filter((x) => x.d <= r).sort((a, b) => a.d - b.d);
}

function suggestSubtype(category, tokens) {
  const rules = st.config.taxonomy.token_rules;
  for (const t of tokens) {
    const rule = rules[t.text.toUpperCase()];
    if (rule && rule[category]) return rule[category][0];
  }
  const subs = st.config.taxonomy.categories[category].map((s) => s.name);
  return category === "switch" ? "standard" : subs[0];
}

function roomOptions(current) {
  const rooms = st.meta.rooms;
  const counts = {};
  rooms.forEach((r) => { counts[r.name] = (counts[r.name] || 0) + 1; });
  return `<option value="">— room —</option>` + rooms.map((r, i) => {
    const label = r.kind === "labeled" && counts[r.name] > 1 ? `${r.name} @(${Math.round(r.label_bbox_pt[0])},${Math.round(r.label_bbox_pt[1])})` : r.name;
    const same = current && current.kind === r.kind && current.name === r.name &&
      JSON.stringify(current.label_bbox_pt || null) === JSON.stringify(r.label_bbox_pt || null);
    return `<option value="${i}" ${same ? "selected" : ""}>${label}</option>`;
  }).join("");
}

// Subtypes are stored by their stable id (s.name) but shown with the sheets' exact legend wording.
function subLabel(category, subtype) {
  const s = (st.config.taxonomy.categories[category] || []).find((x) => x.name === subtype);
  return s ? s.label : subtype;
}

function subtypeOptions(category, current) {
  return st.config.taxonomy.categories[category].map((s) =>
    `<option value="${s.name}" ${s.name === current ? "selected" : ""}>${s.label}</option>`).join("");
}

function deviceForm(prefix, v, tokensNear) {
  const linked = new Set((v.text_tokens || []).map((t) => t.text + JSON.stringify(t.bbox_pt)));
  return `<div class="form" id="${prefix}-form">
    <label>Category <select id="${prefix}-category">${CATS.map((c) => `<option ${c === v.category ? "selected" : ""}>${c}</option>`).join("")}</select></label>
    <label>Subtype <select id="${prefix}-subtype">${subtypeOptions(v.category, v.subtype)}</select></label>
    <label>Room <select id="${prefix}-room">${roomOptions(v.room)}</select></label>
    <div>Nearby text tokens (link the ones that belong to this device):<br>
      ${tokensNear.map(({ t, d }) => `<label style="display:inline-block;margin-right:8px"><input type="checkbox" class="${prefix}-tok" value="${t.id}"
        ${linked.has(t.text + JSON.stringify(t.bbox_pt)) ? "checked" : ""}> ${t.text} <span class="muted">${d.toFixed(0)}pt</span></label>`).join("") || '<span class="muted">none within radius</span>'}</div>
    <label>Notes <textarea id="${prefix}-notes" rows="2">${v.notes || ""}</textarea></label>
  </div>`;
}

function readForm(prefix) {
  const category = document.getElementById(`${prefix}-category`).value;
  const subtype = document.getElementById(`${prefix}-subtype`).value;
  const ri = document.getElementById(`${prefix}-room`).value;
  const room = ri === "" ? null : (({ kind, name, label_bbox_pt }) => ({ kind, name, label_bbox_pt }))(st.meta.rooms[+ri]);
  const ids = [...document.querySelectorAll(`.${prefix}-tok:checked`)].map((e) => e.value);
  const text_tokens = st.meta.tokens.filter((t) => ids.includes(t.id)).map((t) => ({ text: t.text, bbox_pt: t.bbox_pt }));
  const notes = document.getElementById(`${prefix}-notes`).value.trim() || null;
  return { category, subtype, room, text_tokens, notes };
}

function wireCategory(prefix) {
  const cat = document.getElementById(`${prefix}-category`);
  cat.onchange = () => {
    document.getElementById(`${prefix}-subtype`).innerHTML = subtypeOptions(cat.value, suggestSubtype(cat.value, []));
  };
}

function renderDetails() {
  const d = document.getElementById("details");
  const s = st.selected, item = findSelected();
  if (!s || !item) { d.innerHTML = '<span class="muted">Nothing selected. Shift-click or press C to place.</span>'; return; }
  const sid = st.sheetId;
  const act = (html) => { d.innerHTML = html; };
  if (s.type === "new") {
    const near = nearbyTokens(s.point);
    if (blind()) {
      act(`<div class="form">Observation at (${s.point.u.toFixed(4)}, ${s.point.v.toFixed(4)})
        <label>Category <select id="new-category">${CATS.map((c) => `<option ${c === st.category ? "selected" : ""}>${c}</option>`).join("")}</select></label>
        <button class="primary" id="save">Save observation (Enter)</button></div>`);
      document.getElementById("save").onclick = saveNew;
      return;
    }
    const sub = suggestSubtype(st.category, near.map((x) => x.t));
    const autoTokens = near.filter(({ t }) => (st.config.taxonomy.token_rules[t.text.toUpperCase()] || {})[st.category]).slice(0, 1)
      .map(({ t }) => ({ text: t.text, bbox_pt: t.bbox_pt }));
    act(`<b>New annotation</b> <span class="muted">(${s.point.u.toFixed(4)}, ${s.point.v.toFixed(4)})</span>` +
      deviceForm("new", { category: st.category, subtype: sub, text_tokens: autoTokens }, near) +
      `<label><input type="checkbox" id="new-adjudicate"> needs adjudication</label>
       <button class="primary" id="save">Save (Enter)</button> <button id="cancel">Cancel (Esc)</button>`);
    wireCategory("new");
    document.getElementById("save").onclick = saveNew;
    document.getElementById("cancel").onclick = () => select(null);
    return;
  }
  if (s.type === "cluster") {
    const best = item.members[0];
    act(`<b>Candidate cluster</b> <span class="badge">candidate, not a detection</span>
      <div class="muted">${item.members.map((m) => `${m.category_hint ? subLabel(m.category_hint, m.subtype_hint) : m.class_hypothesis} · ${m.generator} · ${m.score ?? "vector"}`).join("<br>")}</div>` +
      deviceForm("acc", { category: best.category_hint || st.category, subtype: best.subtype_hint, text_tokens: [] }, nearbyTokens(item.point)) +
      `<button class="primary" id="accept">Accept / correct (A)</button> <button id="reject">Reject (R)</button>`);
    wireCategory("acc");
    document.getElementById("accept").onclick = () => acceptSelected();
    document.getElementById("reject").onclick = () => rejectSelected();
    return;
  }
  if (s.type === "observation") {
    const o = item;
    if (blind()) {
      act(`<b>Observation</b> ${o.category} <span class="muted">${o.cell_id || "outside grid"}</span>
        <div><button id="del-obs">Remove (Del)</button></div>`);
      document.getElementById("del-obs").onclick = () => deleteSelected();
      return;
    }
    const sug = (st.reconcile?.suggestions || []).find((x) => x.observation_id === o.id);
    let html = `<b>Blind observation</b> ${o.category}${o.subtype ? " · " + subLabel(o.category, o.subtype) : ""} · <span class="badge">${o.reconciliation}</span>`;
    if (o.reconciliation === "pending" && phase() === "reconcile") {
      html += sug && sug.suggested_annotation_id
        ? `<div>Suggested match: ${sug.suggested_annotation_id.slice(0, 8)} at ${sug.distance_pt} pt ${sug.conflicts.length ? '<br><span style="color:#b35900">' + sug.conflicts.join("; ") + "</span>" : ""}</div>`
        : `<div class="muted">No annotation within ${st.config.tolerances_pt.match} pt — likely missed in pass 1.</div>`;
      html += `<div class="form"><label>Promote as subtype <select id="promote-subtype">${subtypeOptions(o.category, o.subtype || suggestSubtype(o.category, nearbyTokens(o.point).map((x) => x.t)))}</select></label></div>
        <button class="primary" id="match" ${sug?.suggested_annotation_id ? "" : "disabled"}>Match to suggestion (M)</button>
        <button id="promote">Promote (new device) (O)</button> <button id="dismiss">Dismiss (D)</button>`;
    }
    act(html);
    if (o.reconciliation === "pending" && phase() === "reconcile") {
      document.getElementById("match").onclick = () => runThenNext(matchSelected);
      document.getElementById("promote").onclick = () => runThenNext(promoteSelected);
      document.getElementById("dismiss").onclick = () => runThenNext(dismissSelected);
    }
    return;
  }
  // annotation
  const a = item;
  const editable = phase() === "pass1" || phase() === "reconcile";
  let html = `<b>Annotation</b> <span class="badge">${a.review_state}</span> <span class="badge">${a.provenance.origin}</span>
    ${a.blind_confirmation ? `<span class="badge">${a.blind_confirmation}</span>` : ""}
    <div class="muted">id ${a.id.slice(0, 8)} · rev ${a.revision} · (${a.point.u.toFixed(4)}, ${a.point.v.toFixed(4)}) · ${a.point_pt ? a.point_pt.map((v) => v.toFixed(1)).join(", ") + " pt" : ""}</div>`;
  html += deviceForm("ed", a, nearbyTokens(a.point));
  if (editable) {
    html += `<label><input type="checkbox" id="ed-adjudicate" ${a.review_state === "needs_adjudication" ? "checked" : ""}> needs adjudication</label>
      <button class="primary" id="save-ann">Save</button> <button id="del-ann">Delete…</button> <button id="hist">History</button>`;
    if (phase() === "reconcile") {
      if (!a.blind_confirmation) html += `<div>Not matched by the blind scan: <button id="keep">Keep… (K)</button> <button id="unseen-del">Delete…</button></div>`;
      else if (a.review_state !== "verified") html += ` <button class="primary" id="verify">Save &amp; verify (V)</button>`;
    }
  }
  act(html + '<pre id="hist-out" class="help"></pre>');
  if (!editable) { d.querySelectorAll("select, textarea, input").forEach((e) => { e.disabled = true; }); return; }
  wireCategory("ed");
  document.getElementById("save-ann").onclick = () => run(() => {
    const f = readForm("ed");
    return api("PATCH", `/api/annotations/${a.id}`, { ...f, review_state: document.getElementById("ed-adjudicate").checked ? "needs_adjudication" : "draft" });
  });
  document.getElementById("del-ann").onclick = () => deleteSelected();
  document.getElementById("hist").onclick = async () => {
    const h = await api("GET", `/api/annotations/${a.id}/history`);
    document.getElementById("hist-out").textContent = h.map((e) => `${e.timestamp.slice(0, 19)} ${e.actor} ${e.action}`).join("\n");
  };
  const keep = document.getElementById("keep");
  if (keep) keep.onclick = () => runThenNext(keepSelected);
  const udel = document.getElementById("unseen-del");
  if (udel) udel.onclick = () => { const r = prompt("Why delete it?"); if (r) run(() => api("POST", `/api/annotations/${a.id}/unseen`, { decision: "delete", reason: r })); };
  const ver = document.getElementById("verify");
  if (ver) ver.onclick = () => runThenNext(verifySelected);
}

function renderLists() {
  const el = document.getElementById("lists");
  if (blind()) { el.innerHTML = ""; return; }
  let html = "";
  const open = st.warnings.filter((w) => !w.acknowledged);
  html += `<h3>Warnings (${open.length} open)</h3>`;
  const byType = {};
  st.warnings.forEach((w) => { (byType[w.type] = byType[w.type] || []).push(w); });
  html += Object.entries(byType).map(([type, ws]) => {
    const n = ws.filter((w) => !w.acknowledged).length;
    return `<details ${ws.length <= 5 ? "open" : ""}><summary>${type} — ${n} open / ${ws.length}</summary>` + ws.map((w) =>
      `<div class="list-item" data-warning="${w.id}">${w.acknowledged ? "✓ " : "⚠ "}${w.message}</div>`).join("") + "</details>";
  }).join("") || '<span class="muted">none</span>';
  if (st.reconcile) {
    const pending = st.observations.filter((o) => !o.deleted && o.reconciliation === "pending");
    html += `<h3>Pending observations (${pending.length})</h3>` + pending.map((o) =>
      `<div class="list-item" data-obs="${o.id}">◆ ${o.category} ${o.cell_id ? o.cell_id.split(":")[1] : ""}</div>`).join("");
    html += `<h3 data-testid="unseen-heading">Annotations without a blind-scan match (${st.reconcile.unseen.length})</h3>` +
      (pending.length ? '<div class="muted">Reconcile pending observations first; the rest need an explicit keep/delete decision.</div>' : "") +
      st.reconcile.unseen.map((a) =>
      `<div class="list-item unseen" data-ann="${a.id}">● ${a.category} · ${subLabel(a.category, a.subtype)}</div>`).join("");
  }
  el.innerHTML = html;
  el.querySelectorAll("[data-warning]").forEach((n) => n.onclick = () => {
    const w = st.warnings.find((x) => x.id === n.dataset.warning);
    if (w.point) centerOnNorm(w.point.u, w.point.v);
    if (!w.acknowledged) { const r = prompt(`Acknowledge "${w.message}"? Give a reason (leave empty to just jump):`); if (r) run(() => api("POST", `/api/sheets/${st.sheetId}/warnings/ack`, { warning_id: w.id, reason: r })); }
  });
  el.querySelectorAll("[data-obs]").forEach((n) => n.onclick = () => {
    const o = st.observations.find((x) => x.id === n.dataset.obs); centerOnNorm(o.point.u, o.point.v); select({ type: "observation", id: o.id });
  });
  el.querySelectorAll("[data-ann]").forEach((n) => n.onclick = () => {
    const a = st.reconcile.unseen.find((x) => x.id === n.dataset.ann); centerOnNorm(a.point.u, a.point.v); select({ type: "annotation", id: a.id });
  });
}

// ---------------------------------------------------------------- actions

async function run(fn) { try { await fn(); await refresh(); } catch (e) { /* toast shown */ } }

async function saveNew() {
  const s = st.selected;
  if (!s || s.type !== "new") return;
  if (blind()) {
    const category = document.getElementById("new-category")?.value || st.category;
    await run(() => api("POST", `/api/sheets/${st.sheetId}/observations`, { point: s.point, category }));
    st.selected = null; renderAll();
    return;
  }
  const f = readForm("new");
  const body = { ...f, point: s.point, review_state: document.getElementById("new-adjudicate").checked ? "needs_adjudication" : "draft" };
  try {
    const a = await api("POST", `/api/sheets/${st.sheetId}/annotations`, body);
    await refresh();
    select({ type: "annotation", id: a.id });
    toast(`Saved ${subLabel(a.category, a.subtype)}`);
  } catch (e) { /* toast shown */ }
}

async function acceptSelected() {
  const s = st.selected;
  if (s?.type !== "cluster") return;
  const f = readForm("acc");
  await run(() => api("POST", `/api/sheets/${st.sheetId}/clusters/${s.id}/accept`, f));
  st.selected = null; renderAll();
}

async function rejectSelected() {
  const s = st.selected;
  if (s?.type !== "cluster") return;
  const r = prompt("Reason for rejecting this candidate?", "not a device");
  if (!r) return;
  await run(() => api("POST", `/api/sheets/${st.sheetId}/clusters/${s.id}/reject`, { reason: r }));
  st.selected = null; renderAll();
}

// --- reconciliation: shared by the buttons and the M / O / D / K / V / G keys

// What still blocks "Complete sheet", in the order the gates require it: pending observations,
// then annotations the blind scan did not see, then annotations not yet verified.
function workItems() {
  if (phase() !== "reconcile" || !st.reconcile) return [];
  const unseen = new Set(st.reconcile.unseen.map((a) => a.id));
  return [
    ...st.observations.filter((o) => !o.deleted && o.reconciliation === "pending").map((o) => ({ type: "observation", id: o.id, point: o.point })),
    ...st.reconcile.unseen.map((a) => ({ type: "annotation", id: a.id, point: a.point })),
    ...st.annotations.filter((a) => !unseen.has(a.id) && a.review_state !== "verified").map((a) => ({ type: "annotation", id: a.id, point: a.point })),
  ];
}

function goToNextWorkItem() {
  const items = workItems();
  if (!items.length) { toast("Nothing left to reconcile — Complete sheet"); return; }
  const s = st.selected;
  const i = s ? items.findIndex((x) => x.type === s.type && x.id === s.id) : -1;
  const next = items[(i + 1) % items.length];
  centerOnNorm(next.point.u, next.point.v);
  select({ type: next.type, id: next.id });
}

// Runs a reconcile action; on success refreshes and moves to the next work item. An action
// returns false when the reviewer cancels its prompt.
async function runThenNext(fn) {
  try { if ((await fn()) === false) return; await refresh(); } catch (e) { return; /* toast shown */ }
  goToNextWorkItem();
}

function pendingObservation() {
  const s = st.selected;
  const o = s?.type === "observation" && st.observations.find((x) => x.id === s.id);
  if (!o || o.reconciliation !== "pending") { toast("Select a pending observation first (G)", true); return null; }
  return o;
}

async function matchSelected() {
  const o = pendingObservation();
  if (!o) return false;
  const sug = (st.reconcile?.suggestions || []).find((x) => x.observation_id === o.id);
  if (!sug?.suggested_annotation_id) { toast("No annotation to match — promote (O) or dismiss (D)", true); return false; }
  return api("POST", `/api/observations/${o.id}/match`, { annotation_id: sug.suggested_annotation_id });
}

async function promoteSelected() {
  const o = pendingObservation();
  if (!o) return false;
  return api("POST", `/api/observations/${o.id}/promote`, { subtype: document.getElementById("promote-subtype").value });
}

async function dismissSelected() {
  const o = pendingObservation();
  if (!o) return false;
  const r = prompt("Why dismiss this observation?", "mis-click");
  if (!r) return false;
  return api("POST", `/api/observations/${o.id}/dismiss`, { reason: r });
}

function selectedAnnotation() {
  const s = st.selected;
  const a = s?.type === "annotation" && st.annotations.find((x) => x.id === s.id);
  if (!a) toast("Select an annotation first (G)", true);
  return a || null;
}

async function keepSelected() {
  const a = selectedAnnotation();
  if (!a) return false;
  if (a.blind_confirmation) { toast("This annotation already has a blind-scan decision", true); return false; }
  const r = prompt("Re-inspected: why keep it?");
  if (!r) return false;
  return api("POST", `/api/annotations/${a.id}/unseen`, { decision: "keep", reason: r });
}

// Saves unsaved edits in the panel (room, subtype, tokens…) first, so they are not lost, then verifies.
async function verifySelected() {
  const a = selectedAnnotation();
  if (!a) return false;
  if (!a.blind_confirmation) { toast("Keep (K) or delete this unseen annotation before verifying", true); return false; }
  if (a.review_state === "verified") { toast("Already verified", true); return false; }
  if (document.getElementById("ed-adjudicate")?.checked) { toast("Untick “needs adjudication” before verifying", true); return false; }
  const f = readForm("ed");
  const norm = (x) => JSON.stringify([x.category, x.subtype, x.room || null,
    (x.text_tokens || []).map((t) => [t.text, t.bbox_pt]), x.notes || null]);
  if (norm(f) !== norm(a)) await api("PATCH", `/api/annotations/${a.id}`, { ...f, review_state: "draft" });
  return api("POST", `/api/annotations/${a.id}/verify`);
}

const RECONCILE_KEYS = { g: goToNextWorkItem, m: () => runThenNext(matchSelected), o: () => runThenNext(promoteSelected),
  d: () => runThenNext(dismissSelected), k: () => runThenNext(keepSelected), v: () => runThenNext(verifySelected) };

async function deleteSelected() {
  const s = st.selected;
  if (s?.type === "observation" && blind()) {
    await run(() => api("POST", `/api/observations/${s.id}/delete`)); st.selected = null; renderAll(); return;
  }
  if (s?.type === "annotation") {
    const r = prompt("Reason for deleting this annotation?", "duplicate");
    if (!r) return;
    await run(() => api("POST", `/api/annotations/${s.id}/delete`, { reason: r })); st.selected = null; renderAll();
  }
}

async function markCell() {
  const cell = st.meta.cells[st.cellIndex];
  const done = blind() ? cell.blind_scan_done : cell.pass1_done;
  await run(() => api("POST", `/api/sheets/${st.sheetId}/cells/${cell.id}`, { done: !done }));
  if (!done) goToCell(st.cellIndex + 1);
}

function startPlacing() { st.placing = true; document.getElementById("viewport").classList.add("placing"); renderTopbar(); }
function stopPlacing() { st.placing = false; document.getElementById("viewport").classList.remove("placing"); renderTopbar(); }

// ---------------------------------------------------------------- input

function localXY(e) {
  const r = document.getElementById("viewport").getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function placeAt(sx, sy) {
  const [u, v] = screenToNorm(sx, sy);
  if (u < 0 || u > 1 || v < 0 || v > 1) return;
  stopPlacing();
  st.selected = { type: "new", point: { u, v } };
  renderOverlay(); renderDetails();
  if (blind()) saveNew();
}

function setupInput() {
  const vp = document.getElementById("viewport");
  vp.addEventListener("wheel", (e) => { e.preventDefault(); const [x, y] = localXY(e); zoomAt(x, y, Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
  vp.addEventListener("mousedown", (e) => {
    vp.focus();
    const [x, y] = localXY(e);
    st.drag = { x, y, tx: st.view.tx, ty: st.view.ty, moved: false, shift: e.shiftKey };
  });
  window.addEventListener("mousemove", (e) => {
    if (!st.drag) return;
    const [x, y] = localXY(e);
    if (Math.hypot(x - st.drag.x, y - st.drag.y) > 3) st.drag.moved = true;
    if (st.drag.moved) setView(st.view.s, st.drag.tx + x - st.drag.x, st.drag.ty + y - st.drag.y);
  });
  window.addEventListener("mouseup", (e) => {
    const d = st.drag; st.drag = null;
    if (!d || d.moved) return;
    if (st.placing || d.shift) placeAt(d.x, d.y);
  });
  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
      if (e.key === "Enter" && st.selected?.type === "new" && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); saveNew(); }
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    const k = e.key;
    const rk = phase() === "reconcile" && !e.ctrlKey && !e.metaKey && !e.altKey && RECONCILE_KEYS[k.toLowerCase()];
    if (rk) { e.preventDefault(); rk(); }
    else if (k >= "1" && k <= "8") { st.category = CATS[+k - 1]; renderTopbar(); if (st.selected?.type === "new") renderDetails(); toast(`Category: ${st.category}`); }
    else if (k === "c" || k === "C") startPlacing();
    else if (k === "Escape") { stopPlacing(); select(null); }
    else if (k === "Enter" && st.selected?.type === "new") saveNew();
    else if (k === "n" || k === "N") goToCell(st.cellIndex + 1);
    else if (k === "p" || k === "P") goToCell(st.cellIndex - 1);
    else if (k === " ") { e.preventDefault(); markCell(); }
    else if (k === "f" || k === "F") fitView();
    else if ((k === "a" || k === "A") && !blind()) acceptSelected();
    else if ((k === "r" || k === "R") && !blind()) rejectSelected();
    else if (k === "Delete" || k === "Backspace") deleteSelected();
  });
  for (const id of ["show-candidates", "show-annotations", "show-tokens", "show-rooms", "show-grid"]) {
    document.getElementById(id).addEventListener("change", renderOverlay);
  }
  window.addEventListener("resize", () => st.meta && applyView());
}

// ---------------------------------------------------------------- boot

async function boot() {
  const rev = document.getElementById("reviewer");
  try { rev.value = localStorage.getItem("gt-reviewer") || ""; } catch (e) { /* storage unavailable */ }
  rev.addEventListener("change", () => { try { localStorage.setItem("gt-reviewer", rev.value); } catch (e) { /* ignore */ } });
  st.config = await api("GET", "/api/config");
  st.sheets = await api("GET", "/api/sheets");
  const sel = document.getElementById("sheet");
  sel.innerHTML = st.sheets.map((s) => `<option value="${s.sheet_id}">${s.sheet_id} (p${s.page_number})</option>`).join("");
  const params = new URLSearchParams(location.search);
  const start = params.get("sheet") || st.sheets[0].sheet_id;
  sel.value = start;
  sel.addEventListener("change", () => { st.selected = null; st.cellIndex = 0; loadSheet(sel.value); });
  setupInput();
  await loadSheet(start);
  document.body.dataset.ready = "1";
}

// Test and debugging hooks (read-only views plus the view setter).
window.gt = {
  state: st, normToScreen, screenToNorm, normToImg, imgToNorm, setView, goToCell, refresh, loadSheet, select,
  get view() { return { ...st.view }; },
};

boot();
