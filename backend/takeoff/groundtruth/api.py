"""FastAPI app for the ground-truth reviewer (JSON API + static page).

The server, not the page, enforces the blind scan: while a sheet is in
``blind_scan`` every endpoint that would expose annotations, candidates,
warnings or tallies for it answers 409 ``blind_scan_in_progress``. The blind
view reads only ``/api/sheets/{id}/blind``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pymupdf
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from takeoff.groundtruth import export as E
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.protocol import (CELL_SIZE_PT, DUPLICATE_TOLERANCE_PT, MATCH_TOLERANCE_PT, PROTOCOL_VERSION,
                                          TOKEN_SUGGEST_RADIUS_PT)
from takeoff.groundtruth.review import BlindScanInProgress, GateError, PhaseError, ReviewError, Workflow
from takeoff.groundtruth.store import NotFound, Store, StoreError

STATIC_DIR = Path(__file__).resolve().parents[2] / "reviewer" / "static"
SHARED_DIR = Path(__file__).resolve().parents[2] / "shared" / "static"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionBody(Body):
    confirmed: bool
    reason: str | None = None


class CellBody(Body):
    done: bool = True


class ReasonBody(Body):
    reason: str


class AnnotationBody(Body):
    category: str
    subtype: str
    point: dict
    text_tokens: list[dict] = []
    room: dict | None = None
    notes: str | None = None
    rotation: int | None = None
    review_state: Literal["draft", "needs_adjudication"] = "draft"


class AnnotationPatch(Body):
    category: str | None = None
    subtype: str | None = None
    point: dict | None = None
    text_tokens: list[dict] | None = None
    room: dict | None = None
    notes: str | None = None
    rotation: int | None = None
    review_state: Literal["draft", "needs_adjudication"] | None = None


class AcceptBody(Body):
    category: str | None = None
    subtype: str | None = None
    point: dict | None = None
    text_tokens: list[dict] = []
    room: dict | None = None
    notes: str | None = None


class ObservationBody(Body):
    point: dict
    category: str
    subtype: str | None = None
    note: str | None = None


class ObservationPatch(Body):
    point: dict | None = None
    category: str | None = None
    subtype: str | None = None
    note: str | None = None


class MatchBody(Body):
    annotation_id: str
    reason: str | None = None


class PromoteBody(Body):
    subtype: str | None = None
    room: dict | None = None
    text_tokens: list[dict] = []
    notes: str | None = None


class UnseenBody(Body):
    decision: Literal["keep", "delete"]
    reason: str


class AckBody(Body):
    warning_id: str
    reason: str


class ReopenBody(Body):
    reason: str


def _dump(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, list):
        return [_dump(i) for i in x]
    return x


def _set(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True)


def create_app(pdf_path: Path, db_path: Path, cache_dir: Path, export_dir: Path,
               default_reviewer: str = "reviewer", pages: list[int] | None = None) -> FastAPI:
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")  # read-only: the file on disk is never opened for writing
    pages = pages or list(range(1, doc.page_count + 1))
    descriptions, images = {}, {}
    for n in pages:
        d = S.describe_sheet(doc[n - 1], pdf_sha, cache_dir)
        descriptions[d["sheet_id"]] = d
        images[d["sheet_id"]] = S.render_region_image(doc[n - 1], d["region"]["bbox_pt"], pdf_sha, cache_dir)[0]
    doc.close()
    store = Store(db_path)
    wf = Workflow(store, {sid: S.SheetSpec.from_description(d) for sid, d in descriptions.items()})

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.close()

    app = FastAPI(title="Ground-truth reviewer", version=PROTOCOL_VERSION, lifespan=lifespan)
    app.state.workflow = wf
    app.state.pdf_sha = pdf_sha

    @app.middleware("http")
    async def _no_stale_ui(request: Request, call_next):
        # The page and its scripts must never be served stale after an update.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def actor(x_reviewer: str | None) -> str:
        return (x_reviewer or default_reviewer).strip() or default_reviewer

    @app.exception_handler(ReviewError)
    async def _review_error(request: Request, exc: ReviewError):
        status = 409 if isinstance(exc, (PhaseError, GateError)) else 400
        body = {"code": exc.code, "message": str(exc)}
        if isinstance(exc, GateError):
            body["reasons"] = exc.reasons
        return JSONResponse(body, status_code=status)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError):
        return JSONResponse({"code": "invalid", "message": str(exc)}, status_code=400)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"code": "invalid", "message": "; ".join(e["msg"] for e in exc.errors())}, status_code=422)

    # --- reads ---------------------------------------------------------------------------------------------

    @app.get("/api/config")
    def config():
        return {"protocol_version": PROTOCOL_VERSION, "input_pdf_sha256": pdf_sha, "default_reviewer": default_reviewer,
                "tolerances_pt": {"duplicate": DUPLICATE_TOLERANCE_PT, "match": MATCH_TOLERANCE_PT,
                                  "token_suggest": TOKEN_SUGGEST_RADIUS_PT, "cell": CELL_SIZE_PT},
                "taxonomy": wf.taxonomy()}

    @app.get("/api/sheets")
    def list_sheets():
        out = []
        for sid, d in descriptions.items():
            s = wf.sheet(sid)
            cells = store.list("cells", sid)
            out.append({"sheet_id": sid, "page_number": d["page_number"], "phase": s.phase,
                        "region_confirmed": s.region_confirmed, "cells_total": len(cells),
                        "cells_pass1_done": sum(c.pass1_done for c in cells),
                        "cells_blind_scan_done": sum(c.blind_scan_done for c in cells)})
        return out

    @app.get("/api/sheets/{sid}/meta")
    def meta(sid: str):
        """Drawing facts plus review state that is safe in every phase (no annotations or candidates)."""
        d = descriptions.get(sid)
        if d is None:
            raise NotFound(f"sheet {sid} not found")
        s = wf.sheet(sid)
        cells = {c.id: c for c in store.list("cells", sid)}
        return {**d, "cells": [{**c, "pass1_done": cells[c["id"]].pass1_done,
                                "blind_scan_done": cells[c["id"]].blind_scan_done} for c in d["cells"]],
                "review": _dump(s)}

    @app.get("/api/sheets/{sid}/image")
    def image(sid: str):
        if sid not in images:
            raise NotFound(f"sheet {sid} not found")
        return FileResponse(images[sid], media_type="image/png")

    @app.get("/api/sheets/{sid}/progress")
    def progress(sid: str):
        return wf.progress(sid)

    @app.get("/api/sheets/{sid}/annotations")
    def annotations(sid: str, include_deleted: bool = False):
        wf.require_not_blind(sid)
        return _dump(store.list("annotations", sid, include_deleted=include_deleted))

    @app.get("/api/sheets/{sid}/candidates")
    def candidates(sid: str):
        return {"note": "candidate locations only; never detections or counts", "clusters": wf.clusters(sid)}

    @app.get("/api/sheets/{sid}/warnings")
    def warnings(sid: str):
        return wf.warnings(sid)

    @app.get("/api/sheets/{sid}/blind")
    def blind(sid: str):
        """The blind-scan view: cells, this scan's observations and blind progress. Nothing from pass 1."""
        if wf.phase(sid) != "blind_scan":
            raise PhaseError(f"sheet {sid} is not in blind scan")
        cells = store.list("cells", sid)
        return {"phase": "blind_scan",
                "cells": [{"id": c.id, "blind_scan_done": c.blind_scan_done} for c in cells],
                "observations": _dump([o for o in wf.observations(sid) if not o.deleted]),
                "progress": wf.progress(sid)}

    @app.get("/api/sheets/{sid}/reconcile")
    def reconcile(sid: str):
        return {"observations": _dump(wf.observations(sid)), "suggestions": wf.suggestions(sid),
                "unseen": _dump(wf.unseen(sid))}

    @app.get("/api/annotations/{aid}/history")
    def history(aid: str):
        a = store.get("annotations", aid)
        wf.require_not_blind(a.sheet_id)
        return _dump(store.events("annotation", aid))

    # --- pass 1 ---------------------------------------------------------------------------------------------------

    @app.post("/api/sheets/{sid}/region")
    def region(sid: str, body: RegionBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.confirm_region(sid, actor(x_reviewer), body.confirmed, body.reason))

    @app.post("/api/sheets/{sid}/cells/{cell_id}")
    def cell(sid: str, cell_id: str, body: CellBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.mark_cell(sid, cell_id, actor(x_reviewer), body.done))

    @app.post("/api/sheets/{sid}/cells/{cell_id}/reject-candidates")
    def reject_cell(sid: str, cell_id: str, body: ReasonBody, x_reviewer: str | None = Header(None)):
        return {"rejected": wf.reject_unreviewed_in_cell(sid, cell_id, actor(x_reviewer), body.reason)}

    @app.post("/api/sheets/{sid}/annotations")
    def create(sid: str, body: AnnotationBody, x_reviewer: str | None = Header(None)):
        b = _set(body)
        return _dump(wf.create_annotation(sid, actor(x_reviewer), b.pop("category"), b.pop("subtype"), b.pop("point"), **b))

    @app.patch("/api/annotations/{aid}")
    def patch(aid: str, body: AnnotationPatch, x_reviewer: str | None = Header(None)):
        return _dump(wf.update_annotation(aid, actor(x_reviewer), **_set(body)))

    @app.post("/api/annotations/{aid}/delete")
    def delete(aid: str, body: ReasonBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.delete_annotation(aid, actor(x_reviewer), body.reason))

    @app.post("/api/sheets/{sid}/clusters/{cluster_id}/accept")
    def accept(sid: str, cluster_id: str, body: AcceptBody, x_reviewer: str | None = Header(None)):
        b = _set(body)
        return _dump(wf.accept_cluster(sid, cluster_id, actor(x_reviewer), b.pop("category", None),
                                       b.pop("subtype", None), b.pop("point", None), **b))

    @app.post("/api/sheets/{sid}/clusters/{cluster_id}/reject")
    def reject(sid: str, cluster_id: str, body: ReasonBody, x_reviewer: str | None = Header(None)):
        return {"rejected": wf.reject_cluster(sid, cluster_id, actor(x_reviewer), body.reason)}

    @app.post("/api/sheets/{sid}/warnings/ack")
    def ack(sid: str, body: AckBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.acknowledge_warning(sid, body.warning_id, actor(x_reviewer), body.reason))

    # --- phases --------------------------------------------------------------------------------------------------------

    @app.post("/api/sheets/{sid}/phase/start-blind-scan")
    def start_blind(sid: str, x_reviewer: str | None = Header(None)):
        return _dump(wf.start_blind_scan(sid, actor(x_reviewer)))

    @app.post("/api/sheets/{sid}/phase/open-reconcile")
    def open_reconcile(sid: str, x_reviewer: str | None = Header(None)):
        return _dump(wf.open_reconcile(sid, actor(x_reviewer)))

    @app.post("/api/sheets/{sid}/phase/complete")
    def complete(sid: str, x_reviewer: str | None = Header(None)):
        return _dump(wf.complete_sheet(sid, actor(x_reviewer)))

    @app.post("/api/sheets/{sid}/phase/reopen")
    def reopen(sid: str, body: ReopenBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.reopen_sheet(sid, actor(x_reviewer), body.reason))

    # --- blind scan ------------------------------------------------------------------------------------------------------

    @app.post("/api/sheets/{sid}/observations")
    def add_obs(sid: str, body: ObservationBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.add_observation(sid, actor(x_reviewer), body.point, body.category, body.subtype, body.note))

    @app.patch("/api/observations/{oid}")
    def patch_obs(oid: str, body: ObservationPatch, x_reviewer: str | None = Header(None)):
        return _dump(wf.update_observation(oid, actor(x_reviewer), **_set(body)))

    @app.post("/api/observations/{oid}/delete")
    def delete_obs(oid: str, x_reviewer: str | None = Header(None)):
        return _dump(wf.delete_observation(oid, actor(x_reviewer)))

    # --- reconciliation -----------------------------------------------------------------------------------------------------

    @app.post("/api/observations/{oid}/match")
    def match(oid: str, body: MatchBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.match(oid, body.annotation_id, actor(x_reviewer), body.reason))

    @app.post("/api/observations/{oid}/promote")
    def promote(oid: str, body: PromoteBody, x_reviewer: str | None = Header(None)):
        b = _set(body)
        return _dump(wf.promote(oid, actor(x_reviewer), b.pop("subtype", None), **b))

    @app.post("/api/observations/{oid}/dismiss")
    def dismiss(oid: str, body: ReasonBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.dismiss(oid, actor(x_reviewer), body.reason))

    @app.post("/api/annotations/{aid}/unseen")
    def unseen(aid: str, body: UnseenBody, x_reviewer: str | None = Header(None)):
        return _dump(wf.decide_unseen(aid, actor(x_reviewer), body.decision, body.reason))

    @app.post("/api/annotations/{aid}/verify")
    def verify(aid: str, x_reviewer: str | None = Header(None)):
        return _dump(wf.verify(aid, actor(x_reviewer)))

    # --- export --------------------------------------------------------------------------------------------------------------

    @app.post("/api/export")
    def do_export(x_reviewer: str | None = Header(None)):
        d = E.export(wf, export_dir, actor(x_reviewer), pdf_sha)
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        return {"path": str(d), "level": m["level"], "sheets": {k: v["phase"] for k, v in m["sheets"].items()}}

    # --- page ---------------------------------------------------------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if SHARED_DIR.exists():
        app.mount("/shared", StaticFiles(directory=SHARED_DIR), name="shared")

    return app
