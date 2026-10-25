"""Evaluate the ground-truth candidate generators against a verified export (``eval/1``, tracker B-11).

Inputs are fixed and checked before any metric is computed:

* the export must pass ``export.require_verified`` (level ``verified``, file hashes intact);
* the PDF must be the one the export was made from (SHA-256 in the manifest);
* the candidates must reproduce the run hashes in the manifest, i.e. be exactly
  the candidates the reviewer saw.

Candidates are evaluated by the category their template implies; subtype comes
from adjacent text tokens and rooms from labels, neither of which the
generators predict, so neither is scored. The ground truth was built with these
candidates as hints, so the report states that bias next to every number.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from takeoff.evaluation import matching as EM
from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth import models as M
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth.export import require_verified
from takeoff.groundtruth.protocol import MATCH_TOLERANCE_PT

# eval/1.1 adds detectors, subtype accuracy for systems that predict subtypes, and
# sheet-group totals. Matching and every eval/1 field are unchanged; a test checks
# the generator metrics against the committed eval/1 report.
EVALUATOR_VERSION = "eval/1.1"
TOLERANCE_PT = MATCH_TOLERANCE_PT
SENSITIVITY_TOLERANCES_PT = (3.0, 12.0)
EXAMPLES_PER_OUTCOME = 8  # per generator, for false positives and for misses
TILE_PT = 48.0
ROOT = Path(__file__).resolve().parents[3]
NOT_EVALUATED = {
    "subtype": "Not scored for generators: each template implies one fixed subtype, while real subtypes come from adjacent text tokens. Scored for detectors.",
    "room": "Generators assign no room; room extraction (B-06) is not built yet.",
    "status": "Status follows from category (ceiling fans are optional_pre_wire); not predicted separately.",
}


class EvaluationError(RuntimeError):
    pass


@dataclass
class Evaluation:
    metrics: dict
    matches: list[dict]
    gt: list[EM.Item]
    preds: dict[str, list[EM.Item]]
    pred_info: dict[str, dict]
    regions: dict[str, list[float]]
    page_numbers: dict[str, int]
    primary: dict[str, tuple[EM.Result, EM.Kinds]] = field(default_factory=dict)
    detections: dict[str, dict] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git() -> dict:
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True,
                                    check=True).stdout.strip())
        return {"git_commit": head, "git_dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": None, "git_dirty": None}


def comparable(metrics: dict) -> dict:
    """Metrics without the fields that legitimately differ between identical runs."""
    return {k: v for k, v in metrics.items() if k not in ("created_at", "code")}


def _rates_only(summary: dict) -> dict:
    keep = ("n_gt", "n_pred", "tp", "fp", "fn", "precision", "recall", "f1")
    return {"total": {k: summary["total"][k] for k in keep},
            "by_category": {c: {k: v[k] for k in keep} for c, v in summary["by_category"].items()}}


def _duplicate_pairs(gt, preds, pred_info, r: EM.Result, k: EM.Kinds) -> list[dict]:
    """For each duplicate false positive: its template and the template of the hit that took the device."""
    pred_by_id = {p.id: p for p in preds}
    gt_by_id = {g.id: g for g in gt}
    pairs = Counter()
    for pid, kind in k.fp.items():
        if kind != "duplicate":
            continue
        p = pred_by_id[pid]
        near = [(math.hypot(gt_by_id[t.gt_id].x_pt - p.x_pt, gt_by_id[t.gt_id].y_pt - p.y_pt), t) for t in r.tp
                if gt_by_id[t.gt_id].sheet_id == p.sheet_id and gt_by_id[t.gt_id].category == p.category]
        _, t = min(near, key=lambda x: (x[0], x[1].gt_id))
        pairs[(pred_info[pid]["class_hypothesis"], pred_info[t.pred_id]["class_hypothesis"])] += 1
    return [{"duplicate_template": a, "matched_template": b, "n": n}
            for (a, b), n in sorted(pairs.items(), key=lambda x: (-x[1], x[0]))]


def _score(name: str, gt: list[EM.Item], items: list[EM.Item]) -> tuple[dict, tuple[EM.Result, EM.Kinds], list[dict]]:
    out, rows, primary = {"sensitivity": {}}, [], None
    for tol in (TOLERANCE_PT, *SENSITIVITY_TOLERANCES_PT):
        r = EM.match(gt, items, tol)
        k = EM.classify(gt, items, r, tol)
        s = EM.summarize(gt, items, r, k)
        if tol == TOLERANCE_PT:
            out["primary"] = s
            primary = (r, k)
        else:
            out["sensitivity"][f"{tol:g}"] = _rates_only(s)
        cat = {x.id: (x.sheet_id, x.category) for x in gt + items}
        rows += [{"generator": name, "tolerance_pt": tol, "outcome": "tp", "gt_id": t.gt_id, "pred_id": t.pred_id,
                  "sheet_id": cat[t.gt_id][0], "category": cat[t.gt_id][1], "distance_pt": t.distance_pt, "kind": None}
                 for t in r.tp]
        rows += [{"generator": name, "tolerance_pt": tol, "outcome": "fp", "gt_id": None, "pred_id": i,
                  "sheet_id": cat[i][0], "category": cat[i][1], "distance_pt": None, "kind": k.fp[i]} for i in r.fp]
        rows += [{"generator": name, "tolerance_pt": tol, "outcome": "fn", "gt_id": i, "pred_id": None,
                  "sheet_id": cat[i][0], "category": cat[i][1], "distance_pt": None, "kind": k.fn[i]} for i in r.fn]
    return out, primary, rows


def _in_region(regions, sheet_id, x, y) -> bool:
    x0, y0, x1, y1 = regions[sheet_id]
    return x0 <= x <= x1 and y0 <= y <= y1


def _subtype_metrics(tp: list[EM.Pair], gt_subtype: dict[str, str], pred_subtype: dict[str, str], gt_cat: dict[str, str]) -> dict:
    """Subtype agreement over category matches (true positives) only."""
    by_cat, confusions = {}, Counter()
    for t in tp:
        c, g, p = gt_cat[t.gt_id], gt_subtype[t.gt_id], pred_subtype[t.pred_id]
        v = by_cat.setdefault(c, {"n": 0, "correct": 0})
        v["n"] += 1
        if g == p:
            v["correct"] += 1
        else:
            confusions[(c, g, p)] += 1
    for v in by_cat.values():
        v["accuracy"] = round(v["correct"] / v["n"], 4)
    n, ok = len(tp), sum(v["correct"] for v in by_cat.values())
    return {"evaluated_on": n, "correct": ok, "accuracy": round(ok / n, 4) if n else None,
            "by_category": dict(sorted(by_cat.items())),
            "confusions": [{"category": c, "gt": g, "pred": p, "n": k}
                           for (c, g, p), k in sorted(confusions.items(), key=lambda x: (-x[1], x[0]))]}


def _group_totals(summary: dict, sheets: list[str]) -> dict:
    cells = [v for s in sheets for v in summary["by_sheet_category"].get(s, {}).values()]
    n = {k: sum(c[k] for c in cells) for k in ("n_gt", "n_pred", "tp", "fp", "fn")}
    return {**n, **EM.rates(n["tp"], n["fp"], n["fn"])}


def evaluate(export_dir: Path, pdf_path: Path, results: list[tuple[M.CandidateRun, list[M.Candidate]]] | None = None,
             detectors: dict | None = None, groups: dict[str, list[str]] | None = None) -> Evaluation:
    """Score the candidate generators and, optionally, detectors built from the same checked candidates.

    ``detectors`` maps a name to ``fn(pdf_path, results) -> {"detector_version", "params", "input_runs",
    "detections": [{"id", "sheet_id", "category", "subtype", "x_pt", "y_pt", "score", ...}]}``. A detector
    only ever sees the PDF and the candidates, never the ground truth. ``groups`` names sets of sheets
    (e.g. development and held-out) whose totals are reported side by side.
    """
    export_dir = Path(export_dir)
    manifest = require_verified(export_dir)
    if _sha256(pdf_path) != manifest["input_pdf_sha256"]:
        raise EvaluationError("the PDF is not the one this export was made from")
    if results is None:
        results = CA.generate(Path(pdf_path))
    expected = {r["generator"]: r["run_hash"] for r in manifest["candidate_runs"]}
    got = {run.generator: run.run_hash for run, _ in results}
    if set(got) != set(expected):
        raise EvaluationError(f"generators {sorted(got)} differ from the export's candidate runs {sorted(expected)}")
    for gen, h in sorted(got.items()):
        if h != expected[gen]:
            raise EvaluationError(f"{gen}: run hash {h[:12]}… differs from the export manifest ({expected[gen][:12]}…); "
                                  "these are not the candidates the reviewer saw")

    annotations = _jsonl(export_dir / "annotations.jsonl")
    gt = [EM.Item(a["id"], a["sheet_id"], a["category"], *a["point_pt"]) for a in annotations]
    regions = {sid: s["region_bbox_pt"] for sid, s in manifest["sheets"].items()}

    preds, pred_info, scopes = {}, {}, {}
    for run, cands in results:
        items, outside, unmapped = [], 0, 0
        for c in cands:
            x, y = c.point_pt
            if c.category_hint is None:
                unmapped += 1
            elif not _in_region(regions, c.sheet_id, x, y):
                outside += 1
            else:
                items.append(EM.Item(c.id, c.sheet_id, c.category_hint, x, y, c.score))
                pred_info[c.id] = {"generator": run.generator, "class_hypothesis": c.class_hypothesis, "score": c.score}
        preds[run.generator] = items
        scopes[run.generator] = {"generated": len(cands), "in_scope": len(items), "outside_region": outside,
                                 "unmapped_class": unmapped}

    gens, matches, primary = {}, [], {}
    for gen in sorted(scopes):
        out, primary[gen], rows = _score(gen, gt, preds[gen])
        matches += rows
        gens[gen] = {"scope": scopes[gen], **out}
        gens[gen]["duplicate_template_pairs"] = _duplicate_pairs(gt, preds[gen], pred_info, *primary[gen])

    found = {t.gt_id for gen in gens for t in primary[gen][0].tp}
    union = {c: {"n_gt": n, "found": sum(1 for g in gt if g.category == c and g.id in found)}
             for c, n in sorted(Counter(g.category for g in gt).items())}
    for v in union.values():
        v["recall"] = round(v["found"] / v["n_gt"], 4)

    dets, detections, det_subtypes = {}, {}, {}
    gt_subtype = {a["id"]: a["subtype"] for a in annotations}
    gt_cat = {g.id: g.category for g in gt}
    gt_sheet = {g.id: g.sheet_id for g in gt}
    for name, fn in sorted((detectors or {}).items()):
        if name in scopes:
            raise EvaluationError(f"detector name {name!r} clashes with a generator")
        d = fn(Path(pdf_path), results)
        detections[name] = d
        items, outside, subtypes = [], 0, {}
        for x in d["detections"]:
            if not _in_region(regions, x["sheet_id"], x["x_pt"], x["y_pt"]):
                outside += 1
                continue
            items.append(EM.Item(x["id"], x["sheet_id"], x["category"], x["x_pt"], x["y_pt"], x.get("score")))
            subtypes[x["id"]] = x["subtype"]
            pred_info[x["id"]] = {"generator": name, "class_hypothesis": f"{x['category']}/{x['subtype']}", "score": x.get("score")}
        preds[name], det_subtypes[name] = items, subtypes
        out, primary[name], rows = _score(name, gt, items)
        matches += rows
        dets[name] = {"detector_version": d["detector_version"], "params": d["params"], "input_runs": d["input_runs"],
                      "scope": {"generated": len(d["detections"]), "in_scope": len(items), "outside_region": outside},
                      **out, "subtype": _subtype_metrics(primary[name][0].tp, gt_subtype, subtypes, gt_cat)}
    group_metrics = None
    if groups:
        systems = {**gens, **dets}
        group_metrics = {"definition": groups, "systems": {
            name: {grp: _group_totals(m["primary"], sheets) for grp, sheets in groups.items()} for name, m in systems.items()}}
        for name in dets:
            for grp, sheets in groups.items():
                tp = [t for t in primary[name][0].tp if gt_sheet[t.gt_id] in sheets]
                group_metrics["systems"][name][grp]["subtype_accuracy"] = _subtype_metrics(
                    tp, gt_subtype, det_subtypes[name], gt_cat)["accuracy"]
    reviews = _jsonl(export_dir / "candidate_reviews.jsonl")
    metrics = {
        "evaluator_version": EVALUATOR_VERSION,
        "created_at": M.utcnow(),
        "code": _git(),
        "tolerance_pt": TOLERANCE_PT,
        "sensitivity_tolerances_pt": list(SENSITIVITY_TOLERANCES_PT),
        "copied_position_eps_pt": EM.COPIED_EPS_PT,
        "export": {"id": export_dir.name, "manifest_sha256": _sha256(export_dir / "manifest.json"),
                   "level": manifest["level"], "export_format": manifest["export_format"],
                   "protocol_version": manifest["protocol_version"], "taxonomy_version": manifest.get("taxonomy_version"),
                   "schema_version": manifest["schema_version"], "input_pdf_sha256": manifest["input_pdf_sha256"]},
        "candidate_runs": [{"generator": run.generator, "run_hash": run.run_hash, "generator_version": run.generator_version,
                            "params": run.params} for run, _ in sorted(results, key=lambda x: x[0].generator)],
        "ground_truth": {"annotations": len(gt), "by_category": dict(sorted(Counter(g.category for g in gt).items())),
                         "provenance": dict(sorted(Counter(a["provenance"]["origin"] for a in annotations).items())),
                         "candidate_review_outcomes": dict(sorted(Counter(r["outcome"] for r in reviews).items()))},
        "generators": gens,
        "union_recall": {"by_category": union,
                         "total": {"n_gt": len(gt), "found": len(found), "recall": round(len(found) / len(gt), 4) if gt else None}},
        "not_evaluated": NOT_EVALUATED,
    }
    if dets:
        metrics["detectors"] = dets
    if group_metrics:
        metrics["groups"] = group_metrics
    pages = {sid: s["page_number"] for sid, s in manifest["sheets"].items()}
    return Evaluation(metrics, matches, gt, preds, pred_info, regions, pages, primary, detections)


# --- examples ---------------------------------------------------------------------------------------------

def _pick(ids: list[str], kind_of: dict[str, str], order: list[str], cat_of: dict[str, str], score_of, n: int) -> list[str]:
    """Up to n ids, spread over kinds (in priority order) and categories, highest score first."""
    buckets = {}
    for i in sorted(ids, key=lambda i: (order.index(kind_of[i]), cat_of[i], -(score_of(i) or 0.0), i)):
        buckets.setdefault((kind_of[i], cat_of[i]), []).append(i)
    picked, keys = [], sorted(buckets, key=lambda k: (order.index(k[0]), k[1]))
    while len(picked) < n and any(buckets[k] for k in keys):
        for k in keys:
            if buckets[k] and len(picked) < n:
                picked.append(buckets[k].pop(0))
    return picked


def _tile(region_img, mapping, center, gt_near, preds_near, focus, label) -> np.ndarray:
    scale = mapping["dpi"] / 72.0
    cx0, cy0 = mapping["covers_pt"][:2]
    half = int(TILE_PT / 2 * scale)
    px = lambda x, y: (int(round((x - cx0) * scale)), int(round((y - cy0) * scale)))  # noqa: E731
    cxp, cyp = px(*center)
    canvas = np.full((2 * half, 2 * half), 255, np.uint8)
    h, w = region_img.shape
    sx0, sy0, sx1, sy1 = max(0, cxp - half), max(0, cyp - half), min(w, cxp + half), min(h, cyp + half)
    if sx1 > sx0 and sy1 > sy0:
        canvas[sy0 - (cyp - half):sy1 - (cyp - half), sx0 - (cxp - half):sx1 - (cxp - half)] = region_img[sy0:sy1, sx0:sx1]
    tile = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)
    off = lambda x, y: (px(x, y)[0] - (cxp - half), px(x, y)[1] - (cyp - half))  # noqa: E731
    r = int(TOLERANCE_PT * scale)
    for g in gt_near:
        cv2.circle(tile, off(g.x_pt, g.y_pt), 9, (40, 160, 40), 2)  # ground truth: green
    for p in preds_near:
        x, y = off(p.x_pt, p.y_pt)
        cv2.rectangle(tile, (x - 7, y - 7), (x + 7, y + 7), (0, 140, 255), 1)  # other candidates: thin orange
    kind, item = focus
    x, y = off(item.x_pt, item.y_pt)
    if kind == "fp":
        cv2.rectangle(tile, (x - 9, y - 9), (x + 9, y + 9), (0, 0, 230), 3)  # the false positive: thick red square
    else:
        cv2.circle(tile, (x, y), 12, (0, 0, 230), 3)  # the missed device: thick red circle
    cv2.circle(tile, (x, y), r, (150, 150, 150), 1)  # match tolerance
    band = np.full((16 + 16 * len(label), tile.shape[1], 3), 255, np.uint8)
    for i, line in enumerate(label):
        cv2.putText(band, line, (4, 16 + 16 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 1, cv2.LINE_AA)
    return np.vstack([tile, band])


def _contact_sheet(tiles: list[np.ndarray], cols: int = 4) -> np.ndarray:
    th, tw = tiles[0].shape[:2]
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.full((rows * (th + 6), cols * (tw + 6), 3), 255, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet[r * (th + 6):r * (th + 6) + th, c * (tw + 6):c * (tw + 6) + tw] = t
    return sheet


def render_examples(ev: Evaluation, pdf_path: Path, out_dir: Path, cache_dir: Path) -> dict[str, dict]:
    """One contact sheet per generator and outcome: false positives (red squares) and misses (red circles)."""
    pdf_bytes = Path(pdf_path).read_bytes()
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = {}
    for sid, n in ev.page_numbers.items():
        page = doc[n - 1]
        path, mapping = S.render_region_image(page, tuple(ev.regions[sid]), sha, cache_dir)
        pages[sid] = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE), mapping)
    gt_by_id = {g.id: g for g in ev.gt}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for gen in sorted(ev.preds):
        r, k = ev.primary[gen]
        pred_by_id = {p.id: p for p in ev.preds[gen]}
        near = lambda items, c: [x for x in items if x.sheet_id == c.sheet_id and abs(x.x_pt - c.x_pt) <= TILE_PT / 2  # noqa: E731
                                 and abs(x.y_pt - c.y_pt) <= TILE_PT / 2]
        for outcome, ids, order in (("fp", r.fp, ["wrong_category", "background", "duplicate"]),
                                    ("fn", r.fn, ["not_found", "found_as_other_category"])):
            if not ids:
                continue
            item_of = pred_by_id if outcome == "fp" else gt_by_id
            kinds = k.fp if outcome == "fp" else k.fn
            chosen = _pick(ids, kinds, order, {i: item_of[i].category for i in ids},
                           (lambda i: ev.pred_info[i]["score"]) if outcome == "fp" else (lambda i: None), EXAMPLES_PER_OUTCOME)
            tiles = []
            for i in chosen:
                it = item_of[i]
                img, mapping = pages[it.sheet_id]
                if outcome == "fp":
                    info = ev.pred_info[i]
                    label = [f"FP {kinds[i]} {it.sheet_id}", info["class_hypothesis"][:30],
                             "score " + (f"{info['score']:.2f}" if info["score"] is not None else "n/a (vector)")]
                else:
                    label = [f"FN {kinds[i]}", f"{it.sheet_id} {it.category}", ""]
                tiles.append(_tile(img, mapping, (it.x_pt, it.y_pt), near(ev.gt, it), near(ev.preds[gen], it), (outcome, it), label))
            name = f"{gen}_{outcome}.png"
            cv2.imwrite(str(out_dir / name), _contact_sheet(tiles))
            written[name] = {"generator": gen, "outcome": outcome, "ids": chosen, "kinds": dict(Counter(kinds[i] for i in chosen))}
    doc.close()
    return written


# --- report -----------------------------------------------------------------------------------------------

def _f(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _loc(loc: dict) -> str:
    s = loc["independent"]
    return "—" if s is None else f"{s['median']:.2f} / {s['p90']:.2f} (n={s['n']})"


def report_md(ev: Evaluation, examples: dict[str, dict], reproduce: str | None = None) -> str:
    m = ev.metrics
    gt = m["ground_truth"]
    dets = m.get("detectors", {})
    systems = {**m["generators"], **dets}
    kind = lambda name: "detector" if name in dets else "generator"  # noqa: E731
    title = "Detector and candidate-generator evaluation" if dets else "Candidate-generator evaluation"
    L = [f"# {title} — {m['export']['id']} ({m['evaluator_version']})", "",
         "**The Milestone 1 generators are review-hint generators, not detectors.** Their numbers measure how well each "
         "template's hits match the hand-verified devices and serve as the baseline for choosing a detector approach (T-01)."]
    if dets:
        L += ["", "**Detectors:** " + "; ".join(
            f"`{n}` ({d['detector_version']}, built from " + ", ".join(f"`{r['generator']}`" for r in d["input_runs"]) + ")"
            for n, d in dets.items()) + ". Experimental; nothing here is a release-quality result."]
    L += ["", "## Read this first: what the numbers can and cannot show", "",
          f"- **The ground truth was built with the candidates as hints.** Of {gt['annotations']} devices, "
          f"{gt['provenance'].get('candidate', 0)} started as a candidate and {gt['provenance'].get('manual', 0)} were added by hand. "
          "A device that every generator and the reviewer missed cannot appear here, so **recall is an upper bound** for the "
          "generators and for any detector built from them.",
          "- **Accepted positions were copied.** Where the reviewer accepted a candidate without moving it, the ground-truth point "
          f"is that candidate's point (distance ≤ {m['copied_position_eps_pt']} pt). Localization is reported only over the "
          "*independent* pairs; the copied ones say nothing about accuracy.",
          "- **Single reviewer**; no inter-annotator agreement (see the export manifest's known limitations).",
          "- **Not evaluated:**", *[f"  - *{k}*: {v}" for k, v in m["not_evaluated"].items()],
          "- **Background false positives are where to spot-check the ground truth.** A hit on nothing is either an error or a "
          "device the review missed; the examples below show some."]
    if "groups" in m:
        L += ["- **Sheet groups:** " + "; ".join(f"*{g}* = {', '.join(s)}" for g, s in m["groups"]["definition"].items())
              + ". A detector's rules may only be tuned on the development group; the held-out group is the honest estimate."]
    L += ["", "## Method", "",
          f"- Export `{m['export']['id']}` (level `{m['export']['level']}`, {m['export']['protocol_version']}, "
          f"{m['export']['taxonomy_version']}, {m['export']['schema_version']}); manifest SHA-256 `{m['export']['manifest_sha256'][:16]}…`.",
          "- Candidates regenerated and checked against the manifest's run hashes: "
          + ", ".join(f"`{r['generator']}` `{r['run_hash'][:12]}`" for r in m["candidate_runs"]) + ".",
          "- Only predictions inside each sheet's confirmed electrical region count. A candidate's category is the one its "
          "template implies; a detector states its own category and subtype.",
          f"- One-to-one matching per sheet and category: pairs within **{m['tolerance_pt']:g} pt** (the protocol's match tolerance; "
          "boundary inclusive), nearest first. Sensitivity at " + " and ".join(f"{t:g}" for t in m["sensitivity_tolerances_pt"]) + " pt below.",
          "- False positives are split into *duplicate* (another hit on a device already matched), *wrong category* (on a device of "
          "another category) and *background*. Misses are *found as another category* or *not found*.",
          "- Subtype accuracy (detectors only) is measured over category matches.",
          f"- Code: `{m['code']['git_commit']}`" + (" (uncommitted changes present)" if m["code"]["git_dirty"] else "") + ".", "",
          "## Summary", "",
          "| System | Kind | In scope | TP | FP | FN | Precision | Recall | F1 | Subtype accuracy | Localization median / p90, pt (independent) |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, g in systems.items():
        t = g["primary"]["total"]
        sub = _f(g["subtype"]["accuracy"]) if "subtype" in g else "not predicted"
        L.append(f"| `{name}` | {kind(name)} | {g['scope']['in_scope']} | {t['tp']} | {t['fp']} | {t['fn']} | {_f(t['precision'])} | "
                 f"{_f(t['recall'])} | {_f(t['f1'])} | {sub} | {_loc(t['localization'])} |")
    L += ["", "Excluded before matching: " + "; ".join(
        f"`{n}` {g['scope']['outside_region']} outside the region"
        + (f", {g['scope']['unmapped_class']} with no category" if "unmapped_class" in g["scope"] else "")
        for n, g in systems.items()) + ".", ""]
    if "groups" in m:
        grps = list(m["groups"]["definition"])
        L += ["## By sheet group", "",
              "| System | " + " | ".join(f"{g} P / R / F1" for g in grps) + " | " + " | ".join(f"{g} subtype acc." for g in grps) + " |",
              "|---|" + "---|" * (2 * len(grps))]
        for name, per in m["groups"]["systems"].items():
            L.append(f"| `{name}` | " + " | ".join(f"{_f(per[g]['precision'])} / {_f(per[g]['recall'])} / {_f(per[g]['f1'])}" for g in grps)
                     + " | " + " | ".join(_f(per[g].get("subtype_accuracy")) if "subtype_accuracy" in per[g] else "—" for g in grps) + " |")
        L.append("")
    u = m["union_recall"]
    L += ["## Found by at least one generator", "",
          "| Category | Devices | Found | Recall (upper bound) |", "|---|---|---|---|"]
    L += [f"| {c} | {v['n_gt']} | {v['found']} | {_f(v['recall'])} |" for c, v in u["by_category"].items()]
    L += [f"| **all** | {u['total']['n_gt']} | {u['total']['found']} | {_f(u['total']['recall'])} |", ""]
    for name, g in systems.items():
        L += [f"## `{name}` by category", "",
              "| Category | GT | Predicted | TP | FP | FN | Precision | Recall | FP kinds | Miss kinds | Localization (independent) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for c, v in g["primary"]["by_category"].items():
            fk = ", ".join(f"{k} {n}" for k, n in v["fp_kinds"].items()) or "—"
            nk = ", ".join(f"{k} {n}" for k, n in v["fn_kinds"].items()) or "—"
            L.append(f"| {c} | {v['n_gt']} | {v['n_pred']} | {v['tp']} | {v['fp']} | {v['fn']} | {_f(v['precision'])} | "
                     f"{_f(v['recall'])} | {fk} | {nk} | {_loc(v['localization'])} |")
        dup = g.get("duplicate_template_pairs")
        if dup:
            L += ["", "Duplicate hits, by template pair (duplicate → the hit that matched the device): "
                  + "; ".join(f"`{d['duplicate_template']}` → `{d['matched_template']}` {d['n']}" for d in dup[:6])
                  + ("; …" if len(dup) > 6 else "") + "."]
        if "subtype" in g:
            s = g["subtype"]
            L += ["", f"**Subtype** over {s['evaluated_on']} category matches: {s['correct']} correct ({_f(s['accuracy'])}). By category: "
                  + "; ".join(f"{c} {v['correct']}/{v['n']}" for c, v in s["by_category"].items()) + ".",
                  "Confusions (ground truth → predicted): "
                  + ("; ".join(f"{c['category']} `{c['gt']}` → `{c['pred']}` {c['n']}" for c in s["confusions"][:10]) or "none")
                  + ("; …" if len(s["confusions"]) > 10 else "") + "."]
            src = Counter(x["subtype_source"] for x in ev.detections[name]["detections"] if "subtype_source" in x)
            if src:
                L += ["Subtype source over all detections: " + ", ".join(f"{k} {n}" for k, n in sorted(src.items())) + "."]
        L.append("")
    cats = list(m["ground_truth"]["by_category"])
    L += ["## Per-sheet counts", "", "Ground-truth count, then each system's in-scope prediction count / true positives.", "",
          "| Sheet | Category | GT | " + " | ".join(f"`{n}`" for n in systems) + " |",
          "|---|---|---|" + "---|" * len(systems)]
    sheets = sorted(next(iter(systems.values()))["primary"]["by_sheet_category"])
    for s in sheets:
        here = set(cats).union(*(g["primary"]["by_sheet_category"].get(s, {}) for g in systems.values()))
        for c in sorted(here):
            cells = [g["primary"]["by_sheet_category"].get(s, {}).get(c) for g in systems.values()]
            n_gt = next((x["n_gt"] for x in cells if x), 0)
            L.append(f"| {s} | {c} | {n_gt} | " + " | ".join(f"{x['n_pred']} / {x['tp']}" if x else "0 / 0" for x in cells) + " |")
    L += ["", "## Tolerance sensitivity", "", "| System | Tolerance (pt) | TP | FP | FN | Precision | Recall |",
          "|---|---|---|---|---|---|---|"]
    for name, g in systems.items():
        rows = [(m["tolerance_pt"], g["primary"]["total"])] + [(float(t), v["total"]) for t, v in g["sensitivity"].items()]
        for tol, t in sorted(rows, key=lambda x: x[0]):
            L.append(f"| `{name}` | {tol:g} | {t['tp']} | {t['fp']} | {t['fn']} | {_f(t['precision'])} | {_f(t['recall'])} |")
    L += ["", "## Examples", "",
          "Each tile is 48 × 48 pt of the plan. Green circles: ground-truth devices. Thin orange squares: other predictions from the "
          "same system. **Thick red square:** the false positive. **Thick red circle:** the missed device. Grey circle: the "
          f"{m['tolerance_pt']:g} pt match tolerance. Chosen to cover every error kind and category, highest score first; "
          "not a random sample.", ""]
    for name, e in examples.items():
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(e["kinds"].items()))
        what = "False positives" if e["outcome"] == "fp" else "Misses"
        L += [f"### `{e['generator']}` — {what} ({kinds})", "", f"![{e['generator']} {e['outcome']}](examples/{name})", ""]
    L += ["## Reproduce", "", "```bash", "cd backend",
          reproduce or f"uv run python tools/evaluate.py --export ../data/ground_truth/exports/{m['export']['id']}", "```", ""]
    return "\n".join(L)


def write(ev: Evaluation, out_dir: Path, pdf_path: Path, cache_dir: Path | None = None, reproduce: str | None = None) -> Path:
    """Write metrics.json, matches.jsonl, detections_<name>.jsonl, examples/ and report.md into a new directory (never overwrites)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    examples = render_examples(ev, pdf_path, out_dir / "examples", cache_dir or ROOT / "data" / "ground_truth" / "cache")
    (out_dir / "metrics.json").write_text(json.dumps({**ev.metrics, "examples": examples}, indent=1, sort_keys=True),
                                          encoding="utf-8")
    (out_dir / "matches.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in ev.matches), encoding="utf-8")
    for name, d in ev.detections.items():
        (out_dir / f"detections_{name}.jsonl").write_text(
            "".join(json.dumps(x, sort_keys=True) + "\n" for x in d["detections"]), encoding="utf-8")
    (out_dir / "report.md").write_text(report_md(ev, examples, reproduce), encoding="utf-8")
    return out_dir
