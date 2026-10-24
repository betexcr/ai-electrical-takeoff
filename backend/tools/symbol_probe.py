"""Symbol-detection feasibility probe.

Usage (from backend/):
    uv run python tools/symbol_probe.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf \
        --symbols ../electrical_takeoff_symbols --diag ../reports/feasibility/diagnostics \
        --out ../reports/feasibility/symbols

Experiments (all outputs are CANDIDATE LOCATIONS, never detections or counts):
  1. Icon/label separation of the supplied reference crops + contact sheet.
  2. Sheet-native templates cropped from each sheet's own legend, located by
     exact legend label text (not by the legend region box, which is ambiguous
     on some sheets).
  3. Raster template matching (TM_CCOEFF_NORMED, rotations 0/90/180/270)
     inside the electrical-plan region hypothesis.
  4. Vector-signature matching of connected stroke components.
  5. Watermark effect: the same matching on an in-memory copy with the
     watermark text blanked. The original file is opened from bytes, never
     written, and its SHA-256 is checked before and after.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from takeoff.pdf import layout as L  # noqa: E402
from takeoff.pdf.symbols import (  # noqa: E402
    MATCH_DPI,
    ZOOM,
    THICKEN_KERNEL,
    LEGEND_LABELS,
    ICON_WINDOWS,
    PROBE_CLASSES,
    norm_text,
    legend_entries,
    find_label,
    icon_window,
    ink_bbox_in,
    render_gray,
    peaks,
    nms,
    match_template,
    segments_in,
    dedupe_segments,
    components,
    signature_matches,
)

TOOL_VERSION = "symbol_probe/0.2.0"  # 0.2.0: primitives moved to takeoff.pdf.symbols
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)
DISPLAY_THRESHOLD = 0.7  # only for drawing overlays; not a validated operating point
WATERMARK_TEXT = b"FOR USE IN THE CITY OF ESCONDIDO"

CLASS_COLOURS = [(0, 0, 230), (230, 0, 0), (0, 160, 0), (200, 0, 200), (0, 140, 255),
                 (160, 120, 0), (0, 200, 200), (90, 90, 90), (255, 0, 140)]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- 1. reference crops ---------------------------------------------------------

def runs(mask_1d: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """Index runs of True, merging runs separated by fewer than ``min_gap`` False."""
    idx = np.flatnonzero(mask_1d)
    if idx.size == 0:
        return []
    out = [[idx[0], idx[0]]]
    for i in idx[1:]:
        if i - out[-1][1] - 1 < min_gap:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b + 1) for a, b in out]


def split_reference(img_gray: np.ndarray) -> dict:
    """Separate icon from label: leftmost ink column cluster, then tallest row band."""
    ink = img_gray < 200
    cols = runs(ink.any(axis=0), min_gap=7)
    if not cols:
        return {"ok": False, "reason": "no ink"}
    c0, c1 = cols[0]
    sub = ink[:, c0:c1]
    bands = runs(sub.any(axis=1), min_gap=3)
    core = max(bands, key=lambda b: b[1] - b[0])
    core_cols = runs(sub[core[0]:core[1]].any(axis=0), min_gap=3)
    cc0, cc1 = core_cols[0][0] + c0, core_cols[-1][1] + c0
    label = None
    if len(cols) > 1:
        label = (cols[1][0], cols[-1][1])
    return {
        "ok": True,
        "icon_cluster_cols": [int(c0), int(c1)],
        "icon_core_box": [int(cc0), int(core[0]), int(cc1), int(core[1])],  # x0,y0,x1,y1 px
        "annotation_bands": [[int(a), int(b)] for a, b in bands if (a, b) != core],
        "label_cols": [int(label[0]), int(label[1])] if label else None,
        "column_clusters": len(cols),
    }


# --- 2. legend entries on the sheet --------------------------------------------------


# --- 3. raster matching -----------------------------------------------------------------


def threshold_counts(cands) -> dict:
    return {str(t): sum(1 for c in cands if c["score"] >= t) for t in THRESHOLDS}


# --- 4. vector signatures ---------------------------------------------------------------


# --- 5. watermark-free copy ------------------------------------------------------------

def blank_watermark(doc: pymupdf.Document) -> int:
    """Replace the watermark string with spaces in every stream that contains it.

    Operates on the in-memory document only; returns the number of streams edited.
    """
    edited = 0
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref):
                continue
            data = doc.xref_stream(xref)
        except Exception:
            continue
        if data and WATERMARK_TEXT in data:
            doc.update_stream(xref, data.replace(WATERMARK_TEXT, b" " * len(WATERMARK_TEXT)))
            edited += 1
    return edited


# --- main ---------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--symbols", type=Path, required=True)
    ap.add_argument("--diag", type=Path, required=True, help="pdf_diagnostics output folder")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pages", default="1,2,3,4")
    args = ap.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "templates").mkdir(exist_ok=True)

    pdf_bytes = args.pdf.read_bytes()
    hash_before = sha256_bytes(pdf_bytes)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    doc_nowm = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    edited_streams = blank_watermark(doc_nowm)

    catalog = list(csv.DictReader(open(args.symbols / "symbol_catalog.csv", encoding="utf-8")))
    report: dict = {"tool_version": TOOL_VERSION, "pymupdf_version": pymupdf.VersionBind,
                    "opencv_version": cv2.__version__, "match_dpi": MATCH_DPI,
                    "status": "candidate locations only; no ground truth exists; not detections or counts",
                    "watermark_streams_blanked_in_memory": edited_streams, "references": {}, "pages": {}}

    # 1. reference crops
    ref_info = {}
    for row in catalog:
        f = row["filename"]
        img = cv2.imread(str(args.symbols / f), cv2.IMREAD_GRAYSCALE)
        info = split_reference(img)
        info.update({"category": row["category"], "subtype": row["subtype"], "shape_px": list(img.shape)})
        ref_info[f] = (img, info)

    pages = [int(p) for p in args.pages.split(",")]
    native_templates: dict[int, dict[str, np.ndarray]] = {}
    for pno in pages:
        page = doc[pno - 1]
        page_nowm = doc_nowm[pno - 1]
        meta = json.loads((args.diag / f"page_{pno}" / "meta.json").read_text(encoding="utf-8"))
        elec = [r for r in meta["regions"] if r["kind"] == "electrical_plan"][0]
        # Exclude the view-title strip at the bottom of the cell.
        ebox = list(elec["bbox"])
        ebox[3] = min(ebox[3], elec["title_bbox"][1] - 2.0)
        lines = L.text_lines(page)
        drawings = page.get_drawings()
        entries = legend_entries(lines)
        page_rep = {"electrical_bbox_searched": [round(v, 2) for v in ebox], "legend_entries": {},
                    "raster": {}, "vector": {}, "watermark_effect": {}}

        # 2. sheet-native templates
        native = {}
        for f, label in LEGEND_LABELS.items():
            hits = find_label(entries, label)
            rec = {"label": label, "label_hits": len(hits)}
            if len(hits) == 1:
                win = icon_window(f, hits[0]["bbox"])
                ib = ink_bbox_in(page, win, drawings)
                sb = ink_bbox_in(page, win, drawings, include_text=False)
                rec["label_bbox"] = [round(v, 2) for v in hits[0]["bbox"]]
                rec["search_window"] = [round(v, 2) for v in win]
                rec["icon_bbox"] = [round(v, 2) for v in ib] if ib else None
                rec["icon_strokes_bbox"] = [round(v, 2) for v in sb] if sb else None
                if ib:
                    cv2.imwrite(str(out / "templates" / f"p{pno}_with_text_{f}"), render_gray(page, ib))
                if sb:
                    # Stroke-only (letters excluded) is what matching uses: the
                    # letters vary by subtype and are read from text instead.
                    t = render_gray(page, sb)
                    native[f] = t
                    cv2.imwrite(str(out / "templates" / f"p{pno}_strokes_{f}"), t)
                    rec["icon_px"] = list(t.shape)
                else:
                    rec["note"] = "no vector strokes in icon window (icon may be text-only)"
            page_rep["legend_entries"][f] = rec
        native_templates[pno] = native

        # 3 + 5. raster matching on the region, with and without watermark
        img = render_gray(page, ebox)
        img_nowm = render_gray(page_nowm, ebox)
        diff = cv2.absdiff(img, img_nowm)
        page_rep["watermark_effect"]["pixels_changed_fraction"] = round(float((diff > 8).mean()), 5)
        page_rep["watermark_effect"]["max_abs_gray_diff"] = int(diff.max())
        cv2.imwrite(str(out / f"p{pno}_watermark_diff_x4.png"), np.clip(diff.astype(np.int32) * 4, 0, 255).astype(np.uint8))
        cv2.imwrite(str(out / f"p{pno}_nowm_{MATCH_DPI}dpi.png"), img_nowm)

        overlay = cv2.cvtColor(cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR)
        img_thick = cv2.erode(img, THICKEN_KERNEL)
        for ci, f in enumerate(PROBE_CLASSES):
            if f not in native:
                page_rep["raster"][f] = {"skipped": "no sheet-native template (legend label not found uniquely)"}
                continue
            t0 = time.perf_counter()
            cands = match_template(img, native[f], min(THRESHOLDS))
            elapsed = time.perf_counter() - t0
            cands_nowm = match_template(img_nowm, native[f], min(THRESHOLDS))
            cands_thick = match_template(img_thick, cv2.erode(native[f], THICKEN_KERNEL), min(THRESHOLDS))
            page_rep["raster"][f] = {
                "template_px": list(native[f].shape),
                "candidates_by_threshold": threshold_counts(cands),
                "candidates_by_threshold_watermark_blanked": threshold_counts(cands_nowm),
                "candidates_by_threshold_strokes_thickened_5px": threshold_counts(cands_thick),
                "seconds": round(elapsed, 2),
                "top_scores": [round(c["score"], 3) for c in cands[:5]],
                "candidates_at_display_threshold": [
                    {"cx_pt": round(ebox[0] + c["cx"] / ZOOM, 2), "cy_pt": round(ebox[1] + c["cy"] / ZOOM, 2),
                     "score": round(c["score"], 3), "rot": c["rot"]}
                    for c in cands if c["score"] >= DISPLAY_THRESHOLD],
            }
            col = CLASS_COLOURS[ci % len(CLASS_COLOURS)]
            for c in cands:
                if c["score"] < DISPLAY_THRESHOLD:
                    continue
                x0 = int((c["cx"] - c["w"] / 2) / 2); y0 = int((c["cy"] - c["h"] / 2) / 2)
                x1 = int((c["cx"] + c["w"] / 2) / 2); y1 = int((c["cy"] + c["h"] / 2) / 2)
                cv2.rectangle(overlay, (x0, y0), (x1, y1), col, 2)
                cv2.putText(overlay, f"{ci}:{c['score']:.2f}", (x0, max(10, y0 - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, col, 1)
        legend_y = 20
        cv2.putText(overlay, f"CANDIDATES (raster NCC >= {DISPLAY_THRESHOLD}, unvalidated)", (8, legend_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        for ci, f in enumerate(PROBE_CLASSES):
            legend_y += 18
            cv2.putText(overlay, f"{ci}: {f[:-4]}", (8, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        CLASS_COLOURS[ci % len(CLASS_COLOURS)], 1)
        cv2.imwrite(str(out / f"p{pno}_raster_candidates.png"), overlay)

        # 4. vector signatures
        raw_segs = segments_in(drawings, ebox)
        region_segs = dedupe_segments(raw_segs)
        region_comps = components(region_segs)
        page_rep["vector"]["region_segments_raw"] = len(raw_segs)
        page_rep["vector"]["region_segments_after_dedupe"] = len(region_segs)
        page_rep["vector"]["region_components"] = len(region_comps)
        page_rep["vector"]["region_component_size_hist"] = dict(Counter(
            "1" if c["n"] == 1 else "2-5" if c["n"] <= 5 else "6-20" if c["n"] <= 20 else "21-100" if c["n"] <= 100 else ">100"
            for c in region_comps))
        voverlay = cv2.cvtColor(cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR)
        for ci, f in enumerate(PROBE_CLASSES):
            rec = page_rep["legend_entries"].get(f, {})
            if not rec.get("icon_bbox"):
                page_rep["vector"][f] = {"skipped": "no legend icon bbox"}
                continue
            icon_comps = components(dedupe_segments(segments_in(drawings, rec["icon_bbox"])))
            if not icon_comps:
                page_rep["vector"][f] = {"skipped": "legend icon has no stroke components (text only?)"}
                continue
            key = max(icon_comps, key=lambda c: c["length"])
            hits = signature_matches(key, region_comps)
            page_rep["vector"][f] = {
                "icon_components": len(icon_comps),
                "key_component": {"n_segments": key["n"], "dims_pt": [round(v, 2) for v in key["dims"]],
                                  "length_pt": round(key["length"], 2)},
                "matching_components_in_region": len(hits),
            }
            col = CLASS_COLOURS[ci % len(CLASS_COLOURS)]
            for h in hits:
                b = h["bbox"]
                p0 = (int((b[0] - ebox[0]) * ZOOM / 2) - 3, int((b[1] - ebox[1]) * ZOOM / 2) - 3)
                p1 = (int((b[2] - ebox[0]) * ZOOM / 2) + 3, int((b[3] - ebox[1]) * ZOOM / 2) + 3)
                cv2.rectangle(voverlay, p0, p1, col, 2)
                cv2.putText(voverlay, str(ci), (p0[0], max(10, p0[1] - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1)
        cv2.putText(voverlay, "CANDIDATES (vector signature of largest legend-icon component, unvalidated)",
                    (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        cv2.imwrite(str(out / f"p{pno}_vector_candidates.png"), voverlay)
        report["pages"][str(pno)] = page_rep

    # Reference vs sheet-native comparison (sheet 1 templates) + contact sheet
    scales = []
    p1 = native_templates.get(pages[0], {})
    rows = []
    for f, (img, info) in ref_info.items():
        rec = dict(info)
        legend = report["pages"][str(pages[0])]["legend_entries"].get(f, {})
        if info.get("ok") and info.get("label_cols") and legend.get("label_bbox"):
            label_px = info["label_cols"][1] - info["label_cols"][0]
            label_pt = legend["label_bbox"][2] - legend["label_bbox"][0]
            rec["px_per_pt_estimate"] = round(label_px / label_pt, 3)
            scales.append(rec["px_per_pt_estimate"])
        if info.get("ok") and f in p1:
            x0, y0, x1, y1 = info["icon_core_box"]
            core = img[y0:y1, x0:x1]
            nat = p1[f]
            if core.size and nat.size:
                resized = cv2.resize(core, (nat.shape[1], nat.shape[0]), interpolation=cv2.INTER_AREA)
                rec["ncc_core_vs_native_resized"] = round(float(cv2.matchTemplate(
                    resized, nat, cv2.TM_CCOEFF_NORMED).max()), 3) if resized.std() > 0 and nat.std() > 0 else None
                rec["aspect_core_vs_native"] = [round(core.shape[1] / max(1, core.shape[0]), 2),
                                                round(nat.shape[1] / max(1, nat.shape[0]), 2)]
        report["references"][f] = rec
        rows.append((f, img, info, p1.get(f)))
    report["reference_px_per_pt_median"] = float(np.median(scales)) if scales else None

    # Contact sheet: original | extracted core | sheet-1 native icon
    cell_h = 130
    sheet = []
    for f, img, info, nat in rows:
        vis = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if info.get("ok"):
            x0, y0, x1, y1 = info["icon_core_box"]
            cv2.rectangle(vis, (x0, y0), (x1 - 1, y1 - 1), (0, 0, 255), 1)
        def fit(im, h=cell_h - 20, w=300):
            if im is None or im.size == 0:
                return np.full((h, w, 3), 235, np.uint8)
            if im.ndim == 2:
                im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
            s = min(h / im.shape[0], w / im.shape[1])
            r = cv2.resize(im, (max(1, int(im.shape[1] * s)), max(1, int(im.shape[0] * s))), interpolation=cv2.INTER_AREA)
            canvas = np.full((h, w, 3), 255, np.uint8)
            canvas[:r.shape[0], :r.shape[1]] = r
            return canvas
        core = None
        if info.get("ok"):
            x0, y0, x1, y1 = info["icon_core_box"]
            core = img[y0:y1, x0:x1]
        row = np.hstack([fit(vis), np.full((cell_h - 20, 10, 3), 200, np.uint8), fit(core, w=160),
                         np.full((cell_h - 20, 10, 3), 200, np.uint8), fit(nat, w=160)])
        header = np.full((20, row.shape[1], 3), 255, np.uint8)
        ncc = report["references"][f].get("ncc_core_vs_native_resized")
        cv2.putText(header, f"{f}   ncc(core,native)={ncc}", (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
        sheet.append(np.vstack([header, row]))
    cs = np.vstack(sheet)
    top = np.full((24, cs.shape[1], 3), 255, np.uint8)
    cv2.putText(top, "reference crop (red=extracted icon core) | extracted core | sheet-1 legend icon (native, 300dpi)",
                (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    cv2.imwrite(str(out / "contact_sheet.png"), np.vstack([top, cs]))

    report["input_sha256_before"] = hash_before
    report["input_sha256_after"] = sha256_bytes(args.pdf.read_bytes())
    report["input_unchanged"] = report["input_sha256_before"] == report["input_sha256_after"]
    (out / "symbol_probe.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"input_unchanged": report["input_unchanged"], "watermark_streams_blanked": edited_streams,
                      "reference_px_per_pt_median": report["reference_px_per_pt_median"]}, indent=1))
    return 0 if report["input_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
