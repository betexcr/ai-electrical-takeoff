"""Flags: condition flags follow state, decision flags are only closed by a reviewer, bulk accept skips flagged."""

from __future__ import annotations

import copy

import pytest

from takeoff.app import flags as F
from takeoff.app import models as TM
from takeoff.groundtruth import sheets as S

DOC = "a" * 64
SHEET = "A1-111"
NOW = "2026-10-25T12:00:00.000+00:00"


def label(name, x0, y0=0.0, w=40.0, h=10.0):
    return {"kind": "labeled", "name": name, "label_bbox_pt": [x0, y0, x0 + w, y0 + h], "bbox": None}


ROOMS = [label("Kitchen", 120), label("Hall", 400), S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
KITCHEN = {"kind": "labeled", "name": "Kitchen", "label_bbox_pt": [120.0, 0.0, 160.0, 10.0]}


def dev(id, pt=(100.0, 5.0), category="switch", state="proposed", room=KITCHEN, source="proposed_nearest_label",
        **kw):
    base = dict(id=id, document_id=DOC, sheet_id=SHEET, category=category, status="as_drawn",
                point={"u": 0.1, "v": 0.1}, point_pt=pt, review_state=state, origin="detector",
                room=room, room_source=source if room is not None else None)
    base.update(kw)
    return TM.Device(**base)


def flag(device_id, type, severity="review", open=True, **kw):
    return TM.Flag(device_id=device_id, document_id=DOC, sheet_id=SHEET, type=type, severity=severity, open=open, **kw)


def run_conditions(devices, rooms=ROOMS):
    before = copy.deepcopy(devices)
    out = F.conditions(devices, rooms)
    assert devices == before
    return out


def test_type_constants():
    assert F.VALIDATION_TYPES == ("duplicate_warning", "room_ambiguous", "no_room")
    assert F.DECISION_TYPES == ("classification_conflict", "ambiguous_match", "not_redetected", "model_disagrees",
                                "ai_only_candidate")


def test_duplicate_within_3pt_flags_both():
    got = run_conditions([dev("a", (100.0, 5.0)), dev("b", (103.0, 5.0)), dev("c", (110.0, 5.0)),
                          dev("d", (100.0, 5.0), category="outlet")])
    assert "duplicate_warning" in got["a"] and "duplicate_warning" in got["b"]  # exactly 3.0 pt counts
    assert got["a"]["duplicate_warning"]["other_device_ids"] == ["b"]
    assert got["b"]["duplicate_warning"]["other_device_ids"] == ["a"]
    assert "duplicate_warning" not in got.get("c", {})  # 7 pt from b
    assert "duplicate_warning" not in got.get("d", {})  # other category
    just_over = run_conditions([dev("a", (100.0, 5.0)), dev("b", (103.01, 5.0))])
    assert "duplicate_warning" not in just_over.get("a", {})


def test_duplicate_is_per_sheet_and_ignores_deleted():
    other_sheet = dev("b", (101.0, 5.0), sheet_id="A2-111")
    deleted = dev("c", (101.0, 5.0), deleted=True)
    got = run_conditions([dev("a"), other_sheet, deleted])
    assert "duplicate_warning" not in got.get("a", {})
    assert "c" not in got  # deleted devices carry no conditions at all


def test_rejected_device_never_counts_as_duplicate_partner():
    got = run_conditions([dev("a", (100.0, 5.0)), dev("b", (101.0, 5.0), state="rejected")])
    assert "duplicate_warning" not in got.get("a", {})
    assert "duplicate_warning" not in got.get("b", {})  # a rejected device is already decided


def test_duplicate_distance_is_euclidean_not_per_axis():
    diagonal_far = run_conditions([dev("a", (100.0, 5.0)), dev("b", (102.5, 7.5))])  # ~3.54 pt, within 3 per axis
    assert "duplicate_warning" not in diagonal_far.get("a", {})
    diagonal_near = run_conditions([dev("a", (100.0, 5.0)), dev("b", (102.12, 7.12))])  # ~3.0 pt
    assert "duplicate_warning" in diagonal_near["a"] and "duplicate_warning" in diagonal_near["b"]


def test_rejected_device_carries_no_condition_flags():
    ambiguous_rooms = [label("Kitchen", 120), label("Hall", 124)]
    rejected = [dev("a", (100.0, 5.0), state="rejected"), dev("b", (101.0, 5.0), state="rejected"),
                dev("c", (500.0, 5.0), state="rejected", room=None)]
    assert run_conditions(rejected, ambiguous_rooms) == {}
    # the same devices while still proposed do carry them
    proposed = [dev("a", (100.0, 5.0)), dev("b", (101.0, 5.0)), dev("c", (500.0, 5.0), room=None)]
    got = run_conditions(proposed, ambiguous_rooms)
    assert {"duplicate_warning", "room_ambiguous"} <= set(got["a"]) and "no_room" in got["c"]


def test_reviewer_room_clears_room_ambiguous():
    ambiguous_rooms = [label("Kitchen", 120), label("Hall", 124)]
    d = dev("a", (100.0, 5.0), source="proposed_nearest_label")
    assert "room_ambiguous" in run_conditions([d], ambiguous_rooms)["a"]
    assert run_conditions([d], ambiguous_rooms)["a"]["room_ambiguous"]["reason"] == "two_labels_close"
    reviewed = dev("a", (100.0, 5.0), source="reviewer")
    assert "room_ambiguous" not in run_conditions([reviewed], ambiguous_rooms).get("a", {})
    clear = run_conditions([d], ROOMS)
    assert "room_ambiguous" not in clear.get("a", {})
    far = dev("a", (900.0, 900.0), room=S.ROOM_UNLABELED)  # proposal far from every label
    assert run_conditions([far])["a"]["room_ambiguous"]["reason"] == "far_from_labels"


def test_no_room_when_room_is_none():
    got = run_conditions([dev("a", room=None), dev("b", (150.0, 5.0))])
    assert "no_room" in got["a"] and "room_ambiguous" not in got["a"]
    assert "b" not in got


def test_sync_opens_and_closes_only_condition_flags():
    d1, d2 = dev("a", room=None), dev("b")
    decision = flag("b", "model_disagrees")
    stale = flag("b", "no_room")  # condition no longer holds on b
    kept = flag("a", "no_room")  # still holds on a
    current = {"a": {"no_room": {}, "duplicate_warning": {"other_device_ids": ["c"], "distance_pt": 1.0}}}
    open_flags = [decision, stale, kept]
    before = copy.deepcopy(open_flags)
    to_open, to_close = F.sync_validation(open_flags, current, devices=[d1, d2], now=NOW)
    assert open_flags == before
    assert [(f.device_id, f.type) for f in to_open] == [("a", "duplicate_warning")]
    opened = to_open[0]
    assert opened.id == "cond:a:duplicate_warning" and opened.open and opened.severity == "review"
    assert (opened.document_id, opened.sheet_id) == (DOC, SHEET) and opened.run_id is None
    assert opened.details == {"other_device_ids": ["c"], "distance_pt": 1.0}
    assert [f.id for f in to_close] == [stale.id]
    closed = to_close[0]
    assert not closed.open and closed.resolution == "condition cleared" and closed.resolved_at == NOW
    assert stale.open  # input untouched
    assert decision.id not in {f.id for f in to_close}  # decision flags are never touched


def test_sync_ignores_already_closed_flags_and_avoids_taken_ids():
    d = dev("a", room=None)
    closed_old = flag("a", "no_room", open=False, id="cond:a:no_room", resolution="condition cleared")
    to_open, to_close = F.sync_validation([closed_old], {"a": {"no_room": {}}}, devices=[d], now=NOW,
                                          taken_ids={"cond:a:no_room"})
    assert to_close == [] and len(to_open) == 1 and to_open[0].id == "cond:a:no_room:2"
    again, _ = F.sync_validation([], {"a": {"no_room": {}}}, devices=[d], now=NOW,
                                 taken_ids={"cond:a:no_room", "cond:a:no_room:2"})
    assert again[0].id == "cond:a:no_room:3"


def test_sync_is_idempotent_and_needs_the_device_to_open():
    d = dev("a", room=None)
    to_open, _ = F.sync_validation([], {"a": {"no_room": {}}}, devices=[d])
    assert F.sync_validation(to_open, {"a": {"no_room": {}}}, devices=[d]) == ([], [])
    with pytest.raises(ValueError):
        F.sync_validation([], {"a": {"no_room": {}}})


def test_bulk_excludes_every_open_review_flag_including_model_disagrees_and_ai_only():
    d = dev("a")
    assert F.bulk_eligible(d, [])
    for t in F.VALIDATION_TYPES + F.DECISION_TYPES:
        assert not F.bulk_eligible(d, [flag("a", t)]), t
    assert F.bulk_eligible(d, [flag("other", "model_disagrees")])  # another device's flag
    assert F.bulk_eligible(d, [flag("a", "model_disagrees", open=False)])  # closed flag
    assert not F.bulk_eligible(dev("a", state="accepted"), [])
    assert not F.bulk_eligible(dev("a", state="rejected"), [])
    assert not F.bulk_eligible(dev("a", deleted=True), [])


def test_info_flags_do_not_block_bulk():
    d = dev("a")
    assert F.bulk_eligible(d, [flag("a", "not_redetected", severity="info")])
    assert F.bulk_eligible(d, [flag("a", "not_redetected", severity="info"),
                               flag("a", "duplicate_warning", severity="info")])
    assert not F.bulk_eligible(d, [flag("a", "not_redetected", severity="info"), flag("a", "no_room")])


def test_info_ai_only_and_model_disagrees_still_block_bulk():
    d = dev("a")
    for t in ("ai_only_candidate", "model_disagrees"):
        assert not F.bulk_eligible(d, [flag("a", t, severity="info")]), t
        assert F.bulk_eligible(d, [flag("a", t, severity="info", open=False)]), t  # closed no longer blocks


def test_model_disagrees_blocks_acceptance_without_reason_only():
    md, nr = flag("a", "model_disagrees"), flag("a", "no_room")
    others = [flag("a", t) for t in ("classification_conflict", "ambiguous_match", "not_redetected",
                                     "ai_only_candidate")]
    flags = others + [md, nr, flag("b", "model_disagrees"), flag("b", "no_room"), flag("a", "no_room", open=False),
                      flag("a", "room_ambiguous", severity="info")]
    for empty in (None, "", "   "):
        assert F.acceptance_blockers("a", flags, empty) == [md, nr]
    assert F.acceptance_blockers("a", flags, "checked the drawing") == [nr]
    assert F.acceptance_blockers("a", others, None) == []  # decision flags are closed by the acceptance itself
