"""Immutable takeoff export (spec §5.7, §5.9.4, §4.17): ``tk_NNN_<UTC stamp>/``, never overwritten.

* **One snapshot**: every table is read once, while the store lock is held, and every row, total, count and
  reason is built from that prefetch (no per-device re-reads), so a concurrent edit cannot split the files.
* **Official records and totals come only from accepted, non-deleted devices** (detector, AI-found and manual).
  ``devices_official.csv`` and ``totals.json`` are built from the *same* device list in the same call, so the
  CSV re-derives the totals exactly (reconciliation test).
* **Review items** (proposed, rejected, deleted) go to ``devices_review.csv`` with their open flags; they never
  feed a total.
* **The manifest states completeness honestly**: ``complete`` is true only with 0 awaiting devices and 0 open
  review flags; every reason is spelled out in ``incomplete_reasons``. ``AI discovery unavailable on <sheet>``
  lines are informational and never change ``complete``.
* **AI accounting**: per run and stage the status, implementation, model, versions, requests, tokens, errors and
  latency, summed from the saved verdicts with the same function the analyzer uses
  (:func:`takeoff.ai.common.summarize`). A test double is labelled as such in ``ai_note``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from takeoff.ai import common as AC
from takeoff.app import counts as COUNTS
from takeoff.app import models as TM
from takeoff.app.models import Device, Flag
from takeoff.app.service import TakeoffError, TakeoffService
from takeoff.app.store import TakeoffStore
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.export import room_key as gt_room_key
from takeoff.groundtruth.models import utcnow

EXPORT_FORMAT = "takeoff-export/1"
BRIEF_COLUMNS = ["id", "sheet_id", "category", "subtype", "subtype_label", "status", "room_kind", "room_name",
                    "u", "v", "x_pt", "y_pt", "review_state", "origin_reported", "accept_kind", "created_by_run",
                    "detector_version", "match_score", "subtype_source", "ai_summary", "ai_implementation",
                    "ai_model"]
# ``room_key`` is appended last to both files: the disambiguated row key of totals.json ("CL @(x,y)").
OFFICIAL_COLUMNS = BRIEF_COLUMNS + ["room_key"]
REVIEW_COLUMNS = BRIEF_COLUMNS + ["deleted", "delete_reason", "open_flags", "room_key"]
ORIGIN_VOCABULARY = {
    "devices_official.csv": "origin_reported is the reported origin of an accepted device: "
                            "detector_accepted | ai_accepted | manual",
    "devices_review.csv": "origin_reported is the internal origin: detector | ai | manual; none of these rows "
                          "is in any total",
}
AI_NOTE = ("AI verdicts and AI-only candidates are suggestions; totals come from accepted and manual "
           "device records")
FAKE_LABEL = "(test double, not a model)"
STAGES = ("verify", "discover")
_STAGE_NAME = {"verify": "verification", "discover": "discovery"}
_DIR_RETRIES = 5


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _is_official(d: Device) -> bool:
    return d.review_state == "accepted" and not d.deleted


class _Snapshot:
    """Every record the export reads, fetched once while the store lock is held."""

    def __init__(self, service: TakeoffService, document_id: str):
        store = service.store
        self.document = store.find("documents", document_id)
        self.devices = service.devices(document_id, include_deleted=True)
        flags = [f for f in store.list("flags", include_deleted=True) if f.document_id == document_id]
        self.flags = flags
        self.open_flags = [f for f in flags if f.open]
        self.runs = {r.id: r for r in store.list("runs") if r.document_id == document_id}
        self.verdicts = store.list("verdicts")
        detections = {x.id: x for x in store.list("detections")}
        ids = {d.id for d in self.devices}
        self.evidence: dict[str, list[dict]] = {}
        for e in store.list("evidence"):
            if e.device_id in ids:
                self.evidence.setdefault(e.device_id, []).append(
                    {"evidence": e, "run": self.runs.get(e.run_id), "detection": detections.get(e.detection_id)})
        self.detections = detections
        self.counts = service.derive_counts(document_id, self.devices, self.open_flags)
        self.rooms: dict[str, list[dict]] = {}
        for sheet_id in sorted({d.sheet_id for d in self.devices}):
            try:
                self.rooms[sheet_id] = service.views(document_id, sheet_id)["rooms"]
            except (LookupError, TakeoffError):  # rows fall back to the saved rooms, as in the totals
                pass


class _Rows:
    """Per-device CSV cells built from a :class:`_Snapshot` (the service's own AI-summary logic, no re-reads)."""

    def __init__(self, service: TakeoffService, snap: _Snapshot):
        self.service, self.snap = service, snap
        self.flags_of: dict[str, list[str]] = {}
        for f in snap.open_flags:
            self.flags_of.setdefault(f.device_id, []).append(f.type)
        self.all_flags_of: dict[str, list[Flag]] = {}
        for f in snap.flags:
            self.all_flags_of.setdefault(f.device_id, []).append(f)
        self.verdicts_of_sheet: dict[str, list[TM.ModelVerdict]] = {}
        for v in snap.verdicts:
            self.verdicts_of_sheet.setdefault(v.sheet_id, []).append(v)
        self.verdict_by_id = {v.id: v for v in snap.verdicts}
        self.official_of_sheet: dict[str, list[Device]] = {}
        for d in snap.devices:
            if _is_official(d):
                self.official_of_sheet.setdefault(d.sheet_id, []).append(d)

    def room_key(self, d: Device) -> str:
        """The row key totals.json uses for this device's room (same rooms, same function as ``derive``)."""
        rooms = self.snap.rooms.get(d.sheet_id)
        if rooms is not None:
            return COUNTS.sheet_room_key(d, rooms)
        here = self.official_of_sheet.get(d.sheet_id, [])
        return gt_room_key(d, here if d in here else [*here, d])

    def row(self, d: Device, internal_origin: bool) -> dict:
        rows = self.snap.evidence.get(d.id, [])
        flags = self.all_flags_of.get(d.id, [])
        verdicts = self.service._linked_verdicts(d, rows, flags, self.verdicts_of_sheet.get(d.sheet_id, []))
        summary = self.service._ai_summary(d, rows, flags, verdicts)
        # the detection that created the device (its own run), else the first evidence it has
        mine = [r for r in rows if d.created_by_run and r["evidence"].run_id == d.created_by_run]
        created = next((r for r in mine if r["evidence"].outcome == "created"), mine[0] if mine else None)
        detection = created["detection"] if created is not None else None
        run = self.snap.runs.get(d.created_by_run) if d.created_by_run else None
        verdict = {v.id: v for v in verdicts}.get(summary.get("verdict_id"))
        if verdict is None and detection is not None and detection.verdict_id:
            verdict = self.verdict_by_id.get(detection.verdict_id)
        return {
            "id": d.id, "sheet_id": d.sheet_id, "category": d.category, "subtype": d.subtype or "",
            "subtype_label": T.label(d.category, d.subtype) if d.subtype else "", "status": d.status,
            "room_kind": d.room.kind if d.room else "", "room_name": d.room.name if d.room else "",
            "u": d.point.u, "v": d.point.v,
            "x_pt": d.point_pt[0] if d.point_pt else "", "y_pt": d.point_pt[1] if d.point_pt else "",
            "review_state": d.review_state,
            "origin_reported": d.origin if internal_origin else COUNTS.REPORTED_ORIGIN[d.origin],
            "accept_kind": d.accept_kind or "", "created_by_run": d.created_by_run or "",
            "detector_version": run.detector_version if run is not None else "",
            "match_score": detection.score if detection is not None and detection.score is not None else "",
            "subtype_source": (detection.subtype_source or "") if detection is not None else "",
            "ai_summary": summary["text"],
            "ai_implementation": (summary.get("implementation") or (verdict.implementation if verdict else "")),
            "ai_model": verdict.model if verdict is not None else "",
            "deleted": "true" if d.deleted else "false", "delete_reason": d.delete_reason or "",
            "open_flags": ";".join(self.flags_of.get(d.id, ())),
            "room_key": self.room_key(d),
        }


def _csv(columns: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _json(data) -> bytes:
    return json.dumps(data, indent=1, sort_keys=True).encode("utf-8")


# --- AI accounting ----------------------------------------------------------------------------------------


def _stage(run: TM.Run, kind: str, verdicts: list[TM.ModelVerdict]) -> tuple[dict, set[str]]:
    """One run's stage: status per sheet, the verdict-derived usage, and the implementations seen."""
    mine = [v for v in verdicts if v.kind == kind]
    usage = AC.summarize(mine)  # the analyzer's own summing (Analyzer.ai_usage)
    sheets = {}
    for sid, s in run.sheets.items():
        st = getattr(s, f"ai_{kind}")
        sheets[sid] = {"status": st.status, "reason": st.reason, "implementation": st.implementation,
                       "requests": st.requests, "errors_by_code": dict(st.errors_by_code),
                       "tokens": st.tokens.model_dump(), "latency_ms": st.latency_ms.model_dump(),
                       "n_items": st.n_items}
    statuses = sorted({s["status"] for s in sheets.values()})
    versions = run.ai_versions
    impls = {v.implementation for v in mine} | {s["implementation"] for s in sheets.values() if s["implementation"]}
    if versions.get(f"{kind}_implementation"):
        impls.add(versions[f"{kind}_implementation"])
    models = sorted({v.model for v in mine}) or [m for m in (versions.get(f"{kind}_model") or run.ai_model,) if m]
    prompts = sorted({v.prompt_version for v in mine})
    stage = {
        "status": statuses[0] if len(statuses) == 1 else ("mixed" if statuses else "off"),
        "implementation": usage["implementation"] or versions.get(f"{kind}_implementation")
        or next(iter(sorted(impls)), None),
        "model": ", ".join(models) or None,
        "prompt_version": versions.get(f"{kind}_prompt") or (", ".join(prompts) or None),
        "ai_version": versions.get(kind),
        "requests": usage["requests"], "errors_by_code": usage["errors_by_code"], "tokens": usage["tokens"],
        "latency_ms": usage["latency_ms"], "n_items": usage["n_items"],
        "sheets": sheets,
    }
    return stage, impls


def _runs(snap: _Snapshot) -> tuple[list[dict], bool]:
    runs = sorted(snap.runs.values(), key=lambda r: r.id)
    verdicts_by_run: dict[str, list[TM.ModelVerdict]] = {}
    for v in snap.verdicts:
        verdicts_by_run.setdefault(v.run_id, []).append(v)
    out, fake = [], False
    for run in runs:
        ai: dict = {"model": run.ai_model}
        for kind in STAGES:
            ai[kind], impls = _stage(run, kind, verdicts_by_run.get(run.id, []))
            fake = fake or "fake" in impls
        out.append({"id": run.id, "detector": run.detector, "detector_version": run.detector_version,
                    "merge_version": run.merge_version, "created_by": run.created_by,
                    "created_at": run.created_at, "finished_at": run.finished_at,
                    "sheets": {sid: {"status": s.status, "message": s.message, "n_detections": s.n_detections}
                               for sid, s in run.sheets.items()},
                    "ai": ai})
    return out, fake


def _unavailable(runs: list[dict]) -> list[str]:
    """Informational lines: a sheet's AI stage unavailable in the latest run that covered it."""
    latest: dict[str, dict] = {}
    for run in runs:  # ascending run ids: a later run supersedes an earlier one on the same sheet
        for sid in run["sheets"]:
            latest[sid] = run
    lines = []
    for sid in sorted(latest):
        for kind in ("discover", "verify"):
            st = latest[sid]["ai"][kind]["sheets"].get(sid)
            if st is not None and st["status"] == "unavailable":
                lines.append(f"AI {_STAGE_NAME[kind]} unavailable on {sid}: {st['reason'] or 'no reason recorded'}")
    return lines


# --- completeness ------------------------------------------------------------------------------------------


def _blocking_reasons(review: dict, devices: list[Device], open_flags: list[Flag]) -> list[str]:
    """Every reason the takeoff is not complete; ``complete`` is true exactly when there are none."""
    live = {d.id: d for d in devices if not d.deleted}
    reasons = []
    awaiting = review["awaiting"]
    if awaiting:
        reasons.append(f"{_plural(awaiting, 'detected device', 'detected devices')} awaiting review")
    ai_awaiting = sum(1 for d in live.values() if d.origin == "ai" and d.review_state == "proposed")
    if ai_awaiting:
        reasons.append(f"{_plural(ai_awaiting, 'AI-only candidate', 'AI-only candidates')} awaiting review")
    blocking = Counter()
    for f in open_flags:
        d = live.get(f.device_id)
        if d is None or not COUNTS.needs_review(f):
            continue
        # an AI-only candidate still awaiting review is already counted by its own line
        if f.type == "ai_only_candidate" and d.review_state == "proposed":
            continue
        blocking[f.type] += 1
    for flag_type, n in sorted(blocking.items()):
        reasons.append(f"{_plural(n, f'open {flag_type} flag', f'open {flag_type} flags')}")
    return reasons


def _next_dir(out_root: Path, stamp: str) -> Path:
    for _ in range(_DIR_RETRIES):
        n = 1 + max((int(p.name[3:6]) for p in out_root.iterdir()
                     if p.is_dir() and len(p.name) > 6 and p.name.startswith("tk_") and p.name[3:6].isdigit()
                     and p.name[6] == "_"), default=0)
        target = out_root / f"tk_{n:03d}_{stamp}"
        try:
            target.mkdir(exist_ok=False)  # never overwrite an existing export
            return target
        except FileExistsError:  # another export took this number between listing and creating
            continue
    raise FileExistsError(f"could not reserve a new export directory under {out_root}")


# --- the export ---------------------------------------------------------------------------------------------


def export_takeoff(service: TakeoffService, document_id: str, out_root: Path, actor: str) -> Path:
    """Write a new immutable export directory for ``document_id`` and return its path."""
    with service.store.snapshot():  # one consistent read of every table; nothing below re-reads the store
        snap = _Snapshot(service, document_id)
        cells = _Rows(service, snap)
        official = [d for d in snap.devices if _is_official(d)]
        review_items = [d for d in snap.devices if not _is_official(d)]
        counts = snap.counts
        files = {
            "devices_official.csv": _csv(OFFICIAL_COLUMNS, [cells.row(d, False) for d in official]),
            "devices_review.csv": _csv(REVIEW_COLUMNS, [cells.row(d, True) for d in review_items]),
            "totals.json": _json(counts["official"]),
        }
        runs, fake = _runs(snap)

    live = {d.id for d in snap.devices if not d.deleted}
    flags = [f for f in snap.open_flags if f.device_id in live]  # a deleted device's flags are in no figure
    blocking = _blocking_reasons(counts["review"], snap.devices, flags)
    informational = _unavailable(runs) + [
        f"Rooms unavailable on {sid} ({why}); its devices are counted under their saved rooms"
        for sid, why in sorted(counts["rooms_unavailable"].items())]
    manifest = {
        "export_format": EXPORT_FORMAT, "schema_version": TM.SCHEMA_VERSION, "merge_version": TM.MERGE_VERSION,
        "taxonomy_version": T.TAXONOMY_VERSION, "store_version": TakeoffStore.STORE_VERSION,
        "document_sha256": document_id,
        "document_name": snap.document.file_name if snap.document is not None else None,
        "runs": runs,
        "review": counts["review"],
        "open_flags_by_type": dict(sorted(Counter(f.type for f in flags).items())),
        "complete": not blocking,
        "incomplete_reasons": blocking + informational,
        "ai_note": f"{AI_NOTE} {FAKE_LABEL}" if fake else AI_NOTE,
        "ai_test_double": fake,
        "origin_vocabulary": ORIGIN_VOCABULARY,
        "counts": {"official_records": len(official), "review_records": len(review_items)},
        "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in files.items()},
        "created_by": actor, "created_at": utcnow(),
    }

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    target = _next_dir(out_root, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    try:
        for name, data in files.items():
            (target / name).write_bytes(data)
        (target / "manifest.json").write_bytes(_json(manifest))  # last: a directory without it is unfinished
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)  # only the directory this call just created
        raise
    return target
