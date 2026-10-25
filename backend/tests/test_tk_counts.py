"""Counts: official totals over accepted, non-deleted devices only; review figures per spec §4.17."""

from __future__ import annotations

import random

from takeoff.app import counts as C
from takeoff.app import flags as F
from takeoff.app import models as TM

DOC = "a" * 64
SHEET = "A1-111"
KITCHEN = {"kind": "labeled", "name": "Kitchen", "label_bbox_pt": [100.0, 100.0, 160.0, 112.0]}


def dev(id, state="accepted", origin="detector", category="switch", subtype="standard", room=KITCHEN,
        sheet=SHEET, **kw):
    base = dict(id=id, document_id=DOC, sheet_id=sheet, category=category, subtype=subtype, status="as_drawn",
                point={"u": 0.1, "v": 0.1}, point_pt=(100.0, 100.0), room=room,
                room_source="reviewer" if room else None, review_state=state, origin=origin)
    base.update(kw)
    return TM.Device(**base)


def flag(device_id, type="duplicate_warning", severity="review", open=True, n=0):
    return TM.Flag(id=f"f:{device_id}:{type}:{n}", device_id=device_id, document_id=DOC, sheet_id=SHEET, type=type,
                   severity=severity, open=open)


def test_only_accepted_non_deleted_devices_count():
    devices = [dev("acc"), dev("prop", state="proposed"), dev("rej", state="rejected"),
               dev("del", deleted=True, delete_reason="duplicate"),
               dev("man", origin="manual", accept_kind="manual"),
               dev("man-del", origin="manual", deleted=True, delete_reason="mistake")]
    got = C.derive(devices, [])
    sheet = got["official"][SHEET]
    assert sheet["total"] == 2
    assert sheet["by_category"] == {"switch": {"standard": 2}}
    assert sheet["by_room"] == {"Kitchen": {"switch": {"standard": 2}}}
    assert got["review"]["official_total"] == 2
    # proposals never count; with nothing accepted, a sheet has no official entry at all
    assert C.derive([dev("p", state="proposed"), dev("r", state="rejected")], [])["official"] == {}


def test_origins_reported_separately_including_ai_accepted():
    devices = [dev("d1"), dev("d2", subtype="dimmer"), dev("a1", origin="ai"),
               dev("m1", origin="manual", category="receptacle", subtype="gfi"),
               dev("a2", origin="ai", state="proposed")]
    got = C.derive(devices, [])
    sheet = got["official"][SHEET]
    assert sheet["by_origin"] == {"detector_accepted": 2, "ai_accepted": 1, "manual": 1}
    assert sheet["total"] == 4 == sum(sheet["by_origin"].values())
    assert sheet["by_category"] == {"receptacle": {"gfi": 1}, "switch": {"dimmer": 1, "standard": 2}}
    review = got["review"]
    assert review["accepted_detected"] == 3  # detector + ai
    assert review["manual"] == 1
    assert review["detected_ai_only"] == 2


def test_two_cl_rooms_are_separate_rows():
    cl_west = {"kind": "labeled", "name": "CL", "label_bbox_pt": [200.0, 300.0, 215.0, 309.0]}
    cl_east = {"kind": "labeled", "name": "CL", "label_bbox_pt": [700.0, 300.0, 715.0, 309.0]}
    devices = [dev("w1", room=cl_west), dev("w2", room=cl_west), dev("e1", room=cl_east), dev("k1")]
    by_room = C.derive(devices, [])["official"][SHEET]["by_room"]
    assert by_room == {"CL @(200,300)": {"switch": {"standard": 2}},
                       "CL @(700,300)": {"switch": {"standard": 1}},
                       "Kitchen": {"switch": {"standard": 1}}}
    # a single CL label on a sheet keeps its plain name; an unassigned device has its own row
    alone = C.derive([dev("w1", room=cl_west), dev("x", room=None)], [])["official"][SHEET]["by_room"]
    assert set(alone) == {"CL", "UNASSIGNED"}


def test_review_partition_example():
    devices, flags = [], []
    for i in range(32):
        devices.append(dev(f"w{i}", state="proposed", origin="ai" if i < 3 else "detector"))
        if i < 5:
            flags.append(flag(f"w{i}", "ai_only_candidate" if i < 3 else "duplicate_warning"))
    devices += [dev(f"a{i}") for i in range(18)]
    devices += [dev(f"r{i}", state="rejected") for i in range(4)]
    devices.append(dev("m0", origin="manual", accept_kind="manual"))
    flags.append(flag("w10", "not_redetected", severity="info"))  # info flags do not make a proposal "flagged"
    flags.append(flag("w11", "duplicate_warning", open=False))  # closed flags never count
    review = C.derive(devices, flags)["review"]
    assert review["detected"] == 54
    assert review["accepted_detected"] == 18
    assert review["rejected"] == 4
    assert review["awaiting"] == 32
    assert review["awaiting_unflagged"] == 27
    assert review["awaiting_flagged"] == 5
    assert review["manual"] == 1
    assert review["official_total"] == 19
    assert review["detected_ai_only"] == 3
    assert review["accepted_with_open_flags"] == 0


def test_review_identities_hold_for_random_states():
    rng = random.Random(20261025)
    devices, flags = [], []
    for i in range(500):
        d = dev(f"d{i}", state=rng.choice(["proposed", "accepted", "rejected"]),
                origin=rng.choice(["detector", "ai", "manual"]), deleted=rng.random() < 0.1,
                sheet=rng.choice([SHEET, "A2-111"]))
        devices.append(d)
        for n in range(rng.choice([0, 0, 1, 2])):
            flags.append(flag(d.id, rng.choice(F.VALIDATION_TYPES + F.DECISION_TYPES),
                              severity=rng.choice(["review", "info"]), open=rng.random() < 0.7, n=n))
    got = C.derive(devices, flags)
    r = got["review"]

    assert r["detected"] == r["accepted_detected"] + r["rejected"] + r["awaiting"]
    assert r["awaiting"] == r["awaiting_unflagged"] + r["awaiting_flagged"]
    assert r["official_total"] == r["accepted_detected"] + r["manual"]
    assert 0 < r["detected_ai_only"] <= r["detected"]
    assert r["official_total"] == sum(s["total"] for s in got["official"].values())
    for s in got["official"].values():
        assert s["total"] == sum(s["by_origin"].values())
        assert s["total"] == sum(n for subs in s["by_category"].values() for n in subs.values())
        assert s["total"] == sum(n for cats in s["by_room"].values() for subs in cats.values() for n in subs.values())

    # K and W_unflagged recomputed here from the spec's words, not with the production helpers:
    # "flagged" = an open flag of severity review, or an open ai_only_candidate / model_disagrees (never bulk).
    def flagged(device_id):
        return any(f.open and f.device_id == device_id
                   and (f.severity == "review" or f.type in ("ai_only_candidate", "model_disagrees")) for f in flags)

    live = [d for d in devices if not d.deleted]
    awaiting = [d for d in live if d.origin in ("detector", "ai") and d.review_state == "proposed"]
    unflagged = [d for d in awaiting if not flagged(d.id)]
    assert r["awaiting_unflagged"] == len(unflagged)  # the bulk button's N is W_unflagged
    assert 0 < len(unflagged) < len(awaiting)
    assert r["awaiting_unflagged"] == sum(F.bulk_eligible(d, flags) for d in awaiting)  # and the gate agrees
    accepted = [d for d in live if d.review_state == "accepted"]
    k = [d for d in accepted if flagged(d.id)]
    assert r["accepted_with_open_flags"] == len(k)
    assert 0 < len(k) < len(accepted)
    # proposed and rejected devices with open review flags exist, and never reach K
    assert any(flagged(d.id) and d.review_state != "accepted" for d in live)
    assert r["official_total"] == len(accepted)  # every accepted, non-deleted device, whatever its origin


def test_review_by_sheet_holds_the_identities_and_sums_to_the_document():
    rng = random.Random(20261026)
    sheets = [SHEET, "A2-111", "A3-111"]
    devices, flags = [], []
    for i in range(400):
        d = dev(f"d{i}", state=rng.choice(["proposed", "accepted", "rejected"]),
                origin=rng.choice(["detector", "ai", "manual"]), deleted=rng.random() < 0.1, sheet=rng.choice(sheets))
        devices.append(d)
        for n in range(rng.choice([0, 0, 1])):
            flags.append(flag(d.id, rng.choice(F.VALIDATION_TYPES + F.DECISION_TYPES),
                              severity=rng.choice(["review", "info"]), open=rng.random() < 0.7, n=n))
    got = C.derive(devices, flags)
    doc, per = got["review"], got["review_by_sheet"]
    assert set(per) == {d.sheet_id for d in devices if not d.deleted}
    for sid, r in per.items():
        assert r["detected"] == r["accepted_detected"] + r["rejected"] + r["awaiting"]
        assert r["awaiting"] == r["awaiting_unflagged"] + r["awaiting_flagged"]
        assert r["official_total"] == r["accepted_detected"] + r["manual"]
        # the sheet's unflagged figure is that sheet's bulk-eligible proposals (the page's per-sheet N)
        here = [d for d in devices if d.sheet_id == sid and not d.deleted and d.origin in ("detector", "ai")
                and d.review_state == "proposed"]
        assert r["awaiting_unflagged"] == sum(F.bulk_eligible(d, [f for f in flags if f.open and f.device_id == d.id])
                                              for d in here)
        assert r["official_total"] == got["official"].get(sid, {}).get("total", 0)
    for key in doc:  # every figure is additive over the sheets
        assert sum(r[key] for r in per.values()) == doc[key], key
    assert all(0 < per[s]["awaiting_unflagged"] for s in sheets)


def test_k_and_unflagged_hand_built():
    devices = [dev("acc-review"), dev("acc-info"), dev("acc-md-info"), dev("acc-closed"),
               dev("p-review", state="proposed"), dev("p-info", state="proposed"),
               dev("p-ai-info", state="proposed", origin="ai"), dev("p-clean", state="proposed"),
               dev("r-review", state="rejected")]
    flags = [flag("acc-review", "not_redetected"), flag("acc-info", "not_redetected", severity="info"),
             flag("acc-md-info", "model_disagrees", severity="info"), flag("acc-closed", "no_room", open=False),
             flag("p-review", "ambiguous_match"), flag("p-info", "not_redetected", severity="info"),
             flag("p-ai-info", "ai_only_candidate", severity="info"), flag("r-review", "classification_conflict")]
    review = C.derive(devices, flags)["review"]
    assert review["accepted_with_open_flags"] == 2  # acc-review, acc-md-info
    assert (review["awaiting_unflagged"], review["awaiting_flagged"]) == (2, 2)  # p-info, p-clean | p-review, p-ai-info


def test_room_rows_disambiguate_over_sheet_rooms():
    cl_west = {"kind": "labeled", "name": "CL", "label_bbox_pt": [200.0, 300.0, 215.0, 309.0]}
    cl_east = {"kind": "labeled", "name": "CL", "label_bbox_pt": [700.0, 300.0, 715.0, 309.0]}
    rooms = {SHEET: [{**KITCHEN, "bbox": None}, {**cl_west, "bbox": None}, {**cl_east, "bbox": None}]}
    # only the west CL has a counted device; the sheet still has two CL labels, so the row name is stable
    devices = [dev("w1", room=cl_west), dev("e1", room=cl_east, state="proposed")]
    assert set(C.derive(devices, [], rooms)["official"][SHEET]["by_room"]) == {"CL @(200,300)"}
    assert set(C.derive(devices, [])["official"][SHEET]["by_room"]) == {"CL"}  # fallback: counted devices only
