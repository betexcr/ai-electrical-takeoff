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

    live = [d for d in devices if not d.deleted]
    open_by = {}
    for f in flags:
        if f.open:
            open_by.setdefault(f.device_id, []).append(f)
    awaiting = [d for d in live if d.origin != "manual" and d.review_state == "proposed"]
    # the bulk button's N is W_unflagged
    assert r["awaiting_unflagged"] == sum(F.bulk_eligible(d, open_by.get(d.id, [])) for d in awaiting)
    accepted = [d for d in live if d.review_state == "accepted"]
    k = sum(1 for d in accepted if any(f.severity == "review" or f.type in F.BULK_NEVER_TYPES
                                       for f in open_by.get(d.id, [])))
    assert r["accepted_with_open_flags"] == k
    assert 0 < k <= len(accepted)
    # proposed and rejected devices with open review flags exist, and never reach K
    assert any(open_by.get(d.id) and d.review_state != "accepted" for d in live)
    assert r["official_total"] == len(accepted)  # every accepted, non-deleted device, whatever its origin
