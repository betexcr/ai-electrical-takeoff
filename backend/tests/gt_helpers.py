"""Shared synthetic fixtures for the ground-truth workflow tests."""

from __future__ import annotations

from takeoff.groundtruth import models as M
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.review import Workflow
from takeoff.groundtruth.store import Store
from takeoff.pdf import coords as C

W, H = 2592.0, 1728.0
GEOM = C.PageGeometry(0, 0, W, H)
REGION = (100.0, 100.0, 500.0, 400.0)  # -> 4 x 3 cells of 100 pt
ROOM = {"kind": "exterior", "name": "Exterior"}


def npt(x: float, y: float) -> dict:
    """Normalized point for PyMuPDF unrotated point (x, y) on the synthetic page."""
    u, v = C.unrotated_to_normalized(GEOM, (x, y))
    return {"u": u, "v": v}


def spec(sheet_id: str = "S1", page: int = 1) -> S.SheetSpec:
    return S.SheetSpec(sheet_id, page, GEOM, REGION, S.coverage_grid(sheet_id, GEOM, REGION))


def workflow(path, sheet_ids=("S1",)) -> Workflow:
    store = Store(path)
    return Workflow(store, {sid: spec(sid, i + 1) for i, sid in enumerate(sheet_ids)})


def import_candidates(wf: Workflow, sheet_id: str, points_pt, cls="detector_smoke", hint=("detector", "smoke_detector"),
                      generator="raster_plain", run_hash=None):
    run = M.CandidateRun(generator=generator, generator_version="gt_candidates/1+symbols/1", params={"threshold": 0.7},
                         input_sha256="0" * 64, run_hash=run_hash or f"hash-{sheet_id}-{generator}-{len(points_pt)}")
    cands = [M.Candidate(run_id=run.id, sheet_id=sheet_id, page_number=wf.spec(sheet_id).page_number,
                         class_hypothesis=cls, category_hint=hint[0], subtype_hint=hint[1],
                         score=0.9 - i / 100 if generator != "vector_signature" else None,
                         point=npt(*p), point_pt=p) for i, p in enumerate(points_pt)]
    wf.store.import_candidate_run(run, cands, actor="importer")
    return cands


def finish_pass1(wf: Workflow, sheet_id: str, actor="rev"):
    wf.confirm_region(sheet_id, actor, True)
    for cl in wf.clusters(sheet_id):
        if cl["unreviewed"]:
            wf.reject_cluster(sheet_id, cl["id"], actor, "not a device")
    for c in wf.store.list("cells", sheet_id):
        if not c.pass1_done:
            wf.mark_cell(sheet_id, c.id, actor)


def finish_blind(wf: Workflow, sheet_id: str, actor="rev"):
    for c in wf.store.list("cells", sheet_id):
        if not c.blind_scan_done:
            wf.mark_cell(sheet_id, c.id, actor)
