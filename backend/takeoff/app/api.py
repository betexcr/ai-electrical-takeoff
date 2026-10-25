"""FastAPI app for the takeoff workflow (spec §5.1, §5.6, §5.7, §5.9): a JSON API over the store, the service, the
analyzer, the export and the AI stages, plus the layout-A page.

* **Documents** are PDFs identified by SHA-256. The supplied PDF is registered at startup (``source="supplied"``);
  an upload is a raw ``application/pdf`` body that must open with PyMuPDF, otherwise ``invalid_pdf`` (400) and
  nothing is written. The same bytes twice are one document: no second file, no second intake, the takeoff
  untouched. Uploads are stored under ``<data>/documents/<sha256>.pdf``; no PDF is ever modified.
* **Everything is document-scoped**: a sheet id such as ``A1-111`` is unique only within its document.
* **Errors** are ``{code, message, ...details}`` with the code's status (``TakeoffError``); request-shape errors are
  ``invalid`` (422). The actor of every mutation is the ``X-Reviewer`` header, else the default reviewer.
* **AI**: ``ai="claude"`` builds the Claude verifier and discoverer over one ``AIClient`` and makes no request at
  startup; availability is checked lazily (``GET /api/config``, or the first run) and a failure is reported, never
  raised. ``ai="off"`` runs det/0 only. Tests pass the test doubles as a ``(verifier, discoverer)`` tuple.
* **Runs**: interrupted runs are failed once at startup (``Analyzer.recover_interrupted``), before any worker starts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pymupdf
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from takeoff.ai import common as AC
from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import analyze as AN
from takeoff.app import models as TM
from takeoff.app import sheets as TS
from takeoff.app.export import EXPORT_FORMAT, export_takeoff
from takeoff.app.merge import MATCH_TOL_PT
from takeoff.app.rooms import ROOM_PARAMS
from takeoff.app.service import TakeoffError, TakeoffService
from takeoff.app.store import NotFound, StoreError, TakeoffStore
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.protocol import DUPLICATE_TOLERANCE_PT, TOKEN_SUGGEST_RADIUS_PT

API_VERSION = "takeoff-api/1"
BACKEND = Path(__file__).resolve().parents[2]
STATIC_DIR = BACKEND / "app" / "static"
SHARED_DIR = BACKEND / "shared" / "static"
SCORE_LABEL = "match score (not a probability)"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UI_PREFIXES = ("/static/", "/shared/")

log = logging.getLogger("takeoff.api")


# --- request bodies ----------------------------------------------------------------------------------------------

class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalyzeBody(Body):
    sheet_ids: list[str] | None = None
    ai: bool | None = None


class DeviceBody(Body):
    sheet_id: str
    category: str
    subtype: str
    point: dict
    room: dict | None = None
    notes: str | None = None


class PatchBody(Body):
    changes: dict
    reason: str | None = None


class ReasonBody(Body):
    reason: str | None = None


class ChoiceBody(Body):
    choice: str


class BulkBody(Body):
    expected_count: int
    sheet_id: str | None = None


def _dump(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, dict):
        return {k: _dump(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_dump(i) for i in x]
    return x


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


# --- sheet views (memoized) ----------------------------------------------------------------------------------------

class SheetViews:
    """``views(document_id, sheet_id)`` for the service and the analyzer: ``sheets.sheet_view`` of a stored sheet,
    computed once per (document, sheet) under that key's lock and handed out as a copy.

    ``KeyError`` for a sheet that is not stored (the service maps it to ``not_found``). A sheet whose view cannot be
    built (for example a rotated page) is remembered as unavailable and raises ``TakeoffError("invalid")``.
    """

    def __init__(self, store: TakeoffStore, pdf_path_of, cache_dir: Path):
        self.store = store
        self.pdf_path_of = pdf_path_of
        self.cache_dir = Path(cache_dir)
        self._lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._views: dict[tuple[str, str], dict | str] = {}
        self._images: dict[tuple[str, str], Path] = {}

    def info(self, document_id: str, sheet_id: str) -> TM.SheetInfo:
        found = next((s for s in self.store.list("sheets", sheet_id) if s.document_id == document_id), None)
        if found is None:
            raise KeyError((document_id, sheet_id))
        return found

    def _key_lock(self, key) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _entry(self, document_id: str, sheet_id: str) -> dict | str:
        key = (document_id, sheet_id)
        with self._key_lock(key):
            if key not in self._views:
                info = self.info(document_id, sheet_id)  # a missing sheet is not cached: it may be stored later
                try:
                    self._views[key] = TS.sheet_view(Path(self.pdf_path_of(document_id)), info, document_id,
                                                     self.cache_dir)
                except Exception as e:
                    log.warning("sheet %s of %s cannot be displayed: %s", sheet_id, document_id[:12], _why(e))
                    self._views[key] = _why(e)
            return self._views[key]

    def __call__(self, document_id: str, sheet_id: str) -> dict:
        hit = self._entry(document_id, sheet_id)
        if isinstance(hit, str):
            raise TakeoffError("invalid", f"sheet {sheet_id} cannot be displayed: {hit}")
        return copy.deepcopy(hit)

    def unavailable(self, document_id: str, sheet_id: str) -> str | None:
        hit = self._entry(document_id, sheet_id)
        return hit if isinstance(hit, str) else None

    def image(self, document_id: str, sheet_id: str) -> Path:
        view = self(document_id, sheet_id)
        key = (document_id, sheet_id)
        with self._key_lock(key):
            if key not in self._images:
                info = self.info(document_id, sheet_id)
                doc = pymupdf.open(stream=Path(self.pdf_path_of(document_id)).read_bytes(), filetype="pdf")
                try:  # the render is cached on disk by sheet_view; this only resolves its path
                    path, _ = S.render_region_image(doc[info.page_number - 1], view["region"]["bbox_pt"], document_id,
                                                    self.cache_dir)
                finally:
                    doc.close()
                self._images[key] = path
            return self._images[key]


# --- documents -------------------------------------------------------------------------------------------------------

def check_pdf(data: bytes) -> int:
    """The page count of a PDF that opens with PyMuPDF; ``invalid_pdf`` otherwise. Reads only."""
    if not data:
        raise TakeoffError("invalid_pdf", "the upload is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise TakeoffError("invalid_pdf", f"the upload is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise TakeoffError("invalid_pdf", f"not a readable PDF ({type(e).__name__})") from e
    try:
        if not doc.is_pdf:
            raise TakeoffError("invalid_pdf", "not a PDF")
        if doc.needs_pass:
            raise TakeoffError("invalid_pdf", "password-protected PDFs are not supported")
        if doc.page_count < 1:
            raise TakeoffError("invalid_pdf", "the PDF has no pages")
        return doc.page_count
    finally:
        doc.close()


def _clean_name(name: str | None) -> str:
    base = Path((name or "").replace("\\", "/")).name.strip()
    if not base:
        raise TakeoffError("invalid", "file_name is required")
    return base[:255]


# --- the app ------------------------------------------------------------------------------------------------------------

def create_app(data_dir: Path | str, supplied_pdf: Path | str | None, default_reviewer: str = "reviewer",
               background: bool = True, ai: Literal["claude", "off"] | tuple = "claude", *,
               max_requests: int | None = None, detector=None, cache_dir: Path | str | None = None) -> FastAPI:
    """The takeoff app over ``data_dir`` (``takeoff.sqlite``, ``documents/``, ``exports/``, ``cache/``).

    ``detector`` (default det/0) and ``cache_dir`` (default ``<data>/cache``) exist for tests; ``max_requests`` is the
    per-run AI request cap (default ``TAKEOFF_AI_MAX_REQUESTS`` or 200).
    """
    data_dir = Path(data_dir)
    documents_dir = data_dir / "documents"
    exports_dir = data_dir / "exports"
    cache_dir = Path(cache_dir) if cache_dir is not None else data_dir / "cache"
    data_dir.mkdir(parents=True, exist_ok=True)
    if max_requests is None:
        max_requests = AC.RequestBudget.from_env().max_requests

    store = TakeoffStore(data_dir / "takeoff.sqlite")

    def pdf_path_of(document_id: str) -> Path:
        doc = store.find("documents", document_id)
        if doc is None:
            raise KeyError(document_id)
        return Path(doc.stored_path)

    views = SheetViews(store, pdf_path_of, cache_dir)
    service = TakeoffService(store, views)

    if ai == "off":
        mode, verifier, discoverer = "off", None, None
    elif ai == "claude":
        client = AC.AIClient()  # builds nothing and calls nothing until a stage needs it
        mode, verifier, discoverer = "claude", AV.ClaudeVerifier(client), AD.ClaudeDiscoverer(client)
    elif isinstance(ai, tuple) and len(ai) == 2:
        mode, (verifier, discoverer) = "injected", ai
    else:
        raise ValueError("ai must be 'claude', 'off' or a (verifier, discoverer) tuple")

    analyzer = AN.Analyzer(service, pdf_path_of, verifier, discoverer, background=background,
                           max_requests=max_requests, cache_dir=cache_dir, detector=detector)
    recovered = analyzer.recover_interrupted()  # once, before any worker can start
    if recovered:
        log.warning("runs interrupted by a previous stop were marked failed: %s", ", ".join(recovered))

    register_lock = threading.Lock()

    def register(data: bytes, file_name: str, source: Literal["supplied", "upload"], actor: str,
                 path: Path | None = None) -> tuple[TM.Document, bool]:
        """The document for these bytes: the existing one (nothing written), or a new one with its sheets."""
        pages = check_pdf(data)
        sha = hashlib.sha256(data).hexdigest()
        with register_lock:
            existing = store.find("documents", sha)
            if existing is not None:
                return existing, False
            wrote = None
            if path is None:
                path = documents_dir / f"{sha}.pdf"
                if not path.exists():
                    documents_dir.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".pdf.tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                    wrote = path
            try:
                infos = TS.intake(path, sha)
                doc = TM.Document(id=sha, file_name=file_name, page_count=pages, source=source,
                                  stored_path=str(path))
                with store.transaction() as conn:
                    doc = store.insert("documents", doc, actor, "register", conn=conn)
                    for info in infos:
                        store.insert("sheets", info, actor, "intake", conn=conn)
            except BaseException:
                if wrote is not None:
                    wrote.unlink(missing_ok=True)  # only the file this call just wrote
                raise
            return doc, True

    supplied_id = None
    if supplied_pdf is not None:
        supplied_pdf = Path(supplied_pdf)
        doc, _ = register(supplied_pdf.read_bytes(), supplied_pdf.name, "supplied", "system", path=supplied_pdf)
        supplied_id = doc.id

    @asynccontextmanager
    async def lifespan(_app):
        yield
        analyzer.close(timeout=30)
        store.close()

    app = FastAPI(title="AI electrical takeoff", version=API_VERSION, lifespan=lifespan)
    app.state.store = store
    app.state.service = service
    app.state.analyzer = analyzer
    app.state.views = views

    @app.middleware("http")
    async def _no_stale_ui(request: Request, call_next):
        # The page and its scripts must never be served stale after an update.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(_UI_PREFIXES):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def actor(x_reviewer: str | None) -> str:
        return (x_reviewer or default_reviewer).strip() or default_reviewer

    @app.exception_handler(TakeoffError)
    async def _takeoff_error(request: Request, exc: TakeoffError):
        return JSONResponse(_dump({**exc.details, "code": exc.code, "message": exc.message}), status_code=exc.status)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError):
        return JSONResponse({"code": "invalid", "message": str(exc)}, status_code=400)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"code": "invalid", "message": "; ".join(e["msg"] for e in exc.errors())}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError):
        msg = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors())
        return JSONResponse({"code": "invalid", "message": msg or "invalid request"}, status_code=422)

    # --- lookups (document-scoped) ---------------------------------------------------------------------------------

    def document(doc_id: str) -> TM.Document:
        doc = store.find("documents", doc_id)
        if doc is None:
            raise TakeoffError("not_found", f"document {doc_id[:12]} not found")
        return doc

    def sheet_info(doc_id: str, sid: str) -> TM.SheetInfo:
        document(doc_id)
        try:
            return views.info(doc_id, sid)
        except KeyError:
            raise TakeoffError("not_found", f"sheet {sid!r} of document {doc_id[:12]} not found") from None

    def sheets_of(doc_id: str) -> list[TM.SheetInfo]:
        return sorted((s for s in store.list("sheets") if s.document_id == doc_id), key=lambda s: s.page_number)

    # --- config --------------------------------------------------------------------------------------------------------

    def stage_config(stage, module) -> dict:
        out = {"version": module.AI_VERSION, "prompt_version": module.PROMPT_VERSION,
               "implementation": getattr(stage, "implementation", None), "available": False, "reason": None}
        if stage is None:
            out["reason"] = "off" if mode == "off" else AN.NOT_CONFIGURED
            return out
        try:
            ok, reason = stage.available()
        except Exception as e:  # reported, never raised
            ok, reason = False, f"availability check failed: {_why(e)}"
        out["available"] = bool(ok)
        out["reason"] = None if ok else (reason or "unavailable")
        return out

    @app.get("/api/config")
    def config():
        model = next((s.model for s in (verifier, discoverer) if s is not None and s.model), None)
        return {
            "api_version": API_VERSION,
            "default_reviewer": default_reviewer,
            "default_document": supplied_id,
            "versions": {"schema": TM.SCHEMA_VERSION, "merge": TM.MERGE_VERSION, "store": TakeoffStore.STORE_VERSION,
                         "taxonomy": T.TAXONOMY_VERSION, "export": EXPORT_FORMAT,
                         "detector": analyzer.detector.version, "detector_name": analyzer.detector.name},
            "tolerances_pt": {"match": MATCH_TOL_PT, "duplicate": DUPLICATE_TOLERANCE_PT,
                              "token_suggest": TOKEN_SUGGEST_RADIUS_PT},
            "room_params": ROOM_PARAMS,
            "taxonomy": T.as_dict(),
            "score_label": SCORE_LABEL,
            "ai": {"mode": mode, "model": model, "max_requests": analyzer.max_requests,
                   "verify": stage_config(verifier, AV), "discover": stage_config(discoverer, AD)},
        }

    # --- documents and sheets ----------------------------------------------------------------------------------------

    @app.get("/api/documents")
    def list_documents():
        return _dump(store.list("documents"))

    @app.post("/api/documents")
    async def upload_document(request: Request, file_name: str | None = None, x_reviewer: str | None = Header(None)):
        name = _clean_name(file_name)
        data = await request.body()
        doc, created = await run_in_threadpool(register, data, name, "upload", actor(x_reviewer))
        return JSONResponse({"document": _dump(doc), "created": created}, status_code=201 if created else 200)

    @app.get("/api/documents/{doc_id}/sheets")
    def list_sheets(doc_id: str):
        document(doc_id)
        return _dump(sheets_of(doc_id))

    @app.get("/api/documents/{doc_id}/sheets/{sid}/meta")
    def sheet_meta(doc_id: str, sid: str):
        info = sheet_info(doc_id, sid)
        base = {"id": info.id, "document_id": doc_id, "sheet_id": info.sheet_id, "page_number": info.page_number,
                "legend_found": info.legend_found, "note": info.note}
        why = views.unavailable(doc_id, sid)
        if why is not None:  # serve what is known; the page shows the note and offers no drawing
            return {**base, "geometry": None, "region": {"bbox_pt": info.region_bbox_pt}, "image": None,
                    "tokens": [], "rooms": [], "view_error": why}
        return {**views(doc_id, sid), **base, "view_error": None}

    @app.get("/api/documents/{doc_id}/sheets/{sid}/image")
    def sheet_image(doc_id: str, sid: str):
        sheet_info(doc_id, sid)
        why = views.unavailable(doc_id, sid)
        if why is not None:
            raise TakeoffError("not_found", f"sheet {sid} has no image: {why}")
        return FileResponse(views.image(doc_id, sid), media_type="image/png")

    # --- runs ------------------------------------------------------------------------------------------------------------

    def run_out(run: TM.Run) -> dict:
        return {**_dump(run), "ai_usage": analyzer.ai_usage(run.id)}

    @app.post("/api/documents/{doc_id}/analyze")
    def analyze(doc_id: str, body: AnalyzeBody | None = None, x_reviewer: str | None = Header(None)):
        document(doc_id)
        body = body or AnalyzeBody()
        use_ai = body.ai if body.ai is not None else mode != "off"
        return run_out(analyzer.start(doc_id, actor(x_reviewer), sheet_ids=body.sheet_ids, ai=use_ai))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return run_out(analyzer.run(run_id))

    @app.get("/api/documents/{doc_id}/runs")
    def list_runs(doc_id: str):
        document(doc_id)
        return [run_out(r) for r in analyzer.runs(doc_id)]

    # --- devices -------------------------------------------------------------------------------------------------------

    @app.get("/api/documents/{doc_id}/devices")
    def list_devices(doc_id: str, sheet_id: str | None = None, include_deleted: bool = False):
        if sheet_id is not None:
            sheet_info(doc_id, sheet_id)
        else:
            document(doc_id)
        devices = service.devices(doc_id, sheet_id, include_deleted=include_deleted)
        ids = {d.id for d in devices}
        flags = [f for f in store.list("flags", sheet_id) if f.document_id == doc_id and f.open and f.device_id in ids]
        return {"devices": _dump(devices), "open_flags": _dump(flags)}

    @app.post("/api/documents/{doc_id}/devices", status_code=201)
    def create_device(doc_id: str, body: DeviceBody, x_reviewer: str | None = Header(None)):
        sheet_info(doc_id, body.sheet_id)
        return _dump(service.create_device(doc_id, body.sheet_id, actor(x_reviewer), body.category, body.subtype,
                                           body.point, room=body.room, notes=body.notes))

    @app.get("/api/devices/{device_id}")
    def device_detail(device_id: str):
        return _dump(service.device_detail(device_id))

    @app.patch("/api/devices/{device_id}")
    def update_device(device_id: str, body: PatchBody, x_reviewer: str | None = Header(None)):
        return _dump(service.update_device(device_id, actor(x_reviewer), body.changes, body.reason))

    @app.post("/api/devices/{device_id}/accept")
    def accept(device_id: str, body: ReasonBody | None = None, x_reviewer: str | None = Header(None)):
        return _dump(service.accept(device_id, actor(x_reviewer), (body or ReasonBody()).reason))

    @app.post("/api/devices/{device_id}/reject")
    def reject(device_id: str, body: ReasonBody | None = None, x_reviewer: str | None = Header(None)):
        return _dump(service.reject(device_id, actor(x_reviewer), (body or ReasonBody()).reason))

    @app.post("/api/devices/{device_id}/delete")
    def delete(device_id: str, body: ReasonBody | None = None, x_reviewer: str | None = Header(None)):
        return _dump(service.delete(device_id, actor(x_reviewer), (body or ReasonBody()).reason or ""))

    # --- flags ---------------------------------------------------------------------------------------------------------

    @app.post("/api/flags/{flag_id}/acknowledge")
    def acknowledge(flag_id: str, body: ReasonBody | None = None, x_reviewer: str | None = Header(None)):
        return _dump(service.acknowledge_flag(flag_id, actor(x_reviewer), (body or ReasonBody()).reason or ""))

    @app.post("/api/flags/{flag_id}/resolve")
    def resolve(flag_id: str, body: ChoiceBody, x_reviewer: str | None = Header(None)):
        return _dump(service.resolve_conflict(flag_id, actor(x_reviewer), body.choice))

    # --- bulk accept ---------------------------------------------------------------------------------------------------

    @app.get("/api/documents/{doc_id}/bulk-eligible")
    def bulk_eligible(doc_id: str, sheet_id: str | None = None):
        document(doc_id)
        ids = service.bulk_eligible_ids(doc_id, sheet_id)
        return {"count": len(ids), "ids": ids}

    @app.post("/api/documents/{doc_id}/bulk-accept")
    def bulk_accept(doc_id: str, body: BulkBody, x_reviewer: str | None = Header(None)):
        document(doc_id)
        return _dump(service.bulk_accept(doc_id, actor(x_reviewer), body.expected_count, sheet_id=body.sheet_id))

    @app.get("/api/documents/{doc_id}/bulk-actions")
    def bulk_actions(doc_id: str):
        """The document's bulk actions (store order) and the latest bulk accept not yet undone, for *Undo*."""
        document(doc_id)
        actions = [a for a in store.list("bulk_actions") if a.document_id == doc_id]
        undone = {a.undo_of for a in actions if a.kind == "undo"}
        last = next((a for a in reversed(actions) if a.kind == "bulk_accept" and a.id not in undone), None)
        return {"actions": _dump(actions), "last_undoable": _dump(last)}

    @app.post("/api/bulk/{bulk_id}/undo")
    def undo_bulk(bulk_id: str, x_reviewer: str | None = Header(None)):
        return _dump(service.undo_bulk(bulk_id, actor(x_reviewer)))

    # --- counts and export ------------------------------------------------------------------------------------------------

    @app.get("/api/documents/{doc_id}/counts")
    def counts(doc_id: str):
        document(doc_id)
        return service.counts(doc_id)

    @app.post("/api/documents/{doc_id}/export")
    def export(doc_id: str, x_reviewer: str | None = Header(None)):
        document(doc_id)
        out = export_takeoff(service, doc_id, exports_dir, actor(x_reviewer))
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        return {"path": str(out), "manifest": manifest}

    # --- page -------------------------------------------------------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if SHARED_DIR.exists():
        app.mount("/shared", StaticFiles(directory=SHARED_DIR), name="shared")

    return app
