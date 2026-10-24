"""Tests for the symbol-probe building blocks.

Unit tests use synthetic inputs; the two real-PDF tests assert observed
structure (legend labels found once per sheet, watermark blanking is
in-memory only). None of these assert detection accuracy.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
sys.path.insert(0, str(ROOT / "backend" / "tools"))

import symbol_probe as sp  # noqa: E402

from takeoff.pdf import layout as L  # noqa: E402

needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")


def test_split_reference_separates_icon_from_label():
    img = np.full((60, 200), 255, np.uint8)
    cv2.putText(img, "3D", (8, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, 0, 1)   # annotation above icon
    cv2.rectangle(img, (10, 20), (22, 50), 0, 1)                          # the icon
    cv2.putText(img, "LABEL TEXT", (60, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
    info = sp.split_reference(img)
    assert info["ok"] and info["column_clusters"] >= 2
    x0, y0, x1, y1 = info["icon_core_box"]
    assert x0 <= 10 and x1 >= 23 and y0 <= 20 and y1 >= 51 and y0 > 14  # excludes "3D"
    assert info["label_cols"][0] >= 55


def test_dedupe_segments_ignores_endpoint_order():
    s = [(0, 0, 1, 0, 1.0), (1, 0, 0, 0, 1.0), (0, 0, 1, 0, 1.0), (0, 1, 1, 1, 1.0)]
    assert len(sp.dedupe_segments(s)) == 2


def test_components_join_touching_segments_only():
    segs = [(0, 0, 1, 0, 1.0), (1.1, 0, 2, 0, 0.9), (10, 10, 11, 10, 1.0)]
    comps = sp.components(segs, eps=0.3)
    assert sorted(c["n"] for c in comps) == [1, 2]


def test_signature_match_is_rotation_invariant_for_90_degrees():
    sig = {"n": 10, "length": 30.0, "dims": [4.0, 9.0]}
    rotated = {"n": 10, "length": 30.5, "dims": sorted((9.0, 4.0)), "bbox": (0, 0, 9, 4)}
    other = {"n": 10, "length": 30.0, "dims": [6.0, 9.0], "bbox": (0, 0, 9, 6)}
    assert sp.signature_matches(sig, [rotated, other]) == [rotated]


def test_nms_keeps_best_of_overlapping_candidates():
    c = [{"cx": 0, "cy": 0, "score": 0.7}, {"cx": 2, "cy": 0, "score": 0.9}, {"cx": 50, "cy": 0, "score": 0.6}]
    kept = sp.nms(c, radius=5)
    assert [k["score"] for k in kept] == [0.9, 0.6]


def test_legend_entries_group_multiline_labels():
    lines = [
        L.TextLine("DUPLEX OUTLET", (100, 10, 170, 19), 9.6, "Arial", (1.0, 0.0)),
        L.TextLine("240 VOLTS", (100, 20, 150, 29), 9.6, "Arial", (1.0, 0.0)),
        L.TextLine("EXHAUST FAN", (100, 50, 160, 59), 9.6, "Arial", (1.0, 0.0)),
    ]
    texts = [e["text"] for e in sp.legend_entries(lines)]
    assert texts == ["DUPLEX OUTLET 240 VOLTS", "EXHAUST FAN"]


@needs_pdf
def test_every_catalogued_legend_label_found_once_per_sheet():
    doc = pymupdf.open(PDF)
    for page in doc:
        entries = sp.legend_entries(L.text_lines(page))
        for fname, label in sp.LEGEND_LABELS.items():
            assert len(sp.find_label(entries, label)) == 1, (page.number + 1, fname)


@needs_pdf
def test_watermark_blanking_is_in_memory_only():
    before = hashlib.sha256(PDF.read_bytes()).hexdigest()
    doc = pymupdf.open(stream=PDF.read_bytes(), filetype="pdf")
    assert sp.blank_watermark(doc) >= 1
    for page in doc:
        assert not [l for l in L.find_watermarks(L.text_lines(page)) if "ESCONDIDO" in l.text]
    assert hashlib.sha256(PDF.read_bytes()).hexdigest() == before


def test_catalog_covers_every_reference_crop():
    import csv
    rows = list(csv.DictReader(open(ROOT / "electrical_takeoff_symbols" / "symbol_catalog.csv", encoding="utf-8")))
    assert {r["filename"] for r in rows} == set(sp.LEGEND_LABELS)
