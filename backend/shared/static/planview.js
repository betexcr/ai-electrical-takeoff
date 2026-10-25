// Shared plan-view code: coordinate transforms, view (pan/zoom) handling, SVG and taxonomy helpers.
// Used by the ground-truth reviewer and the takeoff page. Moved verbatim from reviewer/static/app.js.
//
// Coordinates: annotations are stored in canonical normalized (u, v) page space.
// The served image covers meta.image.covers (normalized) at width_px x height_px.
// View transform: screen = (tx + imgX * s, ty + imgY * s).
//
// makePlanView(state, { viewport, image, onChange }):
//   state     { meta, view: {s, tx, ty}, config, ... } read live on every call (so it may be reassigned)
//   viewport  the element that clips the plan and receives wheel / drag input
//   image     the element the view transform is applied to
//   onChange  called after every applyView (the page re-renders its overlay here)

"use strict";

function makePlanView(st, { viewport, image, onChange }) {
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

  // ---------------------------------------------------------------- view

  function applyView() {
    const { s, tx, ty } = st.view;
    image.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
    onChange();
  }
  function setView(s, tx, ty) { st.view = { s, tx, ty }; applyView(); }
  function fitView() {
    const vp = viewport.getBoundingClientRect();
    const m = st.meta.image;
    const s = Math.min(vp.width / m.width_px, vp.height / m.height_px) * 0.96;
    setView(s, (vp.width - m.width_px * s) / 2, (vp.height - m.height_px * s) / 2);
  }
  function minScale() {
    const vp = viewport.getBoundingClientRect();
    return Math.min(vp.width / st.meta.image.width_px, vp.height / st.meta.image.height_px) * 0.25;
  }
  function zoomAt(sx, sy, factor) {
    const s = Math.min(8, Math.max(minScale(), st.view.s * factor));
    const [x, y] = screenToImg(sx, sy);
    setView(s, sx - x * s, sy - y * s);
  }
  function centerOnNorm(u, v, s) {
    const vp = viewport.getBoundingClientRect();
    const [x, y] = normToImg(u, v);
    s = s || st.view.s;
    setView(s, vp.width / 2 - x * s, vp.height / 2 - y * s);
  }

  // Wheel zoom and drag pan. The drag record lives in state.drag ({x, y, tx, ty, moved, shift}); on mouse-up
  // without movement, `onClick(drag)` is called so the page can do its own click handling (placement, select).
  function attachPanZoom({ onClick } = {}) {
    const vp = viewport;
    function localXY(e) {
      const r = vp.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    }
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
      if (onClick) onClick(d);
    });
  }

  // ---------------------------------------------------------------- rendering helpers

  function svg(tag, attrs, parent) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (parent) parent.appendChild(el);
    return el;
  }

  // ---------------------------------------------------------------- taxonomy

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

  return {
    normToImg, imgToNorm, imgToScreen, screenToImg, normToScreen, screenToNorm, distPt, ptToScreenLen,
    applyView, setView, fitView, minScale, zoomAt, centerOnNorm, attachPanZoom,
    svg, subLabel, subtypeOptions, suggestSubtype, nearbyTokens, roomOptions,
  };
}

window.makePlanView = makePlanView;
