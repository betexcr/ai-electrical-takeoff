"""Structural tests for the diagnostic tool against the supplied plan PDF.

These assert observed facts about the input and the *shape* of the outputs.
They do not assert detection accuracy; region boxes are hypotheses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
sys.path.insert(0, str(ROOT / "backend" / "tools"))

import pdf_diagnostics  # noqa: E402

from takeoff.pdf import layout as L  # noqa: E402

pytestmark = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("diag")
    code = pdf_diagnostics.main([str(PDF), "--out", str(out)])
    return code, out, json.loads((out / "summary.json").read_text(encoding="utf-8"))


def test_input_unchanged_and_exit_ok(run):
    code, _, summary = run
    assert code == 0
    assert summary["input_unchanged"] is True
    assert summary["input_sha256_before"] == pdf_diagnostics.sha256(PDF)


def test_four_pages_with_observed_dimensions(run):
    _, _, summary = run
    assert summary["page_count"] == 4
    for p in summary["pages"]:
        assert p["size_pt"] == [2592.0, 1728.0]
        assert p["rotation"] == 0
        assert p["counts"]["words"] > 0 and p["counts"]["drawings"] > 0


def test_expected_files_written(run):
    _, out, _ = run
    for n in range(1, 5):
        d = out / f"page_{n}"
        for name in ("meta.json", "text.json", f"page_{n}_overlay_60dpi.png",
                     f"page_{n}_render_150dpi.png", f"page_{n}_electrical_crop_150dpi.png"):
            assert (d / name).stat().st_size > 0, name


def test_sheet_ids_are_read_from_text(run):
    _, _, summary = run
    assert [p["sheet_id_candidates"] for p in summary["pages"]] == [["A1-111"], ["A2-111"], ["A3-111"], ["A4-111"]]


def test_region_schema_and_hypothesis_status(run):
    _, out, _ = run
    for n in range(1, 5):
        meta = json.loads((out / f"page_{n}" / "meta.json").read_text(encoding="utf-8"))
        kinds = [r["kind"] for r in meta["regions"]]
        assert kinds.count("electrical_plan") == 1
        for r in meta["regions"]:
            assert r["status"] == "hypothesis"
            assert r["method"]
            assert set(r["sides"]) == {"left", "top", "right", "bottom"}
            x0, y0, x1, y1 = r["bbox"]
            assert x0 < x1 and y0 < y1
            assert all(0.0 <= v <= 1.0 for v in r["bbox_normalized"])
            assert isinstance(r["warnings"], list)


def test_single_diagonal_watermark_per_page(run):
    _, out, _ = run
    for n in range(1, 5):
        meta = json.loads((out / f"page_{n}" / "meta.json").read_text(encoding="utf-8"))
        assert [w["text"] for w in meta["watermarks"]] == ["FOR USE IN THE CITY OF ESCONDIDO"]


def test_title_patterns_tolerate_observed_typo():
    line = L.TextLine("A4-111 MECHNICAL PLAN - PLAN 4", (0, 0, 1, 1), 24.9, "Arial", (1.0, 0.0))
    assert L.find_titles([line])[0][0] == "mechanical_plan"
    notes = L.TextLine("ELECTRICAL NOTES", (0, 0, 1, 1), 24.9, "Arial", (1.0, 0.0))
    assert L.find_titles([notes])[0][0] == "electrical_notes"


def test_slightly_off_axis_label_is_not_a_watermark():
    small = L.TextLine("IM", (0, 0, 1, 1), 6.5, "Arial", (0.0040710, -0.9999917))
    diag = L.TextLine("X", (0, 0, 1, 1), 20.0, "Arial", (0.866, -0.5))
    assert L.find_watermarks([small, diag]) == [diag]


def test_ray_cast_falls_back_to_page_edge_and_says_so():
    segs = [L.Segment("V", 100.0, 0.0, 500.0, 0.84)]
    bbox, sides = L.ray_cast_cell(segs, 200.0, 250.0, pymupdf.Rect(0, 0, 1000, 500))
    assert bbox == (100.0, 0, 1000, 500)
    assert sides["left"]["source"] == "frame_line"
    assert sides["right"]["source"] == "page_edge"
