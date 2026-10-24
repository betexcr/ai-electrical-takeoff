"""AC9: candidate generation, deterministic runs, idempotent import, clustering.

Candidate numbers here are compared with the Milestone 1 probe's own report
(same primitives, same threshold) as a regression check; they are not
detection results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth import models as M
from takeoff.groundtruth.store import Store
from takeoff.pdf import coords as C

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
PROBE = ROOT / "reports" / "feasibility" / "symbols" / "symbol_probe.json"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")

G = {"A1-111": C.PageGeometry(0, 0, 2592, 1728)}


def cand(i, u, v, score, run_id="r1", cls="switch_standard"):
    return M.Candidate(id=f"c{i}", run_id=run_id, sheet_id="A1-111", page_number=1, class_hypothesis=cls,
                       score=score, point={"u": u, "v": v}, point_pt=(u * 2592, v * 1728))


RUNS = {
    "r1": M.CandidateRun(id="r1", generator="raster_plain", generator_version="v", params={}, input_sha256="0" * 64, run_hash="a"),
    "r2": M.CandidateRun(id="r2", generator="vector_signature", generator_version="v", params={}, input_sha256="0" * 64, run_hash="b"),
}


def test_clusters_merge_colocated_candidates_across_runs():
    du = 3.0 / 2592  # 3 pt apart
    cs = [cand(1, 0.5, 0.5, 0.9), cand(2, 0.5 + du, 0.5, None, run_id="r2", cls="receptacle_gfi_duplex"),
          cand(3, 0.6, 0.5, 0.8)]
    clusters = CA.cluster_candidates(cs, RUNS, G)
    assert len(clusters) == 2
    big = next(c for c in clusters if len(c["members"]) == 2)
    assert big["id"] == "cl-c1" and big["kind"] == "candidate_cluster"
    assert {m["generator"] for m in big["members"]} == {"raster_plain", "vector_signature"}


def test_clustering_is_deterministic_and_respects_tolerance():
    du = 7.0 / 2592  # beyond the 6 pt tolerance
    cs = [cand(1, 0.5, 0.5, 0.7), cand(2, 0.5 + du, 0.5, 0.9)]
    a = CA.cluster_candidates(cs, RUNS, G)
    b = CA.cluster_candidates(list(reversed(cs)), RUNS, G)
    assert len(a) == 2 and a == b


def test_class_hints_follow_the_taxonomy():
    assert CA.class_hint("detector_smoke.png") == ("detector", "smoke_detector")
    assert CA.class_hint("ceiling_fan_optional_pre_wire.png") == ("ceiling_fan", "optional_pre_wire")
    assert CA.class_hint("wiring_run_electrical_REFERENCE_BOM.png") == (None, None)


@pytest.fixture(scope="module")
def generated():
    before = hashlib.sha256(PDF.read_bytes()).hexdigest()
    kw = dict(pages=[1], generators=("raster_plain", "vector_signature"), classes=["detector_smoke.png"])
    first = CA.generate(PDF, **kw)
    second = CA.generate(PDF, **kw)
    after = hashlib.sha256(PDF.read_bytes()).hexdigest()
    return first, second, before, after


@needs_pdf
def test_generation_is_deterministic_and_leaves_input_unchanged(generated):
    first, second, before, after = generated
    assert before == after
    for (r1, c1), (r2, c2) in zip(first, second):
        assert r1.run_hash == r2.run_hash and r1.id == r2.id
        assert [c.id for c in c1] == [c.id for c in c2]


@needs_pdf
def test_run_records_generator_version_and_params(generated):
    (run, _), _ = generated[0]
    assert run.generator == "raster_plain"
    assert run.generator_version == CA.GENERATOR_VERSION
    assert run.params["threshold"] == CA.DEFAULT_PARAMS["raster_plain"]["threshold"]
    assert run.params["classes"] == ["detector_smoke.png"] and run.params["pages"] == [1]
    assert run.input_sha256 == hashlib.sha256(PDF.read_bytes()).hexdigest()


@needs_pdf
def test_candidates_fall_inside_the_electrical_window(generated):
    import pymupdf
    from takeoff.groundtruth import sheets as S

    page = pymupdf.open(PDF)[0]
    x0, y0, x1, y1 = S.electrical_region(page)["window_pt"]
    for run, cands in generated[0]:
        for c in cands:
            assert x0 <= c.point_pt[0] <= x1 and y0 <= c.point_pt[1] <= y1
            assert (c.category_hint, c.subtype_hint) == ("detector", "smoke_detector")


@needs_pdf
@pytest.mark.skipif(not PROBE.exists(), reason="Milestone 1 probe report not present")
def test_generation_reproduces_the_milestone1_probe(generated):
    probe = json.loads(PROBE.read_text(encoding="utf-8"))["pages"]["1"]
    (raster_run, raster), (vector_run, vector) = generated[0]
    expected_raster = probe["raster"]["detector_smoke.png"]["candidates_by_threshold"]["0.7"]
    expected_vector = probe["vector"]["detector_smoke.png"]["matching_components_in_region"]
    assert len(raster) == expected_raster
    assert len(vector) == expected_vector
    assert all(c.score is None for c in vector)


@needs_pdf
def test_import_is_idempotent(generated, tmp_path):
    store = Store(tmp_path / "gt.sqlite")
    first, second, *_ = generated
    for run, cands in first:
        assert store.import_candidate_run(run, cands, actor="importer")[1] is True
    for run, cands in second:
        assert store.import_candidate_run(run, cands, actor="importer")[1] is False
    assert len(store.list("candidates")) == sum(len(c) for _, c in first)
    store.close()
