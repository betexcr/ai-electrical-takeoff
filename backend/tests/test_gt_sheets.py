"""Sheet assets: coverage grid (AC6), tokens/rooms, and real-sheet image alignment (AC1 server side)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.protocol import CELL_SIZE_PT
from takeoff.pdf import coords as C

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")


# --- coverage grid ------------------------------------------------------------------------

@pytest.mark.parametrize("region", [(854.4, 37.08, 1439.04, 870.12), (143.88, 37.08, 1439.04, 556.68), (0, 0, 50, 30)])
def test_grid_tiles_region_exactly(region):
    g = C.PageGeometry(0, 0, 2592, 1728)
    cells = S.coverage_grid("A1-111", g, region)
    x0, y0, x1, y1 = region
    area = sum((c["bbox_pt"][2] - c["bbox_pt"][0]) * (c["bbox_pt"][3] - c["bbox_pt"][1]) for c in cells)
    assert area == pytest.approx((x1 - x0) * (y1 - y0))
    for c in cells:
        w, h = c["bbox_pt"][2] - c["bbox_pt"][0], c["bbox_pt"][3] - c["bbox_pt"][1]
        assert 0 < w <= CELL_SIZE_PT + 1e-9 and 0 < h <= CELL_SIZE_PT + 1e-9
    # no overlaps: pairwise intersection area is zero
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            ix = min(a["bbox_pt"][2], b["bbox_pt"][2]) - max(a["bbox_pt"][0], b["bbox_pt"][0])
            iy = min(a["bbox_pt"][3], b["bbox_pt"][3]) - max(a["bbox_pt"][1], b["bbox_pt"][1])
            assert ix <= 1e-9 or iy <= 1e-9
    assert len({c["id"] for c in cells}) == len(cells)
    assert cells == S.coverage_grid("A1-111", g, region)  # deterministic


# --- real sheets ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def doc():
    return pymupdf.open(PDF)


@pytest.fixture(scope="module")
def pdf_sha():
    return hashlib.sha256(PDF.read_bytes()).hexdigest()


@needs_pdf
def test_sheet_ids_and_regions(doc):
    assert [S.sheet_id(p) for p in doc] == ["A1-111", "A2-111", "A3-111", "A4-111"]
    for p in doc:
        r = S.electrical_region(p)
        assert r["status"] == "hypothesis"
        assert r["window_pt"][3] < r["bbox_pt"][3]  # title strip excluded from the window


@needs_pdf
def test_overprinted_tokens_are_merged_on_A3(doc):
    p = doc[2]
    g = C.PageGeometry.from_page(p)
    toks = S.text_tokens(p, g, S.electrical_region(p)["bbox_pt"])
    keys = [(t["text"], t["bbox_pt"]) for t in toks]
    assert len(keys) == len(set(keys))
    j = [t for t in toks if t["text"] == "J"]
    assert len(j) == 2 and all(t["dup_count"] == 2 for t in j)  # Milestone 1: each J drawn twice


@needs_pdf
def test_room_labels_keep_separate_instances(doc):
    p = doc[0]
    g = C.PageGeometry.from_page(p)
    rooms = S.room_labels(__import__("takeoff.pdf.layout", fromlist=["x"]).text_lines(p), g,
                          S.electrical_region(p)["bbox_pt"])
    names = [r["name"] for r in rooms]
    assert names.count("CL") == 2 and "KITCHEN / DINING" in names
    assert all(r["kind"] == "labeled" and r["label_bbox_pt"] for r in rooms)


@needs_pdf
def test_rotated_page_is_refused(tmp_path):
    d = pymupdf.open()
    d.new_page(width=200, height=100).set_rotation(90)
    with pytest.raises(S.SheetError, match="rotation 0 only"):
        S.render_region_image(d[0], (0, 0, 50, 50), "x" * 64, tmp_path)


@needs_pdf
def test_image_is_cached(doc, pdf_sha, tmp_path):
    p = doc[0]
    region = S.electrical_region(p)["bbox_pt"]
    path1, m1 = S.render_region_image(p, region, pdf_sha, tmp_path)
    mtime = path1.stat().st_mtime_ns
    path2, m2 = S.render_region_image(p, region, pdf_sha, tmp_path)
    assert path1 == path2 and path2.stat().st_mtime_ns == mtime and m1 == m2


@needs_pdf
@pytest.mark.parametrize("page_index", [0, 1, 2, 3])
def test_served_image_aligns_with_pdf_words(doc, pdf_sha, tmp_path, page_index):
    """Words cut directly from the PDF land where the mapping says (<= 1 px at 300 dpi)."""
    p = doc[page_index]
    g = C.PageGeometry.from_page(p)
    region = S.electrical_region(p)["bbox_pt"]
    path, mapping = S.render_region_image(p, region, pdf_sha, tmp_path)
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    assert img.shape == (mapping["height_px"], mapping["width_px"])
    words = [t for t in S.text_tokens(p, g, region) if t["text"] in ("LIVING", "J")]
    assert words, "expected LIVING or J on every sheet"
    for t in words:
        pix = p.get_pixmap(dpi=mapping["dpi"], clip=pymupdf.Rect(t["bbox_pt"]), colorspace=pymupdf.csGRAY)
        tmpl = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
        # Where the word's own render starts, measured on the renderer's pixel grid.
        true_x, true_y = pix.x - mapping["origin_px"][0], pix.y - mapping["origin_px"][1]
        m = 8
        win = img[true_y - m:true_y + tmpl.shape[0] + m, true_x - m:true_x + tmpl.shape[1] + m]
        res = cv2.matchTemplate(win, tmpl, cv2.TM_SQDIFF_NORMED)
        _, _, best, _ = cv2.minMaxLoc(res)
        # 1) The pixels really are there: best match at the renderer's own offset.
        assert (best[0] - m, best[1] - m) == (0, 0), (t["text"], best)
        # 2) The mapping predicts that offset to within the pixel snapping (< 1 px).
        zoom = mapping["dpi"] / 72.0
        snapped = (pix.x / zoom, pix.y / zoom)
        px, py = S.normalized_to_image_px(mapping, C.unrotated_to_normalized(g, snapped))
        assert abs(px - true_x) < 1e-6 and abs(py - true_y) < 1e-6
        u0, v0 = t["bbox"]["u0"], t["bbox"]["v0"]
        wx, wy = S.normalized_to_image_px(mapping, (u0, v0))
        assert abs(wx - true_x) <= 1.0 and abs(wy - true_y) <= 1.0, (t["text"], wx - true_x, wy - true_y)
        assert S.image_px_to_normalized(mapping, (wx, wy)) == pytest.approx((u0, v0))


@needs_pdf
def test_describe_sheet_payload(doc, pdf_sha, tmp_path):
    d = S.describe_sheet(doc[0], pdf_sha, tmp_path)
    assert d["sheet_id"] == "A1-111" and d["page_number"] == 1
    assert d["region"]["status"] == "hypothesis"
    assert d["rooms"][-2]["kind"] == "exterior" and d["rooms"][-1]["kind"] == "unlabeled"
    assert len(d["cells"]) == 6 * 9  # 585 x 833 pt region -> 6 x 9 cells of <= 100 pt
    assert all(0 <= t["center"]["u"] <= 1 for t in d["tokens"])
