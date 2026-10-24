"""Reproducible PDF feasibility diagnostics for the supplied plan set.

Usage (from backend/):
    uv run python tools/pdf_diagnostics.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf \
        --out ../reports/feasibility

The input PDF is opened read-only and its SHA-256 is recorded before and after.
Everything is written under ``--out``. Region boxes are *hypotheses* (see
takeoff.pdf.layout); nothing here is a detection or a count of components.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from takeoff.pdf import coords as C  # noqa: E402
from takeoff.pdf import layout as L  # noqa: E402

TOOL_VERSION = "pdf_diagnostics/0.1.0"
RENDER_DPI = 150
OVERVIEW_DPI = 60
CROP_DPI = 150

KIND_COLOURS = {  # BGR
    "electrical_plan": (0, 0, 230),
    "mechanical_plan": (200, 120, 0),
    "finish_plan": (0, 150, 0),
    "legend": (0, 140, 255),
    "electrical_notes": (160, 0, 160),
}
DEFAULT_COLOUR = (120, 120, 120)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray:
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR).copy()


def to_px(g: C.PageGeometry, pt, img) -> tuple[int, int]:
    u, v = C.unrotated_to_normalized(g, pt)
    x, y = C.normalized_to_raster((u, v), img.shape[1], img.shape[0])
    return int(round(x)), int(round(y))


def draw_rect(img, g, bbox, colour, thickness=2, label=None, scale=0.5):
    p0 = to_px(g, (bbox[0], bbox[1]), img)
    p1 = to_px(g, (bbox[2], bbox[3]), img)
    cv2.rectangle(img, p0, p1, colour, thickness)
    if label:
        cv2.putText(img, label, (min(p0[0], p1[0]) + 3, min(p0[1], p1[1]) + int(18 * scale * 2)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, colour, max(1, thickness // 2), cv2.LINE_AA)


def draw_grid(img):
    h, w = img.shape[:2]
    for i in range(1, 10):
        x, y = int(w * i / 10), int(h * i / 10)
        cv2.line(img, (x, 0), (x, h), (210, 210, 210), 1)
        cv2.line(img, (0, y), (w, y), (210, 210, 210), 1)
        cv2.putText(img, f"u={i / 10:.1f}", (x + 2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 150, 150), 1)
        cv2.putText(img, f"v={i / 10:.1f}", (2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 150, 150), 1)


def drawing_stats(drawings, bbox=None) -> dict:
    """Vector statistics; containment uses coordinates, not Rect.intersects,
    because PyMuPDF treats zero-height/width rects (axis-aligned lines) as empty."""
    def inside(d):
        if bbox is None:
            return True
        r = d["rect"]
        return bbox[0] <= r.x0 and r.x1 <= bbox[2] and bbox[1] <= r.y0 and r.y1 <= bbox[3]

    ds = [d for d in drawings if inside(d)]
    kinds = collections.Counter()
    seg_len = collections.Counter()
    items_per_path = collections.Counter()
    degenerate = 0
    for d in ds:
        items_per_path[min(len(d["items"]), 10)] += 1
        r = d["rect"]
        if r.width == 0 or r.height == 0:
            degenerate += 1
        for it in d["items"]:
            kinds[it[0]] += 1
            if it[0] == "l":
                ln = abs(it[1] - it[2])
                bucket = "<1" if ln < 1 else "1-3" if ln < 3 else "3-10" if ln < 10 else "10-50" if ln < 50 else ">=50"
                seg_len[bucket] += 1
    return {
        "paths": len(ds),
        "path_types": dict(collections.Counter(d["type"] for d in ds)),
        "item_kinds": dict(kinds),
        "items_per_path_capped_10": {str(k): v for k, v in sorted(items_per_path.items())},
        "line_length_pt_buckets": dict(seg_len),
        "stroke_widths": {str(round(k, 3)): v for k, v in
                          collections.Counter(round(d.get("width") or 0, 3) for d in ds).most_common(12)},
        "stroke_colours": {str(k): v for k, v in collections.Counter(
            tuple(round(c, 3) for c in d["color"]) if d.get("color") else None for d in ds).most_common(8)},
        "fill_colours": {str(k): v for k, v in collections.Counter(
            tuple(round(c, 3) for c in d["fill"]) if d.get("fill") else None for d in ds).most_common(8)},
        "dash_attribute_values": {str(k): v for k, v in collections.Counter(d.get("dashes") for d in ds).most_common(8)},
        "degenerate_rect_paths": degenerate,
    }


def analyse_page(doc, page: pymupdf.Page, out: Path, inputs_name: str) -> dict:
    n = page.number + 1
    pdir = out / f"page_{n}"
    pdir.mkdir(parents=True, exist_ok=True)
    g = C.PageGeometry.from_page(page)

    t0 = time.perf_counter()
    lines = L.text_lines(page)
    words = page.get_text("words")
    drawings = page.get_drawings()
    t_extract = time.perf_counter() - t0

    watermarks = L.find_watermarks(lines)
    titles = L.find_titles(lines)
    regions = L.region_hypotheses(page, lines, drawings)
    frames = L.frame_segments(page, drawings)

    sheet_ids = [l.text for l in lines if l.size > 30 and l.size < L.WATERMARK_MIN_FONT_SIZE]

    images = []
    for info in page.get_image_info(xrefs=True):
        images.append({"xref": info.get("xref"), "width": info["width"], "height": info["height"],
                       "bbox": [round(v, 1) for v in info["bbox"]],
                       "bbox_normalized": [round(v, 4) for v in C.unrotated_rect_to_normalized(g, info["bbox"])]})

    region_dicts = []
    for r in regions:
        d = r.to_dict()
        d["bbox_normalized"] = [round(v, 5) for v in C.unrotated_rect_to_normalized(g, r.bbox)]
        if r.kind.endswith("_plan"):
            d["room_label_candidates"] = [
                {"text": l.text, "bbox": [round(v, 2) for v in l.bbox], "size": round(l.size, 2)}
                for l in L.room_label_candidates(lines, r.bbox)]
        if r.kind == "electrical_plan":
            toks = L.short_tokens(page, r.bbox)
            d["short_token_counts"] = dict(collections.Counter(t["text"] for t in toks).most_common())
            d["vector_stats"] = drawing_stats(drawings, r.bbox)
        region_dicts.append(d)

    text_dump = {
        "coordinate_space": "pymupdf_unrotated_points",
        "lines": [{"text": l.text, "bbox": [round(v, 2) for v in l.bbox], "size": round(l.size, 2),
                   "font": l.font, "dir": [round(v, 4) for v in l.direction]} for l in lines],
        "words": [{"text": w[4], "bbox": [round(v, 2) for v in w[:4]]} for w in words],
    }
    (pdir / "text.json").write_text(json.dumps(text_dump, indent=1, ensure_ascii=False), encoding="utf-8")

    # --- renders and overlays ---------------------------------------------------
    t1 = time.perf_counter()
    pix = page.get_pixmap(dpi=RENDER_DPI)
    t_render = time.perf_counter() - t1
    pix.save(pdir / f"page_{n}_render_{RENDER_DPI}dpi.png")

    ov = pixmap_to_bgr(page.get_pixmap(dpi=OVERVIEW_DPI))
    draw_grid(ov)
    for s in frames:
        a = (s.pos, s.lo) if s.orient == "V" else (s.lo, s.pos)
        b = (s.pos, s.hi) if s.orient == "V" else (s.hi, s.pos)
        cv2.line(ov, to_px(g, a, ov), to_px(g, b, ov), (255, 200, 0), 1)
    for wm in watermarks:
        draw_rect(ov, g, wm.bbox, (170, 170, 170), 1, "watermark bbox", 0.4)
    for r in regions:
        col = KIND_COLOURS.get(r.kind, DEFAULT_COLOUR)
        tag = f"{r.kind} [hypothesis]" + (" !" if r.warnings else "")
        draw_rect(ov, g, r.bbox, col, 3 if r.kind == "electrical_plan" else 2, tag, 0.45)
        draw_rect(ov, g, r.title_bbox, col, 1)
    for r in regions:
        if r.kind.endswith("_plan"):
            for l in L.room_label_candidates(lines, r.bbox):
                draw_rect(ov, g, l.bbox, (0, 160, 0), 1)
    cv2.imwrite(str(pdir / f"page_{n}_overlay_{OVERVIEW_DPI}dpi.png"), ov)

    # Electrical-region crop at higher DPI with text evidence.
    for i, r in enumerate([r for r in regions if r.kind == "electrical_plan"]):
        clip = pymupdf.Rect(*r.bbox)
        cpix = page.get_pixmap(dpi=CROP_DPI, clip=clip)
        crop = pixmap_to_bgr(cpix)
        zoom = CROP_DPI / 72.0

        def cp(pt):
            return int(round((pt[0] - clip.x0) * zoom)), int(round((pt[1] - clip.y0) * zoom))

        for l in L.room_label_candidates(lines, r.bbox):
            cv2.rectangle(crop, cp(l.bbox[:2]), cp(l.bbox[2:]), (0, 160, 0), 2)
        for t in L.short_tokens(page, r.bbox):
            cv2.rectangle(crop, cp(t["bbox"][:2]), cp(t["bbox"][2:]), (200, 0, 200), 1)
        cv2.putText(crop, "green: room-label text  magenta: 1-3 char tokens  [text evidence only]",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.imwrite(str(pdir / f"page_{n}_electrical_crop_{CROP_DPI}dpi.png"), crop)

    meta = {
        "page_number": n,
        "geometry": {
            "page_rect_displayed": [round(v, 3) for v in page.rect],
            "cropbox_native": [g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1],
            "rotation": g.rotation,
            "displayed_size_pt": [g.displayed_width, g.displayed_height],
            "size_in": [round(g.displayed_width / 72, 3), round(g.displayed_height / 72, 3)],
            "pymupdf_transformation_matrix": list(page.transformation_matrix),
            "pymupdf_rotation_matrix": list(page.rotation_matrix),
            "render_px_at_150dpi": [pix.width, pix.height],
            "nominal_px_at_150dpi": list(C.raster_size(g, RENDER_DPI)),
        },
        "sheet_id_candidates": sheet_ids,
        "counts": {"text_lines": len(lines), "words": len(words), "drawings": len(drawings),
                   "frame_segments": len(frames), "images": len(images)},
        "xobjects": [list(x[:3]) + [[round(v, 2) for v in x[3]]] for x in page.get_xobjects()],
        "images": images,
        "watermarks": [{"text": w.text, "size": round(w.size, 2), "dir": [round(v, 4) for v in w.direction],
                        "bbox": [round(v, 1) for v in w.bbox]} for w in watermarks],
        "titles": [{"kind": k, "text": l.text, "bbox": [round(v, 1) for v in l.bbox], "size": round(l.size, 2)}
                   for k, l in titles],
        "regions": region_dicts,
        "page_vector_stats": drawing_stats(drawings),
        "timings_s": {"extract_text_and_drawings": round(t_extract, 3), f"render_{RENDER_DPI}dpi": round(t_render, 3)},
        "frame_assumptions": {"stroke_widths": L.FRAME_STROKE_WIDTHS, "min_length_pt": L.FRAME_MIN_LENGTH},
    }
    (pdir / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    before = sha256(args.pdf)
    doc = pymupdf.open(args.pdf)
    pages = [analyse_page(doc, p, args.out, args.pdf.name) for p in doc]
    doc.close()
    after = sha256(args.pdf)

    summary = {
        "tool_version": TOOL_VERSION,
        "pymupdf_version": pymupdf.VersionBind,
        "mupdf_version": pymupdf.VersionFitz,
        "input": str(args.pdf.name),
        "input_sha256_before": before,
        "input_sha256_after": after,
        "input_unchanged": before == after,
        "page_count": len(pages),
        "pages": [{
            "page": m["page_number"],
            "size_pt": m["geometry"]["displayed_size_pt"],
            "rotation": m["geometry"]["rotation"],
            "sheet_id_candidates": m["sheet_id_candidates"],
            "titles": [t["text"] for t in m["titles"]],
            "electrical_regions": [{"bbox": r["bbox"], "bbox_normalized": r["bbox_normalized"],
                                    "warnings": r["warnings"],
                                    "room_labels": [x["text"] for x in r.get("room_label_candidates", [])]}
                                   for r in m["regions"] if r["kind"] == "electrical_plan"],
            "regions_with_warnings": [{"kind": r["kind"], "warnings": r["warnings"]}
                                      for r in m["regions"] if r["warnings"]],
            "counts": m["counts"],
        } for m in pages],
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0 if summary["input_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
