"""AC8 (immutable exports, derived totals, verified gate), AC13 manifest fields, evaluator guard."""

from __future__ import annotations

import csv
import hashlib
import json

import pytest

from gt_helpers import ROOM, finish_blind, finish_pass1, import_candidates, npt, workflow
from takeoff.groundtruth import export as E
from takeoff.groundtruth import models as M

PDF_SHA = "a" * 64


@pytest.fixture
def wf(tmp_path):
    w = workflow(tmp_path / "gt.sqlite", sheet_ids=("S1", "S2"))
    yield w
    w.store.close()


def verify_sheet(wf, sid):
    """Drive one sheet through all phases with two devices, one found only in the blind scan."""
    import_candidates(wf, sid, [(150.0, 150.0), (450.0, 350.0)])
    c1, c2 = wf.clusters(sid)
    a = wf.accept_cluster(sid, c1["id"], "rev", room=ROOM)
    wf.reject_cluster(sid, c2["id"], "rev", "noise")
    fan = wf.create_annotation(sid, "rev", "ceiling_fan", "optional_pre_wire", npt(300, 250), room=ROOM)
    finish_pass1(wf, sid)
    wf.start_blind_scan(sid, "rev")
    o1 = wf.add_observation(sid, "rev", npt(151, 150), "detector")
    o2 = wf.add_observation(sid, "rev", npt(300.5, 250), "ceiling_fan")
    o3 = wf.add_observation(sid, "rev", npt(220, 380), "switch", subtype="dimmer")
    finish_blind(wf, sid)
    wf.open_reconcile(sid, "rev")
    wf.match(o1.id, a.id, "rev")
    wf.match(o2.id, fan.id, "rev")
    p = wf.promote(o3.id, "rev", room=ROOM)
    for ann in (a, fan, p):
        wf.verify(ann.id, "rev")
    wf.complete_sheet(sid, "rev")


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_incomplete_export_is_written_and_refused_by_the_guard(wf, tmp_path):
    wf.create_annotation("S1", "rev", "switch", "standard", npt(150, 150))
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["level"] == "incomplete" and m["sheets"]["S1"]["phase"] == "pass1"
    assert any("not confirmed" in r for r in m["sheets"]["S1"]["open_gate_reasons"]["pass1_to_blind_scan"])
    with pytest.raises(E.ExportError, match="'incomplete'"):
        E.require_verified(d)


def test_every_export_is_a_new_directory_and_old_ones_are_untouched(wf, tmp_path):
    root = tmp_path / "exports"
    d1 = E.export(wf, root, "rev", PDF_SHA)
    before = {p.name: p.read_bytes() for p in d1.iterdir()}
    wf.create_annotation("S1", "rev", "switch", "standard", npt(150, 150))
    d2 = E.export(wf, root, "rev", PDF_SHA)
    assert d1 != d2 and d1.name.startswith("gt_001_") and d2.name.startswith("gt_002_")
    assert {p.name: p.read_bytes() for p in d1.iterdir()} == before


def test_manifest_hashes_match_files(wf, tmp_path):
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    for name, meta in m["files"].items():
        assert hashlib.sha256((d / name).read_bytes()).hexdigest() == meta["sha256"]
    for key in ("schema_version", "taxonomy_version", "protocol_version", "input_pdf_sha256", "known_limitations"):
        assert m[key]


def test_verified_export_totals_and_separate_blind_tracking(wf, tmp_path):
    verify_sheet(wf, "S1")
    verify_sheet(wf, "S2")
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    manifest = E.require_verified(d)
    assert manifest["level"] == "verified"
    for sid in ("S1", "S2"):
        s = manifest["sheets"][sid]
        assert s["phase"] == "verified" and s["blind_scan_closed_at"] and s["reconcile_completed_at"]
        assert s["blind_scan_consistency"] == {"observations": 3, "matched": 2, "promoted": 1, "dismissed": 0,
                                               "pending": 0, "annotations_not_seen_in_blind_scan": 0,
                                               "note": "review-process evidence, not detector accuracy"}
    anns = [M.Annotation.model_validate(r) for r in read_jsonl(d / "annotations.jsonl")]
    totals = json.loads((d / "totals.json").read_text(encoding="utf-8"))["totals"]
    assert totals == E.derive_totals(anns)  # derived, never typed
    assert totals["S1"]["by_category"] == {"ceiling_fan": {"optional_pre_wire": 1}, "detector": {"smoke_detector": 1},
                                           "switch": {"dimmer": 1}}
    assert totals["S1"]["total"] == 3
    rows = list(csv.DictReader((d / "annotations.csv").open(encoding="utf-8")))
    assert len(rows) == len(anns) == 6 and {r["review_state"] for r in rows} == {"verified"}
    assert {r["subtype_label"] for r in rows if r["subtype"] == "dimmer"} == {"ELECTRICAL SWITCH-DIMMER"}
    reviews = read_jsonl(d / "candidate_reviews.jsonl")
    assert sorted(r["outcome"] for r in reviews) == ["accepted", "accepted", "rejected", "rejected"]


def test_one_unverified_sheet_keeps_the_export_incomplete(wf, tmp_path):
    verify_sheet(wf, "S1")
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["level"] == "incomplete" and m["sheets"]["S1"]["verified"] and not m["sheets"]["S2"]["verified"]


def test_deleted_annotations_are_excluded(wf, tmp_path):
    a = wf.create_annotation("S1", "rev", "switch", "standard", npt(150, 150))
    wf.create_annotation("S1", "rev", "switch", "dimmer", npt(250, 150))
    wf.delete_annotation(a.id, "rev", "duplicate")
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    ids = [r["id"] for r in read_jsonl(d / "annotations.jsonl")]
    assert a.id not in ids and len(ids) == 1


def test_tampered_export_is_refused(wf, tmp_path):
    verify_sheet(wf, "S1")
    verify_sheet(wf, "S2")
    d = E.export(wf, tmp_path / "exports", "rev", PDF_SHA)
    with open(d / "totals.json", "a", encoding="utf-8") as f:
        f.write(" ")
    with pytest.raises(E.ExportError, match="does not match"):
        E.require_verified(d)


def test_room_keys_disambiguate_duplicate_labels():
    def a(bb):
        return M.Annotation(sheet_id="S1", page_number=1, category="light", subtype="pendant", point={"u": 0.1, "v": 0.1},
                            provenance={"origin": "manual"}, created_by="r",
                            room={"kind": "labeled", "name": "CL", "label_bbox_pt": bb})
    x, y = a((10, 10, 20, 15)), a((100, 10, 110, 15))
    t = E.derive_totals([x, y])["S1"]["by_room"]
    assert set(t) == {"CL @(10,10)", "CL @(100,10)"}
