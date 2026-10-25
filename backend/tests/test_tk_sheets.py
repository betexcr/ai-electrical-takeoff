"""Sheet intake for any PDF, per-request legend references and the discovery legend composite."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

from takeoff.app import sheets as TS
from takeoff.app.models import SheetInfo
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth import taxonomy as T
from takeoff.pdf import symbols as SY

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")
DOC = "d" * 64
SHA = "e" * 64


@pytest.fixture(scope="module")
def sheets():
    return TS.intake(PDF, DOC)


@pytest.fixture(scope="module")
def blank_pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("blank") / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    return path


@needs_pdf
def test_escondido_intake_finds_four_sheets_with_legends(sheets):
    assert [s.sheet_id for s in sheets] == ["A1-111", "A2-111", "A3-111", "A4-111"]
    assert [s.page_number for s in sheets] == [1, 2, 3, 4]
    for s in sheets:
        assert isinstance(s, SheetInfo)
        assert s.id == f"{DOC}:{s.page_number}"
        assert s.document_id == DOC
        assert s.legend_found is True
        assert s.note is None
        assert s.region_bbox_pt is not None and len(s.region_bbox_pt) == 4
    # region is the electrical-plan region reported by the reviewer module
    doc = pymupdf.open(stream=PDF.read_bytes(), filetype="pdf")
    assert sheets[0].region_bbox_pt == pytest.approx(S.electrical_region(doc[0])["bbox_pt"])
    assert TS.LEGEND_MIN_LABELS == 5


def test_blank_pdf_page_is_page_n_without_legend(blank_pdf):
    out = TS.intake(blank_pdf, DOC)
    assert len(out) == 1
    s = out[0]
    assert s.sheet_id == "page-1"
    assert s.page_number == 1
    assert s.id == f"{DOC}:1"
    assert s.legend_found is False
    assert s.note
    doc = pymupdf.open(blank_pdf)
    assert s.region_bbox_pt == pytest.approx(tuple(doc[0].rect))
    doc.close()


def test_blank_pdf_sheet_view_and_icons_do_not_raise(blank_pdf, tmp_path):
    s = TS.intake(blank_pdf, DOC)[0]
    v = TS.sheet_view(blank_pdf, s, SHA, tmp_path)
    assert v["sheet_id"] == "page-1" and v["legend_found"] is False and v["note"]
    assert v["rooms"] == [S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
    assert TS.legend_icons(blank_pdf, s) == {}
    assert {"width_px", "height_px", "dpi", "covers_pt", "covers"} <= set(v["image"])
    assert v["image"]["width_px"] > 0 and v["image"]["height_px"] > 0


@needs_pdf
def test_no_region_fallback_has_the_same_region_keys_as_a_real_sheet(blank_pdf, sheets, tmp_path):
    fallback = TS.sheet_view(blank_pdf, TS.intake(blank_pdf, DOC)[0], SHA, tmp_path / "blank")
    real = TS.sheet_view(PDF, sheets[0], SHA, tmp_path / "real")
    assert set(fallback["region"]) == set(real["region"])
    assert set(fallback) == set(real)
    assert set(fallback["image"]) == set(real["image"])
    assert set(fallback["region"]["bbox"]) == set(real["region"]["bbox"]) == {"u0", "v0", "u1", "v1"}
    assert set(fallback["region"]["window"]) == {"u0", "v0", "u1", "v1"}


def test_unexpected_exception_on_a_page_is_recorded_not_raised(blank_pdf, monkeypatch):
    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(S, "electrical_region", boom)
    monkeypatch.setattr(S, "sheet_id", lambda *a, **k: "A9-999")
    out = TS.intake(blank_pdf, DOC)
    assert len(out) == 1
    assert out[0].sheet_id == "page-1" and out[0].legend_found is False
    assert "ValueError" in out[0].note and "boom" in out[0].note
    assert out[0].region_bbox_pt is not None


def test_rotated_page_is_recorded_as_unsupported(tmp_path):
    path = tmp_path / "rotated.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.set_rotation(90)
    doc.save(path)
    doc.close()
    out = TS.intake(path, DOC)
    assert len(out) == 1
    assert out[0].sheet_id == "page-1" and out[0].legend_found is False
    assert "rotat" in out[0].note.lower() and "not supported" in out[0].note.lower()
    assert out[0].region_bbox_pt is not None


@needs_pdf
def test_sheet_view_matches_reviewer_description(sheets, tmp_path):
    v = TS.sheet_view(PDF, sheets[0], SHA, tmp_path / "a")
    doc = pymupdf.open(stream=PDF.read_bytes(), filetype="pdf")
    d = S.describe_sheet(doc[0], SHA, tmp_path / "b")
    assert set(v) >= {"sheet_id", "page_number", "geometry", "region", "image", "tokens", "rooms",
                      "legend_found", "note"}
    assert v["sheet_id"] == d["sheet_id"] == "A1-111"
    assert v["page_number"] == 1
    assert v["image"] == d["image"]
    assert [r["name"] for r in v["rooms"]] == [r["name"] for r in d["rooms"]]
    assert v["rooms"][-2:] == [S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
    assert v["geometry"] == d["geometry"]
    assert v["region"] == d["region"]
    assert v["tokens"] == d["tokens"]
    assert v["legend_found"] is True and v["note"] is None


@needs_pdf
def test_legend_icons_found_on_a1_and_empty_without_legend(sheets):
    icons = TS.legend_icons(PDF, sheets[0])
    assert len(icons) == 26  # 27 legend labels on A1 minus the wiring-run reference (not a device)
    assert "wiring_run_electrical_REFERENCE_BOM.png" not in icons  # not a device
    hints = {s.catalog_file: (c, s.name) for c, subs in T.CATEGORIES.items() for s in subs if s.catalog_file}
    for f, ic in icons.items():
        assert f in SY.LEGEND_LABELS
        assert (ic["category"], ic["subtype"]) == hints[f]
        assert ic["label"]
        img = cv2.imdecode(np.frombuffer(ic["png"], np.uint8), cv2.IMREAD_GRAYSCALE)
        assert img is not None and img.size > 0 and img.std() > 0
    assert icons["switch_standard.png"]["category"] == "switch"
    assert icons["switch_standard.png"]["subtype"] == "standard"
    # a sheet without a legend gives no references
    blank = SheetInfo(id=f"{DOC}:1", document_id=DOC, page_number=1, sheet_id="A1-111", legend_found=False)
    assert TS.legend_icons(PDF, blank) == {}


@needs_pdf
def test_legend_composite_is_deterministic_and_labelled(sheets):
    icons = TS.legend_icons(PDF, sheets[0])
    a = TS.legend_composite(icons)
    b = TS.legend_composite(dict(reversed(list(icons.items()))))
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    img = cv2.imdecode(np.frombuffer(a, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img.shape[1] <= 2576
    assert img.shape[0] > 0
    # one cell per icon, each with ink in its label band (below the icon area)
    n = len(icons)
    rows = -(-n // TS.COMPOSITE_COLS)
    assert img.shape == (rows * TS.COMPOSITE_CELL_H, TS.COMPOSITE_COLS * TS.COMPOSITE_CELL_W)
    pad = TS.COMPOSITE_PAD
    for i in range(n):
        r, c = divmod(i, TS.COMPOSITE_COLS)
        x0, y0 = c * TS.COMPOSITE_CELL_W, r * TS.COMPOSITE_CELL_H
        band = img[y0 + 2 * pad + TS.COMPOSITE_ICON_H: y0 + TS.COMPOSITE_CELL_H - 2,
                   x0 + 2: x0 + TS.COMPOSITE_CELL_W - 2]
        assert (band < 128).sum() > 50, f"cell {i} has no label text"
    for i in range(n, rows * TS.COMPOSITE_COLS):  # unused trailing cells stay blank
        r, c = divmod(i, TS.COMPOSITE_COLS)
        cell = img[r * TS.COMPOSITE_CELL_H:(r + 1) * TS.COMPOSITE_CELL_H,
                   c * TS.COMPOSITE_CELL_W:(c + 1) * TS.COMPOSITE_CELL_W]
        assert (cell < 128).sum() == 0
    # blanking the labels removes the label ink (the check above cannot be met by the icons alone)
    blank = TS.legend_composite({f: {**ic, "label": ""} for f, ic in icons.items()})
    blank_img = cv2.imdecode(np.frombuffer(blank, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert blank_img.shape == img.shape
    assert (blank_img < 128).sum() < (img < 128).sum()
    assert (blank_img[:, :] != img[:, :]).any()
    # fewer icons give a different (smaller) image
    one = dict(list(icons.items())[:1])
    assert TS.legend_composite(one) != a
