"""B-11 AC-E1, E3-E6: the evaluator on the committed verified export gt_001.

Candidates are regenerated once per module (about 90 s) and must reproduce the
run hashes recorded in the export manifest, i.e. exactly the candidates the
reviewer saw.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from takeoff.evaluation import evaluate as EV
from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth.export import ExportError

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
EXPORT = ROOT / "data" / "ground_truth" / "exports" / "gt_001_20261025T020009Z"
pytestmark = pytest.mark.skipif(not (PDF.exists() and EXPORT.exists()), reason="supplied PDF or gt_001 export missing")
GENERATORS = ("raster_plain", "raster_thickened", "vector_signature")


@pytest.fixture(scope="module")
def results():
    return CA.generate(PDF)


@pytest.fixture(scope="module")
def ev(results):
    return EV.evaluate(EXPORT, PDF, results=results)


# --- AC-E1: guards -------------------------------------------------------------------------------

def test_refuses_an_export_that_is_not_verified(tmp_path, results):
    bad = tmp_path / "gt_x"
    shutil.copytree(EXPORT, bad)
    m = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    m["level"] = "incomplete"
    (bad / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ExportError):
        EV.evaluate(bad, PDF, results=results)


def test_refuses_an_altered_export(tmp_path, results):
    bad = tmp_path / "gt_y"
    shutil.copytree(EXPORT, bad)
    with open(bad / "annotations.jsonl", "ab") as f:
        f.write(b"\n")
    with pytest.raises(ExportError):
        EV.evaluate(bad, PDF, results=results)


def test_refuses_candidates_the_reviewer_did_not_see(results):
    run, cands = results[0]
    other = [(run.model_copy(update={"run_hash": "f" * 64}), cands)] + list(results[1:])
    with pytest.raises(EV.EvaluationError, match="run hash"):
        EV.evaluate(EXPORT, PDF, results=other)


# --- AC-E3: traceability ----------------------------------------------------------------------------

def test_ground_truth_counts_equal_the_export_totals(ev):
    totals = json.loads((EXPORT / "totals.json").read_text(encoding="utf-8"))["totals"]
    for gen in GENERATORS:
        per = ev.metrics["generators"][gen]["primary"]["by_sheet_category"]
        for sheet, t in totals.items():
            for cat, subs in t["by_category"].items():
                assert per[sheet][cat]["n_gt"] == sum(subs.values()), (gen, sheet, cat)
    assert ev.metrics["ground_truth"]["annotations"] == 300


def test_every_candidate_and_device_is_accounted_for(ev, results):
    for run, cands in results:
        g = ev.metrics["generators"][run.generator]
        scope = g["scope"]
        assert scope["generated"] == len(cands)
        assert scope["in_scope"] + scope["outside_region"] + scope["unmapped_class"] == len(cands)
        t = g["primary"]["total"]
        assert t["tp"] + t["fp"] == scope["in_scope"] and t["tp"] + t["fn"] == 300
        rows = [r for r in ev.matches if r["generator"] == run.generator and r["tolerance_pt"] == ev.metrics["tolerance_pt"]]
        kinds = Counter(r["outcome"] for r in rows)
        assert (kinds["tp"], kinds["fp"], kinds["fn"]) == (t["tp"], t["fp"], t["fn"])
        assert len({r["pred_id"] for r in rows if r["pred_id"]}) == scope["in_scope"]
        assert len({r["gt_id"] for r in rows if r["gt_id"]}) == 300
        dups = sum(1 for r in rows if r["kind"] == "duplicate")
        assert sum(d["n"] for d in g["duplicate_template_pairs"]) == dups == t["fp_kinds"].get("duplicate", 0)


def test_union_recall_counts_each_device_once(ev):
    u = ev.metrics["union_recall"]["total"]
    assert u["n_gt"] == 300 and 0 <= u["found"] <= 300


# --- AC-E5 / AC-E6: versioned, deterministic, honest scope ------------------------------------------------

def test_metrics_are_versioned_and_deterministic(ev, results):
    m = ev.metrics
    assert m["evaluator_version"] == EV.EVALUATOR_VERSION and m["tolerance_pt"] == 6.0
    assert m["sensitivity_tolerances_pt"] == [3.0, 12.0]
    assert m["export"]["id"] == EXPORT.name and len(m["export"]["manifest_sha256"]) == 64
    assert {r["generator"]: r["run_hash"] for r in m["candidate_runs"]} == {r.generator: r.run_hash for r, _ in results}
    again = EV.evaluate(EXPORT, PDF, results=results)
    assert EV.comparable(again.metrics) == EV.comparable(m)


def test_report_discloses_the_scope_limits(ev):
    b = ev.metrics["ground_truth"]["provenance"]
    assert b == {"candidate": 298, "manual": 2}
    assert ev.metrics["not_evaluated"].keys() >= {"subtype", "room"}


# --- AC-E4: written outputs ------------------------------------------------------------------------------

def test_writes_report_files_and_never_overwrites(ev, tmp_path):
    out = tmp_path / "eval_out"
    EV.write(ev, out, PDF, cache_dir=tmp_path / "cache")
    for name in ("metrics.json", "matches.jsonl", "report.md"):
        assert (out / name).stat().st_size > 0
    pngs = sorted((out / "examples").glob("*.png"))
    assert pngs
    report = (out / "report.md").read_text(encoding="utf-8")
    assert all(f"examples/{p.name}" in report for p in pngs)
    assert "not detector" in report.lower() or "not a detector" in report.lower()
    with pytest.raises(FileExistsError):
        EV.write(ev, out, PDF, cache_dir=tmp_path / "cache")


# --- eval/1.1: detectors, subtype accuracy, sheet groups (det/0 experiment) ------------------------------------

EVAL1 = ROOT / "reports" / "evaluation" / "gt_001_20261025T020009Z__eval1" / "metrics.json"
GROUPS = {"dev": ["A1-111", "A2-111"], "held_out": ["A3-111", "A4-111"]}


@pytest.fixture(scope="module")
def ev_det(results):
    from takeoff.detection import det0
    return EV.evaluate(EXPORT, PDF, results=results, detectors={"det0": det0.detect}, groups=GROUPS)


def test_generator_metrics_are_unchanged_from_eval1(ev_det):
    old = json.loads(EVAL1.read_text(encoding="utf-8"))
    assert ev_det.metrics["generators"] == old["generators"]
    assert ev_det.metrics["union_recall"] == old["union_recall"]


def test_detector_predictions_are_fully_accounted_for(ev_det):
    d = ev_det.metrics["detectors"]["det0"]
    t = d["primary"]["total"]
    assert t["tp"] + t["fn"] == 300 and t["tp"] + t["fp"] == d["scope"]["in_scope"]
    assert d["detector_version"] == "det/0" and d["params"]["site_radius_pt"] == 4.0
    assert {r["generator"] for r in d["input_runs"]} == {"raster_thickened", "vector_signature"}
    rows = [r for r in ev_det.matches if r["generator"] == "det0" and r["tolerance_pt"] == ev_det.metrics["tolerance_pt"]]
    assert Counter(r["outcome"] for r in rows)["tp"] == t["tp"]


def test_subtype_accuracy_is_over_category_matches(ev_det):
    s = ev_det.metrics["detectors"]["det0"]["subtype"]
    assert s["evaluated_on"] == ev_det.metrics["detectors"]["det0"]["primary"]["total"]["tp"]
    assert s["correct"] + sum(c["n"] for c in s["confusions"]) == s["evaluated_on"]
    assert set(s["by_category"]) <= set(ev_det.metrics["ground_truth"]["by_category"])


def test_group_totals_add_up_to_the_sheet_totals(ev_det):
    g = ev_det.metrics["groups"]
    assert g["definition"] == GROUPS
    for name, sys in [("det0", ev_det.metrics["detectors"]["det0"])] + list(ev_det.metrics["generators"].items()):
        per = sys["primary"]["by_sheet_category"]
        for grp, sheets in GROUPS.items():
            want = sum(v["tp"] for s in sheets for v in per[s].values())
            assert g["systems"][name][grp]["tp"] == want, (name, grp)
        assert sum(g["systems"][name][grp]["n_gt"] for grp in GROUPS) == 300


def test_detector_report_compares_against_the_baseline(ev_det, tmp_path):
    out = tmp_path / "det_out"
    EV.write(ev_det, out, PDF, cache_dir=tmp_path / "cache")
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "`det0`" in report and "held_out" in report and "Subtype" in report
    assert (out / "detections_det0.jsonl").stat().st_size > 0
    assert sorted((out / "examples").glob("det0_*.png"))
