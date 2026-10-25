"""Nearest-label room proposal: the rule, and its calibration provenance against a verified export."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from takeoff.app import rooms as R
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.export import ExportError

from gt_config import gt_export

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "tools"))


def label(name, x0, y0=0.0, w=40.0, h=10.0):
    return {"kind": "labeled", "name": name, "label_bbox_pt": [x0, y0, x0 + w, y0 + h], "bbox": None}


PARAMS = {"ambiguity_ratio": 1.25, "max_label_distance_pt": 100.0}


def test_inside_label_box_is_distance_zero():
    assert R.label_distance_pt((10, 5), [0, 0, 40, 10]) == 0.0
    assert R.label_distance_pt((0, 0), [0, 0, 40, 10]) == 0.0
    assert R.label_distance_pt((43, 14), [0, 0, 40, 10]) == pytest.approx(5.0)
    assert R.label_distance_pt((20, -7), [0, 0, 40, 10]) == pytest.approx(7.0)


def test_clear_nearest_label_is_not_ambiguous():
    rooms = [label("A", 120), label("B", 160), S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
    p = R.propose_room((100, 5), rooms, PARAMS)  # 20 pt from A, 60 pt from B
    assert p.room["name"] == "A" and not p.ambiguous and p.reason is None
    assert p.nearest[0] == ("A", pytest.approx(20.0)) and p.nearest[1] == ("B", pytest.approx(60.0))


def test_two_close_labels_are_ambiguous():
    rooms = [label("A", 120), label("B", 124)]
    p = R.propose_room((100, 5), rooms, PARAMS)  # 20 pt vs 24 pt: 24 <= 1.25 * 20
    assert p.room["name"] == "A" and p.ambiguous and p.reason == "two_labels_close"
    edge = R.propose_room((100, 5), [label("A", 120), label("B", 125)], PARAMS)  # 25 == 1.25 * 20
    assert edge.ambiguous and edge.reason == "two_labels_close"
    clear = R.propose_room((100, 5), [label("A", 120), label("B", 126)], PARAMS)
    assert not clear.ambiguous


def test_far_from_every_label_proposes_unlabeled():
    p = R.propose_room((0, 5), [label("A", 400), label("B", 500)], PARAMS)
    assert p.room["kind"] == "unlabeled" and p.ambiguous and p.reason == "far_from_labels"
    none = R.propose_room((0, 5), [S.ROOM_EXTERIOR, S.ROOM_UNLABELED], PARAMS)
    assert none.room["kind"] == "unlabeled" and none.ambiguous and none.nearest == []
    assert R.propose_room((0, 5), [], PARAMS).room["kind"] == "unlabeled"


def test_two_cl_labels_are_distinct_rooms():
    rooms = [label("CL", 120, 0), label("CL", 120, 200)]
    p = R.propose_room((100, 205), rooms, PARAMS)
    assert p.room["label_bbox_pt"] == [120, 200, 160, 210] and not p.ambiguous
    q = R.propose_room((100, 5), rooms, PARAMS)
    assert q.room["label_bbox_pt"] == [120, 0, 160, 10]


def test_exterior_is_never_proposed():
    rooms = [S.ROOM_EXTERIOR, label("A", 120), S.ROOM_UNLABELED]
    for pt in [(-500, -500), (100, 5), (121, 5), (5000, 5000)]:
        assert R.propose_room(pt, rooms, PARAMS).room["kind"] in ("labeled", "unlabeled")
    # a point right on top of nothing but Exterior still is not proposed Exterior
    assert R.propose_room((0, 0), [S.ROOM_EXTERIOR], PARAMS).room["kind"] == "unlabeled"


def test_default_params_have_calibration_provenance_shape():
    cal = R.ROOM_PARAMS["calibrated_on"]
    assert R.ROOM_PARAMS["ambiguity_ratio"] == 1.25
    assert cal["sheets"] == ["A1-111", "A2-111"] and cal["percentile"] == 95 and cal["date"] == "2026-10-25"
    assert R.ROOM_PARAMS["max_label_distance_pt"] % 10 == 0


def test_calibration_provenance_matches_export():
    export = gt_export()
    manifest_bytes = (export / "manifest.json").read_bytes()
    cal = R.ROOM_PARAMS["calibrated_on"]
    assert cal["export_id"] == export.name
    assert cal["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    # n and the distance bound are recomputable from the export's A1/A2 annotations alone
    pdf = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
    if not pdf.exists():
        pytest.skip("supplied plan PDF not present")
    import eval_rooms as ER  # tools/eval_rooms.py
    n, value, _ = ER.calibrate(export, pdf)
    assert (cal["n"], R.ROOM_PARAMS["max_label_distance_pt"]) == (n, value)


def test_eval_rooms_refuses_unverified_export(tmp_path, capsys):
    import eval_rooms as ER
    bad = tmp_path / "gt_bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"level": "incomplete", "files": {}}), encoding="utf-8")
    assert ER.main(["--export", str(bad)]) == 2
    assert "incomplete" in capsys.readouterr().err
    assert ER.main(["--export", str(tmp_path / "missing")]) == 2


def test_gt_export_explicit_invalid_path_raises(monkeypatch, tmp_path):
    import gt_config
    monkeypatch.setenv("TAKEOFF_GT_EXPORT", str(tmp_path / "nope"))
    with pytest.raises(ExportError):
        gt_config.gt_export()
    monkeypatch.delenv("TAKEOFF_GT_EXPORT")
    monkeypatch.setattr(gt_config, "DEFAULT_EXPORT", tmp_path / "also_missing")
    with pytest.raises(pytest.skip.Exception):
        gt_config.gt_export()
