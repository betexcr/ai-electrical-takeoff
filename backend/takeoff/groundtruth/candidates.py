"""Candidate pre-fill for the ground-truth review.

Candidates are hints that speed up annotation. They are never exhaustive,
never detections and never counts; the reviewer must still sweep every
coverage cell and the blind scan hides them entirely. Generation reuses the
Milestone 1 primitives (takeoff.pdf.symbols) unchanged.

Default thresholds are review-burden parameters, not detector operating
points. Milestone 1 measured 85-432 raster switch candidates per sheet at
NCC >= 0.5 on the plain render (mostly noise), but 7-18 at plain >= 0.7 and
13-28 at thickened >= 0.6 (reports/feasibility/symbols/summary_table.txt).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import pymupdf

from takeoff.groundtruth import models as M
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.protocol import MATCH_TOLERANCE_PT
from takeoff.pdf import coords as C
from takeoff.pdf import layout as L
from takeoff.pdf import symbols as SY

GENERATOR_VERSION = f"gt_candidates/1+{SY.PRIMITIVES_VERSION}"
DEFAULT_PARAMS = {
    "raster_plain": {"threshold": 0.7, "dpi": SY.MATCH_DPI},
    "raster_thickened": {"threshold": 0.6, "dpi": SY.MATCH_DPI, "erode_kernel": 5},
    "vector_signature": {"component_eps_pt": 0.3, "dedupe": True},
}

_HINTS = {s.catalog_file: (c, s.name) for c, subs in T.CATEGORIES.items() for s in subs if s.catalog_file}


def class_hint(class_file: str) -> tuple[str | None, str | None]:
    return _HINTS.get(class_file, (None, None))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clamp_rect(r):
    return tuple(min(1.0, max(0.0, v)) for v in r)


def _sheet_context(page: pymupdf.Page) -> dict:
    lines = L.text_lines(page)
    drawings = page.get_drawings()
    region = S.electrical_region(page, lines, drawings)
    entries = SY.legend_entries(lines)
    templates, icon_boxes = {}, {}
    for f, label in SY.LEGEND_LABELS.items():
        hits = SY.find_label(entries, label)
        if len(hits) != 1:
            continue
        win = SY.icon_window(f, hits[0]["bbox"])
        sb = SY.ink_bbox_in(page, win, drawings, include_text=False)
        if sb:
            templates[f] = SY.render_gray(page, sb)
            icon_boxes[f] = sb
    return {"sheet_id": S.sheet_id(page, lines), "geometry": C.PageGeometry.from_page(page), "region": region,
            "drawings": drawings, "templates": templates, "icon_boxes": icon_boxes}


def _raw_candidate(ctx, page_number, class_file, cx_pt, cy_pt, bbox_pt, score, rotation):
    g = ctx["geometry"]
    u, v = C.unrotated_to_normalized(g, (cx_pt, cy_pt))
    r = _clamp_rect(C.unrotated_rect_to_normalized(g, bbox_pt))
    cat, sub = class_hint(class_file)
    return {"sheet_id": ctx["sheet_id"], "page_number": page_number, "class_hypothesis": class_file[:-4],
            "category_hint": cat, "subtype_hint": sub, "score": None if score is None else round(float(score), 4),
            "point": {"u": round(u, 7), "v": round(v, 7)}, "point_pt": (round(cx_pt, 3), round(cy_pt, 3)),
            "bbox": {"u0": r[0], "v0": r[1], "u1": r[2], "v1": r[3]}, "rotation": rotation}


def _raster(ctx, page, page_number, classes, threshold, thicken):
    win = ctx["region"]["window_pt"]
    img = SY.render_gray(page, win)
    if thicken:
        img = cv2.erode(img, SY.THICKEN_KERNEL)
    out = []
    for f in classes:
        tmpl = ctx["templates"].get(f)
        if tmpl is None:
            continue
        if thicken:
            tmpl = cv2.erode(tmpl, SY.THICKEN_KERNEL)
        for c in SY.match_template(img, tmpl, threshold):
            cx, cy = win[0] + c["cx"] / SY.ZOOM, win[1] + c["cy"] / SY.ZOOM
            hw, hh = c["w"] / SY.ZOOM / 2, c["h"] / SY.ZOOM / 2
            out.append(_raw_candidate(ctx, page_number, f, cx, cy, (cx - hw, cy - hh, cx + hw, cy + hh),
                                      c["score"], c["rot"]))
    return out


def _vector(ctx, page_number, classes):
    win = ctx["region"]["window_pt"]
    comps = SY.components(SY.dedupe_segments(SY.segments_in(ctx["drawings"], win)))
    out = []
    for f in classes:
        ib = ctx["icon_boxes"].get(f)
        if ib is None:
            continue
        icon = SY.components(SY.dedupe_segments(SY.segments_in(ctx["drawings"], ib)))
        if not icon:
            continue
        key = max(icon, key=lambda c: c["length"])
        for h in SY.signature_matches(key, comps):
            b = h["bbox"]
            out.append(_raw_candidate(ctx, page_number, f, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, b, None, None))
    return out


def run_hash(generator: str, params: dict, input_sha256: str, raw: list[dict]) -> str:
    """Content hash: identical inputs, code version, params and outputs give the same hash."""
    body = {"generator": generator, "version": GENERATOR_VERSION, "params": params, "input": input_sha256,
            "candidates": sorted((r["sheet_id"], r["class_hypothesis"], r["point"]["u"], r["point"]["v"],
                                  r["score"] if r["score"] is not None else -1.0) for r in raw)}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def generate(pdf_path: Path, pages: list[int] | None = None, generators: tuple[str, ...] | None = None,
             classes: list[str] | None = None, params: dict | None = None) -> list[tuple[M.CandidateRun, list[M.Candidate]]]:
    """One CandidateRun per generator covering the selected pages (1-based)."""
    generators = generators or tuple(DEFAULT_PARAMS)
    classes = classes or list(SY.PROBE_CLASSES)
    input_sha = _sha256_file(pdf_path)
    doc = pymupdf.open(stream=Path(pdf_path).read_bytes(), filetype="pdf")  # never writes the file
    pages = pages or list(range(1, doc.page_count + 1))
    contexts = {n: _sheet_context(doc[n - 1]) for n in pages}
    results = []
    for gen in generators:
        p = {**DEFAULT_PARAMS[gen], **((params or {}).get(gen, {})), "classes": classes, "pages": pages}
        raw = []
        for n in pages:
            ctx, page = contexts[n], doc[n - 1]
            if gen == "vector_signature":
                raw += _vector(ctx, n, classes)
            else:
                raw += _raster(ctx, page, n, classes, p["threshold"], thicken=(gen == "raster_thickened"))
        h = run_hash(gen, p, input_sha, raw)
        run = M.CandidateRun(id=f"run-{h[:16]}", generator=gen, generator_version=GENERATOR_VERSION, params=p,
                             input_sha256=input_sha, run_hash=h)
        cands = [M.Candidate(id=f"{h[:12]}-{i:05d}", run_id=run.id, **r) for i, r in enumerate(raw)]
        results.append((run, cands))
    doc.close()
    return results


def cluster_candidates(cands: list[M.Candidate], runs: dict[str, M.CandidateRun],
                       geometries: dict[str, C.PageGeometry], tol_pt: float = MATCH_TOLERANCE_PT) -> list[dict]:
    """Group co-located candidates from any run into review clusters (deterministic).

    Greedy by descending score (vector candidates last), each candidate joining
    the first cluster on its sheet whose anchor is within ``tol_pt``.
    """
    order = sorted(cands, key=lambda c: (c.sheet_id, -(c.score if c.score is not None else -1.0), c.id))
    clusters: list[dict] = []
    for c in order:
        g = geometries[c.sheet_id]
        home = None
        for cl in clusters:
            if cl["sheet_id"] == c.sheet_id and S.distance_pt(g, (cl["point"]["u"], cl["point"]["v"]),
                                                                 (c.point.u, c.point.v)) <= tol_pt:
                home = cl
                break
        if home is None:
            home = {"id": f"cl-{c.id}", "kind": "candidate_cluster", "sheet_id": c.sheet_id,
                    "point": c.point.model_dump(), "bbox": c.bbox.model_dump() if c.bbox else None, "members": []}
            clusters.append(home)
        run = runs[c.run_id]
        home["members"].append({"candidate_id": c.id, "class_hypothesis": c.class_hypothesis,
                                "category_hint": c.category_hint, "subtype_hint": c.subtype_hint,
                                "score": c.score, "generator": run.generator,
                                "generator_version": run.generator_version, "rotation": c.rotation})
    return clusters
