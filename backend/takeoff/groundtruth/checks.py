"""Computed warnings, progress and phase-gate conditions.

Warnings are derived on demand from the current state (never stored), with
deterministic ids so a reviewer's acknowledgement (WarningAck) stays attached
to the same issue. Tallies here are review progress, not detector results.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations

from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth.protocol import DUPLICATE_TOLERANCE_PT, MATCH_TOLERANCE_PT
from takeoff.groundtruth.sheets import SheetSpec
from takeoff.groundtruth.store import Store


def _pt(a) -> tuple[float, float]:
    return (a.point.u, a.point.v)


def candidate_clusters(store: Store, spec: SheetSpec) -> list[dict]:
    cands = store.list("candidates", spec.sheet_id)
    runs = {r.id: r for r in store.list("candidate_runs")}
    reviews = {r.candidate_id: r for r in store.list("candidate_reviews")}
    clusters = CA.cluster_candidates(cands, runs, {spec.sheet_id: spec.geometry})
    for cl in clusters:
        for m in cl["members"]:
            r = reviews.get(m["candidate_id"])
            m["outcome"] = r.outcome if r else "unreviewed"
            m["annotation_id"] = r.annotation_id if r else None
        cl["unreviewed"] = sum(1 for m in cl["members"] if m["outcome"] == "unreviewed")
        cl["cell_id"] = spec.cell_at((cl["point"]["u"], cl["point"]["v"]))
    return clusters


def warnings(store: Store, spec: SheetSpec) -> list[dict]:
    anns = store.list("annotations", spec.sheet_id)
    acks = {a.id: a for a in store.list("warning_acks", spec.sheet_id)}
    out = []
    for a, b in combinations(anns, 2):
        d = spec.distance_pt(_pt(a), _pt(b))
        if d <= DUPLICATE_TOLERANCE_PT:
            same = a.category == b.category
            lo, hi = sorted((a.id, b.id))
            out.append({"id": f"dup:{lo}:{hi}", "type": "possible_duplicate" if same else "possible_combined_or_duplicate",
                        "message": (f"two {a.category} annotations {d:.1f} pt apart" if same else
                                    f"{a.category} and {b.category} {d:.1f} pt apart (combined fixture counts once)"),
                        "entity_ids": [lo, hi], "point": a.point.model_dump()})
    for a in anns:
        if not spec.in_region(_pt(a)):
            out.append({"id": f"outside:{a.id}", "type": "outside_region",
                        "message": "annotation lies outside the electrical-region hypothesis",
                        "entity_ids": [a.id], "point": a.point.model_dump()})
        conflicts = a.token_conflicts()
        if conflicts:
            out.append({"id": f"token:{a.id}", "type": "token_conflict", "message": "; ".join(conflicts),
                        "entity_ids": [a.id], "point": a.point.model_dump()})
    claimed = defaultdict(list)
    for a in anns:
        for t in a.text_tokens:
            claimed[(t.text, tuple(round(v, 2) for v in t.bbox_pt))].append(a.id)
    for (text, bb), ids in claimed.items():
        if len(ids) > 1:
            out.append({"id": f"tokshare:{text}:{','.join(f'{v:.2f}' for v in bb)}", "type": "token_shared",
                        "message": f"token {text!r} is linked to {len(ids)} annotations",
                        "entity_ids": sorted(ids), "point": None})
    for cl in candidate_clusters(store, spec):
        if cl["unreviewed"] and not any(spec.distance_pt((cl["point"]["u"], cl["point"]["v"]), _pt(a)) <= MATCH_TOLERANCE_PT
                                        for a in anns):
            out.append({"id": f"orphan:{cl['id']}", "type": "orphan_candidate",
                        "message": f"unreviewed candidate cluster ({cl['unreviewed']} hypotheses) with no annotation nearby",
                        "entity_ids": [m["candidate_id"] for m in cl["members"]], "point": cl["point"]})
    for w in out:
        ack = acks.get(w["id"])
        w["acknowledged"] = bool(ack and not ack.deleted)
        w["ack_reason"] = ack.reason if w["acknowledged"] else None
    return out


def progress(store: Store, spec: SheetSpec) -> dict:
    """Full progress. Includes annotation tallies, so it is never served during a blind scan."""
    cells = store.list("cells", spec.sheet_id)
    anns = store.list("annotations", spec.sheet_id)
    obs = store.list("observations", spec.sheet_id)
    clusters = candidate_clusters(store, spec)
    tallies = Counter(f"{a.category}/{a.subtype}" for a in anns)
    return {
        "cells_total": len(cells),
        "cells_pass1_done": sum(c.pass1_done for c in cells),
        "cells_blind_scan_done": sum(c.blind_scan_done for c in cells),
        "annotations": len(anns),
        "annotations_by_state": dict(Counter(a.review_state for a in anns)),
        "annotations_by_blind_confirmation": dict(Counter(a.blind_confirmation or "undecided" for a in anns)),
        "candidate_clusters": len(clusters),
        "candidates_unreviewed": sum(cl["unreviewed"] for cl in clusters),
        "observations": len(obs),
        "observations_by_reconciliation": dict(Counter(o.reconciliation for o in obs)),
        "tallies": dict(sorted(tallies.items())),
    }


def blind_progress(store: Store, spec: SheetSpec) -> dict:
    """The only progress shown during a blind scan: no annotation or candidate information."""
    cells = store.list("cells", spec.sheet_id)
    return {"cells_total": len(cells), "cells_blind_scan_done": sum(c.blind_scan_done for c in cells),
            "observations": len(store.list("observations", spec.sheet_id))}


def gate_pass1_to_blind(store: Store, spec: SheetSpec) -> list[str]:
    sheet = store.get("sheets", spec.sheet_id)
    reasons = []
    if not sheet.region_confirmed:
        reasons.append("electrical region not confirmed" + (f" (flagged: {sheet.region_flag_reason})" if sheet.region_flag_reason else ""))
    undone = [c.id for c in store.list("cells", spec.sheet_id) if not c.pass1_done]
    if undone:
        reasons.append(f"{len(undone)} cells not done in pass 1")
    unrev = sum(cl["unreviewed"] for cl in candidate_clusters(store, spec))
    if unrev:
        reasons.append(f"{unrev} unreviewed candidates")
    return reasons


def gate_blind_to_reconcile(store: Store, spec: SheetSpec) -> list[str]:
    undone = [c.id for c in store.list("cells", spec.sheet_id) if not c.blind_scan_done]
    return [f"{len(undone)} cells not blind-scanned"] if undone else []


def gate_reconcile_to_verified(store: Store, spec: SheetSpec) -> list[str]:
    reasons = []
    anns = store.list("annotations", spec.sheet_id)
    pending = [o for o in store.list("observations", spec.sheet_id) if o.reconciliation == "pending"]
    if pending:
        reasons.append(f"{len(pending)} blind observations not reconciled")
    undecided = [a for a in anns if a.blind_confirmation is None]
    if undecided:
        reasons.append(f"{len(undecided)} annotations not seen in the blind scan still undecided")
    by_state = Counter(a.review_state for a in anns)
    if by_state.get("draft"):
        reasons.append(f"{by_state['draft']} annotations still draft")
    if by_state.get("needs_adjudication"):
        reasons.append(f"{by_state['needs_adjudication']} annotations need adjudication")
    open_w = [w for w in warnings(store, spec) if not w["acknowledged"]]
    if open_w:
        reasons.append(f"{len(open_w)} warnings neither resolved nor acknowledged")
    return reasons
