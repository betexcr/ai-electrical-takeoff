"""The Analyzer (spec §5.3, §5.8, §5.9): immutable runs, the AI stages and safe re-analysis.

Detectors propose, reviewers decide: a run only adds detections, verdicts, evidence, flags and new proposed devices.
Every test uses a temporary store and cache. The fast tests run on the supplied PDF's real sheet views with a fake
detector (no candidate generation) and the AI fakes; ``test_two_runs_on_a1_preserve_corrections`` runs the real
candidates and det/0 on A1 (about a minute). No test makes a network request.
"""

from __future__ import annotations

import hashlib
import math
import threading
from pathlib import Path

import pymupdf
import pytest

from takeoff.ai import common as AC
from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import analyze as AN
from takeoff.app import models as TM
from takeoff.app import sheets as TS
from takeoff.app.rooms import propose_room
from takeoff.app.service import TakeoffError, TakeoffService
from takeoff.app.store import TakeoffStore
from takeoff.detection import det0
from takeoff.groundtruth import candidates as CA
from takeoff.pdf import coords as C

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
ACTOR = "reviewer@example.com"
A1, A2 = "A1-111", "A2-111"

pytestmark = pytest.mark.skipif(not PDF.exists(), reason="supplied PDF missing")


@pytest.fixture(scope="module")
def sha():
    return hashlib.sha256(PDF.read_bytes()).hexdigest()


class Env:
    def __init__(self, tmp_path: Path, pdf: Path = PDF, intake: bool = True):
        self.pdf = pdf
        self.sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        self.cache = tmp_path / "cache"
        self.store = TakeoffStore(tmp_path / "takeoff.sqlite")
        doc = pymupdf.open(pdf)
        pages = doc.page_count
        doc.close()
        self.store.insert("documents", TM.Document(id=self.sha, file_name=pdf.name, page_count=pages,
                                                   source="supplied", stored_path=str(pdf)), "system")
        if intake:  # as the app registers a document; otherwise the analyzer's first run does it
            for info in TS.intake(pdf, self.sha):
                self.store.insert("sheets", info, "system")
        self.views = AN.sheet_views(self.store, self.pdf_path_of, self.cache)
        self.svc = TakeoffService(self.store, self.views)

    def pdf_path_of(self, document_id: str) -> Path:
        assert document_id == self.sha
        return self.pdf

    def analyzer(self, verifier=None, discoverer=None, detector=None, background=False, **kw) -> AN.Analyzer:
        return AN.Analyzer(self.svc, self.pdf_path_of, verifier, discoverer, background=background,
                           cache_dir=self.cache, detector=detector, **kw)

    def devices(self, sheet_id=A1):
        return self.svc.devices(self.sha, sheet_id)

    def device_of(self, run_id: str, det_id: str) -> TM.Device:
        """The device a detection created or matched (through its evidence)."""
        ev = [e for e in self.store.list("evidence") if e.detection_id == f"{run_id}:{det_id}"]
        assert len(ev) == 1, ev
        return self.store.get("devices", ev[0].device_id)

    def open_flags(self, device_id: str, type_=None):
        return [f for f in self.store.list("flags") if f.device_id == device_id and f.open
                and (type_ is None or f.type == type_)]

    def official(self) -> int:
        return self.svc.counts(self.sha)["review"]["official_total"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


class FakeDetector:
    """A det0-shaped detector over scripted points; can fail a sheet or block until released."""

    name = "fake-detector"
    version = "fake/1"
    params = {"fake": True}

    def __init__(self, points_by_sheet: dict, fail_on=(), block: threading.Event | None = None):
        self.points_by_sheet = points_by_sheet
        self.fail_on = set(fail_on)
        self.block = block
        self.entered = threading.Event()
        self.calls: list[str] = []

    def __call__(self, pdf_path, sheet: TM.SheetInfo) -> dict:
        self.calls.append(sheet.sheet_id)
        self.entered.set()
        if self.block is not None:
            assert self.block.wait(30), "the test never released the detector"
        if sheet.sheet_id in self.fail_on:
            raise RuntimeError(f"boom on {sheet.sheet_id}")
        dets = []
        for n, (category, subtype, x, y) in enumerate(self.points_by_sheet.get(sheet.sheet_id, [])):
            dets.append({"id": f"{sheet.sheet_id}:fake-{n:04d}", "sheet_id": sheet.sheet_id, "category": category,
                         "subtype": subtype, "status": "as_drawn", "x_pt": x, "y_pt": y, "score": 0.9,
                         "members": [], "subtype_source": "default", "tokens": []})
        return {"detector_version": self.version, "params": self.params,
                "input_runs": [{"generator": "fake", "run_hash": f"fakehash-{sheet.sheet_id}"}], "detections": dets}


def clear_points(view: dict, n: int, sep: float = 40.0, avoid=()) -> list[tuple[float, float]]:
    """``n`` points inside the region, each with a clear (non-ambiguous) room proposal, ``sep`` pt apart."""
    x0, y0, x1, y1 = view["region"]["bbox_pt"]
    out = []
    y = y0 + 30
    while y < y1 - 30 and len(out) < n:
        x = x0 + 30
        while x < x1 - 30 and len(out) < n:
            p = (round(x, 3), round(y, 3))
            if (not propose_room(p, view["rooms"]).ambiguous
                    and all(math.dist(p, q) >= sep for q in [*out, *avoid])):
                out.append(p)
            x += 7.0
        y += 7.0
    assert len(out) == n, f"only {len(out)} clear points"
    return out


def points(env: Env, sheet_id: str, n: int, category="switch", subtype="standard"):
    return [(category, subtype, x, y) for x, y in clear_points(env.views(env.sha, sheet_id), n)]


def det_id(sheet_id: str, n: int) -> str:
    return f"{sheet_id}:fake-{n:04d}"


# --- per-sheet outcomes -------------------------------------------------------------------------------------

def test_no_legend_sheet_is_skipped_with_message(tmp_path):
    blank = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.save(blank)
    doc.close()
    e = Env(tmp_path, blank, intake=False)
    try:
        detector = FakeDetector({})
        an = e.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector)
        run = an.start(e.sha, ACTOR)
        (sid,) = run.sheets
        s = run.sheets[sid]
        assert sid == "page-1"
        assert s.status == "skipped_no_legend"
        assert s.message == ("No recognizable symbol legend on this sheet; automated detection did not run. "
                             "Add devices manually.")
        # ruling R17: with AI requested, a sheet without a legend reports the stages as unavailable: no legend
        assert (s.ai_verify.status, s.ai_verify.reason) == ("unavailable", "no legend")
        assert (s.ai_discover.status, s.ai_discover.reason) == ("unavailable", "no legend")
        assert run.finished_at is not None and s.finished_at is not None
        assert detector.calls == []
        assert e.store.list("detections") == [] and e.store.list("verdicts") == [] and e.store.list("devices") == []

        off = an.start(e.sha, ACTOR, ai=False)
        assert off.sheets[sid].status == "skipped_no_legend"
        assert (off.sheets[sid].ai_verify.status, off.sheets[sid].ai_discover.status) == ("off", "off")
        assert detector.calls == []

        # devices can still be added manually on the sheet (AC-T11)
        dev = e.svc.create_device(e.sha, sid, ACTOR, "switch", "standard", {"u": 0.5, "v": 0.5})
        assert dev.review_state == "accepted" and e.official() == 1
    finally:
        e.store.close()


def test_failed_sheet_does_not_stop_others(env):
    detector = FakeDetector({A2: points(env, A2, 3)}, fail_on={A1})
    run = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1, A2])
    assert list(run.sheets) == [A1, A2] and detector.calls == [A1, A2]
    a1, a2 = run.sheets[A1], run.sheets[A2]
    assert a1.status == "failed" and "boom on A1-111" in a1.message and a1.finished_at is not None
    assert (a1.ai_verify.status, a1.ai_discover.status) == ("unavailable", "unavailable")
    assert a2.status == "done" and a2.n_detections == 3 and a2.message is None
    assert (a2.ai_verify.status, a2.ai_discover.status) == ("done", "done")
    assert len(env.devices(A2)) == 3 and env.devices(A1) == []
    assert run.finished_at is not None


def test_second_start_while_running_is_refused(env):
    release = threading.Event()
    detector = FakeDetector({A1: points(env, A1, 2)}, block=release)
    an = env.analyzer(None, None, detector, background=True)
    try:
        run = an.start(env.sha, ACTOR, sheet_ids=[A1], ai=False)
        assert run.sheets[A1].status in ("queued", "running")
        assert detector.entered.wait(30)
        assert env.store.get("runs", run.id).sheets[A1].status == "running"
        for sheets in ([A1], [A2], None):  # one takeoff per document: any sheet in progress blocks a new run
            with pytest.raises(TakeoffError) as e:
                an.start(env.sha, ACTOR, sheet_ids=sheets, ai=False)
            assert e.value.code == "run_in_progress" and e.value.status == 409
            assert e.value.details["run_id"] == run.id
        assert [r.id for r in an.runs(env.sha)] == [run.id]  # the refused starts wrote nothing
    finally:
        release.set()
    assert an.wait_idle(timeout=60)
    assert an.run(run.id).sheets[A1].status == "done"
    again = an.start(env.sha, ACTOR, sheet_ids=[A1], ai=False)
    assert again.id == "run-002"
    assert an.wait_idle(timeout=60)
    an.close()


def test_interrupted_runs_are_failed_on_recovery(env):
    an = env.analyzer(None, None, FakeDetector({}))
    stale = TM.Run(id="run-001", document_id=env.sha, detector="det0", detector_version="det/0", created_by=ACTOR,
                   sheets={A1: TM.SheetRunStatus(status="running"), A2: TM.SheetRunStatus(status="queued")})
    env.store.insert("runs", stale, "system")
    with pytest.raises(TakeoffError):
        an.start(env.sha, ACTOR, ai=False)
    assert an.recover_interrupted() == ["run-001"]
    run = an.run("run-001")
    assert {s.status for s in run.sheets.values()} == {"failed"} and run.finished_at is not None
    assert all("interrupted" in s.message for s in run.sheets.values())
    assert an.start(env.sha, ACTOR, sheet_ids=[A1], ai=False).id == "run-002"


def test_run_records_versions_and_detections(env):
    detector = FakeDetector({A1: points(env, A1, 3)})
    run = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    assert run.id == "run-001" and run.created_by == ACTOR
    assert (run.detector, run.detector_version, run.params["detector"]) == ("fake-detector", "fake/1", {"fake": True})
    assert run.merge_version == "takeoff-merge/1"
    assert run.ai_model == "fake-verifier"
    assert run.ai_versions == {"verify": "ai-verify/1", "verify_prompt": "ai-verify-prompt/1",
                               "verify_implementation": "fake", "verify_model": "fake-verifier",
                               "discover": "ai-discover/1", "discover_prompt": "ai-discover-prompt/1",
                               "discover_implementation": "fake", "discover_model": "fake-discoverer"}
    assert run.params["ai"] == {"enabled": True, "max_requests": AC.DEFAULT_MAX_REQUESTS}
    assert run.sheets[A1].input_runs == ["fakehash-A1-111"] and run.input_runs == ["fakehash-A1-111"]
    view = env.views(env.sha, A1)
    g = C.PageGeometry(*view["geometry"]["crop_native"], rotation=view["geometry"]["rotation"])
    dets = [d for d in env.store.list("detections", A1) if d.source == "det0"]
    assert [d.id for d in dets] == [f"run-001:{det_id(A1, n)}" for n in range(3)]
    for d, (_, _, x, y) in zip(dets, detector.points_by_sheet[A1]):
        assert d.point_pt == (x, y) and d.run_id == "run-001"
        u, v = C.unrotated_to_normalized(g, (x, y))
        assert (d.point.u, d.point.v) == pytest.approx((u, v), abs=1e-12)
    assert {d.created_by_run for d in env.devices()} == {"run-001"}


def test_condition_flags_synced_in_the_merge(env):
    view = env.views(env.sha, A1)
    x0, y0, x1, y1 = view["region"]["bbox_pt"]
    far = next((x, y) for y in range(int(y0) + 10, int(y1) - 10, 5) for x in range(int(x0) + 10, int(x1) - 10, 5)
               if propose_room((x, y), view["rooms"]).reason == "far_from_labels")
    (clear,) = clear_points(view, 1, avoid=[far])
    twin = (clear[0] + 2.0, clear[1])  # a same-category pair within 3 pt
    detector = FakeDetector({A1: [("switch", "standard", *far), ("light", "pendant", *clear),
                                  ("light", "pendant", *twin)]})
    env.analyzer(None, None, detector).start(env.sha, ACTOR, sheet_ids=[A1], ai=False)
    d_far, d_a, d_b = (env.device_of("run-001", det_id(A1, n)) for n in range(3))
    assert [f.type for f in env.open_flags(d_far.id)] == ["room_ambiguous"]
    assert [f.type for f in env.open_flags(d_a.id)] == [f.type for f in env.open_flags(d_b.id)] == ["duplicate_warning"]
    assert {f.id for f in env.open_flags(d_far.id)} == {f"cond:{d_far.id}:room_ambiguous"}  # ruling R8 ids


def test_merge_failure_fails_the_sheet_and_writes_no_devices(env, monkeypatch):
    detector = FakeDetector({A1: points(env, A1, 2)})

    def broken(*a, **kw):
        raise RuntimeError("merge bug")

    monkeypatch.setattr(AN, "merge_sheet", broken)
    run = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    s = run.sheets[A1]
    assert s.status == "failed" and "merge bug" in s.message and s.ai_verify.status == "done"
    assert env.devices() == [] and env.store.list("evidence") == []
    assert len(env.store.list("detections")) == 2 and env.store.list("verdicts")  # saved before the merge
    assert run.finished_at is not None


def test_stage_status_rules():
    def v(code=None):
        return TM.ModelVerdict(run_id="run-001", sheet_id=A1, kind="discover", implementation="fake", model="m",
                               prompt_version="p", ai_version="a", error={"code": code} if code else None)

    assert AN.stage_status([], "fake").model_dump(include={"status", "implementation"}) == {
        "status": "done", "implementation": "fake"}
    assert AN.stage_status([v(), v()], "fake").status == "done"
    no_legend = AN.stage_status([v("skipped_no_legend")] * 3, "fake")
    assert (no_legend.status, no_legend.reason) == ("unavailable", "no legend")  # ruling R17, never partial
    mixed = AN.stage_status([v(), v("skipped_budget"), v("rate_limited")], "fake")
    assert (mixed.status, mixed.errors_by_code, mixed.requests) == ("partial", {"rate_limited": 1, "skipped_budget": 1}, 2)


# --- AI stage statuses and accounting -----------------------------------------------------------------------

class ExplodingDiscoverer(AD.FakeDiscoverer):
    def discover(self, *a, **kw):
        raise RuntimeError("tile renderer exploded")


def test_ai_off_unavailable_partial_statuses_recorded_and_run_completes(env):
    pts = points(env, A1, 3)
    detector = FakeDetector({A1: pts})

    off = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1], ai=False)
    s = off.sheets[A1]
    assert s.status == "done" and (s.ai_verify.status, s.ai_discover.status) == ("off", "off")
    assert off.ai_model is None and off.params["ai"]["enabled"] is False
    assert env.store.list("verdicts") == []

    unavailable = env.analyzer(AV.FakeVerifier(available=(False, "no credentials")), ExplodingDiscoverer(),
                               detector).start(env.sha, ACTOR, sheet_ids=[A1])
    s = unavailable.sheets[A1]
    assert s.status == "done"
    assert (s.ai_verify.status, s.ai_verify.reason) == ("unavailable", "no credentials")
    assert s.ai_discover.status == "unavailable" and "tile renderer exploded" in s.ai_discover.reason
    assert [v for v in env.store.list("verdicts") if v.run_id == unavailable.id] == []
    assert {e.outcome for e in env.store.list("evidence") if e.run_id == unavailable.id} == {"confirmed"}  # merged

    tile0 = AD.tiles(env.views(env.sha, A1)["region"]["bbox_pt"], A1)[0].id
    partial = env.analyzer(AV.FakeVerifier({f"run-003:{det_id(A1, 0)}": {"_error": "rate_limited"}}),
                           AD.FakeDiscoverer({tile0: {"_error": "timeout"}}),
                           detector).start(env.sha, ACTOR, sheet_ids=[A1])
    s = partial.sheets[A1]
    assert partial.id == "run-003" and s.status == "done"
    assert (s.ai_verify.status, s.ai_verify.errors_by_code, s.ai_verify.implementation) == (
        "partial", {"rate_limited": 1}, "fake")
    assert (s.ai_discover.status, s.ai_discover.errors_by_code) == ("partial", {"timeout": 1})
    assert partial.finished_at is not None

    missing = env.analyzer(None, None, detector).start(env.sha, ACTOR, sheet_ids=[A1])
    s = missing.sheets[A1]
    assert s.status == "done"
    assert (s.ai_verify.status, s.ai_verify.reason) == ("unavailable", "not configured")
    assert (s.ai_discover.status, s.ai_discover.reason) == ("unavailable", "not configured")


def test_shared_budget_across_stages_records_skipped_budget(env):
    detector = FakeDetector({A1: points(env, A1, 12), A2: points(env, A2, 3)})  # A1: 2 verify batches
    run = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(), detector,
                       max_requests=3).start(env.sha, ACTOR, sheet_ids=[A1, A2])
    n1 = len(AD.tiles(env.views(env.sha, A1)["region"]["bbox_pt"], A1))
    n2 = len(AD.tiles(env.views(env.sha, A2)["region"]["bbox_pt"], A2))
    a1, a2 = run.sheets[A1], run.sheets[A2]
    assert (a1.ai_verify.status, a1.ai_verify.requests, a1.ai_verify.errors_by_code) == ("done", 2, {})
    assert (a1.ai_discover.status, a1.ai_discover.requests) == ("partial", 1)
    assert a1.ai_discover.errors_by_code == {"skipped_budget": n1 - 1}
    assert (a2.ai_verify.status, a2.ai_verify.requests, a2.ai_verify.errors_by_code) == (
        "partial", 0, {"skipped_budget": 1})
    assert (a2.ai_discover.status, a2.ai_discover.errors_by_code) == ("partial", {"skipped_budget": n2})
    assert a1.status == a2.status == "done"
    skipped = [v for v in env.store.list("verdicts") if (v.error or {}).get("code") == "skipped_budget"]
    assert len(skipped) == n1 - 1 + 1 + n2  # recorded, never dropped


class CountingVerifier(AV.FakeVerifier):
    def _call(self, system, content, batch):
        r = super()._call(system, content, batch)
        first = batch[0].id.endswith("fake-0000")
        r.input_tokens, r.output_tokens = 1000, 50
        r.cache_creation_input_tokens, r.cache_read_input_tokens = 0, 5
        r.latency_ms = 100.0 if first else 300.0
        return r


class CountingDiscoverer(AD.FakeDiscoverer):
    def _call(self, system, content, tile):
        r = super()._call(system, content, tile)
        r.input_tokens, r.output_tokens, r.cache_creation_input_tokens, r.cache_read_input_tokens = 2000, 10, 0, 0
        r.latency_ms = 40.0
        return r


def test_usage_summed_per_stage_in_run_status(env):
    detector = FakeDetector({A1: points(env, A1, 12)})
    run = env.analyzer(CountingVerifier(), CountingDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    n = len(AD.tiles(env.views(env.sha, A1)["region"]["bbox_pt"], A1))
    v, d = run.sheets[A1].ai_verify, run.sheets[A1].ai_discover
    assert (v.status, v.requests, v.n_items) == ("done", 2, 12)
    assert v.tokens == TM.Tokens(input=2000, output=100, cache_creation=0, cache_read=10)
    assert (v.latency_ms.p50, v.latency_ms.p95) == (100.0, 300.0)
    assert (d.status, d.requests) == ("done", n)
    assert d.tokens == TM.Tokens(input=2000 * n, output=10 * n, cache_creation=0, cache_read=0)
    assert (d.latency_ms.p50, d.latency_ms.p95) == (40.0, 40.0)
    usage = env.analyzer().ai_usage(run.id)  # summed over the run's sheets
    assert usage["verify"]["requests"] == 2 and usage["verify"]["tokens"]["input"] == 2000
    assert usage["discover"]["requests"] == n and usage["discover"]["tokens"]["input"] == 2000 * n


# --- AI effects through the merge ---------------------------------------------------------------------------

def tile_point(mapping: dict, region, sheet_id: str, pt, category: str, subtype=None) -> tuple[str, dict]:
    """The tile in which ``pt`` is most central, and the AI point (tile pixels) for it."""
    tile = max(AD.tiles(region, sheet_id), key=lambda t: AD.centrality_pt(t, pt))
    x, y = AD.pt_to_tile_px(mapping, tile, pt)
    return tile.id, {"x_px": x, "y_px": y, "category": category, "subtype": subtype, "uncertain": False,
                     "rationale": "fake: scripted point"}


def test_ai_only_candidates_created_flagged_and_not_bulk_eligible(env):
    view = env.views(env.sha, A1)
    p0, p1, far = clear_points(view, 3, sep=60.0)
    detector = FakeDetector({A1: [("switch", "standard", *p0), ("receptacle", "afi_duplex", *p1)]})
    script: dict[str, list] = {}
    for pt, cat, sub in ((far, "light", "recessed_downlight"), (p0, "switch", None), (p1, "light", None)):
        tid, point = tile_point(view["image"], view["region"]["bbox_pt"], A1, pt, cat, sub)
        script.setdefault(tid, []).append(point)
    run = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer(script), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    s = run.sheets[A1]
    assert s.status == "done" and s.ai_discover.status == "done" and s.ai_discover.n_items == 3

    ai_dets = [d for d in env.store.list("detections", A1) if d.source == "ai_discover"]
    assert len(ai_dets) == 3 and all(d.supporting and d.verdict_id for d in ai_dets)
    ai_devices = [d for d in env.devices() if d.origin == "ai"]
    assert len(ai_devices) == 1
    (cand,) = ai_devices
    assert cand.point_pt == pytest.approx(far, abs=1e-6) and (cand.category, cand.subtype) == ("light", "recessed_downlight")
    assert (cand.review_state, cand.created_by_run) == ("proposed", "run-001")
    (flag,) = env.open_flags(cand.id, "ai_only_candidate")
    assert flag.severity == "review" and flag.details["verdict_id"] and flag.details["tile_id"]
    assert flag.details["rationale"] == "fake: scripted point"

    sw, rec = env.device_of("run-001", det_id(A1, 0)), env.device_of("run-001", det_id(A1, 1))
    assert {e.outcome for e in env.store.list("evidence") if e.device_id == sw.id} == {"created", "ai_corroborates"}
    (md,) = env.open_flags(rec.id, "model_disagrees")
    assert (md.details["ai_category"], md.details["source"]) == ("light", "discover")
    assert (rec.category, rec.review_state) == ("receptacle", "proposed")

    eligible = env.svc.bulk_eligible_ids(env.sha, A1)
    assert cand.id not in eligible and rec.id not in eligible and sw.id in eligible
    assert env.official() == 0  # no AI output feeds a total
    env.svc.accept(cand.id, ACTOR)  # counted only after an individual acceptance
    assert env.official() == 1 and env.open_flags(cand.id, "ai_only_candidate") == []


def test_verify_disagreement_flags_and_changes_nothing(env):
    detector = FakeDetector({A1: points(env, A1, 3)})
    script = {f"run-001:{det_id(A1, 1)}": {"category": "receptacle", "subtype": "gfi", "rationale": "looks like a GFI"},
              f"run-001:{det_id(A1, 2)}": {"uncertain": True}}
    run1 = env.analyzer(AV.FakeVerifier(script), AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    assert run1.sheets[A1].ai_verify.status == "done"
    d0, d1, d2 = (env.device_of("run-001", det_id(A1, n)) for n in range(3))
    (f1,) = env.open_flags(d1.id, "model_disagrees")
    assert (f1.details["ai_category"], f1.details["ai_subtype"], f1.details["source"]) == ("receptacle", "gfi", "verify")
    assert f1.details["rationale"] == "looks like a GFI"
    assert len(env.open_flags(d2.id, "model_disagrees")) == 1 and env.open_flags(d0.id, "model_disagrees") == []
    assert {(d.category, d.subtype, d.review_state) for d in env.devices()} == {("switch", "standard", "proposed")}
    assert env.svc.bulk_eligible_ids(env.sha, A1) == [d0.id]
    assert env.official() == 0  # agreement never auto-accepts

    env.svc.accept(d0.id, ACTOR)
    before = env.store.get("devices", d0.id)
    run2 = env.analyzer(AV.FakeVerifier({f"run-002:{det_id(A1, 0)}": {"category": "light", "subtype": "pendant"}}),
                        AD.FakeDiscoverer(), detector).start(env.sha, ACTOR, sheet_ids=[A1])
    assert run2.sheets[A1].status == "done"
    after = env.store.get("devices", d0.id)
    assert after == before  # the device record is untouched: same fields, state and revision
    (f0,) = env.open_flags(d0.id, "model_disagrees")
    assert f0.run_id == "run-002" and f0.details["ai_category"] == "light"
    assert env.official() == 1 and len(env.devices()) == 3


# --- the real PDF: two runs with corrections in between -------------------------------------------------------

HUMAN = ("category", "subtype", "room", "point", "review_state")


def test_two_runs_on_a1_preserve_corrections(env, sha):
    expected = [d for d in det0.detect(PDF, CA.generate(PDF, pages=[1]))["detections"] if d["sheet_id"] == A1]
    an = env.analyzer(AV.FakeVerifier(), AD.FakeDiscoverer())  # the real candidates and det/0
    run1 = an.start(sha, ACTOR, sheet_ids=[A1])
    s1 = run1.sheets[A1]
    assert s1.status == "done" and s1.n_detections == len(expected) > 0
    assert (run1.detector, run1.detector_version, run1.params["detector"]) == ("det0", "det/0", det0.PARAMS)
    assert run1.ai_versions["verify"] == "ai-verify/1" and run1.ai_versions["discover"] == "ai-discover/1"

    # 1. RunDetections equal det/0's output, with the run id
    dets = sorted((d for d in env.store.list("detections", A1) if d.run_id == run1.id and d.source == "det0"),
                  key=lambda d: d.id)
    assert [d.id for d in dets] == sorted(f"{run1.id}:{x['id']}" for x in expected)
    by_id = {f"{run1.id}:{x['id']}": x for x in expected}
    for d in dets:
        x = by_id[d.id]
        assert (d.category, d.subtype, d.status, d.point_pt, d.score, d.members, d.subtype_source, d.tokens) == (
            x["category"], x["subtype"], x["status"], (x["x_pt"], x["y_pt"]), x["score"], x["members"],
            x["subtype_source"], x["tokens"])
    devices1 = env.devices()
    assert len(devices1) == len(expected)
    assert {(d.origin, d.review_state, d.created_by_run) for d in devices1} == {("detector", "proposed", run1.id)}
    verify = [v for v in env.store.list("verdicts", A1) if v.run_id == run1.id and v.kind == "verify"]
    assert len(verify) == math.ceil(len(expected) / AV.VERIFY_BATCH_SIZE)  # a verdict per batch
    assert all(v.error is None and v.implementation == "fake" for v in verify)
    assert sorted(i for v in verify for i in v.detection_ids) == [d.id for d in dets]

    # 2. corrections: a subtype, a move, a reject, a manual device
    view = env.views(sha, A1)
    g = C.PageGeometry(*view["geometry"]["crop_native"], rotation=view["geometry"]["rotation"])

    def isolated(d):  # nearest other device more than 20 pt away, so a 3 pt move stays unambiguous
        return all(math.dist(d.point_pt, o.point_pt) > 20 for o in devices1 if o.id != d.id)

    pool = [d for d in devices1 if isolated(d)]
    sub_dev = next(d for d in pool if d.category == "switch")
    move_dev = next(d for d in pool if d.id != sub_dev.id)
    rej_dev = next(d for d in pool if d.id not in (sub_dev.id, move_dev.id))
    new_sub = next(s for s in ("dimmer", "three_way", "standard") if s != sub_dev.subtype)
    env.svc.update_device(sub_dev.id, ACTOR, {"subtype": new_sub})
    moved_pt = (move_dev.point_pt[0] + 3.0, move_dev.point_pt[1])
    u, v = C.unrotated_to_normalized(g, moved_pt)
    env.svc.update_device(move_dev.id, ACTOR, {"point": {"u": u, "v": v}})
    env.svc.reject(rej_dev.id, ACTOR, "not a device")
    (mx, my), = clear_points(view, 1, sep=40.0, avoid=[d.point_pt for d in devices1])
    mu, mv = C.unrotated_to_normalized(g, (mx, my))
    manual = env.svc.create_device(sha, A1, ACTOR, "receptacle", "gfi", {"u": mu, "v": mv})
    corrected = {d.id: env.store.get("devices", d.id) for d in env.devices()}
    mark = max(e.id for e in env.store.events())

    # 3. run 2
    run2 = an.start(sha, ACTOR, sheet_ids=[A1])
    assert run2.id != run1.id and run2.sheets[A1].status == "done"
    assert run2.sheets[A1].n_detections == len(expected)
    devices2 = env.devices()
    assert {d.id for d in devices2} == set(corrected)  # every existing device kept, none new, none deleted
    for d in devices2:
        before = corrected[d.id]
        assert all(getattr(d, f) == getattr(before, f) for f in HUMAN), d.id
        assert d.revision == before.revision
    assert env.store.get("devices", sub_dev.id).subtype == new_sub
    assert env.store.get("devices", move_dev.id).point_pt == pytest.approx(moved_pt, abs=1e-3)
    assert env.store.get("devices", rej_dev.id).review_state == "rejected"
    run2_ev = [e for e in env.store.list("evidence") if e.run_id == run2.id]
    assert {e.outcome for e in run2_ev if e.device_id == rej_dev.id} == {"rejected_redetected"}
    assert {e.outcome for e in run2_ev if e.device_id == move_dev.id} == {"confirmed"}
    assert {e.outcome for e in run2_ev if e.device_id == sub_dev.id} == {"conflict"}
    (nr,) = [f for f in env.open_flags(manual.id, "not_redetected") if f.run_id == run2.id]
    assert nr.severity == "info"

    # no event of run 2 changes a device (so none updates a human-set field)
    later = [e for e in env.store.events() if e.id > mark and not (e.entity == "run" and e.action == "create")]
    assert later and all(e.actor == f"analyzer:{run2.id}" for e in later)
    for e in later:
        if e.entity == "device" and e.before is not None:
            assert all(e.before.get(f) == e.after.get(f) for f in e.before.get("human_set", [])), e
    assert not [e for e in later if e.entity == "device"]


def test_det0_reads_only_the_requested_page(monkeypatch):
    """The analyzer's det/0 reads one page, so another page it cannot read never fails the sheet."""
    seen = []
    real = det0._sheet_tokens

    def recording(page):
        seen.append(page.number + 1)
        return real(page)

    monkeypatch.setattr(det0, "_sheet_tokens", recording)
    out = det0.detect(PDF, [], pages=[2])
    assert seen == [2] and out["detections"] == []
    seen.clear()
    det0.detect(PDF, [])
    assert seen == [1, 2, 3, 4]
