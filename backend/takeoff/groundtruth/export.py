"""Immutable, versioned ground-truth exports and the evaluator guard.

An export directory is written once and never overwritten. Its level is
``verified`` only when every sheet has passed all review gates; otherwise it
is ``incomplete``. Totals are derived from the exported annotations and never
entered by hand. ``require_verified`` is what any future evaluation must call
before computing a single metric.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from takeoff.groundtruth import models as M
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.protocol import PROTOCOL_VERSION
from takeoff.groundtruth.review import Workflow
from takeoff.groundtruth.store import STORE_VERSION

EXPORT_FORMAT = "gt-export/1"
KNOWN_LIMITATIONS = [
    "Single reviewer: pass 1 and the blind scan are done by the same person, so memory of pass 1 can leak "
    "into the blind scan; no inter-annotator agreement is available.",
    "Blind-scan consistency counts describe the review process, not detector accuracy.",
    "Room assignment is the reviewer's choice; room polygons are not yet extracted (B-06).",
    "Wiring runs are not annotated in B-03.",
]
CSV_FIELDS = ["id", "sheet_id", "page_number", "category", "subtype", "subtype_label", "status", "u", "v", "x_pt", "y_pt",
              "room_kind", "room_name", "text_tokens", "origin", "review_state", "blind_confirmation", "notes"]


class ExportError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def room_key(a: M.Annotation, annotations: list[M.Annotation]) -> str:
    """Room name, disambiguated when a sheet has two labels with the same text (e.g. two 'CL')."""
    if a.room is None:
        return "UNASSIGNED"
    if a.room.kind != "labeled":
        return a.room.name
    same = {tuple(x.room.label_bbox_pt) for x in annotations
            if x.sheet_id == a.sheet_id and x.room and x.room.kind == "labeled" and x.room.name == a.room.name}
    if len(same) <= 1:
        return a.room.name
    x0, y0 = a.room.label_bbox_pt[:2]
    return f"{a.room.name} @({x0:.0f},{y0:.0f})"


def derive_totals(annotations: list[M.Annotation]) -> dict:
    """sheet -> {by_room: room -> category -> subtype -> n, by_category: ..., total: n}."""
    live = [a for a in annotations if not a.deleted]
    out: dict = {}
    for sheet in sorted({a.sheet_id for a in live}):
        here = [a for a in live if a.sheet_id == sheet]
        by_room: dict = defaultdict(lambda: defaultdict(Counter))
        by_cat: dict = defaultdict(Counter)
        for a in here:
            by_room[room_key(a, here)][a.category][a.subtype] += 1
            by_cat[a.category][a.subtype] += 1
        out[sheet] = {
            "by_room": {r: {c: dict(sorted(s.items())) for c, s in sorted(cats.items())} for r, cats in sorted(by_room.items())},
            "by_category": {c: dict(sorted(s.items())) for c, s in sorted(by_cat.items())},
            "total": len(here),
        }
    return out


def _jsonl(models) -> bytes:
    return "".join(json.dumps(m.model_dump(mode="json"), sort_keys=True) + "\n" for m in models).encode()


def _csv(annotations: list[M.Annotation]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    w.writeheader()
    for a in annotations:
        w.writerow({"id": a.id, "sheet_id": a.sheet_id, "page_number": a.page_number, "category": a.category,
                    "subtype": a.subtype, "subtype_label": T.label(a.category, a.subtype), "status": a.status, "u": a.point.u, "v": a.point.v,
                    "x_pt": a.point_pt[0] if a.point_pt else "", "y_pt": a.point_pt[1] if a.point_pt else "",
                    "room_kind": a.room.kind if a.room else "", "room_name": a.room.name if a.room else "",
                    "text_tokens": " ".join(t.text for t in a.text_tokens), "origin": a.provenance.origin,
                    "review_state": a.review_state, "blind_confirmation": a.blind_confirmation or "",
                    "notes": a.notes or ""})
    return buf.getvalue().encode()


def _blind_consistency(observations: list[M.BlindObservation], annotations: list[M.Annotation]) -> dict:
    obs = [o for o in observations if not o.deleted]
    c = Counter(o.reconciliation for o in obs)
    return {"observations": len(obs), "matched": c.get("matched", 0), "promoted": c.get("promoted", 0),
            "dismissed": c.get("dismissed", 0), "pending": c.get("pending", 0),
            "annotations_not_seen_in_blind_scan": sum(1 for a in annotations if a.blind_confirmation == "not_seen_in_blind_scan"),
            "note": "review-process evidence, not detector accuracy"}


def export(workflow: Workflow, out_root: Path, actor: str, pdf_sha256: str) -> Path:
    from takeoff.groundtruth import checks as K

    store = workflow.store
    out_root.mkdir(parents=True, exist_ok=True)
    sheets, all_verified = {}, True
    for sid, spec in sorted(workflow.specs.items()):
        s = store.get("sheets", sid)
        anns_all = store.list("annotations", sid, include_deleted=True)
        gate = {"pass1_to_blind_scan": K.gate_pass1_to_blind(store, spec) if s.phase == "pass1" else [],
                "blind_scan_to_reconcile": K.gate_blind_to_reconcile(store, spec) if s.phase == "blind_scan" else [],
                "reconcile_to_verified": K.gate_reconcile_to_verified(store, spec) if s.phase in ("reconcile", "verified") else []}
        verified = s.phase == "verified" and not gate["reconcile_to_verified"]
        all_verified &= verified
        sheets[sid] = {"page_number": s.page_number, "phase": s.phase, "verified": verified,
                       "region_bbox_pt": list(s.region_bbox_pt), "region_confirmed": s.region_confirmed,
                       "blind_scan_started_at": s.blind_scan_started_at, "blind_scan_closed_at": s.blind_scan_closed_at,
                       "reconcile_completed_at": s.reconcile_completed_at, "open_gate_reasons": gate,
                       "blind_scan_consistency": _blind_consistency(store.list("observations", sid, include_deleted=True),
                                                                    [a for a in anns_all if not a.deleted])}
    level = "verified" if all_verified and sheets else "incomplete"

    annotations = [a for a in store.list("annotations") if not a.deleted]
    files = {
        "annotations.jsonl": _jsonl(annotations),
        "annotations.csv": _csv(annotations),
        "wiring_runs.jsonl": _jsonl([w for w in store.list("wiring_runs") if not w.deleted]),
        "candidate_reviews.jsonl": _jsonl(store.list("candidate_reviews")),
        "blind_observations.jsonl": _jsonl(store.list("observations", include_deleted=True)),
        "totals.json": json.dumps({"derived_from": "annotations.jsonl", "totals": derive_totals(annotations)},
                                  indent=1, sort_keys=True).encode(),
    }
    existing = [p.name for p in out_root.iterdir() if p.is_dir() and re.match(r"gt_\d{3}_", p.name)]
    n = 1 + max((int(name[3:6]) for name in existing), default=0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out_root / f"gt_{n:03d}_{stamp}"
    target.mkdir(exist_ok=False)  # never overwrite an existing export
    for name, data in files.items():
        (target / name).write_bytes(data)
    manifest = {
        "export_format": EXPORT_FORMAT, "level": level, "created_at": M.utcnow(), "created_by": actor,
        "schema_version": M.SCHEMA_VERSION, "taxonomy_version": T.TAXONOMY_VERSION,
        "protocol_version": PROTOCOL_VERSION, "store_version": STORE_VERSION, "input_pdf_sha256": pdf_sha256,
        "candidate_runs": [{"id": r.id, "generator": r.generator, "generator_version": r.generator_version,
                            "run_hash": r.run_hash, "params": r.params} for r in store.list("candidate_runs")],
        "sheets": sheets,
        "counts": {"annotations": len(annotations)},
        "known_limitations": KNOWN_LIMITATIONS,
        "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in files.items()},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return target


def require_verified(export_dir: Path) -> dict:
    """Evaluator guard: refuse any export that is not verified or has been altered."""
    manifest_path = Path(export_dir) / "manifest.json"
    if not manifest_path.exists():
        raise ExportError(f"{export_dir} has no manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("level") != "verified":
        raise ExportError(f"export level is {manifest.get('level')!r}; metrics require a 'verified' export")
    for name, meta in manifest["files"].items():
        data = (Path(export_dir) / name).read_bytes()
        if _sha256(data) != meta["sha256"]:
            raise ExportError(f"{name} does not match its manifest hash")
    return manifest
