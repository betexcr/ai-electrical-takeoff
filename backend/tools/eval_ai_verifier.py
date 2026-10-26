"""Evaluate ai-verify/1 against a verified ground-truth export (spec §5.9.4). Makes real, billed Claude requests.

From backend/:
    uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z
    uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --batch-size 1 --max-requests 320

det/0 is run on the export's own candidates (the evaluator checks the PDF and the candidate run hashes), every
in-region detection is verified, and each detection is matched to the ground truth at 6 pt to learn what is really
there. Writes metrics.json, verdicts.jsonl and report.md to a new directory (default
reports/evaluation/<export id>__ai-verify1[_b1]); an existing directory is never overwritten.

Exit codes: 0 written; 2 refused input (export not verified or altered, wrong PDF, output exists); 3 refused
verifier (not the Claude implementation, or unavailable: the reason is printed). Credentials are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from takeoff.ai import common as AC  # noqa: E402
from takeoff.ai import verifier as AV  # noqa: E402
from takeoff.app.merge import verify_usable_for  # noqa: E402
from takeoff.evaluation import matching as EM  # noqa: E402
from takeoff.groundtruth.export import ExportError, require_verified  # noqa: E402
from takeoff.groundtruth.models import utcnow  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
DEFAULT_CACHE = ROOT / "data" / "takeoff" / "cache"
GROUPS = {"dev": ["A1-111", "A2-111"], "held_out": ["A3-111", "A4-111"]}
TOLERANCE_PT = 6.0
RUN_ID = "eval-ai-verify"
HEADING = "measured separately; not used by the app's totals"
NOTES = [
    "Verify only sees det/0's detections: devices det/0 missed are not measured here (that is ai-discover/1's job).",
    "Truth at each detection: a same-category ground-truth device within 6 pt (eval/1 matching), else any remaining "
    "ground-truth device within 6 pt (det/0 got the category wrong), else none (det/0 false positive).",
    "An item counts as answered only as the app would use it (ruling R15): it was answered exactly once by a "
    "request that succeeded. An item the model left out or answered twice is unanswered (parse_error) and never "
    "flags; the other items of its batch still count. A whole-request failure leaves all its items unanswered.",
    "A flag is what the merge would open: category or subtype disagreement with det/0, not a device, or uncertain.",
    "The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.",
]


def _rate(a: int, n: int) -> float | None:
    return round(a / n, 4) if n else None


def out_dir(export: Path, batch_size: int, sheets: list[str] | None) -> Path:
    name = f"{Path(export).name}__{AV.AI_VERSION.replace('/', '')}{'_b1' if batch_size == 1 else ''}"
    if sheets:
        name += "__" + "+".join(sheets)
    return ROOT / "reports" / "evaluation" / name


# --- scoring (pure) ------------------------------------------------------------------------------------------

def truth_at(dets, gt_rows: list[dict], tol_pt: float = TOLERANCE_PT) -> dict[str, dict | None]:
    """Detection id -> the ground-truth row at that spot (category-aware match first, then any category), or None."""
    gi = [EM.Item(g["id"], g["sheet_id"], g["category"], *g["point_pt"]) for g in gt_rows]
    pi = [EM.Item(d.id, d.sheet_id, d.category, *d.point_pt) for d in dets]
    first = EM.match(gi, pi, tol_pt)
    pairs = {t.pred_id: t.gt_id for t in first.tp}
    used = set(pairs.values())
    rest_g = [EM.Item(g.id, g.sheet_id, "any", g.x_pt, g.y_pt) for g in gi if g.id not in used]
    rest_p = [EM.Item(p.id, p.sheet_id, "any", p.x_pt, p.y_pt) for p in pi if p.id not in pairs]
    pairs.update({t.pred_id: t.gt_id for t in EM.match(rest_g, rest_p, tol_pt).tp})
    by_id = {g["id"]: g for g in gt_rows}
    return {d.id: by_id[pairs[d.id]] if d.id in pairs else None for d in dets}


def _items(dets, gt_rows, verdicts, tol_pt):
    truth = truth_at(dets, gt_rows, tol_pt)
    by_det = {}
    for v in verdicts:
        for i in v.detection_ids:
            by_det[i] = v
    out = []
    for d in dets:
        t = truth[d.id]
        if t is None:
            err = "not_a_device"
        elif t["category"] != d.category:
            err = "category"
        elif t["subtype"] != d.subtype:
            err = "subtype"
        else:
            err = None
        v = by_det.get(d.id)
        ag = (v.agreement or {}).get(d.id) if v is not None and verify_usable_for(v, d.id) else None  # as the merge
        row = None
        if ag is not None:
            row = next(r for r in v.output["verdicts"] if r["item_id"] == d.id)
        code = None if ag is not None else ("no_verdict" if v is None else (v.error or {}).get("code", "no_answer"))
        flagged = ag is not None and (ag.category is False or ag.subtype is False or row["is_device"] is False
                                      or row["uncertain"] is True)
        out.append({"id": d.id, "sheet_id": d.sheet_id, "det0": [d.category, d.subtype],
                    "truth": None if t is None else [t["category"], t["subtype"]], "truth_id": None if t is None else t["id"],
                    "det0_error": err, "answered": ag is not None, "unanswered_code": code,
                    "ai": None if row is None else [row["category"] if row["is_device"] else "not_a_device", row["subtype"]],
                    "ai_uncertain": None if row is None else row["uncertain"], "flagged": flagged})
    return out


def _group(items: list[dict], verdicts) -> dict:
    answered = [i for i in items if i["answered"]]
    cat_ok = [i for i in answered if i["ai"][0] == (i["truth"][0] if i["truth"] else "not_a_device")]
    sub_eval = [i for i in cat_ok if i["truth"] is not None and i["ai"][1] is not None]
    sub_ok = [i for i in sub_eval if i["ai"][1] == i["truth"][1]]
    errors = [i for i in items if i["det0_error"]]
    flagged = [i for i in items if i["flagged"]]
    true_flags = [i for i in flagged if i["det0_error"]]
    answered_errors = [i for i in errors if i["answered"]]
    det0_cat_ok = sum(1 for i in items if i["det0_error"] not in ("category", "not_a_device"))
    return {
        "items": len(items), "answered": len(answered),
        "unanswered_by_code": dict(sorted(Counter(i["unanswered_code"] for i in items if not i["answered"]).items())),
        "det0_errors": len(errors),
        "det0_error_kinds": dict(sorted(Counter(i["det0_error"] for i in errors).items())),
        "det0_vs_gt": {"category": {"n": len(items), "agree": det0_cat_ok, "rate": _rate(det0_cat_ok, len(items))},
                       "exact": {"n": len(items), "agree": len(items) - len(errors),
                                 "rate": _rate(len(items) - len(errors), len(items))}},
        "ai_vs_gt": {"category": {"n": len(answered), "agree": len(cat_ok), "rate": _rate(len(cat_ok), len(answered))},
                     "subtype": {"n": len(sub_eval), "agree": len(sub_ok), "rate": _rate(len(sub_ok), len(sub_eval))},
                     "uncertain": sum(1 for i in answered if i["ai_uncertain"])},
        "flags": {"flagged": len(flagged), "true_flags": len(true_flags), "precision": _rate(len(true_flags), len(flagged)),
                  "recall": _rate(len(true_flags), len(errors)),
                  "recall_answered": _rate(sum(1 for i in answered_errors if i["flagged"]), len(answered_errors))},
        "usage": AC.summarize(verdicts),
    }


def score(dets, gt_rows: list[dict], verdicts, groups: dict[str, list[str]], tol_pt: float = TOLERANCE_PT) -> dict:
    """Per group and in total: AI agreement with the ground truth, flag precision/recall against det/0's errors,
    failures, tokens and latency."""
    items = _items(dets, gt_rows, verdicts, tol_pt)
    out = {g: _group([i for i in items if i["sheet_id"] in sheets], [v for v in verdicts if v.sheet_id in sheets])
           for g, sheets in groups.items()}
    out["total"] = _group(items, verdicts)
    out["_items"] = items
    return out


# --- the run -------------------------------------------------------------------------------------------------

def det0_detections(export: Path, pdf: Path) -> dict:
    """det/0 on the export's own candidates; ``EV.evaluate`` checks the PDF hash and the candidate run hashes.

    Returns det/0's output with ``detections`` limited to the electrical regions (as the evaluator scores them).
    """
    from takeoff.detection import det0
    from takeoff.evaluation import evaluate as EV
    from takeoff.groundtruth import candidates as CA

    ev = EV.evaluate(export, pdf, results=CA.generate(pdf), detectors={"det0": det0.detect})
    found = ev.detections["det0"]
    return {**found, "detections": [d for d in found["detections"]
                                    if EV._in_region(ev.regions, d["sheet_id"], d["x_pt"], d["y_pt"])]}


def verify_sheets(pdf: Path, raw: list[dict], verifier, sheets: list[str], budget: AC.RequestBudget, cache: Path):
    """``RunDetection``s, crops, nearby text and legend icons for each sheet, then ``verifier.verify``."""
    import pymupdf

    from takeoff.app import models as TM
    from takeoff.app import sheets as TS
    from takeoff.groundtruth import sheets as S
    from takeoff.pdf import coords as C

    sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
    infos = {s.sheet_id: s for s in TS.intake(pdf, sha)}
    dets, verdicts = [], []
    doc = pymupdf.open(stream=Path(pdf).read_bytes(), filetype="pdf")
    try:
        for sid in sheets:
            info = infos[sid]
            view = TS.sheet_view(pdf, info, sha, cache)
            path, mapping = S.render_region_image(doc[info.page_number - 1], view["region"]["bbox_pt"], sha, cache)
            g = C.PageGeometry(*view["geometry"]["crop_native"], rotation=view["geometry"]["rotation"])
            here = []
            for d in (d for d in raw if d["sheet_id"] == sid):
                u, v = C.unrotated_to_normalized(g, (d["x_pt"], d["y_pt"]))
                here.append(TM.RunDetection(id=d["id"], run_id=RUN_ID, document_id=sha, sheet_id=sid,
                                            category=d["category"], subtype=d["subtype"], status=d["status"],
                                            point={"u": u, "v": v}, point_pt=(d["x_pt"], d["y_pt"]), score=d.get("score"),
                                            members=d.get("members", []), subtype_source=d.get("subtype_source"),
                                            tokens=d.get("tokens", [])))
            region = AV.load_gray(path)
            crops = {d.id: AV.build_crop(region, mapping, d.point_pt) for d in here}
            tokens = AV.tokens_near(here, view["tokens"])
            verdicts += verifier.verify(RUN_ID, sid, here, crops, TS.legend_icons(pdf, info), tokens, budget)
            dets += here
            print(f"{sid}: {len(here)} detections, {AC.summarize(verdicts)['requests']} requests so far", file=sys.stderr)
    finally:
        doc.close()
    return dets, verdicts


def ground_truth(export: Path, sheets: list[str]) -> list[dict]:
    rows = [json.loads(line) for line in (export / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [a for a in rows if not a.get("deleted") and a["sheet_id"] in sheets]


def run(export: Path, pdf: Path, verifier, sheets: list[str] | None, budget: AC.RequestBudget, cache: Path):
    """det/0 on the export's checked candidates, then verify for each selected sheet."""
    found = det0_detections(export, pdf)
    chosen = sheets or sorted({d["sheet_id"] for d in found["detections"]})
    dets, verdicts = verify_sheets(pdf, found["detections"], verifier, chosen, budget, cache)
    return dets, ground_truth(export, chosen), verdicts, found, chosen


def build_metrics(export: Path, manifest: dict, verifier, batch_size: int, budget: AC.RequestBudget, dets, gt_rows,
                  verdicts, found: dict, chosen: list[str], reproduce: str) -> dict:
    import anthropic

    from takeoff.evaluation import evaluate as EV

    groups = {g: [s for s in ss if s in chosen] for g, ss in GROUPS.items()}
    groups = {g: ss for g, ss in groups.items() if ss}
    return {
        "ai_version": AV.AI_VERSION, "prompt_version": AV.PROMPT_VERSION, "model": verifier.model,
        "implementation": verifier.implementation, "sdk_version": anthropic.__version__, "batch_size": batch_size,
        "max_output_tokens": AV.MAX_OUTPUT_TOKENS, "tolerance_pt": TOLERANCE_PT, "created_at": utcnow(),
        "code": EV._git(), "heading": HEADING, "reproduce": reproduce, "sheets": chosen, "groups": groups,
        "export": {"id": export.name, "manifest_sha256": hashlib.sha256((export / "manifest.json").read_bytes()).hexdigest(),
                   "level": manifest["level"], "input_pdf_sha256": manifest["input_pdf_sha256"]},
        "det0": {"detector_version": found["detector_version"], "n_detections": len(dets)},
        "budget": {"max_requests": budget.max_requests, "used": budget.used, "skipped": budget.skipped},
        "results": score(dets, gt_rows, verdicts, groups), "notes": NOTES,
    }


def _cell(v):
    return "n/a" if v is None else v


def report(metrics: dict) -> str:
    r = metrics["results"]
    names = [g for g in r if not g.startswith("_")]
    row = lambda label, f: "| " + label + " | " + " | ".join(str(_cell(f(r[g]))) for g in names) + " |"  # noqa: E731
    lines = [
        f"# ai-verify/1 evaluation: {metrics['export']['id']} (batch size {metrics['batch_size']})", "",
        f"**{HEADING}.** This report measures the AI second opinion against verified ground truth. The app's counts "
        "come only from accepted devices; an AI verdict never changes a device or a total.", "",
        f"- Model: `{metrics['model']}` ({metrics['implementation']}), {metrics['ai_version']}, {metrics['prompt_version']}, "
        f"anthropic SDK {metrics['sdk_version']}",
        f"- det/0: {metrics['det0']['detector_version']}, {metrics['det0']['n_detections']} in-region detections on "
        f"{', '.join(metrics['sheets'])}",
        f"- Request budget: {metrics['budget']['max_requests']} (used {metrics['budget']['used']}, "
        f"skipped {metrics['budget']['skipped']})",
        f"- Reproduce (from backend/): `{metrics['reproduce']}`", "",
        "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names),
        row("items (det/0 detections)", lambda x: x["items"]),
        row("answered by the AI", lambda x: x["answered"]),
        row("det/0 errors (category, subtype or not a device)", lambda x: x["det0_errors"]),
        row("AI category agrees with GT", lambda x: f"{x['ai_vs_gt']['category']['agree']}/{x['ai_vs_gt']['category']['n']} "
                                                    f"({_cell(x['ai_vs_gt']['category']['rate'])})"),
        row("AI subtype agrees with GT", lambda x: f"{x['ai_vs_gt']['subtype']['agree']}/{x['ai_vs_gt']['subtype']['n']} "
                                                   f"({_cell(x['ai_vs_gt']['subtype']['rate'])})"),
        row("flags (would open model_disagrees)", lambda x: x["flags"]["flagged"]),
        row("flag precision vs det/0 errors", lambda x: x["flags"]["precision"]),
        row("flag recall vs det/0 errors", lambda x: x["flags"]["recall"]),
        row("flag recall, answered items only", lambda x: x["flags"]["recall_answered"]),
        row("refusals", lambda x: x["usage"]["errors_by_code"].get("refusal", 0)),
        row("budget skips", lambda x: x["usage"]["errors_by_code"].get("skipped_budget", 0)),
        row("errors by code", lambda x: json.dumps(x["usage"]["errors_by_code"])),
        row("requests", lambda x: x["usage"]["requests"]),
        row("input / output tokens", lambda x: f"{x['usage']['tokens']['input']} / {x['usage']['tokens']['output']}"),
        row("cache creation / read tokens",
            lambda x: f"{x['usage']['tokens']['cache_creation']} / {x['usage']['tokens']['cache_read']}"),
        row("latency p50 / p95 (ms)", lambda x: f"{_cell(x['usage']['latency_ms']['p50'])} / {_cell(x['usage']['latency_ms']['p95'])}"),
        "", "## Notes", "", *[f"- {n}" for n in NOTES], "",
    ]
    return "\n".join(lines)


def write(out: Path, metrics: dict, verdicts) -> None:
    out.mkdir(parents=True, exist_ok=False)
    items = metrics["results"].pop("_items")
    (out / "metrics.json").write_text(json.dumps({**metrics, "items": items}, indent=1, sort_keys=True), encoding="utf-8")
    (out / "verdicts.jsonl").write_text("".join(v.model_dump_json() + "\n" for v in verdicts), encoding="utf-8")
    (out / "report.md").write_text(report(metrics), encoding="utf-8")


def main(argv=None, verifier=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="a verified export directory under data/ground_truth/exports")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--batch-size", type=int, choices=[AV.VERIFY_BATCH_SIZE, 1], default=AV.VERIFY_BATCH_SIZE)
    ap.add_argument("--sheets", default=None, help="comma-separated sheet ids (default: every sheet)")
    ap.add_argument("--max-requests", type=int, default=None,
                    help=f"request cap (default: {AC.MAX_REQUESTS_ENV} or {AC.DEFAULT_MAX_REQUESTS}); skips are recorded")
    ap.add_argument("--out", default=None, help="new output directory (default: reports/evaluation/<export id>__ai-verify1[_b1])")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    sheets = [s.strip() for s in args.sheets.split(",") if s.strip()] if args.sheets else None

    try:
        manifest = require_verified(export)
    except ExportError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    v = verifier if verifier is not None else AV.ClaudeVerifier(AC.AIClient(), batch_size=args.batch_size)
    if not isinstance(v, AV.ClaudeVerifier) or v.implementation != "claude":
        print(f"refused: the verifier is {type(v).__name__} (implementation={getattr(v, 'implementation', None)!r}); "
              "only the Claude implementation is evaluated, never a fake or test double", file=sys.stderr)
        return 3
    v.batch_size = args.batch_size
    out = Path(args.out) if args.out else out_dir(export, args.batch_size, sheets)
    if out.exists():  # before available(): a refused run makes no model check at all
        print(f"{out} already exists; evaluations are never overwritten. Pass a new --out.", file=sys.stderr)
        return 2
    unknown = sorted(set(sheets or []) - set(manifest["sheets"]))
    if unknown:
        print(f"refused: sheets {unknown} are not in the export", file=sys.stderr)
        return 2
    ok, reason = v.available()
    if not ok:
        print(f"refused: AI verifier unavailable: {reason}", file=sys.stderr)
        return 3

    from takeoff.evaluation import evaluate as EV
    budget = AC.RequestBudget(args.max_requests) if args.max_requests is not None else AC.RequestBudget.from_env()
    try:
        dets, gt_rows, verdicts, found, chosen = run(export, Path(args.pdf), v, sheets, budget, Path(args.cache))
    except EV.EvaluationError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    cmd = " ".join(["uv run python tools/eval_ai_verifier.py", f"--export ../data/ground_truth/exports/{export.name}",
                    f"--batch-size {args.batch_size}", *([f"--sheets {args.sheets}"] if sheets else []),
                    f"--max-requests {budget.max_requests}"])
    metrics = build_metrics(export, manifest, v, args.batch_size, budget, dets, gt_rows, verdicts, found, chosen, cmd)
    write(out, metrics, verdicts)
    summary = {g: {"items": x["items"], "answered": x["answered"], "flags": x["flags"], "ai_vs_gt": x["ai_vs_gt"],
                   "usage": x["usage"]} for g, x in metrics["results"].items()}
    print(json.dumps({"out": str(out), "results": summary}, indent=1))
    return 0


if __name__ == "__main__":
    from takeoff.envfile import load_for_cli

    load_for_cli()
    raise SystemExit(main())
