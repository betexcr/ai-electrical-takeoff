"""AC3 (candidate actions), AC6 (gates, separate progress), AC12 (reconciliation), AC13 (persistence)."""

from __future__ import annotations

import pytest

from gt_helpers import ROOM, finish_blind, finish_pass1, import_candidates, npt, workflow
from takeoff.groundtruth.review import BlindScanInProgress, GateError, PhaseError, ReviewError
from takeoff.groundtruth.store import Store


@pytest.fixture
def wf(tmp_path):
    w = workflow(tmp_path / "gt.sqlite")
    yield w
    w.store.close()


def make(wf, x, y, category="switch", subtype="standard", **kw):
    return wf.create_annotation("S1", "rev", category, subtype, npt(x, y), **kw)


# --- AC3: candidate actions ---------------------------------------------------------------------

def test_accept_creates_annotation_with_candidate_provenance(wf):
    (c,) = import_candidates(wf, "S1", [(150.0, 150.0)])
    (cl,) = wf.clusters("S1")
    a = wf.accept_cluster("S1", cl["id"], "rev")
    assert (a.category, a.subtype, a.status) == ("detector", "smoke_detector", "as_drawn")
    assert a.provenance.origin == "candidate" and a.provenance.candidate_ids == [c.id]
    assert a.provenance.generator == "raster_plain" and a.review_state == "draft"
    review = wf.store.get("candidate_reviews", c.id)
    assert (review.outcome, review.annotation_id) == ("accepted", a.id)
    assert wf.store.get("candidates", c.id) == c  # imported record unchanged
    assert [e.action for e in wf.store.events("candidate_review", c.id)] == ["import", "accepted"]


def test_reclassify_or_move_marks_corrected(wf):
    import_candidates(wf, "S1", [(150.0, 150.0), (350.0, 350.0)])
    cl1, cl2 = wf.clusters("S1")
    a = wf.accept_cluster("S1", cl1["id"], "rev", category="detector", subtype="combined_smoke_co")
    b = wf.accept_cluster("S1", cl2["id"], "rev", point=npt(351.0, 352.0))
    for ann in (a, b):
        cid = ann.provenance.candidate_ids[0]
        assert wf.store.get("candidate_reviews", cid).outcome == "corrected"
    assert b.point_pt == pytest.approx((351.0, 352.0))


def test_cluster_merges_generators_and_resolves_all_members(wf):
    import_candidates(wf, "S1", [(150.0, 150.0)])
    import_candidates(wf, "S1", [(151.0, 150.0)], generator="vector_signature")
    (cl,) = wf.clusters("S1")
    assert len(cl["members"]) == 2
    a = wf.accept_cluster("S1", cl["id"], "rev")
    assert len(a.provenance.candidate_ids) == 2
    with pytest.raises(ReviewError, match="already reviewed"):
        wf.accept_cluster("S1", cl["id"], "rev")


def test_reject_requires_reason_and_creates_nothing(wf):
    (c,) = import_candidates(wf, "S1", [(150.0, 150.0)])
    (cl,) = wf.clusters("S1")
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.reject_cluster("S1", cl["id"], "rev", "")
    wf.reject_cluster("S1", cl["id"], "rev", "dashed wire arc")
    assert wf.store.get("candidate_reviews", c.id).outcome == "rejected"
    assert wf.store.list("annotations", "S1") == []


def test_cell_cannot_be_marked_with_unreviewed_candidates(wf):
    import_candidates(wf, "S1", [(150.0, 150.0)])
    cell = "S1:r00c00"
    with pytest.raises(ReviewError, match="unreviewed"):
        wf.mark_cell("S1", cell, "rev")
    wf.reject_unreviewed_in_cell("S1", cell, "rev", "noise")
    assert wf.mark_cell("S1", cell, "rev").pass1_done


def test_manual_annotation_anywhere_without_candidates(wf):
    a = make(wf, 499.0, 399.0, "ceiling_fan", "optional_pre_wire")
    assert a.provenance.origin == "manual" and a.status == "optional_pre_wire"
    assert a.point_pt == pytest.approx((499.0, 399.0))


def test_verified_cannot_be_set_in_pass1(wf):
    with pytest.raises(ReviewError, match="verified only"):
        make(wf, 150, 150, review_state="verified")
    a = make(wf, 150, 150)
    with pytest.raises(ReviewError, match="use verify"):
        wf.update_annotation(a.id, "rev", review_state="verified")
    with pytest.raises(ReviewError, match="set by the workflow"):
        wf.update_annotation(a.id, "rev", blind_confirmation="confirmed")


# --- AC6: gates and separate progress --------------------------------------------------------------

def test_pass1_gate_lists_every_unmet_condition(wf):
    import_candidates(wf, "S1", [(150.0, 150.0)])
    with pytest.raises(GateError) as e:
        wf.start_blind_scan("S1", "rev")
    text = " ".join(e.value.reasons)
    assert "region not confirmed" in text and "12 cells not done" in text and "1 unreviewed" in text
    finish_pass1(wf, "S1")
    assert wf.start_blind_scan("S1", "rev").phase == "blind_scan"


def test_flagged_region_blocks_and_needs_reason(wf):
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.confirm_region("S1", "rev", False)
    wf.confirm_region("S1", "rev", False, "porch light outside the box")
    with pytest.raises(GateError, match="flagged: porch light"):
        wf.start_blind_scan("S1", "rev")


def test_pass1_and_blind_progress_are_separate(wf):
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    wf.mark_cell("S1", "S1:r00c00", "rev")
    p = wf.progress("S1")
    assert p == {"phase": "blind_scan", "cells_total": 12, "cells_blind_scan_done": 1, "observations": 0}
    cell = wf.store.get("cells", "S1:r00c00")
    assert cell.pass1_done and cell.blind_scan_done and cell.pass1_at != cell.blind_scan_at


# --- AC11 at the workflow level: nothing from pass 1 is reachable during the blind scan ------------------

def test_blind_scan_hides_annotations_candidates_and_warnings(wf):
    import_candidates(wf, "S1", [(150.0, 150.0)])
    make(wf, 200, 200)
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    for call in (lambda: wf.clusters("S1"), lambda: wf.warnings("S1"), lambda: wf.require_not_blind("S1"),
                 lambda: make(wf, 300, 300)):
        with pytest.raises(BlindScanInProgress):
            call()
    assert "tallies" not in wf.progress("S1") and "annotations" not in wf.progress("S1")
    o = wf.add_observation("S1", "rev", npt(250.0, 150.0), "switch")
    assert o.cell_id == "S1:r00c01" and o.reconciliation == "pending"


def test_observations_editable_only_during_blind_scan(wf):
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    o = wf.add_observation("S1", "rev", npt(150.0, 150.0), "switch")
    o = wf.update_observation(o.id, "rev", point=npt(350.0, 350.0), category="receptacle")
    assert o.cell_id == "S1:r02c02" and o.category == "receptacle"
    with pytest.raises(ReviewError, match="only"):
        wf.update_observation(o.id, "rev", reconciliation="matched")
    finish_blind(wf, "S1")
    wf.open_reconcile("S1", "rev")
    with pytest.raises(PhaseError):
        wf.add_observation("S1", "rev", npt(150.0, 150.0), "switch")
    with pytest.raises(PhaseError):
        wf.update_observation(o.id, "rev", note="late edit")


def test_reconcile_gate_requires_every_cell_blind_scanned(wf):
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    with pytest.raises(GateError, match="12 cells not blind-scanned"):
        wf.open_reconcile("S1", "rev")
    finish_blind(wf, "S1")
    s = wf.open_reconcile("S1", "rev")
    assert s.phase == "reconcile" and s.blind_scan_closed_at


# --- AC12: reconciliation without duplicates ---------------------------------------------------------------

@pytest.fixture
def reconciling(wf):
    a = make(wf, 150, 150)                     # will be matched
    b = make(wf, 350, 350, "light", "pendant")  # will be unseen in the blind scan
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    o_match = wf.add_observation("S1", "rev", npt(152.0, 151.0), "switch")
    o_new = wf.add_observation("S1", "rev", npt(250.0, 250.0), "receptacle", subtype="gfi")
    o_bad = wf.add_observation("S1", "rev", npt(450.0, 150.0), "switch")
    finish_blind(wf, "S1")
    wf.open_reconcile("S1", "rev")
    return wf, a, b, o_match, o_new, o_bad


def test_match_creates_no_record_and_links_both(reconciling):
    wf, a, b, o_match, *_ = reconciling
    before = len(wf.store.list("annotations", "S1", include_deleted=True))
    (sug,) = [s for s in wf.suggestions("S1") if s["observation_id"] == o_match.id]
    assert sug["suggested_annotation_id"] == a.id and sug["distance_pt"] <= 6 and not sug["conflicts"]
    m = wf.match(o_match.id, a.id, "rev")
    assert len(wf.store.list("annotations", "S1", include_deleted=True)) == before
    assert m.blind_confirmation == "confirmed"
    o = wf.store.get("observations", o_match.id)
    assert (o.reconciliation, o.annotation_id) == ("matched", a.id)


def test_annotation_cannot_be_matched_twice(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    wf.match(o_match.id, a.id, "rev")
    with pytest.raises(ReviewError, match="already matched"):
        wf.match(o_bad.id, a.id, "rev", reason="testing")


def test_far_match_requires_reason(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.match(o_bad.id, a.id, "rev")


def test_promote_creates_exactly_one_blind_scan_annotation(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    before = len(wf.store.list("annotations", "S1"))
    p = wf.promote(o_new.id, "rev")
    assert len(wf.store.list("annotations", "S1")) == before + 1
    assert p.provenance.origin == "blind_scan" and p.provenance.observation_id == o_new.id
    assert p.blind_confirmation == "added_from_blind_scan" and (p.category, p.subtype) == ("receptacle", "gfi")
    with pytest.raises(ReviewError, match="already promoted"):
        wf.promote(o_new.id, "rev")


def test_promote_refused_next_to_same_category_annotation(reconciling):
    wf, a, b, o_match, *_ = reconciling
    with pytest.raises(ReviewError, match="match the observation"):
        wf.promote(o_match.id, "rev", subtype="standard")


def test_dismiss_requires_reason(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.dismiss(o_bad.id, "rev", " ")
    assert wf.dismiss(o_bad.id, "rev", "mis-click").reconciliation == "dismissed"


def test_unseen_annotations_block_verification_until_decided(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    wf.match(o_match.id, a.id, "rev")
    assert {x.id for x in wf.unseen("S1")} == {b.id}
    with pytest.raises(ReviewError, match="reconcile every blind observation"):
        wf.decide_unseen(b.id, "rev", "keep", "re-checked")
    wf.promote(o_new.id, "rev")
    wf.dismiss(o_bad.id, "rev", "mis-click")
    with pytest.raises(GateError, match="not seen in the blind scan still undecided"):
        wf.complete_sheet("S1", "rev")
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.decide_unseen(b.id, "rev", "keep", "")
    kept = wf.decide_unseen(b.id, "rev", "keep", "pendant clearly drawn; missed in blind scan")
    assert kept.blind_confirmation == "not_seen_in_blind_scan" and wf.unseen("S1") == []


def test_unseen_delete_is_soft_and_explained(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    for o in (o_match, o_new, o_bad):
        wf.dismiss(o.id, "rev", "test")
    wf.decide_unseen(b.id, "rev", "delete", "not a device on second look")
    (d,) = [x for x in wf.store.list("annotations", "S1", include_deleted=True) if x.id == b.id]
    assert d.deleted and d.blind_confirmation == "not_seen_in_blind_scan"


def test_full_reconciliation_to_verified_and_reopen(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    wf.match(o_match.id, a.id, "rev")
    p = wf.promote(o_new.id, "rev")
    wf.dismiss(o_bad.id, "rev", "mis-click")
    wf.decide_unseen(b.id, "rev", "keep", "present")
    with pytest.raises(ReviewError, match="assign a room"):
        wf.verify(a.id, "rev")
    for ann in (a, b, p):
        wf.update_annotation(ann.id, "rev", room=ROOM)
        wf.verify(ann.id, "rev")
    s = wf.complete_sheet("S1", "rev")
    assert s.phase == "verified"
    with pytest.raises(PhaseError):
        wf.update_annotation(a.id, "rev", notes="late")
    with pytest.raises(ReviewError, match="requires a reason"):
        wf.reopen_sheet("S1", "rev", "")
    assert wf.reopen_sheet("S1", "rev", "found a typo").phase == "reconcile"
    edited = wf.update_annotation(a.id, "rev", notes="typo fixed")
    assert edited.review_state == "draft"  # substantive edit needs re-verification
    assert "reopen: found a typo" in [e.action for e in wf.store.events("sheet", "S1")]


def test_annotation_added_during_reconcile_is_marked_unseen(reconciling):
    wf, *_ = reconciling
    a = wf.create_annotation("S1", "rev", "switch", "dimmer", npt(480, 380))
    assert a.blind_confirmation == "not_seen_in_blind_scan" and a.blind_decision_reason


def test_complete_blocked_by_unacknowledged_warning(reconciling):
    wf, a, b, o_match, o_new, o_bad = reconciling
    wf.match(o_match.id, a.id, "rev")
    wf.dismiss(o_new.id, "rev", "x")
    wf.dismiss(o_bad.id, "rev", "x")
    wf.decide_unseen(b.id, "rev", "keep", "present")
    c = wf.create_annotation("S1", "rev", "light", "recessed_downlight", npt(351, 350), room=ROOM)  # 1 pt from b
    for ann in (a, b, c):
        if ann.room is None:
            wf.update_annotation(ann.id, "rev", room=ROOM)
        wf.verify(ann.id, "rev")
    with pytest.raises(GateError, match="1 warnings"):
        wf.complete_sheet("S1", "rev")
    (w,) = wf.warnings("S1")
    assert w["type"] == "possible_duplicate"  # pendant and recessed are both category 'light'
    wf.acknowledge_warning("S1", w["id"], "rev", "two distinct fixtures drawn close together")
    assert wf.complete_sheet("S1", "rev").phase == "verified"


# --- AC13: persistence across restart ------------------------------------------------------------------------

def test_blind_progress_and_observations_survive_restart(tmp_path):
    path = tmp_path / "gt.sqlite"
    wf = workflow(path)
    finish_pass1(wf, "S1")
    wf.start_blind_scan("S1", "rev")
    wf.mark_cell("S1", "S1:r01c01", "rev")
    o = wf.add_observation("S1", "rev", npt(220.0, 220.0), "light", note="faint circle")
    wf.store.close()
    wf2 = workflow(path)  # restart: re-initialising must not duplicate cells or reset phase
    assert wf2.phase("S1") == "blind_scan"
    assert len(wf2.store.list("cells", "S1")) == 12
    assert [c.id for c in wf2.store.list("cells", "S1") if c.blind_scan_done] == ["S1:r01c01"]
    assert wf2.observations("S1") == [o]
    wf2.store.close()


def test_every_workflow_action_is_in_the_event_log(reconciling):
    wf, a, b, o_match, *_ = reconciling
    wf.match(o_match.id, a.id, "rev")
    actions = [e.action for e in wf.store.events()]
    for expected in ("confirm_region", "pass1_done", "start_blind_scan", "blind_scan_done", "open_reconcile",
                     "matched", "blind_matched"):
        assert expected in actions
