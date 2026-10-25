"""Shared fixtures for the TakeoffService tests: a temporary store and a stub ``views``.

The stub sheet is unrotated, 1200 x 1200 pt, with an electrical region of 0-1000 pt (so a point can be on
the page but outside the region) and two labeled rooms, plus Exterior and Unlabeled.
"""

from __future__ import annotations

import copy

from takeoff.app import models as TM
from takeoff.app.service import TakeoffService
from takeoff.app.store import TakeoffStore
from takeoff.groundtruth import sheets as S

DOC = "d" * 64
SHEET = "A1-111"
PAGE_PT = 1200.0
REGION_PT = [0.0, 0.0, 1000.0, 1000.0]
KITCHEN = {"kind": "labeled", "name": "Kitchen", "label_bbox_pt": [100.0, 100.0, 160.0, 112.0], "bbox": None}
HALL = {"kind": "labeled", "name": "Hall", "label_bbox_pt": [600.0, 100.0, 640.0, 112.0], "bbox": None}
# Two labels with the same text, far from everything else (like the two "CL" on A1).
CL_WEST = {"kind": "labeled", "name": "CL", "label_bbox_pt": [300.0, 900.0, 315.0, 909.0], "bbox": None}
CL_EAST = {"kind": "labeled", "name": "CL", "label_bbox_pt": [700.0, 900.0, 715.0, 909.0], "bbox": None}
ROOMS = [KITCHEN, HALL, CL_WEST, CL_EAST, S.ROOM_EXTERIOR, S.ROOM_UNLABELED]
NEAR_KITCHEN = (130.0, 150.0)  # 38 pt below the Kitchen label, far from Hall: a clear proposal
NEAR_HALL = (580.0, 150.0)  # 43 pt from the Hall label, far from Kitchen
FAR_FROM_LABELS = (380.0, 600.0)  # > 100 pt from every label: proposed Unlabeled, room_ambiguous
RUN_ID = "run-001"
ACTOR = "reviewer@example.com"


def views(document_id: str, sheet_id: str) -> dict:
    if (document_id, sheet_id) != (DOC, SHEET):
        raise KeyError((document_id, sheet_id))
    return {
        "sheet_id": SHEET, "page_number": 1,
        "geometry": {"crop_native": [0.0, 0.0, PAGE_PT, PAGE_PT], "rotation": 0,
                     "displayed_width_pt": PAGE_PT, "displayed_height_pt": PAGE_PT},
        "region": {"bbox_pt": list(REGION_PT)},
        "rooms": copy.deepcopy(ROOMS), "tokens": [], "image": {},
    }


def norm(pt) -> dict:
    return {"u": pt[0] / PAGE_PT, "v": pt[1] / PAGE_PT}


def room_ref(room: dict | None) -> dict | None:
    return None if room is None else {k: room[k] for k in ("kind", "name", "label_bbox_pt")}


def make(tmp_path):
    store = TakeoffStore(tmp_path / "takeoff.sqlite")
    store.insert("sheets", TM.SheetInfo(id=f"{DOC}:1", document_id=DOC, page_number=1, sheet_id=SHEET,
                                        region_bbox_pt=tuple(REGION_PT), legend_found=True), "system")
    store.insert("runs", TM.Run(id=RUN_ID, document_id=DOC, detector="det/0", detector_version="1",
                                created_by="system"), "system")
    return store, TakeoffService(store, views)


def seed_detection(store, id, pt=NEAR_KITCHEN, category="switch", subtype="standard", source="det0", **kw):
    det = TM.RunDetection(id=id, run_id=RUN_ID, document_id=DOC, sheet_id=SHEET, source=source, category=category,
                          subtype=subtype, status="as_drawn", point=norm(pt), point_pt=pt, **kw)
    return store.insert("detections", det, "system")


def seed_device(store, id, pt=NEAR_KITCHEN, category="switch", subtype="standard", state="proposed",
                origin="detector", room=KITCHEN, detection=None, **kw):
    """A device as a merge would leave it, optionally with its ``created`` evidence."""
    base = dict(id=id, document_id=DOC, sheet_id=SHEET, category=category, subtype=subtype,
                status="optional_pre_wire" if category == "ceiling_fan" else "as_drawn",
                point=norm(pt), point_pt=pt, room=room_ref(room),
                room_source="proposed_nearest_label" if room is not None else None,
                review_state=state, origin=origin, created_by_run=RUN_ID if origin != "manual" else None,
                accept_kind=("manual" if origin == "manual" else "accepted") if state == "accepted" else None)
    base.update(kw)
    dev = store.insert("devices", TM.Device(**base), "system")
    if detection is not None:
        store.insert("evidence", TM.Evidence(id=f"ev:{id}", device_id=id, detection_id=detection.id, run_id=RUN_ID,
                                             outcome="created", distance_pt=0.0), "system")
    return dev


def seed_flag(store, device, type, severity="review", id=None, **details):
    fl = TM.Flag(id=id or f"{RUN_ID}:{device.id}:{type}", device_id=device.id, document_id=DOC, sheet_id=SHEET,
                 type=type, severity=severity, details=details, run_id=RUN_ID)
    return store.insert("flags", fl, "system")


def flags_of(store, device_id, open_only=True):
    return [f for f in store.list("flags") if f.device_id == device_id and (f.open or not open_only)]


def flag_types(store, device_id):
    return sorted(f.type for f in flags_of(store, device_id))
