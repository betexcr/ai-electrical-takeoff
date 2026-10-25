"""Every takeoff figure, derived from saved device records (spec §4.17, §5.5). Nothing is cached or stored.

* **Official totals** come only from ``review_state == "accepted"`` devices that are not deleted, both
  automated and manual, per sheet, room, category and subtype, and are reported by origin as
  ``detector_accepted``, ``ai_accepted`` and ``manual``. Proposed, rejected and deleted devices never count.
* **Review figures** partition the non-deleted devices exactly as §4.17 defines them:
  ``detected = accepted_detected + rejected + awaiting``, ``awaiting = awaiting_unflagged + awaiting_flagged``,
  ``official_total = accepted_detected + manual``, ``detected_ai_only <= detected``.

"Flagged" is the bulk-accept gate (:func:`takeoff.app.flags.bulk_eligible`): an open ``review`` flag, or an open
``ai_only_candidate`` / ``model_disagrees`` whatever its severity. So the bulk button's N is ``awaiting_unflagged``.
Pure module: no store, no I/O.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from takeoff.app.flags import BULK_NEVER_TYPES, bulk_eligible
from takeoff.app.models import Device, Flag
from takeoff.groundtruth.export import room_key

AUTOMATED = ("detector", "ai")
REPORTED_ORIGIN = {"detector": "detector_accepted", "ai": "ai_accepted", "manual": "manual"}


def needs_review(flag: Flag) -> bool:
    """An open flag that keeps a proposal out of bulk accept, and marks an accepted device as still flagged."""
    return flag.open and (flag.severity == "review" or flag.type in BULK_NEVER_TYPES)


def _sorted_subtypes(c: Counter) -> dict:
    return dict(sorted(c.items(), key=lambda kv: (kv[0] is None, kv[0] or "")))


def sheet_room_key(device: Device, sheet_rooms: list[dict]) -> str:
    """``groundtruth.export.room_key``'s row name, disambiguated over the sheet's own room labels.

    Two labels with the same text on a sheet (the two "CL" on A1) are separate rows, keyed by label position,
    whether or not both have counted devices, so a row's name does not change as devices are accepted.
    """
    room = device.room
    if room is None:
        return "UNASSIGNED"
    if room.kind != "labeled":
        return room.name
    same = {tuple(float(x) for x in r["label_bbox_pt"]) for r in sheet_rooms
            if r.get("kind") == "labeled" and r.get("name") == room.name and r.get("label_bbox_pt")}
    same.add(tuple(float(x) for x in room.label_bbox_pt))
    if len(same) <= 1:
        return room.name
    x0, y0 = room.label_bbox_pt[:2]
    return f"{room.name} @({x0:.0f},{y0:.0f})"


def _official(accepted: list[Device], rooms: Mapping[str, list[dict]] | None) -> dict:
    out: dict = {}
    for sheet in sorted({d.sheet_id for d in accepted}):
        here = [d for d in accepted if d.sheet_id == sheet]
        sheet_rooms = (rooms or {}).get(sheet)

        def key(d: Device) -> str:
            # without the sheet's rooms, fall back to disambiguating over the counted devices
            return room_key(d, here) if sheet_rooms is None else sheet_room_key(d, sheet_rooms)

        by_room: dict = defaultdict(lambda: defaultdict(Counter))
        by_cat: dict = defaultdict(Counter)
        by_origin = {v: 0 for v in REPORTED_ORIGIN.values()}
        for d in here:
            by_room[key(d)][d.category][d.subtype] += 1  # two same-named labels are two rows
            by_cat[d.category][d.subtype] += 1
            by_origin[REPORTED_ORIGIN[d.origin]] += 1
        out[sheet] = {
            "by_room": {r: {c: _sorted_subtypes(s) for c, s in sorted(cats.items())}
                        for r, cats in sorted(by_room.items())},
            "by_category": {c: _sorted_subtypes(s) for c, s in sorted(by_cat.items())},
            "total": len(here),
            "by_origin": by_origin,
        }
    return out


def derive(devices: Iterable[Device], open_flags: Iterable[Flag],
           rooms: Mapping[str, list[dict]] | None = None) -> dict:
    """``{"review": {...}, "official": {sheet_id: {...}}}`` for one document's devices and flags.

    ``open_flags`` may contain closed flags; they are ignored. ``rooms`` maps a sheet id to that sheet's rooms
    (``sheet_view(...)["rooms"]``); room rows are disambiguated over them when given.
    """
    live = [d for d in devices if not d.deleted]
    flags_by_device: dict[str, list[Flag]] = defaultdict(list)
    for f in open_flags:
        if f.open:
            flags_by_device[f.device_id].append(f)

    automated = [d for d in live if d.origin in AUTOMATED]
    awaiting = [d for d in automated if d.review_state == "proposed"]
    unflagged = sum(1 for d in awaiting if bulk_eligible(d, flags_by_device.get(d.id, ())))
    accepted = [d for d in live if d.review_state == "accepted"]
    accepted_detected = sum(1 for d in accepted if d.origin in AUTOMATED)
    # Manual devices are accepted on creation and never rejected; counting accepted ones keeps
    # official_total == accepted_detected + manual even for a record that breaks that rule.
    manual = sum(1 for d in accepted if d.origin == "manual")
    review = {
        "detected": len(automated),
        "detected_ai_only": sum(1 for d in automated if d.origin == "ai"),
        "accepted_detected": accepted_detected,
        "rejected": sum(1 for d in automated if d.review_state == "rejected"),
        "awaiting": len(awaiting),
        "awaiting_unflagged": unflagged,
        "awaiting_flagged": len(awaiting) - unflagged,
        "manual": manual,
        "official_total": len(accepted),
        "accepted_with_open_flags": sum(1 for d in accepted
                                        if any(needs_review(f) for f in flags_by_device.get(d.id, ()))),
    }
    return {"review": review, "official": _official(accepted, rooms)}
