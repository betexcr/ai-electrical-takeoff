"""Room proposal: the nearest room label to a device, flagged when that is not clear.

This only *proposes* a room for a new device; the reviewer corrects it. Room polygons are not
extracted, so this is a heuristic on label positions and room detection is never claimed to be
reliable. Only ``labeled`` rooms compete; Exterior is never proposed (the reviewer picks it).
Parameters are calibrated on A1/A2 from an explicit verified export (``tools/eval_rooms.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from takeoff.groundtruth import sheets as S

# max_label_distance_pt: 95th percentile of d1 (distance to the nearest label) over the A1/A2 devices
# whose reviewed room is a labeled room and whose nearest label is that room, rounded up to a multiple
# of 10. Reproduce with `uv run python tools/eval_rooms.py --export ../data/ground_truth/exports/<id>`.
ROOM_PARAMS = {
    "ambiguity_ratio": 1.25,
    "max_label_distance_pt": 100.0,
    "calibrated_on": {
        "export_id": "gt_001_20261025T020009Z",
        "manifest_sha256": "f03b11a38264a348e700b618f3342fed64eb30589a922dee715ae07852b14a1f",
        "sheets": ["A1-111", "A2-111"],
        "percentile": 95,
        "n": 84,
        "date": "2026-10-25",
    },
}


@dataclass
class RoomProposal:
    room: dict | None
    ambiguous: bool
    reason: str | None
    nearest: list[tuple[str, float]] = field(default_factory=list)


def label_distance_pt(point_pt, label_bbox_pt) -> float:
    """Euclidean distance from a point to a label box; 0 inside (or on) the box."""
    x, y = point_pt
    x0, y0, x1, y1 = label_bbox_pt
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def _ranked(point_pt, rooms) -> list[tuple[float, dict]]:
    """Labeled rooms by (distance, name, box): deterministic even on ties."""
    scored = [(label_distance_pt(point_pt, r["label_bbox_pt"]), r) for r in rooms
              if r.get("kind") == "labeled" and r.get("label_bbox_pt")]
    return sorted(scored, key=lambda t: (t[0], t[1]["name"], tuple(t[1]["label_bbox_pt"])))


def propose_room(point_pt, rooms, params=ROOM_PARAMS) -> RoomProposal:
    ranked = _ranked(point_pt, rooms)
    nearest = [(r["name"], d) for d, r in ranked[:2]]
    if not ranked:
        return RoomProposal(dict(S.ROOM_UNLABELED), True, "far_from_labels", nearest)
    d1, best = ranked[0]
    if d1 > params["max_label_distance_pt"]:
        return RoomProposal(dict(S.ROOM_UNLABELED), True, "far_from_labels", nearest)
    if len(ranked) > 1 and ranked[1][0] <= params["ambiguity_ratio"] * d1:
        return RoomProposal(best, True, "two_labels_close", nearest)
    return RoomProposal(best, False, None, nearest)
