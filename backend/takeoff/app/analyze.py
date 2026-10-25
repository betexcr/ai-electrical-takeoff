"""The Analyzer: one Analyze is one immutable ``Run`` (spec §5.3, §5.8, §5.9).

Each sheet of a run, independently and in page order:

1. a sheet without a recognizable legend (or that intake could not read) is ``skipped_no_legend`` with
   :data:`NO_LEGEND_MESSAGE`; nothing runs on it;
2. ``running``;
3. the detector (by default ``candidates.generate`` for that page, then det/0), and its detections are saved as
   immutable ``RunDetection`` rows (``id = "{run}:{det/0 id}"``, the det/0 point kept verbatim as ``point_pt``);
4. **verify** (``ai-verify/1``) on det/0's detections: its ``ModelVerdict`` rows are saved;
5. **discover** (``ai-discover/1``) over the region's tiles: its verdicts and ``ai_discover`` detections are saved;
6. **one transaction:** ``merge_sheet`` over the sheet's devices, the new devices, evidence and flags, the condition
   flags re-synced, and the sheet's status ``done``.

Detectors propose, reviewers decide: a run never changes a saved device and never deletes anything. A detector
exception fails only that sheet (``failed`` with the message); an AI stage exception makes only that stage
``unavailable`` with the message, and the merge still runs. Each AI stage is ``off`` (not requested),
``unavailable: <reason>``, ``done`` or ``partial`` (some requests failed or were skipped); a stage whose every verdict
is ``skipped_no_legend``, or a sheet without a legend, is ``unavailable: no legend`` (ruling R17). One
``RequestBudget`` is shared by both stages and every sheet of the run.

Runs are processed by one background worker thread (or inline with ``background=False``). A document has one run in
progress at a time: ``start`` refuses with ``run_in_progress`` while any sheet of any run of the document is
``queued`` or ``running``.
"""

from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import pymupdf

from takeoff.ai import common as AC
from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import sheets as TS
from takeoff.app.merge import merge_sheet
from takeoff.app.models import AIStageStatus, Run, RunDetection, SheetInfo, SheetRunStatus
from takeoff.app.rooms import propose_room
from takeoff.app.service import TakeoffError, TakeoffService
from takeoff.detection import det0
from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.models import utcnow
from takeoff.pdf import coords as C

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE = ROOT / "data" / "takeoff" / "cache"

NO_LEGEND_MESSAGE = ("No recognizable symbol legend on this sheet; automated detection did not run. "
                     "Add devices manually.")
NO_LEGEND = "no legend"
NOT_CONFIGURED = "not configured"
DETECTION_FAILED = "detection failed"
INTERRUPTED = "interrupted: the analyzer stopped before this sheet finished"
INTERNAL_ERROR = "analysis failed: internal error (see the server log)"
IN_PROGRESS = ("queued", "running")
_REASON_CHARS = 300

log = logging.getLogger("takeoff.analyzer")


def _actor(run: Run) -> str:
    """Who writes a run's records: the analyzer, on behalf of the run (``Run.created_by`` is the person)."""
    return f"analyzer:{run.id}"


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:_REASON_CHARS]


# --- detector ----------------------------------------------------------------------------------------------------

class Det0Detector:
    """Candidates for one page (``candidates.generate``), then det/0 on that page only."""

    name = "det0"
    version = det0.DETECTOR_VERSION
    params = det0.PARAMS

    def __call__(self, pdf_path: Path, sheet: SheetInfo) -> dict:
        results = CA.generate(pdf_path, pages=[sheet.page_number])
        return det0.detect(pdf_path, results, pages=[sheet.page_number])


# --- sheet views ---------------------------------------------------------------------------------------------------

def sheet_views(store, pdf_path_of: Callable[[str], Path], cache_dir: Path) -> Callable[[str, str], dict]:
    """A ``views(document_id, sheet_id)`` for :class:`TakeoffService`: ``sheets.sheet_view`` of a stored sheet,
    computed once and handed out as a copy. ``KeyError`` for a sheet that is not stored."""
    cache: dict[tuple[str, str], dict] = {}
    lock = threading.Lock()

    def views(document_id: str, sheet_id: str) -> dict:
        key = (document_id, sheet_id)
        with lock:
            hit = cache.get(key)
        if hit is None:
            info = next((s for s in store.list("sheets", sheet_id) if s.document_id == document_id), None)
            if info is None:
                raise KeyError(key)
            hit = TS.sheet_view(Path(pdf_path_of(document_id)), info, document_id, Path(cache_dir))
            with lock:
                cache[key] = hit
        return copy.deepcopy(hit)

    return views


# --- stage status ------------------------------------------------------------------------------------------------

def _stage(status: str, reason: str | None = None, implementation: str | None = None) -> AIStageStatus:
    return AIStageStatus(status=status, reason=reason, implementation=implementation)


def stage_status(verdicts: list, implementation: str | None, n_items: int | None = None) -> AIStageStatus:
    """``done``, ``partial`` (any verdict with an error, skips included) or, when every verdict was skipped for a
    missing legend, ``unavailable: no legend`` (ruling R17). The usage is :func:`common.summarize`'s."""
    s = AC.summarize(verdicts)
    s["implementation"] = s["implementation"] or implementation
    if n_items is not None:
        s["n_items"] = n_items
    if verdicts and all((v.error or {}).get("code") == AD.NO_LEGEND for v in verdicts):
        return AIStageStatus(status="unavailable", reason=NO_LEGEND, **s)
    return AIStageStatus(status="partial" if any(v.error for v in verdicts) else "done", **s)


# --- the analyzer ------------------------------------------------------------------------------------------------

class Analyzer:
    def __init__(self, service: TakeoffService, pdf_path_of: Callable[[str], Path], verifier: AV.Verifier | None,
                 discoverer: AD.Discoverer | None, background: bool = True,
                 max_requests: int = AC.DEFAULT_MAX_REQUESTS, *, cache_dir: Path | None = None, detector=None):
        self.service = service
        self.store = service.store
        self.pdf_path_of = pdf_path_of
        self.verifier = verifier
        self.discoverer = discoverer
        self.background = background
        self.max_requests = int(max_requests)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE
        self.detector = detector if detector is not None else Det0Detector()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    # --- reads ---------------------------------------------------------------------------------------------

    def run(self, run_id: str) -> Run:
        found = self.store.find("runs", run_id)
        if found is None:
            raise TakeoffError("not_found", f"run {run_id} not found")
        return found

    def runs(self, document_id: str) -> list[Run]:
        return sorted((r for r in self.store.list("runs") if r.document_id == document_id), key=lambda r: r.id)

    def ai_usage(self, run_id: str) -> dict:
        """Each stage's usage summed over the run's sheets (:func:`common.summarize` over its verdicts)."""
        verdicts = [v for v in self.store.list("verdicts") if v.run_id == run_id]
        return {kind: AC.summarize(v for v in verdicts if v.kind == kind) for kind in ("verify", "discover")}

    # --- start -----------------------------------------------------------------------------------------------

    def start(self, document_id: str, actor: str, sheet_ids: list[str] | None = None, ai: bool = True) -> Run:
        if not (isinstance(actor, str) and actor.strip()):
            raise TakeoffError("invalid", "an actor is required")
        if self.store.find("documents", document_id) is None:
            raise TakeoffError("not_found", f"document {document_id[:12]} not found")
        infos = self._sheets(document_id)
        by_id = {s.sheet_id: s for s in infos}
        if sheet_ids is None:
            chosen = [s.sheet_id for s in infos]
        else:
            if isinstance(sheet_ids, str) or not sheet_ids:
                raise TakeoffError("invalid", "sheet_ids must be a non-empty list of sheet ids")
            unknown = [s for s in sheet_ids if s not in by_id]
            if unknown:
                raise TakeoffError("not_found", f"sheets not found: {unknown}")
            wanted = set(sheet_ids)
            chosen = [s.sheet_id for s in infos if s.sheet_id in wanted]  # page order, each once
        with self.store.transaction() as conn:
            busy = self._in_progress(document_id)
            if busy is not None:
                raise TakeoffError("run_in_progress", f"run {busy[0]} is still analyzing sheet {busy[1]}",
                                   {"run_id": busy[0], "sheet_id": busy[1]})
            run = Run(id=self._next_id(), document_id=document_id, detector=self.detector.name,
                      detector_version=self.detector.version,
                      params={"detector": copy.deepcopy(self.detector.params),
                              "ai": {"enabled": bool(ai), "max_requests": self.max_requests}},
                      ai_model=self._ai_model() if ai else None, ai_versions=self._ai_versions(),
                      sheets={sid: SheetRunStatus(ai_verify=self._initial(ai, self.verifier),
                                                  ai_discover=self._initial(ai, self.discoverer)) for sid in chosen},
                      created_by=actor, created_at=utcnow())
            run = self.store.insert("runs", run, actor, "create", conn=conn)
        if self.background:
            self._ensure_worker()
            self._queue.put(run.id)
            return run
        self._process(run.id)
        return self.run(run.id)

    def _sheets(self, document_id: str) -> list[SheetInfo]:
        """The document's sheets in page order; read from the PDF (``sheets.intake``) the first time."""
        stored = [s for s in self.store.list("sheets") if s.document_id == document_id]
        if not stored:
            found = TS.intake(Path(self.pdf_path_of(document_id)), document_id)
            with self.store.transaction() as conn:
                for info in found:
                    if self.store.find("sheets", info.id) is None:
                        self.store.insert("sheets", info, "analyzer", "intake", conn=conn)
            stored = [s for s in self.store.list("sheets") if s.document_id == document_id]
        return sorted(stored, key=lambda s: s.page_number)

    def _in_progress(self, document_id: str) -> tuple[str, str] | None:
        for r in self.runs(document_id):
            for sid, s in r.sheets.items():
                if s.status in IN_PROGRESS:
                    return r.id, sid
        return None

    def _next_id(self) -> str:
        n = max((int(r.id.split("-")[1]) for r in self.store.list("runs")), default=0)
        return f"run-{n + 1:03d}"

    def _ai_model(self) -> str | None:
        return next((s.model for s in (self.verifier, self.discoverer) if s is not None and s.model), None)

    def _ai_versions(self) -> dict[str, str]:
        out = {"verify": AV.AI_VERSION, "verify_prompt": AV.PROMPT_VERSION}
        if self.verifier is not None:
            out.update(verify_implementation=self.verifier.implementation, verify_model=self.verifier.model or "")
        out.update(discover=AD.AI_VERSION, discover_prompt=AD.PROMPT_VERSION)
        if self.discoverer is not None:
            out.update(discover_implementation=self.discoverer.implementation,
                       discover_model=self.discoverer.model or "")
        return out

    @staticmethod
    def _initial(ai: bool, stage) -> AIStageStatus:
        if not ai:
            return _stage("off")
        if stage is None:
            return _stage("unavailable", NOT_CONFIGURED)
        return _stage("pending", implementation=stage.implementation)

    # --- the worker -------------------------------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="takeoff-analyzer", daemon=True)
                self._thread.start()

    def _work(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                if run_id is None:
                    return
                self._process(run_id)
            except Exception:  # _process settles the run itself; this only keeps the worker alive
                log.exception("analyzer worker: run %s raised past its own handling", run_id)
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every queued run is processed; False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout: float | None = 30) -> None:
        """Stop the worker after the runs already queued."""
        with self._thread_lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout)

    def recover_interrupted(self) -> list[str]:
        """Mark every ``queued`` or ``running`` sheet of the stored runs ``failed`` (the process that ran them
        stopped). Call once at startup, before any run is started; returns the runs changed."""
        changed = []
        with self.store.transaction() as conn:
            for r in self.store.list("runs"):
                stale = [sid for sid, s in r.sheets.items() if s.status in IN_PROGRESS]
                if not stale:
                    continue
                now = utcnow()
                sheets = dict(r.sheets)
                for sid in stale:
                    sheets[sid] = sheets[sid].model_copy(update={"status": "failed", "message": INTERRUPTED,
                                                                 "finished_at": now})
                self.store.replace("runs", r.model_copy(update={"sheets": sheets, "finished_at": r.finished_at or now}),
                                   "analyzer", "interrupted", conn=conn)
                changed.append(r.id)
        return changed

    # --- one run -------------------------------------------------------------------------------------------------

    def _process(self, run_id: str) -> None:
        """Every sheet, then :meth:`_settle`, which always runs: no sheet of the run is left ``queued`` or
        ``running`` (that would refuse every later run of the document with ``run_in_progress``)."""
        try:
            run = self.run(run_id)
            ai = bool(run.params.get("ai", {}).get("enabled"))
            budget = AC.RequestBudget(self.max_requests)  # shared by both stages and every sheet of the run
            for sid in run.sheets:
                try:
                    self._process_sheet(run, sid, budget, ai)
                except Exception as e:  # a bug outside the per-step handling: still fail only this sheet
                    log.exception("analyzer: sheet %s of run %s failed unexpectedly", sid, run_id)
                    try:
                        self._set_sheet(run, sid, status="failed", message=f"analysis failed: {_why(e)}",
                                        finished_at=utcnow())
                    except Exception:
                        log.exception("analyzer: could not record the failure of sheet %s of run %s", sid, run_id)
        except Exception:
            log.exception("analyzer: run %s failed unexpectedly", run_id)
        self._settle(run_id)

    def _settle(self, run_id: str) -> None:
        """Best effort, each step on its own: fail any sheet still ``queued`` or ``running``, then set
        ``finished_at``. Failures are logged, never raised."""
        try:
            run = self.run(run_id)
        except Exception:
            log.exception("analyzer: run %s could not be read to settle it", run_id)
            return
        for sid, s in run.sheets.items():
            if s.status in IN_PROGRESS:
                try:
                    self._set_sheet(run, sid, status="failed", message=INTERNAL_ERROR, finished_at=utcnow())
                except Exception:
                    log.exception("analyzer: could not mark sheet %s of run %s failed", sid, run_id)
        try:
            self._finish(run)
        except Exception:
            log.exception("analyzer: could not set finished_at on run %s", run_id)

    def _set_sheet(self, run: Run, sheet_id: str, conn=None, **changes) -> Run:
        def do(c):
            current = self.store.get("runs", run.id)
            sheets = dict(current.sheets)
            sheets[sheet_id] = sheets[sheet_id].model_copy(update=changes)
            return self.store.replace("runs", current.model_copy(update={"sheets": sheets}), _actor(run),
                                      "sheet_status", conn=c)

        if conn is not None:
            return do(conn)
        with self.store.transaction() as c:
            return do(c)

    def _finish(self, run: Run) -> None:
        with self.store.transaction() as conn:
            current = self.store.get("runs", run.id)
            inputs = sorted({h for s in current.sheets.values() for h in s.input_runs})
            self.store.replace("runs", current.model_copy(update={"finished_at": utcnow(), "input_runs": inputs}),
                               _actor(run), "finish", conn=conn)

    def _process_sheet(self, run: Run, sheet_id: str, budget: AC.RequestBudget, ai: bool) -> None:
        info = next(s for s in self.store.list("sheets", sheet_id) if s.document_id == run.document_id)
        if not info.legend_found or info.note:
            unavailable = _stage("off") if not ai else _stage("unavailable", NO_LEGEND)
            now = utcnow()
            self._set_sheet(run, sheet_id, status="skipped_no_legend", message=NO_LEGEND_MESSAGE, started_at=now,
                            finished_at=now, ai_verify=unavailable, ai_discover=unavailable.model_copy())
            return
        self._set_sheet(run, sheet_id, status="running", started_at=utcnow())
        pdf = Path(self.pdf_path_of(run.document_id))
        actor = _actor(run)

        # 3. the detector, and its detections saved unchanged
        try:
            view = self.service.views(run.document_id, sheet_id)
            out = self.detector(pdf, info)
            dets = self._detections(run, info, view, out)
            with self.store.transaction() as conn:
                for d in dets:
                    self.store.insert("detections", d, actor, "create", conn=conn)
        except Exception as e:
            stopped = _stage("off") if not ai else _stage("unavailable", DETECTION_FAILED)
            self._set_sheet(run, sheet_id, status="failed", message=f"{self.detector.version} failed: {_why(e)}",
                            finished_at=utcnow(), ai_verify=self._kept(ai, self.verifier, stopped),
                            ai_discover=self._kept(ai, self.discoverer, stopped))
            return
        self._set_sheet(run, sheet_id, n_detections=len(dets),
                        input_runs=sorted(r["run_hash"] for r in out.get("input_runs", [])))

        # 4-5. the AI stages (each may fail on its own; the merge runs regardless)
        inputs = _SheetInputs(pdf, info, view, run.document_id, self.cache_dir)
        verdicts, ai_dets = [], []
        v_status = self._verify(run, sheet_id, dets, inputs, budget, ai, verdicts)
        d_status = self._discover(run, sheet_id, inputs, budget, ai, verdicts, ai_dets)
        self._set_sheet(run, sheet_id, ai_verify=v_status, ai_discover=d_status)

        # 6. one transaction: merge, inserts, condition flags, status
        try:
            with self.store.transaction() as conn:
                devices = self.service.devices(run.document_id, sheet_id, include_deleted=True)
                result = merge_sheet(run, dets + ai_dets, devices, lambda pt: propose_room(pt, view["rooms"]),
                                     verdicts)
                for d in result.new_devices:
                    self.store.insert("devices", d, actor, "create", conn=conn)
                for e in result.evidence:
                    self.store.insert("evidence", e, actor, "create", conn=conn)
                for f in result.flags:
                    self.store.insert("flags", f, actor, "open", conn=conn)
                self.service.resync_flags(run.document_id, sheet_id, conn=conn, actor=actor)
                self._set_sheet(run, sheet_id, conn=conn, status="done", finished_at=utcnow())
        except Exception as e:
            self._set_sheet(run, sheet_id, status="failed", message=f"merge failed: {_why(e)}", finished_at=utcnow())

    @staticmethod
    def _kept(ai: bool, stage, stopped: AIStageStatus) -> AIStageStatus:
        if ai and stage is None:
            return _stage("unavailable", NOT_CONFIGURED)
        return stopped.model_copy()

    @staticmethod
    def _detections(run: Run, info: SheetInfo, view: dict, out: dict) -> list[RunDetection]:
        """det/0's detections as ``RunDetection`` rows: the point in PDF points kept verbatim, normalized through the
        sheet's page geometry (ruling R3)."""
        geo = view["geometry"]
        g = C.PageGeometry(*geo["crop_native"], rotation=geo["rotation"])
        rows = []
        for d in out["detections"]:
            if d["sheet_id"] != info.sheet_id:
                raise ValueError(f"the detector returned sheet {d['sheet_id']!r} for sheet {info.sheet_id!r}")
            x, y = d["x_pt"], d["y_pt"]
            u, v = C.unrotated_to_normalized(g, (x, y))
            rows.append(RunDetection(
                id=f"{run.id}:{d['id']}", run_id=run.id, document_id=run.document_id, sheet_id=info.sheet_id,
                source="det0", category=d["category"], subtype=d["subtype"], status=d.get("status") or "",
                point={"u": u, "v": v}, point_pt=(x, y), score=d.get("score"),
                members=copy.deepcopy(d.get("members", [])), subtype_source=d.get("subtype_source"),
                tokens=list(d.get("tokens", []))))
        return rows

    # --- the AI stages ---------------------------------------------------------------------------------------------

    @staticmethod
    def _gate(ai: bool, stage) -> AIStageStatus | None:
        """The stage's status when it cannot run at all, or None when it may."""
        if not ai:
            return _stage("off")
        if stage is None:
            return _stage("unavailable", NOT_CONFIGURED)
        try:
            ok, reason = stage.available()
        except Exception as e:
            return _stage("unavailable", f"availability check failed: {_why(e)}", stage.implementation)
        if not ok:
            return _stage("unavailable", reason or "unavailable", stage.implementation)
        return None

    def _store_verdicts(self, run: Run, verdicts: Iterable, detections: Iterable = ()) -> None:
        with self.store.transaction() as conn:
            for v in verdicts:
                self.store.insert("verdicts", v, _actor(run), "create", conn=conn)
            for d in detections:
                self.store.insert("detections", d, _actor(run), "create", conn=conn)

    def _verify(self, run: Run, sheet_id: str, dets: list[RunDetection], inputs: "_SheetInputs",
                budget: AC.RequestBudget, ai: bool, verdicts: list) -> AIStageStatus:
        gated = self._gate(ai, self.verifier)
        if gated is not None:
            return gated
        try:
            region, mapping = inputs.region()
            crops = {d.id: AV.build_crop(region, mapping, d.point_pt) for d in dets}
            tokens = AV.tokens_near(dets, inputs.view["tokens"])
            found = self.verifier.verify(run.id, sheet_id, dets, crops, inputs.icons(), tokens, budget)
            self._store_verdicts(run, found)
        except Exception as e:
            return _stage("unavailable", _why(e), self.verifier.implementation)
        verdicts += found
        return stage_status(found, self.verifier.implementation)

    def _discover(self, run: Run, sheet_id: str, inputs: "_SheetInputs", budget: AC.RequestBudget, ai: bool,
                  verdicts: list, ai_dets: list) -> AIStageStatus:
        gated = self._gate(ai, self.discoverer)
        if gated is not None:
            return gated
        try:
            region, mapping = inputs.region()
            composite = AD.composite_or_none(inputs.icons())
            found, vs = self.discoverer.discover(run.id, run.document_id, sheet_id, region, mapping,
                                                 inputs.view["region"]["bbox_pt"], composite, budget)
            self._store_verdicts(run, vs, found)
        except Exception as e:
            return _stage("unavailable", _why(e), self.discoverer.implementation)
        verdicts += vs
        ai_dets += found
        return stage_status(vs, self.discoverer.implementation, n_items=len(found))


class _SheetInputs:
    """The region image (decoded once) and the sheet's legend icons, loaded on first use by either stage."""

    def __init__(self, pdf: Path, info: SheetInfo, view: dict, document_id: str, cache_dir: Path):
        self.pdf, self.info, self.view, self.document_id, self.cache_dir = pdf, info, view, document_id, cache_dir
        self._region: tuple[Any, dict] | None = None
        self._icons: dict | None = None

    def region(self) -> tuple[Any, dict]:
        if self._region is None:
            doc = pymupdf.open(stream=self.pdf.read_bytes(), filetype="pdf")  # never writes the file
            try:
                path, mapping = S.render_region_image(doc[self.info.page_number - 1], self.view["region"]["bbox_pt"],
                                                      self.document_id, self.cache_dir)
            finally:
                doc.close()
            self._region = (AV.load_gray(path), mapping)
        return self._region

    def icons(self) -> dict:
        if self._icons is None:
            self._icons = TS.legend_icons(self.pdf, self.info)
        return self._icons
