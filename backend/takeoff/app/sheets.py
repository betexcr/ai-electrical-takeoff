"""Sheet intake for any PDF, plus the per-request legend references used by the AI stages.

``intake`` never raises on a readable PDF: a page whose title block or electrical-plan region
cannot be found becomes ``page-N`` with the full page as its region and a note. Everything here
reuses the reviewer-side primitives (``groundtruth.sheets``) and the Milestone 1 symbol
primitives (``pdf.symbols``); nothing is re-implemented. The PDF is opened from bytes and is
never written.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from takeoff.app.models import SheetInfo
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth import taxonomy as T
from takeoff.pdf import coords as C
from takeoff.pdf import layout as L
from takeoff.pdf import symbols as SY

LEGEND_MIN_LABELS = 5  # LEGEND_LABELS found exactly once for legend_found (Escondido: 27/27)

COMPOSITE_COLS = 4
COMPOSITE_CELL_W = 640  # 4 columns -> 2560 px, within the 2576 px image limit
COMPOSITE_ICON_H = 200
COMPOSITE_LABEL_LINES = 3
COMPOSITE_LABEL_CHARS = 50
COMPOSITE_LINE_H = 24
COMPOSITE_PAD = 10
COMPOSITE_CELL_H = COMPOSITE_ICON_H + COMPOSITE_LABEL_LINES * COMPOSITE_LINE_H + 3 * COMPOSITE_PAD

_HINTS = {s.catalog_file: (c, s) for c, subs in T.CATEGORIES.items() for s in subs if s.catalog_file}


def _open(pdf_path: Path) -> pymupdf.Document:
    return pymupdf.open(stream=Path(pdf_path).read_bytes(), filetype="pdf")  # never writes the file


def _full_page_pt(page: pymupdf.Page) -> tuple[float, float, float, float]:
    return tuple(round(v, 3) for v in (page.rect * page.derotation_matrix))


def _legend_locations(page: pymupdf.Page, lines) -> dict[str, dict]:
    """Catalogue file -> legend label block, for labels found exactly once on the page."""
    entries = SY.legend_entries(lines)
    found = {}
    for f, label in SY.LEGEND_LABELS.items():
        hits = SY.find_label(entries, label)
        if len(hits) == 1:
            found[f] = hits[0]
    return found


def intake(pdf_path: Path, document_id: str) -> list[SheetInfo]:
    """One SheetInfo per page of the PDF."""
    doc = _open(pdf_path)
    try:
        out = []
        for page in doc:
            n = page.number + 1
            info = dict(id=f"{document_id}:{n}", document_id=document_id, page_number=n)
            try:
                if page.rotation % 360 != 0:
                    raise S.SheetError(f"page {n} is rotated ({page.rotation}); rotated pages are not supported")
                lines = L.text_lines(page)
                sid = S.sheet_id(page, lines)
                region = S.electrical_region(page, lines)
                legend = _legend_locations(page, lines)
                out.append(SheetInfo(**info, sheet_id=sid, region_bbox_pt=region["bbox_pt"],
                                     legend_found=len(legend) >= LEGEND_MIN_LABELS))
            except Exception as e:  # intake never raises on a readable PDF; the page is recorded with a note
                note = str(e) if isinstance(e, S.SheetError) else f"intake failed: {type(e).__name__}: {e}"
                out.append(SheetInfo(**info, sheet_id=f"page-{n}", region_bbox_pt=_full_page_pt(page),
                                     legend_found=False, note=note))
        return out
    finally:
        doc.close()


def sheet_view(pdf_path: Path, sheet: SheetInfo, pdf_sha: str, cache_dir: Path) -> dict:
    """What the reviewer UI needs about one sheet, in the shapes of ``groundtruth.sheets.describe_sheet``."""
    doc = _open(pdf_path)
    try:
        page = doc[sheet.page_number - 1]
        g = C.PageGeometry.from_page(page)
        lines = L.text_lines(page)
        try:
            region = S.electrical_region(page, lines)
            rooms = S.room_labels(lines, g, region["bbox_pt"]) + [S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
        except S.SheetError:
            bbox = tuple(sheet.region_bbox_pt) if sheet.region_bbox_pt else _full_page_pt(page)
            region = {"bbox_pt": bbox, "window_pt": bbox, "title_bbox_pt": None, "method": "full_page",
                      "status": "not_found", "warnings": [sheet.note] if sheet.note else []}
            rooms = [S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
        _, mapping = S.render_region_image(page, region["bbox_pt"], pdf_sha, Path(cache_dir))
        return {
            "sheet_id": sheet.sheet_id, "page_number": sheet.page_number,
            "geometry": {"crop_native": [g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1], "rotation": g.rotation,
                         "displayed_width_pt": g.displayed_width, "displayed_height_pt": g.displayed_height},
            "region": {**region, "bbox": S.norm_rect(g, region["bbox_pt"]),
                       "window": S.norm_rect(g, region["window_pt"])},
            "image": mapping,
            "tokens": S.text_tokens(page, g, region["bbox_pt"]),
            "rooms": rooms,
            "legend_found": sheet.legend_found, "note": sheet.note,
        }
    finally:
        doc.close()


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def legend_icons(pdf_path: Path, sheet: SheetInfo) -> dict[str, dict]:
    """Catalogue file -> category, subtype, legend label and icon PNG, from the sheet's own legend.

    Empty when the sheet has no legend. The wiring-run reference is not a device and is left out.
    """
    if not sheet.legend_found:
        return {}
    doc = _open(pdf_path)
    try:
        page = doc[sheet.page_number - 1]
        lines = L.text_lines(page)
        drawings = page.get_drawings()
        out = {}
        for f, hit in _legend_locations(page, lines).items():
            if f not in _HINTS:
                continue
            box = SY.ink_bbox_in(page, SY.icon_window(f, hit["bbox"]), drawings, include_text=False)
            if not box:
                continue
            category, sub = _HINTS[f]
            out[f] = {"category": category, "subtype": sub.name,
                      "label": sub.legend_label or SY.LEGEND_LABELS[f],
                      "png": _png(SY.render_gray(page, box))}
        return out
    finally:
        doc.close()


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def legend_composite(icons: dict[str, dict]) -> bytes:
    """One PNG grid of every icon with its legend wording, for AI discovery. Deterministic."""
    names = sorted(icons)
    rows = max(1, -(-len(names) // COMPOSITE_COLS))
    canvas = np.full((rows * COMPOSITE_CELL_H, COMPOSITE_COLS * COMPOSITE_CELL_W), 255, np.uint8)
    for i, f in enumerate(names):
        r, c = divmod(i, COMPOSITE_COLS)
        x0, y0 = c * COMPOSITE_CELL_W, r * COMPOSITE_CELL_H
        icon = cv2.imdecode(np.frombuffer(icons[f]["png"], np.uint8), cv2.IMREAD_GRAYSCALE)
        if icon is not None and icon.size:
            h, w = icon.shape
            scale = min(1.0, (COMPOSITE_CELL_W - 2 * COMPOSITE_PAD) / w, COMPOSITE_ICON_H / h)
            if scale < 1.0:
                icon = cv2.resize(icon, (max(1, round(w * scale)), max(1, round(h * scale))),
                                  interpolation=cv2.INTER_AREA)
            h, w = icon.shape
            ix = x0 + (COMPOSITE_CELL_W - w) // 2
            iy = y0 + COMPOSITE_PAD + (COMPOSITE_ICON_H - h) // 2
            canvas[iy:iy + h, ix:ix + w] = icon
        label = textwrap.wrap(_ascii(icons[f]["label"]), COMPOSITE_LABEL_CHARS)[:COMPOSITE_LABEL_LINES]
        for k, text in enumerate(label):
            cv2.putText(canvas, text, (x0 + COMPOSITE_PAD, y0 + 2 * COMPOSITE_PAD + COMPOSITE_ICON_H
                                       + (k + 1) * COMPOSITE_LINE_H - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, 0, 1, cv2.LINE_AA)
        cv2.rectangle(canvas, (x0, y0), (x0 + COMPOSITE_CELL_W - 1, y0 + COMPOSITE_CELL_H - 1), 190, 1)
    return _png(canvas)
