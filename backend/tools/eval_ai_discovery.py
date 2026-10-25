"""Evaluate ai-discover/1 against a verified ground-truth export (spec §5.9.4). Makes real, billed Claude requests.

From backend/:
    uv run python tools/eval_ai_discovery.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z

Discovery runs on every selected sheet exactly as the app runs it (the sheet's electrical region, tiled, with its
composite legend). The merged candidates (what the app would store as ``ai_discover`` detections) are matched to
the ground truth one-to-one at 6 pt, by category (eval/1 matching). Reported per dev and held-out group: recall,
precision, localization error (median, p90), the share of det/0's misses found (det/0's 6 pt false negatives from
the det/0 evaluation's matches.jsonl), false candidates per sheet, and the actual requests, tokens, latency and
errors. Writes metrics.json, candidates.jsonl, verdicts.jsonl and report.md to a new directory (default
reports/evaluation/<export id>__ai-discover1); an existing directory is never overwritten.

Exit codes: 0 written; 2 refused input (export not verified or altered, wrong PDF, unknown sheet, output exists);
3 refused discoverer (not the Claude implementation, or unavailable: the reason is printed). Credentials are never
printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from takeoff.ai import common as AC  # noqa: E402
from takeoff.ai import discover as AD  # noqa: E402
from takeoff.evaluation import matching as EM  # noqa: E402
from takeoff.groundtruth.export import ExportError, require_verified  # noqa: E402
from takeoff.groundtruth.models import utcnow  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
DEFAULT_CACHE = ROOT / "data" / "takeoff" / "cache"
GROUPS = {"dev": ["A1-111", "A2-111"], "held_out": ["A3-111", "A4-111"]}
TOLERANCE_PT = 6.0
RUN_ID = "eval-ai-discover"
HEADING = "measured separately; candidates need human approval; not used by totals"
NOTES = [
    "Scored are the merged candidates (one per same-category cluster within 6 pt across tiles), which is what the "
    "app stores and proposes; a reviewer must accept each one individually before it can count.",
    "Matching is eval/1's: one-to-one within 6 pt, same sheet and category. A candidate of the wrong category at a "
    "device is a false candidate, and the device stays missed.",
    "det/0's misses are its false negatives at 6 pt in the det/0 evaluation of the same export; the share found is "
    "how many of those ground-truth devices a candidate matched.",
    "Localization error is the distance of matched pairs in PDF points.",
    "A sheet without legend icons sends no request (skipped_no_legend); failed tiles give no candidates.",
    "The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.",
]


def _rate(a: int, n: int) -> float | None:
    return round(a / n, 4) if n else None


def out_dir(export: Path, sheets: list[str] | None) -> Path:
    name = f"{Path(export).name}__{AD.AI_VERSION.replace('/', '')}"
    if sheets:
        name += "__" + "+".join(sheets)
    return ROOT / "reports" / "evaluation" / name


def det0_matches_path(export: Path) -> Path:
    return ROOT / "reports" / "evaluation" / f"{Path(export).name}__eval1.1__det0" / "matches.jsonl"


def det0_fn_ids(path: Path, tol_pt: float = TOLERANCE_PT) -> set[str] | None:
    """Ground-truth ids det/0 missed at ``tol_pt``, or ``None`` when the det/0 evaluation is not there."""
    if not path.exists():
        return None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["gt_id"] for r in rows if r.get("generator") == "det0" and r.get("outcome") == "fn"
            and r.get("tolerance_pt") == tol_pt}


# --- scoring (pure) ------------------------------------------------------------------------------------------

def _group(dets, gt_rows: list[dict], verdicts, sheets: list[str], fn_ids: set[str] | None, tol_pt: float) -> dict:
    gi = [EM.Item(g["id"], g["sheet_id"], g["category"], *g["point_pt"]) for g in gt_rows]
    pi = [EM.Item(d.id, d.sheet_id, d.category, *d.point_pt) for d in dets]
    r = EM.match(gi, pi, tol_pt)
    tp, fp, fn = len(r.tp), len(r.fp), len(r.fn)
    rates = EM.rates(tp, fp, fn)
    loc = EM.localization(r.tp)["all"]
    found = {t.gt_id for t in r.tp}
    misses = None
    if fn_ids is not None:
        here = sorted(g["id"] for g in gt_rows if g["id"] in fn_ids)
        hit = [g for g in here if g in found]
        misses = {"n": len(here), "found": len(hit), "share": _rate(len(hit), len(here)), "ids_found": hit}
    return {
        "sheets": sheets, "gt": len(gi), "candidates": len(pi), "tp": tp, "fp": fp, "fn": fn,
        "recall": rates["recall"], "precision": rates["precision"],
        "localization_pt": None if loc is None else {"n": loc["n"], "median": loc["median"], "p90": loc["p90"],
                                                     "max": loc["max"]},
        "det0_misses": misses,
        "false_candidates_per_sheet": round(fp / len(sheets), 3) if sheets else None,
        "fp_kinds": dict(sorted(_count(EM.classify(gi, pi, r, tol_pt).fp.values()).items())),
        "usage": AC.summarize(verdicts),
        "_pairs": [{"gt_id": t.gt_id, "candidate_id": t.pred_id, "distance_pt": t.distance_pt} for t in r.tp],
    }


def _count(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def score(dets, gt_rows: list[dict], verdicts, groups: dict[str, list[str]], det0_fn_ids: set[str] | None = None,
          tol_pt: float = TOLERANCE_PT) -> dict:
    out = {}
    for g, sheets in groups.items():
        out[g] = _group([d for d in dets if d.sheet_id in sheets], [r for r in gt_rows if r["sheet_id"] in sheets],
                        [v for v in verdicts if v.sheet_id in sheets], list(sheets), det0_fn_ids, tol_pt)
    every = sorted({s for ss in groups.values() for s in ss})
    out["total"] = _group([d for d in dets if d.sheet_id in every], [r for r in gt_rows if r["sheet_id"] in every],
                          [v for v in verdicts if v.sheet_id in every], every, det0_fn_ids, tol_pt)
    return out


# --- the run -------------------------------------------------------------------------------------------------

def discover_sheets(pdf: Path, discoverer, sheets: list[str], budget: AC.RequestBudget, cache: Path):
    """Discovery on each sheet as the app runs it: its region image, region and composite legend."""
    import pymupdf

    from takeoff.app import sheets as TS
    from takeoff.groundtruth import sheets as S

    sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
    infos = {s.sheet_id: s for s in TS.intake(pdf, sha)}
    dets, verdicts, per_sheet = [], [], {}
    doc = pymupdf.open(stream=Path(pdf).read_bytes(), filetype="pdf")
    try:
        for sid in sheets:
            info = infos[sid]
            view = TS.sheet_view(pdf, info, sha, cache)
            region = view["region"]["bbox_pt"]
            path, mapping = S.render_region_image(doc[info.page_number - 1], region, sha, cache)
            icons = TS.legend_icons(pdf, info)
            found, vs = discoverer.discover(RUN_ID, sha, sid, path, mapping, region, AD.composite_or_none(icons), budget)
            dets += found
            verdicts += vs
            per_sheet[sid] = {"region_bbox_pt": list(region), "tiles": len(vs), "legend_icons": len(icons),
                              "candidates": len(found),
                              "observations": sum(len((v.output or {}).get("points", [])) for v in vs),
                              "dropped": sum(len((v.output or {}).get("dropped", [])) for v in vs)}
            print(f"{sid}: {len(vs)} tiles, {len(found)} candidates, {AC.summarize(verdicts)['requests']} requests so far",
                  file=sys.stderr)
    finally:
        doc.close()
    return dets, verdicts, per_sheet


def ground_truth(export: Path, sheets: list[str]) -> list[dict]:
    rows = [json.loads(line) for line in (export / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [a for a in rows if not a.get("deleted") and a["sheet_id"] in sheets]


def build_metrics(export: Path, manifest: dict, discoverer, budget: AC.RequestBudget, dets, gt_rows, verdicts,
                  per_sheet: dict, chosen: list[str], fn_ids, fn_path: Path, reproduce: str) -> dict:
    import anthropic

    from takeoff.evaluation import evaluate as EV

    groups = {g: [s for s in ss if s in chosen] for g, ss in GROUPS.items()}
    groups = {g: ss for g, ss in groups.items() if ss}
    return {
        "ai_version": AD.AI_VERSION, "prompt_version": AD.PROMPT_VERSION, "model": discoverer.model,
        "implementation": discoverer.implementation, "sdk_version": anthropic.__version__,
        "tile_pt": AD.TILE_PT, "overlap_pt": AD.OVERLAP_PT, "max_items": AD.MAX_ITEMS,
        "max_output_tokens": AD.MAX_OUTPUT_TOKENS, "merge_tol_pt": AD.MERGE_TOL_PT, "tolerance_pt": TOLERANCE_PT,
        "created_at": utcnow(), "code": EV._git(), "heading": HEADING, "reproduce": reproduce, "sheets": chosen,
        "groups": groups, "tiles": sum(s["tiles"] for s in per_sheet.values()), "per_sheet": per_sheet,
        "export": {"id": export.name, "manifest_sha256": hashlib.sha256((export / "manifest.json").read_bytes()).hexdigest(),
                   "level": manifest["level"], "input_pdf_sha256": manifest["input_pdf_sha256"]},
        "det0_misses_source": {"path": str(fn_path.relative_to(ROOT)) if fn_path.is_relative_to(ROOT) else str(fn_path),
                               "available": fn_ids is not None, "n": None if fn_ids is None else len(fn_ids)},
        "budget": {"max_requests": budget.max_requests, "used": budget.used, "skipped": budget.skipped},
        "results": score(dets, gt_rows, verdicts, groups, fn_ids), "notes": NOTES,
    }


def _cell(v):
    return "n/a" if v is None else v


def report(metrics: dict) -> str:
    r = metrics["results"]
    names = list(r)
    row = lambda label, f: "| " + label + " | " + " | ".join(str(_cell(f(r[g]))) for g in names) + " |"  # noqa: E731
    loc = lambda x, k: None if x["localization_pt"] is None else x["localization_pt"][k]  # noqa: E731
    miss = lambda x: "n/a" if x["det0_misses"] is None else (  # noqa: E731
        f"{x['det0_misses']['found']}/{x['det0_misses']['n']} ({_cell(x['det0_misses']['share'])})")
    lines = [
        f"# ai-discover/1 evaluation: {metrics['export']['id']}", "",
        f"**{HEADING}.** This report measures AI-proposed device locations against verified ground truth. The app's "
        "counts come only from accepted devices; an AI candidate is a proposal a reviewer must check and accept "
        "individually.", "",
        f"- Model: `{metrics['model']}` ({metrics['implementation']}), {metrics['ai_version']}, "
        f"{metrics['prompt_version']}, anthropic SDK {metrics['sdk_version']}",
        f"- Tiles: {metrics['tiles']} ({metrics['tile_pt']} pt, {metrics['overlap_pt']} pt overlap) on "
        f"{', '.join(metrics['sheets'])}",
        f"- Request budget: {metrics['budget']['max_requests']} (used {metrics['budget']['used']}, "
        f"skipped {metrics['budget']['skipped']})",
        f"- det/0 misses from `{metrics['det0_misses_source']['path']}`"
        + ("" if metrics["det0_misses_source"]["available"] else " (not found: not measured)"),
        f"- Reproduce (from backend/): `{metrics['reproduce']}`", "",
        "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names),
        row("ground-truth devices", lambda x: x["gt"]),
        row("AI candidates", lambda x: x["candidates"]),
        row("matched (TP)", lambda x: x["tp"]),
        row("recall", lambda x: x["recall"]),
        row("precision", lambda x: x["precision"]),
        row("localization median / p90 (pt)", lambda x: f"{_cell(loc(x, 'median'))} / {_cell(loc(x, 'p90'))}"),
        row("det/0 misses found", miss),
        row("false candidates per sheet", lambda x: x["false_candidates_per_sheet"]),
        row("false candidate kinds", lambda x: json.dumps(x["fp_kinds"])),
        row("requests", lambda x: x["usage"]["requests"]),
        row("errors by code", lambda x: json.dumps(x["usage"]["errors_by_code"])),
        row("input / output tokens", lambda x: f"{x['usage']['tokens']['input']} / {x['usage']['tokens']['output']}"),
        row("cache creation / read tokens",
            lambda x: f"{x['usage']['tokens']['cache_creation']} / {x['usage']['tokens']['cache_read']}"),
        row("latency p50 / p95 (ms)",
            lambda x: f"{_cell(x['usage']['latency_ms']['p50'])} / {_cell(x['usage']['latency_ms']['p95'])}"),
        "", "## Notes", "", *[f"- {n}" for n in NOTES], "",
    ]
    return "\n".join(lines)


def write(out: Path, metrics: dict, dets, verdicts) -> None:
    out.mkdir(parents=True, exist_ok=False)
    pairs = {g: x.pop("_pairs") for g, x in metrics["results"].items()}
    (out / "metrics.json").write_text(json.dumps({**metrics, "pairs": pairs}, indent=1, sort_keys=True), encoding="utf-8")
    (out / "candidates.jsonl").write_text("".join(d.model_dump_json() + "\n" for d in dets), encoding="utf-8")
    (out / "verdicts.jsonl").write_text("".join(v.model_dump_json() + "\n" for v in verdicts), encoding="utf-8")
    (out / "report.md").write_text(report(metrics), encoding="utf-8")


def main(argv=None, discoverer=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="a verified export directory under data/ground_truth/exports")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--sheets", default=None, help="comma-separated sheet ids (default: every sheet of the export)")
    ap.add_argument("--max-requests", type=int, default=None,
                    help=f"request cap (default: {AC.MAX_REQUESTS_ENV} or {AC.DEFAULT_MAX_REQUESTS}); skips are recorded")
    ap.add_argument("--det0-matches", default=None,
                    help="det/0 evaluation matches.jsonl (default: reports/evaluation/<export id>__eval1.1__det0/)")
    ap.add_argument("--out", default=None, help="new output directory (default: reports/evaluation/<export id>__ai-discover1)")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    sheets = [s.strip() for s in args.sheets.split(",") if s.strip()] if args.sheets else None

    try:
        manifest = require_verified(export)
    except ExportError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    d = discoverer if discoverer is not None else AD.ClaudeDiscoverer(AC.AIClient())
    if not isinstance(d, AD.ClaudeDiscoverer) or d.implementation != "claude":
        print(f"refused: the discoverer is {type(d).__name__} (implementation={getattr(d, 'implementation', None)!r}); "
              "only the Claude implementation is evaluated, never a fake or test double", file=sys.stderr)
        return 3
    out = Path(args.out) if args.out else out_dir(export, sheets)
    if out.exists():  # before available(): a refused run makes no model check at all
        print(f"{out} already exists; evaluations are never overwritten. Pass a new --out.", file=sys.stderr)
        return 2
    unknown = sorted(set(sheets or []) - set(manifest["sheets"]))
    if unknown:
        print(f"refused: sheets {unknown} are not in the export", file=sys.stderr)
        return 2
    ok, reason = d.available()
    if not ok:
        print(f"refused: AI discoverer unavailable: {reason}", file=sys.stderr)
        return 3
    pdf = Path(args.pdf)
    if not pdf.exists() or hashlib.sha256(pdf.read_bytes()).hexdigest() != manifest["input_pdf_sha256"]:
        print(f"refused: {pdf} is missing or is not the export's input PDF", file=sys.stderr)
        return 2

    budget = AC.RequestBudget(args.max_requests) if args.max_requests is not None else AC.RequestBudget.from_env()
    chosen = sheets or sorted(manifest["sheets"])
    fn_path = Path(args.det0_matches) if args.det0_matches else det0_matches_path(export)
    fn_ids = det0_fn_ids(fn_path)
    dets, verdicts, per_sheet = discover_sheets(pdf, d, chosen, budget, Path(args.cache))
    cmd = " ".join(["uv run python tools/eval_ai_discovery.py", f"--export ../data/ground_truth/exports/{export.name}",
                    *([f"--sheets {args.sheets}"] if sheets else []), f"--max-requests {budget.max_requests}"])
    metrics = build_metrics(export, manifest, d, budget, dets, ground_truth(export, chosen), verdicts, per_sheet,
                            chosen, fn_ids, fn_path, cmd)
    write(out, metrics, dets, verdicts)
    summary = {g: {k: x[k] for k in ("gt", "candidates", "tp", "recall", "precision", "localization_pt",
                                     "det0_misses", "false_candidates_per_sheet", "usage")}
               for g, x in metrics["results"].items()}
    print(json.dumps({"out": str(out), "results": summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
