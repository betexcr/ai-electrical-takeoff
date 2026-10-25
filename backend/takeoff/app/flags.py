"""Review flags: which devices need a human look, what blocks an acceptance, what a bulk accept may take.

Two kinds of flag:

* **Condition flags** (``VALIDATION_TYPES``) describe the *current* state of a device (a duplicate nearby, an
  uncertain or missing room). They are recomputed from state and open or close by themselves.
* **Decision flags** (``DECISION_TYPES``) are opened by a run (conflicts, disagreement with the model, ...)
  and are closed only by a reviewer. ``sync_validation`` never touches them.

Pure module: no store, no I/O, inputs are never mutated.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Iterable

from takeoff.app.models import Device, Flag
from takeoff.app.rooms import ROOM_PARAMS, propose_room
from takeoff.groundtruth.models import utcnow
from takeoff.groundtruth.protocol import DUPLICATE_TOLERANCE_PT

VALIDATION_TYPES = ("duplicate_warning", "room_ambiguous", "no_room")
DECISION_TYPES = ("classification_conflict", "ambiguous_match", "not_redetected", "model_disagrees",
                  "ai_only_candidate")

CONDITION_CLEARED = "condition cleared"
SYSTEM_ACTOR = "system"


def _duplicates(devices: list[Device]) -> dict[str, dict]:
    """Non-rejected, same-category devices on one sheet within the duplicate tolerance, flagged on both sides."""
    groups: dict[tuple, list[Device]] = defaultdict(list)
    for d in devices:
        if d.review_state != "rejected" and d.point_pt is not None:
            groups[(d.document_id, d.sheet_id, d.category)].append(d)
    found: dict[str, dict[str, float]] = defaultdict(dict)  # device id -> {partner id: distance}
    for group in groups.values():
        group.sort(key=lambda d: (d.point_pt[0], d.id))
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if b.point_pt[0] - a.point_pt[0] > DUPLICATE_TOLERANCE_PT:
                    break  # sorted by x: nothing further right can be close enough
                dist = math.dist(a.point_pt, b.point_pt)
                if dist <= DUPLICATE_TOLERANCE_PT:
                    found[a.id][b.id] = dist
                    found[b.id][a.id] = dist
    return {did: {"other_device_ids": sorted(p), "distance_pt": round(min(p.values()), 3)}
            for did, p in found.items()}


def conditions(devices: Iterable[Device], rooms: list[dict], params: dict = ROOM_PARAMS) -> dict[str, dict[str, dict]]:
    """``{device_id: {flag_type: details}}`` for the condition flags that hold now, over non-deleted devices.

    * ``duplicate_warning``: another non-rejected device of the same category (same sheet) within
      ``DUPLICATE_TOLERANCE_PT``, inclusive. Flagged on both; a rejected device is neither flagged nor a partner.
    * ``room_ambiguous``: the room is still the proposed nearest label and that proposal is ambiguous. Once a
      reviewer sets the room (``room_source == "reviewer"``) this clears.
    * ``no_room``: the device has no room.

    Devices with no condition are absent from the result.
    """
    live = [d for d in devices if not d.deleted]
    out: dict[str, dict[str, dict]] = {}
    for did, details in _duplicates(live).items():
        out.setdefault(did, {})["duplicate_warning"] = details
    for d in live:
        if d.room is None:
            out.setdefault(d.id, {})["no_room"] = {}
        elif d.room_source == "proposed_nearest_label" and d.point_pt is not None:
            proposal = propose_room(d.point_pt, rooms, params)
            if proposal.ambiguous:
                out.setdefault(d.id, {})["room_ambiguous"] = {
                    "reason": proposal.reason,
                    "nearest": [[name, round(dist, 3)] for name, dist in proposal.nearest],
                }
    return out


def _flag_id(device_id: str, flag_type: str, taken: Collection[str]) -> str:
    base = f"cond:{device_id}:{flag_type}"
    if base not in taken:
        return base
    n = 2
    while f"{base}:{n}" in taken:
        n += 1
    return f"{base}:{n}"


def sync_validation(open_flags: Iterable[Flag], current: dict[str, dict[str, dict]], *,
                    devices: Iterable[Device] = (), taken_ids: Collection[str] = (),
                    now: str | None = None) -> tuple[list[Flag], list[Flag]]:
    """Reconcile open condition flags with ``current`` (from :func:`conditions`): ``(to_open, to_close)``.

    ``to_open`` are new flags for conditions that hold with no open flag yet; ``to_close`` are copies of open
    condition flags whose condition no longer holds (``open=False``, ``resolution="condition cleared"``).
    Open flags whose condition still holds are left as they are, and decision flags are never touched.

    New ids are ``cond:{device_id}:{type}``, with ``:2``, ``:3``... appended when that id is in ``taken_ids``
    (the service passes the ids of closed flags it already stores). ``devices`` supplies ``document_id`` and
    ``sheet_id`` for new flags and must cover every device in ``current`` that needs a flag opened.
    """
    now = now or utcnow()
    existing = {(f.device_id, f.type) for f in open_flags if f.open and f.type in VALIDATION_TYPES}
    by_id = {d.id: d for d in devices}
    taken = set(taken_ids)

    to_open: list[Flag] = []
    for device_id in sorted(current):
        for flag_type in VALIDATION_TYPES:
            if flag_type not in current[device_id] or (device_id, flag_type) in existing:
                continue
            device = by_id.get(device_id)
            if device is None:
                raise ValueError(f"device {device_id!r} is needed to open a {flag_type} flag but was not supplied")
            fid = _flag_id(device_id, flag_type, taken)
            taken.add(fid)
            to_open.append(Flag(id=fid, device_id=device_id, document_id=device.document_id,
                                sheet_id=device.sheet_id, type=flag_type, severity="review",
                                details=dict(current[device_id][flag_type]), created_at=now))

    to_close = [f.model_copy(update={"open": False, "resolution": CONDITION_CLEARED, "resolved_by": SYSTEM_ACTOR,
                                     "resolved_at": now})
                for f in open_flags
                if f.open and f.type in VALIDATION_TYPES and f.type not in current.get(f.device_id, {})]
    return to_open, to_close


def bulk_eligible(device: Device, open_flags: Iterable[Flag]) -> bool:
    """A bulk accept may take only a proposed, live device with no open ``review`` flag of any type."""
    if device.deleted or device.review_state != "proposed":
        return False
    return not any(f.open and f.severity == "review" and f.device_id == device.id for f in open_flags)


def acceptance_blockers(device_id: str, open_flags: Iterable[Flag], reason: str | None) -> list[Flag]:
    """Open flags that stop an *individual* acceptance of the device.

    The open ``review`` condition flags always block (fix the room, resolve the duplicate); an open
    ``model_disagrees`` blocks unless the reviewer gives a ``reason``. The other decision flags do not block:
    accepting the device is the reviewer's decision on them, and it closes them.
    """
    has_reason = bool(reason and reason.strip())
    out = []
    for f in open_flags:
        if not f.open or f.device_id != device_id:
            continue
        if f.type in VALIDATION_TYPES and f.severity == "review":
            out.append(f)
        elif f.type == "model_disagrees" and not has_reason:
            out.append(f)
    return out
