"""Per-sheet facts the review workflow needs, derived from the PDF itself.

All positions are produced in PyMuPDF unrotated points and converted to the
canonical normalized space with takeoff.pdf.coords; nothing here assumes the
page is unrotated.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pymupdf

from takeoff.groundtruth.protocol import CELL_SIZE_PT
from takeoff.pdf import coords as C
from takeoff.pdf import layout as L

SHEET_ID_MIN_FONT = 30.0  # title-block sheet number ("A1-111") is set at 37.5 pt
IMAGE_DPI = 300
ROOM_EXTERIOR = {"kind": "exterior", "name": "Exterior", "label_bbox_pt": None}
ROOM_UNLABELED = {"kind": "unlabeled", "name": "Unlabeled", "label_bbox_pt": None}


class SheetError(RuntimeError):
    pass


def sheet_id(page: pymupdf.Page, lines: list[L.TextLine] | None = None) -> str:
    """The sheet number printed in the title block, e.g. 'A1-111'."""
    lines = lines if lines is not None else L.text_lines(page)
    ids = [l.text for l in lines if SHEET_ID_MIN_FONT < l.size < L.WATERMARK_MIN_FONT_SIZE and L.is_horizontal(l)]
    if len(ids) != 1:
        raise SheetError(f"page {page.number + 1}: expected one sheet id, found {ids}")
    return ids[0]


def electrical_region(page: pymupdf.Page, lines: list[L.TextLine] | None = None, drawings=None) -> dict:
    """The electrical-plan region hypothesis and the window searched within it.

    ``window_pt`` excludes the view-title strip at the bottom of the cell, as in
    the Milestone 1 probe.
    """
    regions = [r for r in L.region_hypotheses(page, lines, drawings) if r.kind == "electrical_plan"]
    if len(regions) != 1:
        raise SheetError(f"page {page.number + 1}: expected one electrical-plan region, found {len(regions)}")
    r = regions[0]
    window = list(r.bbox)
    window[3] = min(window[3], r.title_bbox[1] - 2.0)
    return {"bbox_pt": tuple(r.bbox), "window_pt": tuple(window), "title_bbox_pt": tuple(r.title_bbox),
            "method": r.method, "status": r.status, "warnings": list(r.warnings)}


def distance_pt(g: C.PageGeometry, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance in points between two normalized points (displayed space is in points)."""
    ax, ay = C.normalized_to_displayed(g, a)
    bx, by = C.normalized_to_displayed(g, b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def norm_rect(g: C.PageGeometry, bbox_pt) -> dict:
    u0, v0, u1, v1 = C.unrotated_rect_to_normalized(g, tuple(bbox_pt))
    return {"u0": u0, "v0": v0, "u1": u1, "v1": v1}


def coverage_grid(sheet: str, g: C.PageGeometry, region_pt, cell_pt: float = CELL_SIZE_PT) -> list[dict]:
    """Equal cells (<= cell_pt per side) that tile ``region_pt`` exactly, row-major."""
    x0, y0, x1, y1 = region_pt
    cols = max(1, math.ceil((x1 - x0) / cell_pt))
    rows = max(1, math.ceil((y1 - y0) / cell_pt))
    xs = [x0 + (x1 - x0) * i / cols for i in range(cols)] + [x1]
    ys = [y0 + (y1 - y0) * j / rows for j in range(rows)] + [y1]
    cells = []
    for r in range(rows):
        for c in range(cols):
            bb = (xs[c], ys[r], xs[c + 1], ys[r + 1])
            cells.append({"id": f"{sheet}:r{r:02d}c{c:02d}", "sheet_id": sheet, "row": r, "col": c,
                          "bbox_pt": bb, "bbox": norm_rect(g, bb)})
    return cells


def text_tokens(page: pymupdf.Page, g: C.PageGeometry, region_pt) -> list[dict]:
    """Words inside the region, with overprinted duplicates merged (dup_count > 1)."""
    x0, y0, x1, y1 = region_pt
    merged: dict[tuple, dict] = {}
    for w in page.get_text("words"):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        key = (w[4], round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1))
        if key in merged:
            merged[key]["dup_count"] += 1
            continue
        bb = tuple(round(v, 3) for v in w[:4])
        u, v = C.unrotated_to_normalized(g, (cx, cy))
        merged[key] = {"text": w[4], "bbox_pt": bb, "bbox": norm_rect(g, bb), "center": {"u": u, "v": v},
                       "short": bool(L.SHORT_TOKEN.match(w[4])), "dup_count": 1}
    out = sorted(merged.values(), key=lambda t: (t["bbox_pt"][1], t["bbox_pt"][0], t["text"]))
    for i, t in enumerate(out):
        t["id"] = f"t{i:04d}"
    return out


def room_labels(lines: list[L.TextLine], g: C.PageGeometry, region_pt) -> list[dict]:
    seen, out = set(), []
    for l in L.room_label_candidates(lines, region_pt):
        bb = tuple(round(v, 3) for v in l.bbox)
        key = (l.text, round(bb[0], 1), round(bb[1], 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": "labeled", "name": l.text, "label_bbox_pt": bb, "bbox": norm_rect(g, bb)})
    out.sort(key=lambda r: (r["label_bbox_pt"][1], r["label_bbox_pt"][0]))
    return out


def render_region_image(page: pymupdf.Page, region_pt, pdf_sha256: str, cache_dir: Path,
                        dpi: int = IMAGE_DPI) -> tuple[Path, dict]:
    """PNG of the region (cached by PDF hash, page, region and DPI) and its exact mapping.

    The mapping states which normalized rectangle the image *actually* covers.
    PyMuPDF snaps a clipped render to whole device pixels (``pix.x``, ``pix.y``),
    so the covered area is derived from the pixmap's pixel origin and size, not
    from the requested clip. Rotated pages are refused: the supplied sheets are
    unrotated and clip semantics on rotated pages are not yet tested.
    """
    if page.rotation % 360 != 0:
        raise SheetError(f"page {page.number + 1} is rotated ({page.rotation}); reviewer images support rotation 0 only")
    g = C.PageGeometry.from_page(page)
    key = hashlib.sha256(f"{pdf_sha256}|{page.number}|{tuple(round(v, 3) for v in region_pt)}|{dpi}".encode()).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"region_p{page.number + 1}_{dpi}dpi_{key}.png"
    sidecar = path.with_suffix(".json")
    if path.exists() and sidecar.exists():
        return path, json.loads(sidecar.read_text(encoding="utf-8"))
    pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(region_pt), colorspace=pymupdf.csGRAY)
    zoom = dpi / 72.0
    covers_pt = (pix.x / zoom, pix.y / zoom, (pix.x + pix.width) / zoom, (pix.y + pix.height) / zoom)
    mapping = {"width_px": pix.width, "height_px": pix.height, "dpi": dpi, "origin_px": [pix.x, pix.y],
               "region_pt": list(region_pt), "covers_pt": list(covers_pt), "covers": norm_rect(g, covers_pt)}
    tmp = path.with_suffix(".tmp.png")
    pix.save(tmp)
    tmp.replace(path)
    sidecar.write_text(json.dumps(mapping), encoding="utf-8")
    return path, mapping


def normalized_to_image_px(mapping: dict, p: tuple[float, float]) -> tuple[float, float]:
    c = mapping["covers"]
    return ((p[0] - c["u0"]) / (c["u1"] - c["u0"]) * mapping["width_px"],
            (p[1] - c["v0"]) / (c["v1"] - c["v0"]) * mapping["height_px"])


def image_px_to_normalized(mapping: dict, p: tuple[float, float]) -> tuple[float, float]:
    c = mapping["covers"]
    return (c["u0"] + p[0] / mapping["width_px"] * (c["u1"] - c["u0"]),
            c["v0"] + p[1] / mapping["height_px"] * (c["v1"] - c["v0"]))


def describe_sheet(page: pymupdf.Page, pdf_sha256: str, cache_dir: Path) -> dict:
    """Everything the reviewer UI needs about a sheet, except review state."""
    lines = L.text_lines(page)
    g = C.PageGeometry.from_page(page)
    sid = sheet_id(page, lines)
    region = electrical_region(page, lines)
    _, mapping = render_region_image(page, region["bbox_pt"], pdf_sha256, cache_dir)
    return {
        "sheet_id": sid, "page_number": page.number + 1,
        "geometry": {"crop_native": [g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1], "rotation": g.rotation,
                     "displayed_width_pt": g.displayed_width, "displayed_height_pt": g.displayed_height},
        "region": {**region, "bbox": norm_rect(g, region["bbox_pt"]), "window": norm_rect(g, region["window_pt"])},
        "image": mapping,
        "cells": coverage_grid(sid, g, region["bbox_pt"]),
        "tokens": text_tokens(page, g, region["bbox_pt"]),
        "rooms": room_labels(lines, g, region["bbox_pt"]) + [ROOM_EXTERIOR, ROOM_UNLABELED],
    }


class SheetSpec:
    """The parts of a sheet description the workflow, checks and export rely on."""

    def __init__(self, sheet_id: str, page_number: int, geometry: C.PageGeometry, region_bbox_pt,
                 cells: list[dict]):
        self.sheet_id = sheet_id
        self.page_number = page_number
        self.geometry = geometry
        self.region_bbox_pt = tuple(region_bbox_pt)
        self.cells = cells

    @classmethod
    def from_description(cls, d: dict) -> "SheetSpec":
        g = d["geometry"]
        geom = C.PageGeometry(*g["crop_native"], rotation=g["rotation"])
        return cls(d["sheet_id"], d["page_number"], geom, d["region"]["bbox_pt"], d["cells"])

    def to_pt(self, p: tuple[float, float]) -> tuple[float, float]:
        return C.normalized_to_unrotated(self.geometry, p)

    def in_region(self, p: tuple[float, float]) -> bool:
        x, y = self.to_pt(p)
        x0, y0, x1, y1 = self.region_bbox_pt
        return x0 <= x <= x1 and y0 <= y <= y1

    def cell_at(self, p: tuple[float, float]) -> str | None:
        x, y = self.to_pt(p)
        for c in self.cells:
            x0, y0, x1, y1 = c["bbox_pt"]
            last_col = abs(x1 - self.region_bbox_pt[2]) < 1e-9
            last_row = abs(y1 - self.region_bbox_pt[3]) < 1e-9
            if x0 <= x and (x < x1 or (last_col and x <= x1)) and y0 <= y and (y < y1 or (last_row and y <= y1)):
                return c["id"]
        return None

    def distance_pt(self, a, b) -> float:
        return distance_pt(self.geometry, a, b)
