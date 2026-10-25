// Takeoff page, layout A (spec 5.6). Vanilla JS, no build.
//
// Detectors propose, reviewers decide, saved device records determine the official totals.
// * Every figure on this page comes from the API: the review lines from GET /counts `review`, the official totals
//   from GET /counts `official`, the bulk button's N from GET /bulk-eligible. Nothing is counted here from markers.
//   Each totals row also names the counted devices it covers (data-ids), so every number traces to markers.
// * Coordinates and the view transform are the shared plan-view code (/shared/planview.js). Markers are drawn in
//   screen space (constant size); window.tk exposes the state and the view for the end-to-end tests.
// * Every PDF- or user-derived string (room names, tokens, notes, reasons, rationales, file names) is escaped
//   before it is put into HTML. The shared option helpers do not escape, so this file builds its own options.

"use strict";

const CATS = ["switch", "receptacle", "light", "ceiling_fan", "fan", "detector", "junction_box", "other_electrical"];
const COLORS = { switch: "#d62728", receptacle: "#1f77b4", light: "#2ca02c", ceiling_fan: "#9467bd", fan: "#8c564b",
  detector: "#e377c2", junction_box: "#ff7f0e", other_electrical: "#7f7f7f" };
const ABBR = { switch: "S", receptacle: "R", light: "L", ceiling_fan: "CF", fan: "F", detector: "D", junction_box: "J",
  other_electrical: "?" };
const FLAG_COLOR = "#ff7f0e";
const FAKE_SUFFIX = " · test double, not a model";
const AI_ONLY_LINE = "AI-proposed location: check position before accepting";
const BULK_NEVER = ["ai_only_candidate", "model_disagrees"]; // never bulk-accepted while open, whatever the severity
const CONDITION_TYPES = ["duplicate_warning", "room_ambiguous", "no_room"];
const ACKNOWLEDGEABLE = ["model_disagrees", "not_redetected", "ambiguous_match", "classification_conflict"];
const IN_PROGRESS = ["queued", "running"];
const STAGE_NAME = { verify: "AI verification", discover: "AI discovery" };

const st = {
  config: null, documents: [], docId: null, sheets: [], sheetId: null, meta: null,
  devices: [], openFlags: [], counts: null, eligible: null, bulk: null, runs: [],
  view: { s: 1, tx: 0, ty: 0 }, drag: null,
  selectedId: null, detail: null, selTok: 0, blocking: null,
  placing: false, moving: false, pendingPoint: null, newPoint: null, category: "switch",
  highlight: new Set(), highlightKey: null, exportResult: null, flaggedNote: "",
  loading: true, polling: false,
};

// ---------------------------------------------------------------- shared plan view

const pv = makePlanView(st, {
  viewport: document.getElementById("viewport"), image: document.getElementById("plan"), onChange: () => renderOverlay(),
});
const { normToScreen, screenToNorm, applyView, setView, fitView, centerOnNorm, svg, subLabel, suggestSubtype,
  nearbyTokens } = pv;

// ---------------------------------------------------------------- small helpers

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function reviewer() {
  return ($("reviewer").value || "").trim() || st.config?.default_reviewer || "reviewer";
}

async function api(method, url, body, { quiet = false, raw = null, contentType = null } = {}) {
  const opts = { method, headers: { "X-Reviewer": reviewer() } };
  if (raw !== null) { opts.headers["Content-Type"] = contentType; opts.body = raw; }
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    const msg = data ? (data.message || data.code) : r.statusText;
    const err = new Error(msg);
    err.data = data; err.status = r.status;
    if (!quiet) toast(msg, true);
    throw err;
  }
  return data;
}

function toast(msg, error = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "show" + (error ? " error" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.className = ""; }, error ? 5000 : 2200);
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

// Python's f"{x:.0f}" (round half to even), for room-row names (counts.sheet_room_key).
function fmt0(x) {
  const f = Math.floor(x), diff = x - f;
  const n = diff > 0.5 ? f + 1 : diff < 0.5 ? f : (f % 2 === 0 ? f : f + 1);
  return String(n);
}

function needsReview(f) { return f.open && (f.severity === "review" || BULK_NEVER.includes(f.type)); }

function flagsByDevice() {
  const out = {};
  for (const f of st.openFlags) (out[f.device_id] = out[f.device_id] || []).push(f);
  return out;
}

function openFlagsOf(id) { return st.openFlags.filter((f) => f.device_id === id && f.open); }

function subName(category, subtype) {
  if (subtype === null || subtype === undefined) return "";
  return (st.config.taxonomy.categories[category] || []).some((s) => s.name === subtype) ? subLabel(category, subtype) : subtype;
}

// "category / legend wording", for the device and for the AI's answer.
function describe(category, subtype) {
  return subtype ? `${category} / ${subName(category, subtype)}` : category;
}

function describeAI(category, subtype) {
  if (category === null || category === undefined) return "missing verdict row";
  if (category === "not_a_device") return "not a device";
  return describe(category, subtype);
}

function disagreementPrompt(flag, device) {
  const d = flag.details || {};
  return `The AI classified this as ${describeAI(d.ai_category, d.ai_subtype)}. ` +
    `Give a reason for keeping ${describe(device.category, device.subtype)}.`;
}

function sameRoom(a, b) {
  return a && b && a.kind === b.kind && a.name === b.name &&
    JSON.stringify(a.label_bbox_pt || null) === JSON.stringify(b.label_bbox_pt || null);
}

function roomIndex(room) { return room ? st.meta.rooms.findIndex((r) => sameRoom(r, room)) : -1; }

function roomRef(r) { return { kind: r.kind, name: r.name, label_bbox_pt: r.label_bbox_pt }; }

function roomLabel(r, counts) {
  if (r.kind === "labeled" && counts[r.name] > 1) return `${r.name} @(${Math.round(r.label_bbox_pt[0])},${Math.round(r.label_bbox_pt[1])})`;
  return r.name;
}

function roomOptionsHtml(selectedIdx, firstLabel) {
  const counts = {};
  st.meta.rooms.forEach((r) => { counts[r.name] = (counts[r.name] || 0) + 1; });
  return `<option value="">${esc(firstLabel)}</option>` + st.meta.rooms.map((r, i) =>
    `<option value="${i}" ${i === selectedIdx ? "selected" : ""}>${esc(roomLabel(r, counts))}</option>`).join("");
}

function categoryOptionsHtml(current) {
  const cats = Object.keys(st.config.taxonomy.categories);
  return cats.map((c) => `<option value="${esc(c)}" ${c === current ? "selected" : ""}>${esc(c)}</option>`).join("");
}

function subtypeOptionsHtml(category, current) {
  return (st.config.taxonomy.categories[category] || []).map((s) =>
    `<option value="${esc(s.name)}" ${s.name === current ? "selected" : ""}>${esc(s.label)}</option>`).join("");
}

// The row name counts.sheet_room_key gives a counted device (two same-named labels are two rows).
function roomKey(d) {
  const room = d.room;
  if (!room) return "UNASSIGNED";
  if (room.kind !== "labeled") return room.name;
  const same = new Set(st.meta.rooms.filter((r) => r.kind === "labeled" && r.name === room.name && r.label_bbox_pt)
    .map((r) => JSON.stringify(r.label_bbox_pt.map(Number))));
  same.add(JSON.stringify(room.label_bbox_pt.map(Number)));
  if (same.size <= 1) return room.name;
  return `${room.name} @(${fmt0(room.label_bbox_pt[0])},${fmt0(room.label_bbox_pt[1])})`;
}

function labelCenter(room) {
  if (!room || room.kind !== "labeled") return null;
  const r = st.meta.rooms.find((x) => sameRoom(x, room));
  if (!r || !r.bbox) return null;
  return { u: (r.bbox.u0 + r.bbox.u1) / 2, v: (r.bbox.v0 + r.bbox.v1) / 2 };
}

function layerOf(d) {
  if (d.review_state === "rejected") return "layer-rejected";
  if (d.review_state === "proposed") return d.origin === "ai" ? "layer-ai" : "layer-proposed";
  return "layer-accepted";
}

function sortedRuns() { return [...st.runs].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)); }
function latestRun() { const r = sortedRuns(); return r[r.length - 1] || null; }
function latestRunFor(sheetId) { return sortedRuns().filter((r) => r.sheets && r.sheets[sheetId]).pop() || null; }
function runInProgress(run) { return !!run && Object.values(run.sheets || {}).some((s) => IN_PROGRESS.includes(s.status)); }

function selectedDevice() { return st.devices.find((d) => d.id === st.selectedId) || null; }

// ---------------------------------------------------------------- loading

async function loadDocuments() {
  st.documents = await api("GET", "/api/documents");
  $("document").innerHTML = st.documents.map((d) =>
    `<option value="${esc(d.id)}">${esc(d.file_name)} (${esc(d.source)})</option>`).join("");
}

async function refreshData() {
  const doc = st.docId;
  if (!doc) return;
  const tok = st.selTok; // the selection this refresh started with (select() bumps it)
  const [devs, counts, eligible, bulk, runs] = await Promise.all([
    api("GET", `/api/documents/${doc}/devices`),
    api("GET", `/api/documents/${doc}/counts`),
    api("GET", `/api/documents/${doc}/bulk-eligible`),
    api("GET", `/api/documents/${doc}/bulk-actions`),
    api("GET", `/api/documents/${doc}/runs`),
  ]);
  Object.assign(st, { devices: devs.devices, openFlags: devs.open_flags, counts, eligible, bulk, runs });
  if (tok !== st.selTok || !st.selectedId) return; // the selection changed meanwhile: select() loads its own detail
  if (!st.devices.some((d) => d.id === st.selectedId)) { clearSelection(); return; }
  const id = st.selectedId;
  let det = null;
  try { det = await api("GET", `/api/devices/${id}`, undefined, { quiet: true }); } catch (e) { det = null; }
  if (tok === st.selTok && st.selectedId === id) st.detail = det; // never a stale detail over a newer selection
}

async function refresh() { await refreshData(); renderAll(); }

async function loadDocument(docId, sheetId = null) {
  st.loading = true;
  st.docId = docId;
  $("document").value = docId;
  clearSelection();
  st.exportResult = null;
  st.sheets = await api("GET", `/api/documents/${docId}/sheets`);
  $("sheet").innerHTML = st.sheets.map((s) => `<option value="${esc(s.sheet_id)}">${esc(s.sheet_id)} (p${s.page_number})</option>`).join("");
  await refreshData();
  const start = st.sheets.some((s) => s.sheet_id === sheetId) ? sheetId : st.sheets[0]?.sheet_id;
  if (start) await loadSheet(start);
  else { st.meta = null; st.loading = false; renderAll(); }
}

async function loadSheet(sheetId, keepView = false) {
  st.loading = true;
  const meta = await api("GET", `/api/documents/${st.docId}/sheets/${sheetId}/meta`);
  if (st.sheetId !== sheetId) { st.newPoint = null; st.highlight = new Set(); st.highlightKey = null; }
  st.sheetId = sheetId; st.meta = meta;
  $("sheet").value = sheetId;
  const sel = selectedDevice();
  if (sel && sel.sheet_id !== sheetId) clearSelection();
  const img = $("plan");
  if (!meta.image || meta.view_error) {
    img.classList.add("hidden"); img.removeAttribute("src"); img.dataset.key = "";
  } else {
    const key = `${st.docId}:${sheetId}`;
    if (img.dataset.key !== key) {
      img.dataset.key = key;
      img.classList.remove("hidden");
      img.width = meta.image.width_px; img.height = meta.image.height_px;
      await new Promise((res) => { img.onload = res; img.onerror = res; img.src = `/api/documents/${st.docId}/sheets/${sheetId}/image`; });
      if (!keepView) fitView();
    }
  }
  renderAll();
  st.loading = false;
}

// ---------------------------------------------------------------- rendering

function renderAll() {
  renderTopbar();
  renderStatus();
  if (st.meta && st.meta.image) applyView(); else renderOverlay();
  renderPanel();
}

function renderTopbar() {
  $("analyze").disabled = st.polling || !st.docId;
  const banner = st.moving ? "Move: click the new position, then Save (Esc to cancel)"
    : st.placing ? `Add device: ${st.category} — click on the plan (Esc to cancel)` : "";
  $("mode-banner").textContent = banner;
  $("viewport").classList.toggle("placing", st.moving || st.placing);
  const notes = [];
  if (st.meta?.view_error) notes.push(`This sheet cannot be displayed: ${st.meta.view_error}`);
  else if (st.meta?.note) notes.push(st.meta.note);
  const run = st.sheetId ? latestRunFor(st.sheetId) : null;
  const sr = run?.sheets?.[st.sheetId];
  if (sr && sr.message && !notes.includes(sr.message)) notes.push(`${run.id}: ${sr.message}`);
  $("sheet-note").textContent = notes.join(" · ");
}

function stageLine(kind) {
  const name = STAGE_NAME[kind];
  const run = st.sheetId ? latestRunFor(st.sheetId) : null;
  let text, impl;
  const s = run?.sheets?.[st.sheetId]?.[`ai_${kind}`];
  if (s) {
    impl = s.implementation;
    if (s.status === "done") text = `done (${s.n_items})`;
    else if (s.status === "partial") text = `partial (${Object.values(s.errors_by_code || {}).reduce((a, b) => a + b, 0)} errors)`;
    else if (s.status === "unavailable") text = `unavailable: ${s.reason || "no reason recorded"}`;
    else if (s.status === "off") text = "off";
    else text = "running"; // pending: the run is still in progress
  } else {
    const cfg = st.config.ai[kind];
    impl = cfg.implementation;
    if (st.config.ai.mode === "off") text = "off";
    else if (!cfg.available) text = `unavailable: ${cfg.reason || "no reason recorded"}`;
    else text = "not run on this sheet yet";
  }
  return `${name}: ${text}` + (impl === "fake" ? FAKE_SUFFIX : "");
}

function runSummary(run) {
  if (!run) return "No analysis yet";
  const sheets = Object.values(run.sheets || {});
  const by = {};
  sheets.forEach((s) => { by[s.status] = (by[s.status] || 0) + 1; });
  const label = { done: "done", running: "running", queued: "queued", failed: "failed", skipped_no_legend: "skipped (no legend)" };
  const parts = [run.id, `${run.detector} ${run.detector_version}`];
  for (const k of ["done", "running", "queued", "failed", "skipped_no_legend"]) {
    if (by[k]) parts.push(`${plural(by[k], "sheet")} ${label[k]}`);
  }
  parts.push(plural(sheets.reduce((a, s) => a + (s.n_detections || 0), 0), "detection"));
  const u = run.ai_usage;
  if (u && (u.verify.requests || u.discover.requests)) {
    const tok = (f) => (u.verify.tokens?.[f] || 0) + (u.discover.tokens?.[f] || 0);
    parts.push(`AI requests: verify ${u.verify.requests}, discover ${u.discover.requests} · tokens ${tok("input")} in / ` +
      `${tok("output")} out / ${tok("cache_read")} cache read / ${tok("cache_creation")} cache write`);
  }
  const v = run.ai_versions || {};
  if (v.verify_implementation === "fake" || v.discover_implementation === "fake") parts.push("AI: test double, not a model");
  return parts.join(" · ");
}

function renderStatus() {
  const run = latestRun();
  $("run-summary").textContent = runSummary(run);
  $("analyze-progress").textContent = run
    ? Object.entries(run.sheets || {}).map(([sid, s]) => `${sid}: ${s.status === "skipped_no_legend" ? "skipped (no legend)" : s.status}`).join(" · ")
    : "";
  if (st.config && st.sheetId) {
    $("stage-verify").textContent = stageLine("verify");
    $("stage-discover").textContent = stageLine("discover");
  }
  const ex = $("export-status");
  const r = st.exportResult;
  if (!r) { ex.textContent = ""; ex.className = ""; ex.title = ""; return; }
  const m = r.manifest || {};
  const reasons = m.incomplete_reasons || [];
  if (m.complete) {
    ex.textContent = `Export complete · ${r.path}`;
    ex.className = "complete";
  } else {
    ex.textContent = `Export incomplete: ${reasons.slice(0, 3).join("; ")}${reasons.length > 3 ? ` (and ${reasons.length - 3} more)` : ""} · ${r.path}`;
    ex.className = "incomplete";
  }
  ex.title = reasons.join("\n");
}

function renderOverlay() {
  const o = $("overlay");
  o.innerHTML = "";
  if (!st.meta || !st.meta.image) return;
  const show = (id) => $(id).checked;
  if (show("layer-region") && st.meta.region?.bbox) {
    const b = st.meta.region.bbox;
    const [x0, y0] = normToScreen(b.u0, b.v0), [x1, y1] = normToScreen(b.u1, b.v1);
    svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, class: "region" }, o);
  }
  const here = st.devices.filter((d) => d.sheet_id === st.sheetId && !d.deleted);
  const visible = here.filter((d) => show(layerOf(d)));
  if (show("layer-rooms")) {
    const g = svg("g", { class: "layer-rooms" }, o);
    for (const r of st.meta.rooms.filter((r) => r.kind === "labeled" && r.bbox)) {
      const [x0, y0] = normToScreen(r.bbox.u0, r.bbox.v0), [x1, y1] = normToScreen(r.bbox.u1, r.bbox.v1);
      svg("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, class: "room-label" }, g);
    }
    for (const d of visible) {
      const c = labelCenter(d.room);
      if (!c) continue;
      const [x1, y1] = normToScreen(d.point.u, d.point.v), [x2, y2] = normToScreen(c.u, c.v);
      svg("line", { x1, y1, x2, y2, class: "leader", "data-id": d.id }, g);
    }
  }
  const flags = flagsByDevice();
  const g = svg("g", { class: "layer-devices" }, o);
  for (const d of visible) drawMarker(g, d, flags[d.id] || []);
  const sel = selectedDevice();
  if (st.pendingPoint && sel && sel.sheet_id === st.sheetId) {
    const [x0, y0] = normToScreen(sel.point.u, sel.point.v), [x, y] = normToScreen(st.pendingPoint.u, st.pendingPoint.v);
    svg("line", { x1: x0, y1: y0, x2: x, y2: y, stroke: "#000", "stroke-width": 1, "stroke-dasharray": "2 2" }, o);
    svg("circle", { cx: x, cy: y, r: 8, fill: "none", stroke: "#000", "stroke-width": 2, class: "pending-move" }, o);
  }
  if (st.newPoint) {
    const [x, y] = normToScreen(st.newPoint.u, st.newPoint.v);
    svg("circle", { cx: x, cy: y, r: 8, fill: "none", stroke: "#000", "stroke-width": 2, class: "pending-new" }, o);
  }
}

function drawMarker(parent, d, flags) {
  const [x, y] = normToScreen(d.point.u, d.point.v);
  const color = COLORS[d.category] || "#555";
  const flagged = flags.some(needsReview);
  const counted = d.review_state === "accepted" && !d.deleted;
  const selected = d.id === st.selectedId;
  const lit = st.highlight.has(d.id);
  const cls = ["device", "hit", `state-${d.review_state}`, `origin-${d.origin}`, `cat-${d.category}`];
  if (counted) cls.push("counted");
  if (flagged) cls.push("flagged");
  if (selected) cls.push("selected");
  if (lit) cls.push("highlight");
  const g = svg("g", { class: cls.join(" "), "data-id": d.id, "data-cx": x, "data-cy": y }, parent);
  const r = 7;
  if (lit) svg("circle", { cx: x, cy: y, r: r + 11, fill: "rgba(255, 210, 0, 0.45)", stroke: "none", class: "halo" }, g);
  if (flagged) svg("circle", { cx: x, cy: y, r: r + 5, fill: "none", stroke: FLAG_COLOR, "stroke-width": 2.5, class: "flag-ring" }, g);
  if (d.origin === "manual") svg("circle", { cx: x, cy: y, r: r + 2.5, fill: "none", stroke: color, "stroke-width": 1.5, class: "outline2" }, g);
  const fill = d.review_state === "accepted" ? color : d.review_state === "rejected" ? "rgba(255,255,255,0.3)" : "rgba(255,255,255,0.6)";
  const core = { cx: x, cy: y, r, fill, stroke: color, "stroke-width": selected ? 4 : 2.5, class: "core" };
  if (d.origin === "ai") core["stroke-dasharray"] = "3 2";
  svg("circle", core, g);
  if (d.review_state === "rejected") {
    svg("line", { x1: x - r, y1: y - r, x2: x + r, y2: y + r, stroke: "#333", "stroke-width": 2, class: "cross" }, g);
    svg("line", { x1: x - r, y1: y + r, x2: x + r, y2: y - r, stroke: "#333", "stroke-width": 2, class: "cross" }, g);
  }
  svg("text", { x: x + r + 3, y: y - r, fill: color, class: "abbr" }, g).textContent = ABBR[d.category] || "?";
  if (d.origin === "ai") svg("text", { x: x - r - 16, y: y - r, fill: color, class: "ai-tag" }, g).textContent = "AI";
  g.addEventListener("mousedown", (e) => {
    if (st.placing || st.moving) return; // the click places or moves; the plan handles it
    e.stopPropagation();
    select(d.id);
  });
}

function renderPanel(opts = {}) {
  renderDevice(opts);
  renderReview();
  renderActions();
  renderTotals();
}

function renderReview() {
  const r = st.counts?.review;
  if (!r) { $("review-lines").innerHTML = ""; return; }
  const lines = [
    ["detected", `Detected by analysis ${r.detected} = ${r.accepted_detected} accepted + ${r.rejected} rejected + ` +
      `${r.awaiting} awaiting review · of which AI-only candidates ${r.detected_ai_only}`],
    ["awaiting", `Awaiting review ${r.awaiting} = ${r.awaiting_unflagged} unflagged + ${r.awaiting_flagged} flagged`],
    ["manual", `Added manually ${r.manual}`],
    ["official", `Official takeoff total ${r.official_total} = ${r.accepted_detected} accepted + ${r.manual} manual`],
    ["open_flags", `Accepted with open flags ${r.accepted_with_open_flags} (still counted)`],
  ];
  $("review-lines").innerHTML = lines.map(([k, t]) => `<div data-line="${k}">${esc(t)}</div>`).join("") +
    `<div class="muted">All sheets of this document, from the saved device records.</div>`;
}

function renderActions() {
  const d = selectedDevice();
  $("accept-selected").disabled = !d || d.review_state === "accepted";
  $("reject-selected").disabled = !d || d.origin === "manual" || d.review_state === "rejected";
  $("reject-selected").title = d && d.origin === "manual" ? "A manual device is deleted, not rejected" : "";
  const n = st.eligible?.count || 0;
  const b = $("bulk-accept");
  b.textContent = `Accept all unflagged (${n})`;
  b.disabled = n === 0;
  b.title = "Every sheet of this document; flagged proposals and AI-only candidates are never included";
  const last = st.bulk?.last_undoable;
  $("undo-bulk").disabled = !last;
  $("undo-bulk").title = last ? `${plural(last.device_ids.length, "device")}, ${last.created_at.slice(0, 19)}` : "";
  $("review-flagged-status").textContent = st.flaggedNote;
}

function renderTotals() {
  const el = $("totals");
  const k = st.counts;
  if (!k || !st.meta) { el.innerHTML = ""; return; }
  const sheet = k.official[st.sheetId];
  const accepted = st.devices.filter((d) => d.sheet_id === st.sheetId && d.review_state === "accepted" && !d.deleted);
  let html = "";
  if (!sheet) {
    html += `<div class="muted">No counted devices on sheet ${esc(st.sheetId)}.</div>`;
  } else {
    html += `<table class="totals"><thead><tr><th>Room</th><th>Type</th><th class="n">Count</th></tr></thead><tbody>`;
    for (const [room, cats] of Object.entries(sheet.by_room)) {
      for (const [cat, subs] of Object.entries(cats)) {
        for (const [sub, n] of Object.entries(subs)) {
          const ids = accepted.filter((d) => roomKey(d) === room && d.category === cat &&
            String(d.subtype ?? "null") === sub).map((d) => d.id);
          const key = `${room}|${cat}|${sub}`;
          const mismatch = ids.length !== n;
          html += `<tr class="total-row${st.highlightKey === key ? " active" : ""}${mismatch ? " mismatch" : ""}" data-key="${esc(key)}" ` +
            `data-room="${esc(room)}" data-category="${esc(cat)}" data-subtype="${esc(sub)}" data-count="${n}" data-ids="${esc(ids.join(","))}"` +
            `${mismatch ? ' title="the counted markers do not match this row"' : ""}>` +
            `<td>${esc(room)}</td><td>${esc(cat)} · ${esc(subName(cat, sub === "null" ? null : sub))}</td><td class="n">${n}</td></tr>`;
        }
      }
    }
    html += `</tbody></table>`;
  }
  const o = sheet?.by_origin || { detector_accepted: 0, ai_accepted: 0, manual: 0 };
  // Each sheet is one home (spec 5.5, 5.6): the sheet's figure is the home total.
  html += `<div id="sheet-total" class="home-total" data-total="${sheet ? sheet.total : 0}">Home total (this sheet): ${sheet ? sheet.total : 0} ` +
    `<span class="muted">(detector_accepted ${o.detector_accepted} · ai_accepted ${o.ai_accepted} · manual ${o.manual})</span></div>`;
  const home = { detector_accepted: 0, ai_accepted: 0, manual: 0 };
  Object.values(k.official).forEach((s) => { for (const key of Object.keys(home)) home[key] += s.by_origin[key] || 0; });
  html += `<div id="official-total" data-total="${k.review.official_total}">All sheets total (${plural(st.sheets.length, "home")}): ` +
    `${k.review.official_total} <span class="muted">(detector_accepted ${home.detector_accepted} · ai_accepted ${home.ai_accepted} · manual ${home.manual})</span></div>`;
  el.innerHTML = html;
  el.querySelectorAll("tr.total-row").forEach((tr) => tr.addEventListener("click", () => {
    if (st.highlightKey === tr.dataset.key) { st.highlightKey = null; st.highlight = new Set(); }
    else { st.highlightKey = tr.dataset.key; st.highlight = new Set(tr.dataset.ids.split(",").filter(Boolean)); }
    el.querySelectorAll("tr.total-row").forEach((x) => x.classList.toggle("active", x.dataset.key === st.highlightKey));
    renderOverlay();
  }));
}

// --- the selected device

function kv(rows) {
  return `<div class="kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><span>${v}</span>`).join("")}</div>`;
}

function aiLine(det) {
  const ai = det.ai_summary || {};
  let text;
  if (ai.status === "agrees") text = "AI second opinion: agrees";
  else if (ai.status === "disagrees") {
    const f = det.flags.find((x) => x.id === ai.flag_id);
    const what = f && f.details && "ai_category" in f.details ? describeAI(f.details.ai_category, f.details.ai_subtype) : ai.what;
    text = `AI second opinion: disagrees (${what})`;
  } else if (ai.status === "ai_only") text = "AI second opinion: not verified (AI-only candidate: found by AI discovery alone)";
  else text = `AI second opinion: not verified (${ai.reason || "no AI result"})`;
  return text + (ai.implementation === "fake" ? FAKE_SUFFIX : "");
}

function aiRationale(det) {
  const ai = det.ai_summary || {};
  const f = det.flags.find((x) => x.id === ai.flag_id) || det.flags.find((x) => x.type === "model_disagrees" && x.open);
  if (f?.details?.rationale) return f.details.rationale;
  const detIds = new Set(det.evidence.map((r) => r.evidence.detection_id));
  const v = det.verdicts.find((x) => x.id === ai.verdict_id);
  if (v) {
    const row = ((v.output || {}).verdicts || []).find((x) => detIds.has(x.item_id));
    if (row?.rationale) return row.rationale;
  }
  const ao = det.flags.find((x) => x.type === "ai_only_candidate");
  if (ao?.details?.rationale) return ao.details.rationale;
  const dd = det.evidence.map((r) => r.detection).find((x) => x && x.rationale);
  return dd ? dd.rationale : null;
}

function flagText(f) {
  const d = f.details || {};
  switch (f.type) {
    case "model_disagrees": return `the AI (${d.source || "verify"}) says ${describeAI(d.ai_category, d.ai_subtype)}`;
    case "ai_only_candidate": return "found only by AI discovery: accept, correct and save, or reject";
    case "duplicate_warning": return `another device of this category within ${d.distance_pt} pt`;
    case "room_ambiguous": return `room proposal is uncertain${d.reason ? ` (${d.reason})` : ""}: choose the room`;
    case "no_room": return "no room: choose the room";
    case "classification_conflict": return `a run detected ${describe(d.run_category, d.run_subtype)} here`;
    case "not_redetected": return "not detected again by a later run";
    case "ambiguous_match": return "a later detection matched more than one device";
    default: return "";
  }
}

function flagsHtml(det) {
  const flags = [...det.flags].sort((a, b) => (b.open - a.open));
  if (!flags.length) return '<span class="muted">none</span>';
  return flags.map((f) => {
    let acts = "";
    if (f.open) {
      if (ACKNOWLEDGEABLE.includes(f.type)) acts += ` <button class="ack" data-flag="${esc(f.id)}" type="button">Acknowledge…</button>`;
      if (f.type === "classification_conflict") {
        acts += ["keep", "change_category", "separate_device"].map((c) =>
          ` <button class="resolve" data-flag="${esc(f.id)}" data-choice="${c}" type="button">${c.replace("_", " ")}</button>`).join("");
      }
      if (CONDITION_TYPES.includes(f.type)) acts += ' <span class="muted">clears when the condition is fixed</span>';
    }
    const status = f.open ? `<span class="badge">${esc(f.severity)}</span>`
      : `<span class="muted">closed: ${esc(f.resolution || "")}${f.resolved_by ? ` · ${esc(f.resolved_by)}` : ""}</span>`;
    return `<div class="flag${f.open ? "" : " closed"}" data-flag-id="${esc(f.id)}"><b>${esc(f.type)}</b> ${status}<br>` +
      `<span>${esc(flagText(f))}</span>${acts}</div>`;
  }).join("");
}

function historyHtml(det) {
  const fields = ["category", "subtype", "room", "point_pt", "review_state", "accept_kind", "notes", "deleted"];
  const show = (v) => (v && typeof v === "object" ? (v.name ?? JSON.stringify(v)) : String(v));
  const items = det.history.map((e) => {
    let change = "";
    if (e.entity === "device" && e.before && e.after) {
      change = fields.filter((f) => JSON.stringify(e.before[f] ?? null) !== JSON.stringify(e.after[f] ?? null))
        .map((f) => `${f} ${show(e.before[f])} → ${show(e.after[f])}`).join("; ");
    } else if (e.entity === "flag" && e.after) {
      change = `${e.after.type}${e.after.open ? "" : ` closed: ${e.after.resolution || ""}`}`;
    }
    return `<li>${esc(e.timestamp.slice(0, 19))} · ${esc(e.actor)} · ${esc(e.entity)} ${esc(e.action)}` +
      `${change ? ` <span class="muted">(${esc(change)})</span>` : ""}</li>`;
  });
  return `<details open><summary>History (${det.history.length})</summary><ol class="list">${items.join("")}</ol></details>`;
}

function captureForm() {
  if (!$("ed-form")) return null;
  return { id: $("ed-form").dataset.id, category: $("ed-category").value, subtype: $("ed-subtype").value,
    room: $("ed-room").value, notes: $("ed-notes").value };
}

function renderDevice({ keepForm = false } = {}) {
  const el = $("device");
  if (st.newPoint) { renderNewForm(el); return; }
  if (!st.selectedId) {
    el.innerHTML = '<span class="muted">Nothing selected. Click a marker, or press C to add a device.</span>';
    return;
  }
  const det = st.detail;
  if (!det || det.device.id !== st.selectedId) { el.innerHTML = '<span class="muted">Loading…</span>'; return; }
  const kept = keepForm ? captureForm() : null;
  // what the selectors show before any edit: a change is a difference from these, never from a placeholder
  const dv = det.device;
  const rows = [...det.evidence].sort((a, b) => (a.evidence.run_id < b.evidence.run_id ? -1 : 1));
  const latest = rows[rows.length - 1];
  const detn = latest?.detection;
  const run = latest?.run;
  const roomSource = dv.room_source ? dv.room_source.replace(/_/g, " ") : "none";
  const ai = det.ai_summary || {};
  const fields = [
    ["Category", esc(dv.category)],
    ["Subtype", `${esc(subName(dv.category, dv.subtype))} <span class="muted">(${esc(dv.subtype)})</span>`],
    ["Status", esc(dv.status)],
    ["Room", `${esc(dv.room ? dv.room.name : "none")} <span class="muted">· source: ${esc(roomSource)}</span>`],
    ["State", `${esc(dv.review_state)}${dv.accept_kind ? ` <span class="muted">(${esc(dv.accept_kind)})</span>` : ""}`],
    ["Origin", esc(dv.origin)],
    [st.config.score_label, detn && detn.score !== null && detn.score !== undefined ? Number(detn.score).toFixed(2) : '<span class="muted">none (no detection)</span>'],
    ["Run", run ? esc(`${run.id} · ${run.detector} ${run.detector_version} · ${run.merge_version}` +
      (run.ai_versions?.verify ? ` · ${run.ai_versions.verify} · ${run.ai_versions.discover}` : "")) : '<span class="muted">none (added manually)</span>'],
    ["Subtype source", esc(detn?.subtype_source || "none")],
    ["Text tokens", esc(detn && detn.tokens && detn.tokens.length ? detn.tokens.join(", ") : "none")],
    ["Position", `${dv.point.u.toFixed(4)}, ${dv.point.v.toFixed(4)}${dv.point_pt ? ` · ${dv.point_pt.map((v) => v.toFixed(1)).join(", ")} pt` : ""}`],
    ["Id", `${esc(dv.id)} · rev ${dv.revision}`],
  ];
  if (dv.notes) fields.push(["Notes", esc(dv.notes)]);
  const rationale = aiRationale(det);
  const evidence = rows.length ? rows.map((r) => {
    const e = r.evidence, x = r.detection;
    return `<li>${esc(e.run_id)} · ${esc(x ? x.source : "?")} · ${esc(e.outcome)}` +
      `${e.distance_pt !== null && e.distance_pt !== undefined ? ` · ${e.distance_pt.toFixed(1)} pt` : ""}` +
      `${x ? ` · ${esc(describe(x.category, x.subtype))}` : ""}</li>`;
  }).join("") : '<li class="muted">none (added manually)</li>';
  const blocking = st.blocking && st.blocking.length
    ? `Saved; still awaiting review. Blocked by: ${st.blocking.map((f) => esc(f.type)).join(", ")}` : "";
  const ri = roomIndex(dv.room);
  const firstRoom = ri >= 0 ? "— no room —" : (dv.room ? `${dv.room.name} (current)` : "— no room —");
  el.innerHTML =
    `<div><b>${esc(dv.category)}</b> · ${esc(subName(dv.category, dv.subtype))} <span class="badge">${esc(dv.review_state)}</span> ` +
    `<span class="badge">${esc(dv.origin)}</span></div>` + kv(fields) +
    `<div class="line"><span id="ai-line" class="ai-line">${esc(aiLine(det))}</span>` +
    `${ai.resolution === "resolved_by_correction" ? ' <span class="muted">(resolved by correction)</span>' : ""}</div>` +
    `<div id="ai-rationale" class="muted">${rationale ? `rationale: ${esc(rationale)}` : "no rationale recorded"}</div>` +
    (dv.origin === "ai" ? `<div id="ai-location" class="line ai-location">${esc(AI_ONLY_LINE)}</div>` : "") +
    `<div class="line"><b>Evidence</b><ul class="list">${evidence}</ul></div>` +
    `<div class="line"><b>Flags</b><div id="flags">${flagsHtml(det)}</div></div>` +
    `<div id="blocking" class="blocking">${blocking}</div>` +
    `<div class="form" id="ed-form" data-id="${esc(dv.id)}">
      <label>Category <select id="ed-category">${categoryOptionsHtml(dv.category)}</select></label>
      <label>Subtype <select id="ed-subtype">${subtypeOptionsHtml(dv.category, dv.subtype)}${dv.subtype === null || dv.subtype === undefined ? '<option value="" selected>— no subtype —</option>' : ""}</select></label>
      <label>Room <select id="ed-room" data-initial="${ri >= 0 ? ri : ""}">${roomOptionsHtml(ri, firstRoom)}</select></label>
      <label>Notes <textarea id="ed-notes" rows="2">${esc(dv.notes || "")}</textarea></label>
      <div id="move-note" class="muted">${st.pendingPoint ? "New position set: Save to apply it (Esc to cancel)." : ""}</div>
      <button class="primary" id="save" type="button">Save</button>
      <button id="move" type="button">Move</button>
      <button id="delete" type="button">Delete…</button>
      <span class="muted">Save is a correction: a proposed device is accepted unless a flag still blocks it.</span>
    </div>` +
    `<div id="history">${historyHtml(det)}</div>`;
  $("ed-category").dataset.initial = $("ed-category").value;
  $("ed-subtype").dataset.initial = $("ed-subtype").value;
  if (kept && kept.id === dv.id) {
    $("ed-category").value = kept.category;
    if (kept.category !== $("ed-category").dataset.initial) $("ed-subtype").innerHTML = subtypeOptionsHtml(kept.category, kept.subtype);
    $("ed-subtype").value = kept.subtype;
    $("ed-room").value = kept.room;
    $("ed-notes").value = kept.notes;
  }
  $("ed-category").onchange = () => {
    const c = $("ed-category").value;
    $("ed-subtype").innerHTML = subtypeOptionsHtml(c, suggestSubtype(c, nearbyTokens(dv.point).map((x) => x.t)));
  };
  $("save").onclick = saveSelected;
  $("move").onclick = startMoving;
  $("delete").onclick = deleteSelected;
  el.querySelectorAll("button.ack").forEach((b) => { b.onclick = () => acknowledgeFlag(b.dataset.flag); });
  el.querySelectorAll("button.resolve").forEach((b) => { b.onclick = () => resolveFlag(b.dataset.flag, b.dataset.choice); });
}

function nearestLabel(point) {
  let best = null;
  for (const r of st.meta.rooms.filter((x) => x.kind === "labeled" && x.bbox)) {
    const c = { u: (r.bbox.u0 + r.bbox.u1) / 2, v: (r.bbox.v0 + r.bbox.v1) / 2 };
    const d = pv.distPt(point, c);
    if (!best || d < best.d) best = { r, d };
  }
  return best;
}

function renderNewForm(el) {
  const p = st.newPoint;
  const near = nearbyTokens(p);
  const sub = suggestSubtype(st.category, near.map((x) => x.t));
  const nl = nearestLabel(p);
  el.innerHTML = `<div class="form" id="new-form"><b>New device</b> <span class="muted">(${p.u.toFixed(4)}, ${p.v.toFixed(4)}) ·
      saved as manual and accepted</span>
    <label>Category <select id="new-category">${categoryOptionsHtml(st.category)}</select></label>
    <label>Subtype <select id="new-subtype">${subtypeOptionsHtml(st.category, sub)}</select></label>
    <label>Room <select id="new-room">${roomOptionsHtml(-1, "proposed: nearest room label, set when saved" + (nl ? ` (nearest: ${nl.r.name})` : ""))}</select></label>
    <label>Notes <textarea id="new-notes" rows="2"></textarea></label>
    <div class="muted">Nearby text: ${near.length ? near.map(({ t, d }) => `${esc(t.text)} (${d.toFixed(0)} pt)`).join(", ") : "none within radius"}</div>
    <button class="primary" id="new-save" type="button">Save</button> <button id="new-cancel" type="button">Cancel</button>
  </div>`;
  $("new-category").onchange = () => {
    const c = $("new-category").value;
    st.category = c;
    $("new-subtype").innerHTML = subtypeOptionsHtml(c, suggestSubtype(c, near.map((x) => x.t)));
  };
  $("new-save").onclick = saveNew;
  $("new-cancel").onclick = () => { st.newPoint = null; renderOverlay(); renderDevice(); };
}

// ---------------------------------------------------------------- selection

function clearSelection() {
  st.selectedId = null; st.detail = null; st.blocking = null; st.pendingPoint = null; st.moving = false;
  st.selTok++;
}

async function select(id) {
  clearSelection();
  st.newPoint = null; st.placing = false;
  st.selectedId = id || null;
  const tok = st.selTok;
  renderTopbar(); renderOverlay(); renderDevice(); renderActions();
  if (!id) return;
  try {
    const det = await api("GET", `/api/devices/${id}`);
    if (tok !== st.selTok) return;
    st.detail = det;
  } catch (e) { return; }
  renderDevice(); renderActions();
}

// ---------------------------------------------------------------- actions

async function guarded(fn) {
  try { return await fn(); } catch (e) { return undefined; /* the toast says why */ }
}

function readChanges(dv) {
  const changes = {};
  const cat = $("ed-category"), sub = $("ed-subtype");
  if (cat.value !== cat.dataset.initial || sub.value !== sub.dataset.initial) {
    changes.category = cat.value; changes.subtype = sub.value;
  }
  const room = $("ed-room");
  if (room.value !== "" && room.value !== room.dataset.initial) changes.room = roomRef(st.meta.rooms[+room.value]);
  const notes = $("ed-notes").value.trim() || null;
  if (notes !== (dv.notes || null)) changes.notes = notes;
  if (st.pendingPoint) changes.point = st.pendingPoint;
  return changes;
}

async function saveSelected() {
  const dv = st.detail?.device;
  if (!dv || dv.id !== st.selectedId) return;
  const changes = readChanges(dv);
  if (!Object.keys(changes).length) { toast("Nothing changed to save"); return; }
  // Keeping a classification an open model_disagrees conflicts with needs a reason (spec 5.9.3): ask first, so the
  // correction and its reason are one PATCH (corrected_and_accepted). The rule is the service's: a correction
  // resolves the flag only if it changes the classification to the AI's category (and subtype, unless the AI gave
  // none). A notes-only save takes no decision, so it needs no reason.
  const openMd = (st.detail.flags || []).find((f) => f.type === "model_disagrees" && f.open);
  const correction = ["category", "subtype", "room", "point"].some((k) => k in changes);
  let asked = false, reason = null;
  if (openMd && correction) {
    const cat = changes.category ?? dv.category, sub = "subtype" in changes ? changes.subtype : dv.subtype;
    const ai = openMd.details || {};
    const classChanged = cat !== dv.category || sub !== dv.subtype;
    const resolves = classChanged && cat === ai.ai_category &&
      (ai.ai_subtype === null || ai.ai_subtype === undefined || sub === ai.ai_subtype);
    if (!resolves) {
      asked = true;
      const r = prompt(disagreementPrompt(openMd, { category: cat, subtype: sub }));
      reason = r && r.trim() ? r.trim() : null; // cancel: saved without a reason, the device stays proposed
    }
  }
  let res;
  try { res = await api("PATCH", `/api/devices/${dv.id}`, reason ? { changes, reason } : { changes }); }
  catch (e) { return; }
  st.pendingPoint = null; st.moving = false;
  let blocking = res.blocking_flags || [];
  const md = blocking.find((f) => f.type === "model_disagrees");
  if (md && !asked) {
    // Only a race (the flag opened after this panel loaded): the correction is saved; ask now and accept.
    const reason = prompt(disagreementPrompt(md, res.device));
    if (reason && reason.trim()) {
      try {
        await api("POST", `/api/devices/${dv.id}/accept`, { reason: reason.trim() }, { quiet: true });
        blocking = [];
      } catch (e) {
        blocking = e.data?.blocking_flags || blocking;
        toast(e.message, true);
      }
    }
  }
  st.blocking = blocking.length ? blocking : null;
  await guarded(refresh);
  const now = st.devices.find((d) => d.id === dv.id);
  toast(now && now.review_state === "accepted" ? "Saved and accepted" : "Saved; still awaiting review");
}

async function acceptSelected() {
  const dv = selectedDevice();
  if (!dv || dv.review_state === "accepted") return;
  const md = openFlagsOf(dv.id).find((f) => f.type === "model_disagrees");
  let body = {};
  if (md) {
    const reason = prompt(disagreementPrompt(md, dv));
    if (!reason || !reason.trim()) { toast("Not accepted: keeping a classification the AI disagrees with needs a reason", true); return; }
    body = { reason: reason.trim() };
  }
  try {
    await api("POST", `/api/devices/${dv.id}/accept`, body, { quiet: true });
    st.blocking = null;
  } catch (e) {
    st.blocking = e.data?.blocking_flags || null;
    toast(e.message, true);
  }
  await guarded(refresh);
}

async function rejectSelected() {
  const dv = selectedDevice();
  if (!dv || dv.origin === "manual" || dv.review_state === "rejected") return;
  if (await guarded(() => api("POST", `/api/devices/${dv.id}/reject`, {})) === undefined) return;
  st.blocking = null;
  await guarded(refresh);
}

async function deleteSelected() {
  const dv = selectedDevice();
  if (!dv) return;
  const reason = prompt("Reason for deleting this device?");
  if (!reason || !reason.trim()) return;
  if (await guarded(() => api("POST", `/api/devices/${dv.id}/delete`, { reason: reason.trim() })) === undefined) return;
  clearSelection();
  await guarded(refresh);
}

async function acknowledgeFlag(flagId) {
  const f = st.detail?.flags.find((x) => x.id === flagId);
  const reason = prompt(`Reason for acknowledging ${f ? f.type : "this flag"}? The device keeps its current state.`);
  if (!reason || !reason.trim()) return;
  if (await guarded(() => api("POST", `/api/flags/${flagId}/acknowledge`, { reason: reason.trim() })) === undefined) return;
  await guarded(refresh);
}

async function resolveFlag(flagId, choice) {
  if (await guarded(() => api("POST", `/api/flags/${flagId}/resolve`, { choice })) === undefined) return;
  await guarded(refresh);
}

async function saveNew() {
  const p = st.newPoint;
  if (!p) return;
  const ri = $("new-room").value;
  const body = { sheet_id: st.sheetId, category: $("new-category").value, subtype: $("new-subtype").value, point: p };
  if (ri !== "") body.room = roomRef(st.meta.rooms[+ri]);
  const notes = $("new-notes").value.trim();
  if (notes) body.notes = notes;
  let dev;
  try { dev = await api("POST", `/api/documents/${st.docId}/devices`, body); } catch (e) { return; }
  st.newPoint = null;
  await guarded(refreshData);
  await select(dev.id);
  renderAll();
  toast(`Added ${describe(dev.category, dev.subtype)} (manual, counted)`);
}

async function bulkAccept() {
  const n = st.eligible?.count || 0;
  if (!n) return;
  if (!confirm(`Accept ${n} unflagged proposals? Official totals will update.`)) return;
  try { await api("POST", `/api/documents/${st.docId}/bulk-accept`, { expected_count: n }); }
  catch (e) { await guarded(refresh); return; }
  await guarded(refresh);
  toast(`Bulk accept done: ${plural(n, "device")}`);
}

async function undoBulk() {
  const last = st.bulk?.last_undoable;
  if (!last) return;
  if (!confirm(`Undo the last bulk accept (${plural(last.device_ids.length, "device")})? Devices changed since then keep their newer state.`)) return;
  let res;
  try { res = await api("POST", `/api/bulk/${last.id}/undo`); } catch (e) { return; }
  await guarded(refresh);
  toast(`Undone: ${res.device_ids.length} restored${res.skipped.length ? `, ${res.skipped.length} kept (changed since)` : ""}`);
}

// Awaiting devices with review flags first, then accepted devices with open flags (still counted).
function flaggedQueue() {
  const fb = flagsByDevice();
  const order = (s) => st.sheets.findIndex((x) => x.sheet_id === s);
  const isFlagged = (d) => (fb[d.id] || []).some(needsReview);
  const sort = (a, b) => order(a.sheet_id) - order(b.sheet_id) || (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0);
  const awaiting = st.devices.filter((d) => d.review_state === "proposed" && isFlagged(d)).sort(sort);
  const accepted = st.devices.filter((d) => d.review_state === "accepted" && isFlagged(d)).sort(sort);
  return [...awaiting.map((d) => ({ d, kind: "awaiting review" })), ...accepted.map((d) => ({ d, kind: "accepted, still counted" }))];
}

async function reviewFlagged() {
  const q = flaggedQueue();
  if (!q.length) { st.flaggedNote = "No flagged devices."; renderActions(); return; }
  const i = q.findIndex((x) => x.d.id === st.selectedId);
  const next = q[(i + 1) % q.length];
  const pos = (i + 1) % q.length;
  if (next.d.sheet_id !== st.sheetId) await loadSheet(next.d.sheet_id);
  centerOnNorm(next.d.point.u, next.d.point.v);
  st.flaggedNote = `Flagged device ${pos + 1} of ${q.length}: ${next.kind}`;
  await select(next.d.id);
}

async function analyze() {
  if (!st.docId || st.polling) return;
  const body = $("analyze-sheet-only").checked ? { sheet_ids: [st.sheetId] } : {};
  st.polling = true;
  renderTopbar();
  try {
    const run = await api("POST", `/api/documents/${st.docId}/analyze`, body);
    upsertRun(run);
    renderStatus();
    await pollRun(run.id);
    await guarded(refresh);
  } catch (e) { /* the toast says why */ }
  finally { st.polling = false; renderTopbar(); }
}

function upsertRun(run) {
  const i = st.runs.findIndex((r) => r.id === run.id);
  if (i >= 0) st.runs[i] = run; else st.runs.push(run);
}

async function pollRun(runId) {
  let run = st.runs.find((r) => r.id === runId);
  while (runInProgress(run)) {
    await sleep(1000);
    run = await api("GET", `/api/runs/${runId}`);
    upsertRun(run);
    renderStatus();
  }
}

async function resumePolling() {
  const run = latestRun();
  if (!runInProgress(run) || st.polling) return;
  st.polling = true;
  renderTopbar();
  try { await pollRun(run.id); await refresh(); } catch (e) { /* the toast says why */ }
  finally { st.polling = false; renderTopbar(); }
}

async function exportTakeoff() {
  try { st.exportResult = await api("POST", `/api/documents/${st.docId}/export`); } catch (e) { return; }
  renderStatus();
}

async function uploadFile(file) {
  if (!file) return;
  let res;
  try {
    res = await api("POST", `/api/documents?file_name=${encodeURIComponent(file.name)}`, undefined,
      { raw: file, contentType: "application/pdf" });
  } catch (e) { return; }
  await loadDocuments();
  await loadDocument(res.document.id);
  toast(res.created ? `Uploaded ${res.document.file_name}` : `Already stored as ${res.document.file_name}`);
}

function startPlacing() {
  if (!st.meta || !st.meta.image) { toast("This sheet has no drawing to place on", true); return; }
  st.moving = false; st.placing = true; renderTopbar();
}

function startMoving() {
  if (!st.selectedId) return;
  st.placing = false; st.moving = true; renderTopbar();
}

function cancelModes() {
  if (st.moving || st.placing || st.pendingPoint || st.newPoint) {
    st.moving = false; st.placing = false; st.pendingPoint = null; st.newPoint = null;
    renderTopbar(); renderOverlay(); renderDevice({ keepForm: true });
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- input

function onPlanClick(d) {
  if (!st.meta || !st.meta.image) return;
  const [u, v] = screenToNorm(d.x, d.y);
  if (u < 0 || u > 1 || v < 0 || v > 1) return;
  if (st.moving) {
    st.moving = false; st.pendingPoint = { u, v };
    renderTopbar(); renderOverlay(); renderDevice({ keepForm: true });
    return;
  }
  if (st.placing || d.shift) {
    clearSelection();
    st.placing = false; st.newPoint = { u, v };
    renderTopbar(); renderOverlay(); renderDevice(); renderActions();
  }
}

function setupInput() {
  pv.attachPanZoom({ onClick: onPlanClick });
  // the layer switches sit on the plan: using them is neither a pan nor a click on the drawing
  for (const ev of ["mousedown", "wheel"]) $("layers").addEventListener(ev, (e) => e.stopPropagation());
  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    if (k >= "1" && k <= "8") {
      st.category = CATS[+k - 1];
      renderTopbar();
      if (st.newPoint) renderDevice();
      toast(`Category for a new device: ${st.category}`);
    } else if (k === "c" || k === "C") startPlacing();
    else if (k === "Escape") { if (!cancelModes()) select(null); }
    else if (k === "a" || k === "A") acceptSelected();
    else if (k === "r" || k === "R") rejectSelected();
    else if (k === "g" || k === "G") reviewFlagged();
    else if (k === "f" || k === "F") { if (st.meta?.image) fitView(); }
    else if (k === "Delete" || k === "Backspace") deleteSelected();
  });
  for (const id of ["layer-proposed", "layer-accepted", "layer-rejected", "layer-ai", "layer-rooms", "layer-region"]) {
    $(id).addEventListener("change", renderOverlay);
  }
  window.addEventListener("resize", () => { if (st.meta?.image) applyView(); });
  $("accept-selected").onclick = acceptSelected;
  $("reject-selected").onclick = rejectSelected;
  $("bulk-accept").onclick = bulkAccept;
  $("review-flagged").onclick = reviewFlagged;
  $("undo-bulk").onclick = undoBulk;
  $("add").onclick = startPlacing;
  $("analyze").onclick = analyze;
  $("export").onclick = exportTakeoff;
  $("upload-btn").onclick = () => $("upload").click();
  $("upload").onchange = () => { const f = $("upload").files[0]; $("upload").value = ""; uploadFile(f); };
  $("document").onchange = () => guarded(() => loadDocument($("document").value));
  $("sheet").onchange = () => { clearSelection(); st.newPoint = null; guarded(() => loadSheet($("sheet").value)); };
}

// ---------------------------------------------------------------- boot

async function boot() {
  const rev = $("reviewer");
  try { rev.value = localStorage.getItem("tk-reviewer") || ""; } catch (e) { /* storage unavailable */ }
  rev.addEventListener("change", () => { try { localStorage.setItem("tk-reviewer", rev.value); } catch (e) { /* ignore */ } });
  st.config = await api("GET", "/api/config");
  await loadDocuments();
  setupInput();
  const params = new URLSearchParams(location.search);
  const doc = [params.get("doc"), st.config.default_document, st.documents[0]?.id]
    .find((d) => d && st.documents.some((x) => x.id === d));
  if (doc) await loadDocument(doc, params.get("sheet"));
  else { st.loading = false; renderAll(); }
  document.body.dataset.ready = "1";
  resumePolling();
}

// Test and debugging hooks (state, refresh, selection and the view setter).
window.tk = {
  state: st, refresh, select, setView,
  get view() { return { ...st.view }; },
};

boot();
