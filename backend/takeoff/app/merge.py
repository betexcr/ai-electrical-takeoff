"""``takeoff-merge/2``: how one run's detections join the living set of reviewable devices.

Detectors propose, reviewers decide. A run never changes the category, subtype, room, point or
review state of an existing device and never deletes anything: it only adds ``Evidence``, ``Flag``
records and new *proposed* devices. This module is pure (no store, no I/O, inputs are not mutated)
and deterministic: shuffled inputs give an equal ``MergeResult``, with ids derived from the run,
the detection and the device (spec §5.3, §5.9.2, §5.9.3).

A merge is for exactly one sheet: the detections and devices passed in must all share one
``sheet_id`` (``ValueError`` otherwise), and every non-deleted device needs a ``point_pt``.
Distances are Euclidean, in PDF points (``point_pt``), and the tolerance boundary is inclusive
(with a tiny epsilon so that float rounding cannot push an exact-tolerance match outside it).

**det/0 assignment is one-to-one, nearest first** (merge/2, final review): each detection looks at its
nearest live device within the tolerance. Claims on a device are settled by (distance, detection id,
device id); the winner matches it, and a detection whose nearest device went to a closer detection is
``ambiguous_match``. A detection is also ambiguous when its two nearest candidates are tied (equal
distance within :data:`TIE_PT`). So re-running the same detections over the devices they created adds
nothing: each finds its own device at distance 0. (merge/1 called every detection with two candidates
ambiguous, so each re-run of two close detections added a device.)

**A deleted detector or AI device blocks re-proposal** (delete is only for duplicates of those): a
detection that would otherwise create a new device, but has a deleted non-manual device within the
tolerance, creates nothing (its ``RunDetection`` row stays stored). Deleted manual devices are ignored.

A device counts as re-detected, and so never gets ``not_redetected``, when this run produced
same-position evidence for it: det/0 ``confirmed``, ``conflict`` or ``rejected_redetected``, or an AI
``ai_corroborates`` or discover ``conflict``. A device that is only a candidate of an ambiguous
detection is not re-detected.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from takeoff.app.models import Device, Evidence, Flag, ModelVerdict, Run, RunDetection
from takeoff.app.rooms import RoomProposal
from takeoff.groundtruth.models import RoomRef, utcnow
from takeoff.groundtruth.taxonomy import default_status

MATCH_TOL_PT = 6.0
TIE_PT = 1e-6  # two candidates whose distances differ by no more than this are tied (ambiguous)
_EPS = 1e-9
_ROOM_KEYS = ("kind", "name", "label_bbox_pt")


@dataclass
class MergeResult:
    new_devices: list[Device] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)


def _order(det: RunDetection) -> tuple:
    """det/0 first, then top-to-bottom, left-to-right, then id."""
    return (det.source == "ai_discover", det.point_pt[1], det.point_pt[0], det.id)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _possible(det: RunDetection, pool: list[Device], tol_pt: float) -> list[tuple[float, Device]]:
    """Devices within the tolerance (inclusive), nearest first. The pool is a single sheet's devices."""
    found = []
    for dev in pool:
        d = _dist(det.point_pt, dev.point_pt)
        if d <= tol_pt + _EPS:
            found.append((d, dev))
    return sorted(found, key=lambda t: (t[0], t[1].id))


def verify_usable_for(v: ModelVerdict, detection_id: str) -> bool:
    """Whether a verify verdict's answer for one detection may be used (ruling R15).

    A verdict without an error is usable for all its items. A per-item ``parse_error`` (the model left an
    item out or answered it twice; ``error["detection_ids"]`` lists those items and ``agreement`` omits them)
    stays usable for every other item of the batch. Any other error, a whole-request failure, is unusable.
    """
    if v.kind != "verify" or detection_id not in v.detection_ids:
        return False
    if v.error is None:
        return True
    bad = v.error.get("detection_ids")
    return (v.error.get("code") == "parse_error" and isinstance(bad, list) and v.agreement is not None
            and detection_id not in bad)


def _room_ref(prop: RoomProposal) -> RoomRef | None:
    """A copy of the proposed room as a ``RoomRef`` (the proposal may hold the caller's own dict)."""
    if prop.room is None:
        return None
    return RoomRef.model_validate({k: copy.deepcopy(prop.room.get(k)) for k in _ROOM_KEYS})


def merge_sheet(
    run: Run,
    detections: list[RunDetection],
    devices: list[Device],
    propose: Callable[[tuple[float, float]], RoomProposal],
    verdicts: list[ModelVerdict] | None = None,
    tol_pt: float = MATCH_TOL_PT,
    *,
    now: str | None = None,
) -> MergeResult:
    now = utcnow() if now is None else now
    sheets = {d.sheet_id for d in detections} | {d.sheet_id for d in devices}
    if len(sheets) > 1:
        raise ValueError(f"merge_sheet takes one sheet's detections and devices, got sheets {sorted(sheets)}")
    live = [d for d in devices if not d.deleted]
    unplaced = sorted(d.id for d in live if d.point_pt is None)
    if unplaced:
        raise ValueError(f"devices without point_pt cannot be matched: {unplaced}")
    # deleted duplicates of detections: they block re-proposal at their spot (never matched, never flagged)
    blockers = [d for d in devices if d.deleted and d.origin != "manual" and d.point_pt is not None]
    ordered = sorted(detections, key=_order)
    det0s = [d for d in ordered if d.source == "det0"]
    ais = [d for d in ordered if d.source == "ai_discover"]

    new_devices: dict[str, Device] = {}
    evidence: dict[str, Evidence] = {}
    flags: dict[str, Flag] = {}
    touched: set[str] = set()          # existing devices with same-position evidence from this run
    det_device: dict[str, Device] = {}  # det/0 detection id -> the device it created or touched

    def add_evidence(det: RunDetection, dev: Device, outcome: str, distance: float) -> None:
        ev = Evidence(id=f"{run.id}:{det.id}:ev:{dev.id}", device_id=dev.id, detection_id=det.id,
                      run_id=run.id, outcome=outcome, distance_pt=distance)
        evidence[ev.id] = ev

    def add_flag(subject: str, dev: Device, type_: str, severity: str, details: dict[str, Any]) -> None:
        fl = Flag(id=f"{run.id}:{subject}:{type_}", device_id=dev.id, document_id=dev.document_id,
                  sheet_id=dev.sheet_id, type=type_, severity=severity, details=details, run_id=run.id,
                  created_at=now)
        flags[fl.id] = fl

    def create(det: RunDetection, origin: str) -> Device:
        prop = propose(det.point_pt)
        room = _room_ref(prop)
        dev = Device(
            id=f"{run.id}:{det.id}", document_id=det.document_id, sheet_id=det.sheet_id,
            category=det.category, subtype=det.subtype,
            status=det.status or default_status(det.category),
            point=det.point.model_copy(), point_pt=det.point_pt, room=room,
            room_source="proposed_nearest_label" if room is not None else None,
            review_state="proposed", origin=origin, created_by_run=run.id, human_set=[],
            created_at=now, updated_at=now)
        new_devices[dev.id] = dev
        add_evidence(det, dev, "created", 0.0)
        return dev

    def blocked(det: RunDetection) -> bool:
        return bool(_possible(det, blockers, tol_pt))

    def ambiguous(det: RunDetection, poss: list[tuple[float, Device]]) -> None:
        det_device[det.id] = create(det, "detector")
        add_flag(det.id, det_device[det.id], "ambiguous_match", "review",
                 {"possible_device_ids": [d.id for _, d in poss]})

    # ---- det/0: one-to-one, nearest first -------------------------------------------------------
    claims: list[tuple[float, str, str, RunDetection, Device, list]] = []
    for det in det0s:
        poss = _possible(det, live, tol_pt)
        if not poss:
            if not blocked(det):
                det_device[det.id] = create(det, "detector")  # rule 5
            continue
        if len(poss) > 1 and poss[1][0] - poss[0][0] <= TIE_PT:
            ambiguous(det, poss)  # rule 4: the nearest candidates are tied
            continue
        dist, dev = poss[0]
        if dev.category != det.category:
            # rule 3: another category at the same spot; the reviewer decides, nothing new is created
            add_evidence(det, dev, "conflict", dist)
            add_flag(det.id, dev, "classification_conflict", "review",
                     {"run_category": det.category, "run_subtype": det.subtype, "detection_id": det.id,
                      "kind": "category"})
            touched.add(dev.id)
            det_device[det.id] = dev
            continue
        claims.append((dist, det.id, dev.id, det, dev, poss))

    taken: set[str] = set()
    for dist, _, dev_id, winner, dev, poss in sorted(claims, key=lambda t: t[:3]):
        if dev_id in taken:  # rule 4: its nearest device went to a closer detection
            if not blocked(winner):  # e.g. the deleted duplicate of that device
                ambiguous(winner, poss)
            continue
        taken.add(dev_id)
        touched.add(dev.id)
        det_device[winner.id] = dev
        if dev.review_state == "rejected":
            add_evidence(winner, dev, "rejected_redetected", dist)
        elif winner.subtype == dev.subtype:
            add_evidence(winner, dev, "confirmed", dist)
        else:
            add_evidence(winner, dev, "conflict", dist)
            add_flag(winner.id, dev, "classification_conflict", "review",
                     {"run_category": winner.category, "run_subtype": winner.subtype, "detection_id": winner.id})

    # ---- verify verdicts -------------------------------------------------------------------------
    usable = sorted((v for v in (verdicts or []) if v.kind == "verify"), key=lambda v: v.id)
    for det in det0s:
        target = det_device.get(det.id)
        if target is None or target.review_state == "rejected":  # None: blocked by a deleted duplicate
            continue
        for v in usable:
            if not verify_usable_for(v, det.id):  # per item: a batch's parse error spares its other items (R15)
                continue
            row = next((r for r in ((v.output or {}).get("verdicts") or []) if r.get("item_id") == det.id), None)
            ag = (v.agreement or {}).get(det.id)
            disagrees = (ag is not None and (ag.category is False or ag.subtype is False)) or (
                row is not None and (row.get("is_device") is False or row.get("uncertain") is True))
            if disagrees:
                add_flag(det.id, target, "model_disagrees", "review",
                         {"verdict_id": v.id, "ai_category": (row or {}).get("category"),
                          "ai_subtype": (row or {}).get("subtype"), "rationale": (row or {}).get("rationale"),
                          "source": "verify"})
                break

    # ---- ai_discover -----------------------------------------------------------------------------
    verdict_tile = {v.id: v.tile_id for v in (verdicts or []) if v.kind == "discover"}
    pool = [d for d in live]
    for det in ais:
        poss = _possible(det, pool + list(new_devices.values()), tol_pt)
        same = [p for p in poss if p[1].category == det.category]
        if not poss:
            if blocked(det):  # a deleted duplicate is never re-proposed
                continue
            dev = create(det, "ai")
            tile_id = next((s.tile_id for s in det.supporting if s.verdict_id == det.verdict_id), None)
            if tile_id is None:
                tile_id = verdict_tile.get(det.verdict_id) or (det.supporting[0].tile_id if det.supporting else None)
            add_flag(det.id, dev, "ai_only_candidate", "review",
                     {"verdict_id": det.verdict_id, "tile_id": tile_id, "rationale": det.rationale})
            continue
        dist, dev = same[0] if same else poss[0]
        touched.add(dev.id)  # AI corroboration or a discover conflict counts as re-detection (spec §5.3)
        rejected = dev.review_state == "rejected"
        if same:
            add_evidence(det, dev, "rejected_redetected" if rejected else "ai_corroborates", dist)
        else:
            add_evidence(det, dev, "conflict", dist)
            if not rejected:
                add_flag(det.id, dev, "model_disagrees", "review",
                         {"verdict_id": det.verdict_id, "ai_category": det.category, "ai_subtype": det.subtype,
                          "rationale": det.rationale, "source": "discover"})

    # ---- not re-detected ---------------------------------------------------------------------------
    for dev in live:
        if dev.review_state != "rejected" and dev.id not in touched:
            add_flag(dev.id, dev, "not_redetected", "review" if dev.origin == "detector" else "info", {})

    return MergeResult(
        new_devices=[new_devices[k] for k in sorted(new_devices)],
        evidence=[evidence[k] for k in sorted(evidence)],
        flags=[flags[k] for k in sorted(flags)])
