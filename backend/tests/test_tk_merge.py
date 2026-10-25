"""takeoff-merge/1: det/0 and AI detections joined to the living device set. Detectors propose, reviewers decide."""

from __future__ import annotations

import copy
import random

import pytest

from takeoff.app import models as TM
from takeoff.app.merge import MergeResult, merge_sheet
from takeoff.app.rooms import RoomProposal

DOC = "a" * 64
SHEET = "A1-111"
NOW = "2026-10-25T12:00:00.000+00:00"
ROOM = {"kind": "labeled", "name": "Kitchen", "label_bbox_pt": [10.0, 10.0, 50.0, 20.0], "bbox": None}
RUN = TM.Run(id="run-002", document_id=DOC, detector="det/0", detector_version="1", created_by="tester")


def propose(point_pt):
    return RoomProposal(room=ROOM, ambiguous=False, reason=None, nearest=[("Kitchen", 1.0)])


def norm(pt):
    return {"u": min(max(pt[0] / 1000.0, 0.0), 1.0), "v": min(max(pt[1] / 1000.0, 0.0), 1.0)}


def dev(id, pt=(100.0, 200.0), category="switch", subtype="standard", state="proposed", origin="detector", **kw):
    base = dict(id=id, document_id=DOC, sheet_id=SHEET, category=category, subtype=subtype, status="as_drawn",
                point=norm(pt), point_pt=pt, review_state=state, origin=origin, created_by_run="run-001",
                created_at="2026-10-01T00:00:00.000+00:00", updated_at="2026-10-01T00:00:00.000+00:00")
    base.update(kw)
    return TM.Device(**base)


def det(id, pt=(100.0, 200.0), category="switch", subtype="standard", source="det0", **kw):
    base = dict(id=id, run_id=RUN.id, document_id=DOC, sheet_id=SHEET, source=source, category=category,
                subtype=subtype, status="as_drawn", point=norm(pt), point_pt=pt)
    base.update(kw)
    return TM.RunDetection(**base)


def ai(id, pt=(100.0, 200.0), category="switch", subtype="standard", **kw):
    base = dict(source="ai_discover", rationale=f"why {id}", verdict_id=f"v-{id}",
                supporting=[TM.SupportingObservation(tile_id=f"tile-{id}", verdict_id=f"v-{id}", point_pt=pt,
                                                     centrality_pt=50.0)])
    base.update(kw)
    return det(id, pt, category, subtype, **base)


def run_merge(detections, devices=(), verdicts=None, **kw):
    """Merge, asserting that no input was mutated."""
    detections, devices = list(detections), list(devices)
    before = (copy.deepcopy(RUN), copy.deepcopy(detections), copy.deepcopy(devices), copy.deepcopy(verdicts))
    res = merge_sheet(RUN, detections, devices, propose, verdicts, now=NOW, **kw)
    assert isinstance(res, MergeResult)
    assert (RUN, detections, devices, verdicts) == before
    return res


def outcomes(res):
    return {(e.detection_id, e.device_id): e.outcome for e in res.evidence}


def flags_of(res, type=None):
    return [f for f in res.flags if type is None or f.type == type]


def verify_verdict(id="vv-1", detection_ids=("d1",), items=None, agreement=None, error=None):
    return TM.ModelVerdict(
        id=id, run_id=RUN.id, sheet_id=SHEET, kind="verify", implementation="fake", model="m",
        prompt_version="ai-verify-prompt/1", ai_version="ai-verify/1", detection_ids=list(detection_ids),
        output=None if error else {"verdicts": items or []}, agreement=agreement, error=error)


def item(item_id, is_device=True, category="switch", subtype="standard", uncertain=False, rationale="because"):
    return dict(item_id=item_id, is_device=is_device, category=category, subtype=subtype,
                rationale=rationale, uncertain=uncertain)


def test_first_run_creates_proposals_with_created_evidence():
    d_fan = det("d2", pt=(300.0, 50.0), category="ceiling_fan", subtype="fan", status="optional_pre_wire")
    d_sw = det("d1", pt=(100.0, 200.0))
    res = run_merge([d_sw, d_fan])
    assert [d.id for d in res.new_devices] == ["run-002:d1", "run-002:d2"]
    sw, fan = res.new_devices
    assert (sw.category, sw.subtype, sw.status, sw.point_pt, sw.point) == ("switch", "standard", "as_drawn", (100.0, 200.0), d_sw.point)
    assert (fan.category, fan.subtype, fan.status) == ("ceiling_fan", "fan", "optional_pre_wire")
    for d in res.new_devices:
        assert (d.review_state, d.origin, d.created_by_run, d.human_set) == ("proposed", "detector", "run-002", [])
        assert (d.room_source, d.room.name, d.room.kind, d.room.label_bbox_pt) == ("proposed_nearest_label", "Kitchen", "labeled", (10.0, 10.0, 50.0, 20.0))
        assert (d.document_id, d.sheet_id, d.created_at, d.updated_at, d.deleted, d.accept_kind) == (DOC, SHEET, NOW, NOW, False, None)
    assert [(e.id, e.device_id, e.detection_id, e.run_id, e.outcome) for e in res.evidence] == [
        ("run-002:d1:ev:run-002:d1", "run-002:d1", "d1", "run-002", "created"),
        ("run-002:d2:ev:run-002:d2", "run-002:d2", "d2", "run-002", "created")]
    assert res.flags == []


def test_new_device_status_comes_from_the_detection_and_ceiling_fan_defaults():
    res = run_merge([det("d1", status=""), det("d2", pt=(300.0, 50.0), category="ceiling_fan", subtype="fan", status="")])
    assert {d.category: d.status for d in res.new_devices} == {"switch": "as_drawn", "ceiling_fan": "optional_pre_wire"}


def test_new_device_does_not_alias_detection_point():
    d = det("d1")
    res = run_merge([d])
    assert res.new_devices[0].point == d.point and res.new_devices[0].point is not d.point


def test_same_category_same_subtype_confirms():
    existing = dev("x1", pt=(103.0, 204.0))  # 5.0 pt away
    res = run_merge([det("d1")], [existing])
    assert res.new_devices == []
    assert [(e.device_id, e.detection_id, e.outcome, e.distance_pt) for e in res.evidence] == [("x1", "d1", "confirmed", 5.0)]
    assert res.flags == []


def test_same_category_other_subtype_flags_conflict_and_keeps_record():
    existing = dev("x1", subtype="standard")
    res = run_merge([det("d1", subtype="dimmer")], [existing])
    assert res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "conflict"}
    [f] = res.flags
    assert (f.type, f.severity, f.device_id, f.open, f.run_id) == ("classification_conflict", "review", "x1", True, "run-002")
    assert f.id == "run-002:d1:classification_conflict"
    assert f.details == {"run_category": "switch", "run_subtype": "dimmer", "detection_id": "d1"}
    assert (f.document_id, f.sheet_id, f.created_at) == (DOC, SHEET, NOW)
    assert existing.subtype == "standard" and existing.revision == 1  # unchanged (inputs are also deep-compared)


def test_other_category_flags_conflict_without_new_device():
    existing = dev("x1", category="switch")
    res = run_merge([det("d1", category="receptacle", subtype="duplex")], [existing])
    assert res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "conflict"}
    [f] = res.flags
    assert (f.type, f.severity, f.device_id) == ("classification_conflict", "review", "x1")
    assert f.details == {"run_category": "receptacle", "run_subtype": "duplex", "detection_id": "d1", "kind": "category"}


def test_two_possible_devices_create_flagged_proposal():
    a, b = dev("xa", pt=(100.0, 200.0)), dev("xb", pt=(104.0, 200.0), category="receptacle", subtype="duplex")
    res = run_merge([det("d1", pt=(102.0, 200.0))], [b, a])
    [new] = res.new_devices
    assert (new.id, new.review_state, new.origin) == ("run-002:d1", "proposed", "detector")
    assert outcomes(res)[("d1", "run-002:d1")] == "created"
    amb = flags_of(res, "ambiguous_match")
    assert [(f.device_id, f.severity, f.details) for f in amb] == [
        ("run-002:d1", "review", {"possible_device_ids": ["xa", "xb"]})]
    # neither candidate was matched by the detection, so both are reported as not re-detected
    assert sorted(f.device_id for f in flags_of(res, "not_redetected")) == ["xa", "xb"]


def test_second_claim_on_same_device_becomes_ambiguous_proposal():
    existing = dev("x1", pt=(100.0, 200.0))
    far, near = det("d1", pt=(105.0, 200.0)), det("d2", pt=(101.0, 200.0))  # d2 is nearer, so it claims
    res = run_merge([far, near], [existing])
    assert outcomes(res) == {("d2", "x1"): "confirmed", ("d1", "run-002:d1"): "created"}
    assert [d.id for d in res.new_devices] == ["run-002:d1"]
    [f] = res.flags
    assert (f.type, f.severity, f.device_id, f.details) == ("ambiguous_match", "review", "run-002:d1", {"possible_device_ids": ["x1"]})


def test_nearest_claimant_wins_even_when_it_sorts_later():
    existing = dev("x1", pt=(100.0, 200.0))
    sorts_first = det("d1", pt=(100.0, 194.0))  # y=194 sorts first, but is 6.0 away
    nearer = det("d2", pt=(100.0, 201.0))       # y=201 sorts later, 1.0 away: it must win
    for order in ([sorts_first, nearer], [nearer, sorts_first]):
        res = run_merge(order, [existing])
        assert outcomes(res) == {("d2", "x1"): "confirmed", ("d1", "run-002:d1"): "created"}
        assert [(f.type, f.device_id, f.details) for f in res.flags] == [
            ("ambiguous_match", "run-002:d1", {"possible_device_ids": ["x1"]})]
    # equal distances fall back to the sort order (y, x, id), deterministically
    tie = run_merge([det("d3", pt=(100.0, 203.0)), det("d4", pt=(100.0, 197.0))], [existing])
    assert outcomes(tie) == {("d4", "x1"): "confirmed", ("d3", "run-002:d3"): "created"}


def test_rejected_device_redetected_stays_rejected_with_evidence():
    existing = dev("x1", state="rejected")
    res = run_merge([det("d1")], [existing])
    assert res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "rejected_redetected"}
    assert res.flags == []  # a rejected device is touched, and never flagged not_redetected


def test_not_redetected_severity_by_origin():
    devices = [dev("det", pt=(10.0, 10.0), origin="detector"), dev("man", pt=(20.0, 20.0), origin="manual", created_by_run=None),
               dev("aid", pt=(30.0, 30.0), origin="ai"), dev("rej", pt=(40.0, 40.0), state="rejected"),
               dev("acc", pt=(50.0, 50.0), state="accepted")]
    res = run_merge([], devices)
    assert res.new_devices == [] and res.evidence == []
    assert {(f.device_id, f.type, f.severity) for f in res.flags} == {
        ("det", "not_redetected", "review"), ("man", "not_redetected", "info"),
        ("aid", "not_redetected", "info"), ("acc", "not_redetected", "review")}
    assert {f.id for f in res.flags} == {f"run-002:{d}:not_redetected" for d in ("det", "man", "aid", "acc")}
    assert all(f.details == {} for f in res.flags)


def test_confirmed_device_is_not_flagged_not_redetected():
    res = run_merge([det("d1")], [dev("x1")])
    assert flags_of(res, "not_redetected") == []


def test_device_seen_only_by_other_category_det0_is_not_flagged_not_redetected():
    res = run_merge([det("d1", category="receptacle", subtype="duplex")], [dev("x1")])
    assert [f.type for f in res.flags] == ["classification_conflict"]


def test_device_seen_only_by_subtype_conflict_is_not_flagged_not_redetected():
    res = run_merge([det("d1", subtype="dimmer")], [dev("x1")])
    assert [f.type for f in res.flags] == ["classification_conflict"]


def test_rejected_unseen_device_gets_no_flag():
    res = run_merge([], [dev("x1", state="rejected"), dev("x2", pt=(300.0, 300.0), state="rejected", origin="manual")])
    assert res.flags == [] and res.evidence == [] and res.new_devices == []


def test_human_set_fields_never_change():
    reviewer_fixed = dev("x1", category="receptacle", subtype="gfi", state="accepted", accept_kind="corrected_and_accepted",
                         human_set=["category", "subtype", "room", "review_state"], notes="n", revision=4,
                         room={"kind": "labeled", "name": "Bath", "label_bbox_pt": (1, 2, 3, 4)}, room_source="reviewer")
    res = run_merge([det("d1", category="switch", subtype="standard"), det("d2", pt=(100.0, 203.0), category="receptacle", subtype="duplex")],
                    [reviewer_fixed])
    assert res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "conflict", ("d2", "x1"): "conflict"}
    assert {(f.type, f.device_id) for f in res.flags} == {("classification_conflict", "x1")}
    assert len(res.flags) == 2
    assert reviewer_fixed.category == "receptacle" and reviewer_fixed.human_set == ["category", "subtype", "room", "review_state"]


def test_deleted_devices_are_ignored():
    gone = dev("x1", deleted=True)
    res = run_merge([det("d1")], [gone])
    assert [d.id for d in res.new_devices] == ["run-002:d1"]
    assert outcomes(res) == {("d1", "run-002:d1"): "created"}
    assert res.flags == []  # neither a match, an ambiguity nor not_redetected


def test_mixed_sheets_raise_and_empty_lists_are_fine():
    other = dev("x1", sheet_id="A2-111")
    with pytest.raises(ValueError, match="one sheet"):
        merge_sheet(RUN, [det("d1")], [other], propose, now=NOW)  # detection vs device
    with pytest.raises(ValueError, match="one sheet"):
        merge_sheet(RUN, [det("d1"), det("d2", sheet_id="A2-111")], [], propose, now=NOW)  # two detections
    with pytest.raises(ValueError, match="one sheet"):
        merge_sheet(RUN, [], [dev("x1"), other], propose, now=NOW)  # two devices
    with pytest.raises(ValueError, match="one sheet"):
        merge_sheet(RUN, [], [dev("x1"), dev("x2", sheet_id="A2-111", deleted=True)], propose, now=NOW)
    assert run_merge([], []) == MergeResult()
    assert run_merge([det("d1")], []).new_devices[0].sheet_id == SHEET
    assert [f.type for f in run_merge([], [dev("x1")]).flags] == ["not_redetected"]


def test_device_without_point_pt_raises_unless_deleted():
    with pytest.raises(ValueError, match="point_pt"):
        merge_sheet(RUN, [det("d1")], [dev("x1", point_pt=None)], propose, now=NOW)
    res = run_merge([det("d1")], [dev("x1", point_pt=None, deleted=True)])  # deleted devices are ignored
    assert outcomes(res) == {("d1", "run-002:d1"): "created"}


def test_diagonal_exact_tolerance_survives_float_rounding():
    # (3.6, 4.8) is exactly 6 away on paper; in floats hypot gives 6.000000000000005
    assert ((103.6 - 100.0) ** 2 + (204.8 - 200.0) ** 2) ** 0.5 > 6.0
    res = run_merge([det("d1", pt=(100.0, 200.0))], [dev("x1", pt=(103.6, 204.8))])
    assert outcomes(res) == {("d1", "x1"): "confirmed"}
    res = run_merge([det("d1", pt=(100.0, 200.0))], [dev("x1", pt=(103.6, 204.81))])
    assert outcomes(res) == {("d1", "run-002:d1"): "created"}


def test_boundary_6pt_inclusive_and_order_independent():
    inside = dev("x1", pt=(106.0, 200.0))   # exactly 6.0 away: a match
    outside = dev("x2", pt=(300.0, 206.01))  # 6.01 away: not a match
    dets = [det("d1", pt=(100.0, 200.0)), det("d2", pt=(300.0, 200.0))]
    res = run_merge(dets, [inside, outside])
    assert outcomes(res) == {("d1", "x1"): "confirmed", ("d2", "run-002:d2"): "created"}
    assert [e.distance_pt for e in res.evidence if e.device_id == "x1"] == [6.0]
    assert flags_of(res, "ambiguous_match") == []
    # a custom tolerance moves the boundary
    res2 = run_merge(dets, [inside, outside], tol_pt=5.0)
    assert outcomes(res2) == {("d1", "run-002:d1"): "created", ("d2", "run-002:d2"): "created"}
    # shuffled inputs give an equal result
    rng = random.Random(7)
    for _ in range(10):
        d2, v2 = dets[:], [inside, outside]
        rng.shuffle(d2), rng.shuffle(v2)
        assert run_merge(d2, v2) == res


def test_shuffled_inputs_give_equal_results_for_a_complex_case():
    devices = [dev("x1", pt=(100.0, 200.0)), dev("x2", pt=(103.0, 200.0), category="receptacle", subtype="duplex"),
               dev("x3", pt=(400.0, 400.0), origin="manual"), dev("x4", pt=(600.0, 600.0), state="rejected")]
    dets = [det("d1", pt=(101.0, 200.0)), det("d2", pt=(400.0, 402.0), subtype="dimmer"), det("d3", pt=(601.0, 600.0)),
            det("d4", pt=(700.0, 700.0)), det("d5", pt=(405.0, 400.0)),
            ai("a1", pt=(900.0, 100.0), category="smoke_detector", subtype=None), ai("a2", pt=(902.0, 100.0), category="switch"),
            ai("a3", pt=(100.0, 201.0), category="switch")]
    verdicts = [verify_verdict("vv-1", ["d2", "d4"], [item("d2", uncertain=True), item("d4", category="receptacle")],
                               agreement={"d2": TM.Agreement(category=True, subtype=False), "d4": TM.Agreement(category=False)}),
                verify_verdict("vv-2", ["d5"], error={"code": "timeout"})]
    base = run_merge(dets, devices, verdicts)
    assert base.new_devices and base.flags and base.evidence
    for seed in range(15):
        rng = random.Random(seed)
        d2, dv2, vd2 = dets[:], devices[:], verdicts[:]
        rng.shuffle(d2), rng.shuffle(dv2), rng.shuffle(vd2)
        assert run_merge(d2, dv2, vd2) == base
    for coll in (base.new_devices, base.evidence, base.flags):
        assert [x.id for x in coll] == sorted(x.id for x in coll)


def test_verify_disagreement_flags_without_changing_device():
    existing = dev("x1")
    v = verify_verdict("vv-1", ["d1"], [item("d1", category="receptacle", subtype="duplex", rationale="looks like a plug")],
                       agreement={"d1": TM.Agreement(category=False, subtype=False)})
    res = run_merge([det("d1")], [existing], [v])
    [f] = flags_of(res, "model_disagrees")
    assert (f.device_id, f.severity, f.run_id, f.id) == ("x1", "review", "run-002", "run-002:d1:model_disagrees")
    assert f.details == {"verdict_id": "vv-1", "ai_category": "receptacle", "ai_subtype": "duplex",
                         "rationale": "looks like a plug", "source": "verify"}
    assert outcomes(res) == {("d1", "x1"): "confirmed"}
    assert existing.category == "switch" and existing.review_state == "proposed"


def test_verify_disagreement_on_a_new_device_flags_that_device():
    v = verify_verdict("vv-1", ["d1"], [item("d1", is_device=False, category="not_a_device", subtype=None)],
                       agreement={"d1": TM.Agreement(category=False)})
    res = run_merge([det("d1")], [], [v])
    [f] = flags_of(res, "model_disagrees")
    assert f.device_id == "run-002:d1" and f.details["ai_category"] == "not_a_device" and f.details["source"] == "verify"


def test_verify_subtype_only_disagreement_and_uncertain_and_not_a_device_each_flag():
    devices = [dev(f"x{i}", pt=(100.0 * i, 100.0)) for i in (1, 2, 3, 4)]
    dets = [det(f"d{i}", pt=(100.0 * i, 100.0)) for i in (1, 2, 3, 4)]
    v = verify_verdict("vv-1", ["d1", "d2", "d3", "d4"],
                       [item("d1", subtype="dimmer"), item("d2", uncertain=True), item("d3", is_device=False), item("d4")],
                       agreement={"d1": TM.Agreement(category=True, subtype=False), "d2": TM.Agreement(category=True, subtype=True),
                                  "d3": TM.Agreement(category=True), "d4": TM.Agreement(category=True, subtype=None)})
    res = run_merge(dets, devices, [v])
    assert sorted(f.device_id for f in flags_of(res, "model_disagrees")) == ["x1", "x2", "x3"]


def test_verify_agreement_adds_no_flag_and_no_acceptance():
    existing = dev("x1")
    v = verify_verdict("vv-1", ["d1"], [item("d1")], agreement={"d1": TM.Agreement(category=True, subtype=True)})
    res = run_merge([det("d1")], [existing], [v])
    assert res.flags == [] and res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "confirmed"}
    assert existing.review_state == "proposed" and existing.accept_kind is None


def test_verdict_error_adds_no_flag():
    v = verify_verdict("vv-1", ["d1"], error={"code": "timeout", "message": "boom"},
                       agreement={"d1": TM.Agreement(category=False)})  # even a stray agreement must not flag
    res = run_merge([det("d1")], [dev("x1")], [v])
    assert res.flags == []


def test_verify_disagreement_on_rejected_device_is_skipped():
    v = verify_verdict("vv-1", ["d1"], [item("d1", category="receptacle")], agreement={"d1": TM.Agreement(category=False)})
    res = run_merge([det("d1")], [dev("x1", state="rejected")], [v])
    assert res.flags == []


def test_ai_point_near_same_category_corroborates():
    existing = dev("x1", pt=(100.0, 200.0))
    res = run_merge([ai("a1", pt=(103.0, 200.0))], [existing])
    assert res.new_devices == []
    assert [(e.device_id, e.detection_id, e.outcome, e.distance_pt) for e in res.evidence] == [("x1", "a1", "ai_corroborates", 3.0)]
    assert flags_of(res, "model_disagrees") == [] and flags_of(res, "ai_only_candidate") == []
    assert flags_of(res, "not_redetected") == []  # AI corroboration at the same position is a re-detection (R5)


def test_ai_origin_device_corroborated_by_discover_is_not_flagged_not_redetected():
    ai_dev = dev("x1", origin="ai", pt=(500.0, 500.0))
    unseen = dev("x2", origin="ai", pt=(900.0, 900.0))
    res = run_merge([ai("a1", pt=(502.0, 500.0))], [ai_dev, unseen])
    assert outcomes(res) == {("a1", "x1"): "ai_corroborates"}
    assert [(f.type, f.device_id, f.severity) for f in res.flags] == [("not_redetected", "x2", "info")]


def test_mixed_neighbourhood_same_category_nearest_wins_over_nearer_other_category():
    same = dev("x1", category="switch", pt=(100.0, 200.0))            # 4.0 away
    other = dev("x2", category="receptacle", subtype="duplex", pt=(100.0, 202.0))  # 2.0 away, nearer
    res = run_merge([ai("a1", pt=(100.0, 204.0), category="switch")], [other, same])
    assert res.new_devices == []
    assert outcomes(res) == {("a1", "x1"): "ai_corroborates"}
    assert flags_of(res, "model_disagrees") == []
    assert [(f.type, f.device_id) for f in res.flags] == [("not_redetected", "x2")]
    # two same-category neighbours: the nearest one is linked
    near_same = dev("x3", category="switch", pt=(100.0, 203.0))
    res2 = run_merge([ai("a1", pt=(100.0, 204.0), category="switch")], [same, near_same])
    assert outcomes(res2) == {("a1", "x3"): "ai_corroborates"}


def test_ai_point_near_other_category_flags_model_disagrees():
    existing = dev("x1", category="switch")
    a = ai("a1", pt=(103.0, 200.0), category="receptacle", subtype="duplex")
    res = run_merge([a], [existing])
    assert res.new_devices == []
    assert outcomes(res) == {("a1", "x1"): "conflict"}
    assert flags_of(res, "not_redetected") == []  # a discover conflict at the same position is a re-detection (R5)
    [f] = flags_of(res, "model_disagrees")
    assert (f.device_id, f.severity, f.id) == ("x1", "review", "run-002:a1:model_disagrees")
    assert f.details == {"verdict_id": "v-a1", "ai_category": "receptacle", "ai_subtype": "duplex",
                         "rationale": "why a1", "source": "discover"}


def test_ai_point_alone_creates_ai_origin_flagged_proposal():
    res = run_merge([ai("a1", pt=(500.0, 500.0), category="ceiling_fan", subtype="fan", status="")])
    [new] = res.new_devices
    assert (new.id, new.origin, new.review_state, new.category, new.subtype, new.status) == (
        "run-002:a1", "ai", "proposed", "ceiling_fan", "fan", "optional_pre_wire")
    assert (new.room_source, new.room.name, new.created_by_run, new.human_set) == ("proposed_nearest_label", "Kitchen", "run-002", [])
    assert outcomes(res) == {("a1", "run-002:a1"): "created"}
    [f] = res.flags
    assert (f.type, f.severity, f.device_id, f.id) == ("ai_only_candidate", "review", "run-002:a1", "run-002:a1:ai_only_candidate")
    assert f.details == {"verdict_id": "v-a1", "tile_id": "tile-a1", "rationale": "why a1"}


def test_ai_point_near_det0_device_created_in_same_merge_is_not_duplicated():
    res = run_merge([ai("a1", pt=(102.0, 200.0)), det("d1", pt=(100.0, 200.0))])
    assert [d.id for d in res.new_devices] == ["run-002:d1"]
    assert outcomes(res) == {("d1", "run-002:d1"): "created", ("a1", "run-002:d1"): "ai_corroborates"}
    assert res.flags == []


def test_ai_point_near_rejected_device_only_links_evidence():
    res = run_merge([ai("a1", pt=(102.0, 200.0)), ai("a2", pt=(101.0, 200.0), category="receptacle", subtype="duplex")],
                    [dev("x1", state="rejected")])
    assert res.new_devices == []
    assert outcomes(res) == {("a1", "x1"): "rejected_redetected", ("a2", "x1"): "conflict"}
    assert res.flags == []


def test_second_ai_point_of_other_category_flags_first_ai_proposal():
    first = ai("a1", pt=(500.0, 500.0), category="switch")
    second = ai("a2", pt=(502.0, 500.0), category="receptacle", subtype="duplex")
    res = run_merge([second, first])
    assert [d.id for d in res.new_devices] == ["run-002:a1"]  # sorted by (y, x): a1 first, so a1 is the proposal
    assert outcomes(res) == {("a1", "run-002:a1"): "created", ("a2", "run-002:a1"): "conflict"}
    assert {(f.type, f.device_id, f.severity) for f in res.flags} == {
        ("ai_only_candidate", "run-002:a1", "review"), ("model_disagrees", "run-002:a1", "review")}
    [md] = flags_of(res, "model_disagrees")
    assert md.details["ai_category"] == "receptacle" and md.details["source"] == "discover" and md.details["verdict_id"] == "v-a2"


def test_det0_is_processed_before_ai_regardless_of_position():
    # the AI point is higher on the page than the det/0 point, but det/0 still goes first
    res = run_merge([ai("a1", pt=(100.0, 10.0), category="receptacle", subtype="duplex"), det("d1", pt=(100.0, 12.0))])
    assert [d.id for d in res.new_devices] == ["run-002:d1"]
    assert outcomes(res)[("a1", "run-002:d1")] == "conflict"
    assert [f.type for f in res.flags] == ["model_disagrees"]


def test_other_category_det0_on_rejected_device_keeps_the_flag():
    existing = dev("x1", category="switch", state="rejected")
    res = run_merge([det("d1", category="receptacle", subtype="duplex")], [existing])
    assert res.new_devices == []
    assert outcomes(res) == {("d1", "x1"): "conflict"}
    [f] = res.flags
    assert (f.type, f.severity, f.device_id, f.details["kind"]) == ("classification_conflict", "review", "x1", "category")


def test_room_dict_from_propose_is_copied_not_aliased():
    room = {"kind": "labeled", "name": "Kitchen", "label_bbox_pt": [10.0, 10.0, 50.0, 20.0], "bbox": None}
    snapshot = copy.deepcopy(room)
    res = merge_sheet(RUN, [det("d1"), det("d2", pt=(300.0, 300.0))], [],
                      lambda pt: RoomProposal(room=room, ambiguous=False, reason=None), now=NOW)
    assert room == snapshot  # the proposal's dict is not mutated
    room["name"] = "Changed"
    room["label_bbox_pt"][0] = 999.0
    assert [d.room.name for d in res.new_devices] == ["Kitchen", "Kitchen"]
    assert [d.room.label_bbox_pt for d in res.new_devices] == [(10.0, 10.0, 50.0, 20.0)] * 2
    assert res.new_devices[0].room is not res.new_devices[1].room
