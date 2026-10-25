"""TakeoffService: a saved correction is an acceptance decision; reject and delete never count (spec §4.13)."""

from __future__ import annotations

import pytest

from takeoff.app import flags as F
from takeoff.app import models as TM
from takeoff.app.service import TakeoffError
from tk_helpers import (ACTOR, DOC, FAR_FROM_LABELS, HALL, KITCHEN, NEAR_HALL, NEAR_KITCHEN, RUN_ID, SHEET, flag_types,
                              flags_of, make, norm, room_ref, seed_detection, seed_device, seed_flag)


@pytest.fixture
def env(tmp_path):
    store, svc = make(tmp_path)
    yield store, svc
    store.close()


def official_total(svc):
    return svc.counts(DOC)["review"]["official_total"]


def raises(code, fn, *a, **kw):
    with pytest.raises(TakeoffError) as e:
        fn(*a, **kw)
    assert e.value.code == code
    return e.value


def test_saved_category_correction_of_proposal_is_corrected_and_accepted(env):
    store, svc = env
    d = seed_device(store, "d1")
    conflict = seed_flag(store, d, "classification_conflict", run_category="receptacle", run_subtype="gfi",
                         detection_id="x", kind="category")
    assert official_total(svc) == 0

    out = svc.update_device("d1", ACTOR, {"category": "receptacle", "subtype": "gfi"})
    dev = out["device"]
    assert out["blocking_flags"] == []
    assert (dev.review_state, dev.accept_kind) == ("accepted", "corrected_and_accepted")
    assert (dev.category, dev.subtype) == ("receptacle", "gfi")
    assert {"category", "subtype", "review_state"} <= set(dev.human_set)
    assert dev.revision == 2
    closed = store.get("flags", conflict.id)
    assert not closed.open and closed.resolution == "resolved_by_correction" and closed.resolved_by == ACTOR
    assert official_total(svc) == 1

    ev = store.events("device", "d1")[-1]  # history: the original proposal, the corrected values, who, the result
    assert ev.actor == ACTOR and ev.action == "correct_and_accept"
    assert (ev.before["category"], ev.before["review_state"]) == ("switch", "proposed")
    assert (ev.after["category"], ev.after["review_state"]) == ("receptacle", "accepted")
    assert store.get("devices", "d1") == dev  # persisted, not only returned

    raises("invalid", svc.update_device, "d1", ACTOR, {"category": "receptacle", "subtype": "dimmer"})
    raises("invalid", svc.update_device, "d1", ACTOR, {"colour": "red"})
    raises("invalid", svc.update_device, "d1", ACTOR, {})
    raises("not_found", svc.update_device, "nope", ACTOR, {"subtype": "dimmer"})


def test_correction_blocked_by_open_duplicate_stays_proposed(env):
    store, svc = env
    seed_device(store, "a")
    seed_device(store, "b", pt=(NEAR_KITCHEN[0] + 1.5, NEAR_KITCHEN[1]))

    out = svc.update_device("a", ACTOR, {"subtype": "dimmer"})
    dev = out["device"]
    assert dev.review_state == "proposed" and dev.accept_kind is None
    assert dev.subtype == "dimmer" and "subtype" in dev.human_set  # the correction itself is saved
    assert [f.type for f in out["blocking_flags"]] == ["duplicate_warning"]
    assert flag_types(store, "a") == ["duplicate_warning"] and flag_types(store, "b") == ["duplicate_warning"]
    assert official_total(svc) == 0


def test_moved_position_is_a_correction_and_accepts(env):
    store, svc = env
    seed_device(store, "d1")
    out = svc.update_device("d1", ACTOR, {"point": norm((168.0, 168.0))})
    dev = out["device"]
    assert dev.point_pt == pytest.approx((168.0, 168.0))
    assert dev.point.u == pytest.approx(0.14) and dev.point.v == pytest.approx(0.14)
    assert (dev.review_state, dev.accept_kind) == ("accepted", "corrected_and_accepted")
    assert "point" in dev.human_set and "room" not in dev.human_set
    assert dev.room.name == "Kitchen" and dev.room_source == "proposed_nearest_label"
    assert official_total(svc) == 1


def test_room_change_resolves_room_ambiguous_and_accepts(env):
    store, svc = env
    d = seed_device(store, "d1", pt=FAR_FROM_LABELS,
                    room={"kind": "unlabeled", "name": "Unlabeled", "label_bbox_pt": None})
    amb = seed_flag(store, d, "room_ambiguous", id="cond:d1:room_ambiguous", reason="far_from_labels")
    assert F.bulk_eligible(d, flags_of(store, "d1")) is False

    out = svc.update_device("d1", ACTOR, {"room": HALL})
    dev = out["device"]
    assert dev.room == TM.RoomRef(**room_ref(HALL)) and dev.room_source == "reviewer"
    assert "room" in dev.human_set
    assert (dev.review_state, dev.accept_kind) == ("accepted", "corrected_and_accepted")
    closed = store.get("flags", amb.id)
    assert not closed.open and closed.resolution == "condition cleared"
    assert flags_of(store, "d1") == []
    assert official_total(svc) == 1
    # a room that is not one of this sheet's rooms is refused
    other = {"kind": "labeled", "name": "Hall", "label_bbox_pt": [1.0, 2.0, 3.0, 4.0]}
    raises("invalid", svc.update_device, "d1", ACTOR, {"room": other})


def test_accept_refuses_when_condition_flag_open(env):
    store, svc = env
    seed_device(store, "d1", room=None)  # no room: a no_room condition holds
    err = raises("blocked_by_flags", svc.accept, "d1", ACTOR)
    assert err.status == 409
    assert [f["type"] for f in err.details["blocking_flags"]] == ["no_room"]
    assert store.get("devices", "d1").review_state == "proposed"
    assert official_total(svc) == 0

    # a condition flag is checked before the AI disagreement, and a reason does not override it
    d2 = seed_device(store, "d2", pt=(500.0, 150.0), room=None)
    seed_flag(store, d2, "model_disagrees", verdict_id="v", ai_category="receptacle", ai_subtype=None)
    raises("blocked_by_flags", svc.accept, "d2", ACTOR)
    raises("blocked_by_flags", svc.accept, "d2", ACTOR, reason="legend says switch")

    svc.update_device("d1", ACTOR, {"room": KITCHEN})  # fixing the condition accepts the correction
    assert store.get("devices", "d1").review_state == "accepted"


def test_reject_and_delete_never_count(env):
    store, svc = env
    seed_device(store, "a")
    seed_device(store, "b", pt=NEAR_HALL, room=HALL)
    seed_device(store, "c", pt=(170.0, 150.0))
    for did in ("a", "b", "c"):
        assert svc.accept(did, ACTOR).accept_kind == "accepted"
    assert official_total(svc) == 3

    rejected = svc.reject("a", ACTOR, reason="not a switch")
    assert (rejected.review_state, rejected.accept_kind) == ("rejected", None)
    raises("reason_required", svc.delete, "b", ACTOR, "  ")
    deleted = svc.delete("b", ACTOR, "duplicate of c")
    assert deleted.deleted and deleted.delete_reason == "duplicate of c"
    assert [d.id for d in svc.devices(DOC)] == ["a", "c"]
    assert [d.id for d in svc.devices(DOC, include_deleted=True)] == ["a", "b", "c"]

    counts = svc.counts(DOC)
    assert counts["official"][SHEET]["total"] == 1
    assert counts["review"]["rejected"] == 1
    assert counts["review"]["detected"] == 2  # a deleted device is in no figure
    assert [e.action for e in store.events("device", "b")][-1] == "delete"
    raises("invalid", svc.accept, "b", ACTOR)  # a deleted device is never accepted again
    raises("not_found", svc.accept, "nope", ACTOR)


def test_manual_create_counts_as_manual(env):
    store, svc = env
    dev = svc.create_device(DOC, SHEET, ACTOR, "ceiling_fan", "optional_pre_wire", norm(NEAR_KITCHEN))
    assert (dev.origin, dev.review_state, dev.accept_kind) == ("manual", "accepted", "manual")
    assert dev.status == "optional_pre_wire"
    assert dev.point_pt == pytest.approx(NEAR_KITCHEN)
    assert dev.room == TM.RoomRef(**room_ref(KITCHEN)) and dev.room_source == "proposed_nearest_label"
    assert dev.created_by_run is None
    assert store.get("devices", dev.id) == dev
    assert [e.action for e in store.events("device", dev.id)] == ["create_manual"]

    chosen = svc.create_device(DOC, SHEET, ACTOR, "receptacle", "other", norm(NEAR_HALL), room=HALL,
                               notes="floor box")
    assert chosen.room_source == "reviewer" and chosen.notes == "floor box"
    counts = svc.counts(DOC)
    assert counts["review"]["manual"] == 2 and counts["review"]["detected"] == 0
    assert counts["official"][SHEET]["by_origin"] == {"detector_accepted": 0, "ai_accepted": 0, "manual": 2}
    assert counts["official"][SHEET]["by_room"] == {"Hall": {"receptacle": {"other": 1}},
                                                    "Kitchen": {"ceiling_fan": {"optional_pre_wire": 1}}}

    raises("invalid", svc.create_device, DOC, SHEET, ACTOR, "receptacle", "other", norm(NEAR_KITCHEN))  # needs a note
    raises("invalid", svc.create_device, DOC, SHEET, ACTOR, "wiring_run", "x", norm(NEAR_KITCHEN))
    raises("not_found", svc.create_device, DOC, "Z9-999", ACTOR, "switch", "standard", norm(NEAR_KITCHEN))


def test_create_or_move_outside_region_is_refused(env):
    store, svc = env
    on_page_outside_region = norm((1080.0, 150.0))  # on the 1200 pt page, beyond the 1000 pt region
    for point in (on_page_outside_region, {"u": 1.2, "v": 0.5}, {"u": -0.01, "v": 0.5}):
        err = raises("outside_region", svc.create_device, DOC, SHEET, ACTOR, "switch", "standard", point)
        assert err.status == 400
    assert svc.devices(DOC, include_deleted=True) == []

    seed_device(store, "d1")
    raises("outside_region", svc.update_device, "d1", ACTOR, {"point": on_page_outside_region})
    after = store.get("devices", "d1")
    assert after.point_pt == NEAR_KITCHEN and after.review_state == "proposed" and after.revision == 1
    assert [e.action for e in store.events("device", "d1")] == ["create"]


def test_resolve_conflict_three_choices(env):
    store, svc = env
    flags = {}
    for i, choice in enumerate(("keep", "change_category", "separate_device")):
        pt = (130.0 + 200.0 * i, 300.0)
        det = seed_detection(store, f"det-{choice}", pt=(pt[0] + 2.0, pt[1]), category="receptacle", subtype="gfi")
        dev = seed_device(store, f"dev-{choice}", pt=pt, room=KITCHEN, room_source="reviewer")
        flags[choice] = seed_flag(store, dev, "classification_conflict", run_category="receptacle",
                                  run_subtype="gfi", detection_id=det.id, kind="category")

    kept = svc.resolve_conflict(flags["keep"].id, ACTOR, "keep")
    assert (kept.category, kept.review_state) == ("switch", "proposed")
    kept_flag = store.get("flags", flags["keep"].id)
    assert (kept_flag.open, kept_flag.resolution, kept_flag.resolved_by) == (False, "kept_existing", ACTOR)

    changed = svc.resolve_conflict(flags["change_category"].id, ACTOR, "change_category")
    assert (changed.category, changed.subtype) == ("receptacle", "gfi")
    assert (changed.review_state, changed.accept_kind) == ("accepted", "corrected_and_accepted")
    assert store.get("flags", flags["change_category"].id).resolution == "resolved_by_correction"

    new = svc.resolve_conflict(flags["separate_device"].id, ACTOR, "separate_device")
    assert new.id != "dev-separate_device"
    assert (new.category, new.subtype, new.review_state, new.origin) == ("receptacle", "gfi", "proposed", "detector")
    assert new.point_pt == (532.0, 300.0) and new.created_by_run == RUN_ID
    ev = [e for e in store.list("evidence") if e.device_id == new.id]
    assert [(e.outcome, e.detection_id, e.run_id) for e in ev] == [("created", "det-separate_device", RUN_ID)]
    original = store.get("devices", "dev-separate_device")
    assert (original.category, original.review_state, original.revision) == ("switch", "proposed", 1)
    assert store.get("flags", flags["separate_device"].id).open is False

    raises("invalid", svc.resolve_conflict, flags["keep"].id, ACTOR, "keep")  # already closed
    raises("invalid", svc.resolve_conflict, flags["keep"].id, ACTOR, "merge")
    raises("not_found", svc.resolve_conflict, "nope", ACTOR, "keep")


def test_deleting_duplicate_partner_clears_the_other_flag(env):
    store, svc = env
    a = seed_device(store, "a")
    b = seed_device(store, "b", pt=(NEAR_KITCHEN[0] + 1.0, NEAR_KITCHEN[1]))
    seed_flag(store, a, "duplicate_warning", id="cond:a:duplicate_warning", other_device_ids=["b"])
    seed_flag(store, b, "duplicate_warning", id="cond:b:duplicate_warning", other_device_ids=["a"])
    raises("blocked_by_flags", svc.accept, "a", ACTOR)

    svc.delete("b", ACTOR, "same switch drawn twice")
    for fid in ("cond:a:duplicate_warning", "cond:b:duplicate_warning"):
        fl = store.get("flags", fid)
        assert not fl.open and fl.resolution == "condition cleared"
    assert svc.accept("a", ACTOR).review_state == "accepted"
    assert official_total(svc) == 1


def test_manual_device_cannot_be_rejected_only_deleted(env):
    store, svc = env
    dev = svc.create_device(DOC, SHEET, ACTOR, "switch", "standard", norm(NEAR_KITCHEN))
    err = raises("manual_cannot_be_rejected", svc.reject, dev.id, ACTOR)
    assert err.status == 400
    assert store.get("devices", dev.id).review_state == "accepted"
    svc.delete(dev.id, ACTOR, "added by mistake")
    assert official_total(svc) == 0
    assert svc.counts(DOC)["review"]["manual"] == 0


def test_ai_only_candidate_individual_accept_and_correction_close_flag(env):
    store, svc = env
    a = seed_device(store, "ai-a", origin="ai")
    b = seed_device(store, "ai-b", pt=NEAR_HALL, room=HALL, origin="ai")
    fa = seed_flag(store, a, "ai_only_candidate", verdict_id="v-a", tile_id="t1", rationale="looks like a switch")
    fb = seed_flag(store, b, "ai_only_candidate", verdict_id="v-b", tile_id="t2", rationale="looks like a switch")
    for d in (a, b):
        assert F.bulk_eligible(d, flags_of(store, d.id)) is False  # individual review only
    counts = svc.counts(DOC)["review"]
    assert (counts["awaiting_flagged"], counts["awaiting_unflagged"], counts["detected_ai_only"]) == (2, 0, 2)

    accepted = svc.accept("ai-a", ACTOR)
    assert (accepted.review_state, accepted.accept_kind, accepted.origin) == ("accepted", "accepted", "ai")
    assert store.get("flags", fa.id).resolution == "accepted by reviewer"

    corrected = svc.update_device("ai-b", ACTOR, {"subtype": "dimmer"})["device"]
    assert (corrected.review_state, corrected.accept_kind) == ("accepted", "corrected_and_accepted")
    assert store.get("flags", fb.id).resolution == "accepted by reviewer (saved correction)"

    official = svc.counts(DOC)["official"][SHEET]
    assert official["by_origin"] == {"detector_accepted": 0, "ai_accepted": 2, "manual": 0}


def test_device_detail_ai_summary_variants(env):
    store, svc = env
    v_common = dict(run_id=RUN_ID, sheet_id=SHEET, kind="verify", model="claude-opus-5-5",
                    prompt_version="ai-verify-prompt/1", ai_version="ai-verify/1")
    det_ok = seed_detection(store, "det-ok", verdict_id=None)
    det_err = seed_detection(store, "det-err", pt=NEAR_HALL)
    det_ai = seed_detection(store, "det-ai", pt=(170.0, 150.0), source="ai_discover", verdict_id="v-disc",
                            rationale="a switch symbol")
    det_no = seed_detection(store, "det-no", pt=(620.0, 150.0))
    store.insert("verdicts", TM.ModelVerdict(
        id="v-ok", implementation="claude", detection_ids=[det_ok.id],
        output={"verdicts": [{"item_id": det_ok.id, "category": "switch", "subtype": "standard",
                              "is_device": True, "uncertain": False}]},
        agreement={det_ok.id: TM.Agreement(category=True, subtype=True)}, **v_common), "system")
    store.insert("verdicts", TM.ModelVerdict(id="v-err", implementation="fake", detection_ids=[det_err.id],
                                             error={"code": "timeout"}, **v_common), "system")
    store.insert("verdicts", TM.ModelVerdict(
        id="v-no", implementation="claude", detection_ids=[det_no.id],
        output={"verdicts": [{"item_id": det_no.id, "category": "receptacle", "subtype": "gfi",
                              "is_device": True, "uncertain": False, "rationale": "GFI tag"}]},
        agreement={det_no.id: TM.Agreement(category=False, subtype=False)}, **v_common), "system")

    seed_device(store, "ok", detection=det_ok)
    seed_device(store, "err", pt=NEAR_HALL, room=HALL, detection=det_err)
    ai = seed_device(store, "ai", pt=(170.0, 150.0), origin="ai", detection=det_ai)
    seed_flag(store, ai, "ai_only_candidate", verdict_id="v-disc", tile_id="t1", rationale="a switch symbol")
    no = seed_device(store, "no", pt=(620.0, 150.0), room=HALL, detection=det_no)
    seed_flag(store, no, "model_disagrees", verdict_id="v-no", ai_category="receptacle", ai_subtype="gfi",
              rationale="GFI tag", source="verify")
    manual = svc.create_device(DOC, SHEET, ACTOR, "switch", "standard", norm((130.0, 600.0)))

    detail = svc.device_detail("ok")
    assert detail["device"].id == "ok"
    assert detail["ai_summary"]["status"] == "agrees"
    assert detail["ai_summary"]["text"] == "AI second opinion: agrees"
    [ev] = detail["evidence"]
    assert ev["evidence"].outcome == "created" and ev["run"].id == RUN_ID and ev["detection"].id == "det-ok"
    assert [v.id for v in detail["verdicts"]] == ["v-ok"]
    assert [e.action for e in detail["history"]] == ["create", "create"]  # the device, then its evidence

    no_detail = svc.device_detail("no")
    assert no_detail["ai_summary"]["status"] == "disagrees"
    assert no_detail["ai_summary"]["text"] == "AI second opinion: disagrees (receptacle / gfi)"
    assert [f.type for f in no_detail["flags"]] == ["model_disagrees"]
    assert [v.id for v in no_detail["verdicts"]] == ["v-no"]

    err = svc.device_detail("err")["ai_summary"]
    assert err["status"] == "not_verified"
    assert err["text"] == "AI second opinion: not verified (AI error: timeout) · test double, not a model"

    assert svc.device_detail("ai")["ai_summary"]["text"] == "AI-proposed location: check position before accepting"
    assert svc.device_detail(manual.id)["ai_summary"]["text"] == "AI second opinion: not verified (added manually)"
    raises("not_found", svc.device_detail, "nope")


def test_ai_only_candidate_cannot_be_acknowledged(env):
    store, svc = env
    d = seed_device(store, "ai-a", origin="ai")
    fl = seed_flag(store, d, "ai_only_candidate", verdict_id="v-a", tile_id="t1", rationale="looks like a switch")
    err = raises("invalid", svc.acknowledge_flag, fl.id, ACTOR, "looks fine")
    assert err.status == 400
    assert store.get("flags", fl.id).open
    assert F.bulk_eligible(store.get("devices", "ai-a"), flags_of(store, "ai-a")) is False
    assert svc.counts(DOC)["review"]["awaiting_flagged"] == 1


def test_condition_flags_cannot_be_acknowledged(env):
    store, svc = env
    d = seed_device(store, "d1")
    for type_ in F.VALIDATION_TYPES:
        fl = seed_flag(store, d, type_, id=f"cond:d1:{type_}")
        raises("invalid", svc.acknowledge_flag, fl.id, ACTOR, "it is fine")
        assert store.get("flags", fl.id).open


def test_room_only_correction_closes_decision_flags_on_accept(env):
    store, svc = env
    d = seed_device(store, "d1")
    opened = [seed_flag(store, d, "classification_conflict", run_category="receptacle", run_subtype="gfi",
                        detection_id="x", kind="category"),
              seed_flag(store, d, "ambiguous_match", possible_device_ids=["other"]),
              seed_flag(store, d, "not_redetected")]
    out = svc.update_device("d1", ACTOR, {"room": HALL})
    assert out["blocking_flags"] == []
    assert (out["device"].review_state, out["device"].accept_kind) == ("accepted", "corrected_and_accepted")
    for fl in opened:
        closed = store.get("flags", fl.id)
        assert (closed.open, closed.resolution, closed.resolved_by) == (
            False, "accepted by reviewer (saved correction)", ACTOR)


def test_notes_only_or_no_op_save_is_not_a_correction(env):
    store, svc = env
    seed_device(store, "d1")
    out = svc.update_device("d1", ACTOR, {"notes": "check the elevation"})
    dev = out["device"]
    assert (dev.review_state, dev.accept_kind, dev.notes) == ("proposed", None, "check the elevation")
    assert dev.human_set == [] and out["blocking_flags"] == []
    for no_op in ({"category": "switch", "subtype": "standard"}, {"point": norm(NEAR_KITCHEN)}):
        dev = svc.update_device("d1", ACTOR, no_op)["device"]
        assert (dev.review_state, dev.accept_kind, dev.human_set) == ("proposed", None, [])
        assert dev.point_pt == NEAR_KITCHEN and dev.room_source == "proposed_nearest_label"
    assert [e.action for e in store.events("device", "d1")] == ["create", "update", "update", "update"]
    assert official_total(svc) == 0
    # confirming the proposed room as the reviewer's choice changes room_source: that is a correction
    dev = svc.update_device("d1", ACTOR, {"room": KITCHEN})["device"]
    assert (dev.review_state, dev.room_source, dev.human_set) == ("accepted", "reviewer", ["room", "review_state"])


def test_counts_room_rows_use_sheet_rooms(env):
    store, svc = env
    # the sheet has two "CL" labels; only the west one gets a counted device
    dev = svc.create_device(DOC, SHEET, ACTOR, "switch", "standard", norm((305.0, 870.0)))
    assert dev.room.name == "CL" and dev.room.label_bbox_pt == (300.0, 900.0, 315.0, 909.0)
    assert svc.counts(DOC)["official"][SHEET]["by_room"] == {"CL @(300,900)": {"switch": {"standard": 1}}}


def test_unknown_sheet_view_is_not_found(env):
    store, svc = env
    store.insert("sheets", TM.SheetInfo(id=f"{DOC}:2", document_id=DOC, page_number=2, sheet_id="A2-111"), "system")
    err = raises("not_found", svc.create_device, DOC, "A2-111", ACTOR, "switch", "standard", norm(NEAR_KITCHEN))
    assert err.status == 404
    seed_device(store, "d2", sheet_id="A2-111")
    raises("not_found", svc.update_device, "d2", ACTOR, {"subtype": "dimmer"})
    raises("not_found", svc.accept, "d2", ACTOR)
    assert store.get("devices", "d2").review_state == "proposed"


def test_ai_summary_reflects_resolution_and_missing_rows(env):
    store, svc = env
    det = seed_detection(store, "det-1")
    d = seed_device(store, "d1", detection=det)
    seed_flag(store, d, "model_disagrees", verdict_id="v-1", ai_category="receptacle", ai_subtype="gfi",
              rationale="GFI tag", source="verify")
    svc.update_device("d1", ACTOR, {"category": "receptacle", "subtype": "gfi"})
    summary = svc.device_detail("d1")["ai_summary"]
    assert summary["status"] == "agrees" and summary["resolution"] == "resolved_by_correction"
    assert summary["text"] == "AI second opinion: agrees (resolved by correction)"

    for i, (ai_category, what) in enumerate(((None, "missing verdict row"), ("not_a_device", "not a device"))):
        dev = seed_device(store, f"x{i}", pt=(580.0 + 20 * i, 150.0), room=HALL)
        seed_flag(store, dev, "model_disagrees", verdict_id="v-x", ai_category=ai_category, ai_subtype=None,
                  rationale=None, source="verify")
        assert svc.device_detail(dev.id)["ai_summary"]["text"] == f"AI second opinion: disagrees ({what})"
