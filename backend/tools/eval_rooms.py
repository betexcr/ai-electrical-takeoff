"""Calibrate and check the nearest-label room proposal against a verified ground-truth export.

From backend/:
    uv run python tools/eval_rooms.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z

Prints the calibration computed on A1-111/A2-111 (the value to put in ``ROOM_PARAMS``) and writes
reports/takeoff/room_proposal_eval.{json,md} for the parameters currently in ``ROOM_PARAMS``.
The export is only read; ``require_verified`` runs first and the tool exits 2 if it fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
DEFAULT_OUT = ROOT / "reports" / "takeoff"
DEFAULT_CACHE = ROOT / "data" / "takeoff" / "cache"
CALIBRATION_SHEETS = ["A1-111", "A2-111"]
GROUPS = {"dev": ["A1-111", "A2-111"], "held_out": ["A3-111", "A4-111"]}
PERCENTILE = 95
HEADING = "not shown in the app; not a room-detection accuracy claim"
DOC_ID = "e" * 64  # placeholder document id: only the sheet geometry is used here
_TOL = 0.01  # pt: export boxes carry 3 decimals


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rooms_match(gt: dict | None, room: dict | None) -> bool:
    """Same kind, name and label box (two 'CL' labels are two rooms)."""
    if not gt or not room or gt.get("kind") != room.get("kind") or gt.get("name") != room.get("name"):
        return False
    a, b = gt.get("label_bbox_pt"), room.get("label_bbox_pt")
    if a is None or b is None:
        return a is None and b is None
    return all(abs(x - y) <= _TOL for x, y in zip(a, b))


def load_annotations(export: Path) -> list[dict]:
    text = (export / "annotations.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r for r in rows if not r.get("deleted")]


def sheet_rooms(pdf: Path, cache: Path) -> dict[str, list[dict]]:
    """sheet_id -> candidate rooms, exactly as the app builds them."""
    from takeoff.app import sheets as TS
    sha = _sha256(pdf.read_bytes())
    return {s.sheet_id: TS.sheet_view(pdf, s, sha, cache)["rooms"] for s in TS.intake(pdf, DOC_ID)}


def device_rows(export: Path, pdf: Path, cache: Path = DEFAULT_CACHE, params=None) -> list[dict]:
    from takeoff.app import rooms as R
    params = params or R.ROOM_PARAMS
    rooms = sheet_rooms(pdf, cache)
    out = []
    for a in load_annotations(export):
        cands = rooms[a["sheet_id"]]
        ranked = R._ranked(a["point_pt"], cands)
        p = R.propose_room(a["point_pt"], cands, params)
        out.append({
            "id": a["id"], "sheet_id": a["sheet_id"], "gt_room": a["room"],
            "d1": ranked[0][0] if ranked else None,
            "nearest_correct": bool(ranked) and rooms_match(a["room"], ranked[0][1]),
            "proposed": p.room, "ambiguous": p.ambiguous, "reason": p.reason,
            "correct": rooms_match(a["room"], p.room),
        })
    return out


def calibrate(export: Path, pdf: Path, cache: Path = DEFAULT_CACHE) -> tuple[int, float, list[float]]:
    """(n, max_label_distance_pt, sorted d1 values): 95th percentile of d1 over A1/A2 devices whose GT
    room is labeled and whose nearest label is correct, rounded up to the next multiple of 10."""
    import numpy as np
    rows = device_rows(export, pdf, cache)
    d1s = sorted(r["d1"] for r in rows if r["sheet_id"] in CALIBRATION_SHEETS
                 and r["gt_room"]["kind"] == "labeled" and r["nearest_correct"])
    p = float(np.percentile(d1s, PERCENTILE))
    return len(d1s), float(math.ceil(round(p, 6) / 10) * 10), d1s


def _rate(n, d):
    return round(n / d, 4) if d else None


def summarize(rows: list[dict]) -> dict:
    labeled = [r for r in rows if r["gt_room"]["kind"] == "labeled"]
    correct = [r for r in labeled if r["correct"]]
    amb = [r for r in labeled if r["ambiguous"]]
    reasons: dict[str, int] = {}
    for r in amb:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    ext = [r for r in rows if r["gt_room"]["kind"] == "exterior"]
    unl = [r for r in rows if r["gt_room"]["kind"] == "unlabeled"]
    flagged_correct = sum(r["ambiguous"] for r in correct)
    return {
        "devices": len(rows), "gt_labeled": len(labeled),
        "correct": len(correct), "accuracy": _rate(len(correct), len(labeled)),
        "ambiguous": len(amb), "ambiguous_rate": _rate(len(amb), len(labeled)), "ambiguous_reasons": reasons,
        "correct_but_flagged": flagged_correct,
        "correct_but_flagged_rate_of_correct": _rate(flagged_correct, len(correct)),
        "wrong_and_unflagged": sum(1 for r in labeled if not r["correct"] and not r["ambiguous"]),
        "gt_exterior": len(ext),
        "gt_exterior_given_a_labeled_proposal": sum(r["proposed"]["kind"] == "labeled" for r in ext),
        "gt_unlabeled": len(unl),
        "gt_unlabeled_given_a_labeled_proposal": sum(r["proposed"]["kind"] == "labeled" for r in unl),
    }


def evaluate(export: Path, pdf: Path, cache: Path = DEFAULT_CACHE) -> dict:
    import numpy as np
    from takeoff.app import rooms as R
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    pdf_sha = _sha256(pdf.read_bytes())
    if pdf_sha != manifest["input_pdf_sha256"]:
        raise SystemExit(f"{pdf} does not match the export's input PDF hash")
    rows = device_rows(export, pdf, cache)
    n, value, d1s = calibrate(export, pdf, cache)
    groups = {g: summarize([r for r in rows if r["sheet_id"] in sheets]) for g, sheets in GROUPS.items()}
    per_sheet = {s: summarize([r for r in rows if r["sheet_id"] == s]) for g in GROUPS.values() for s in g}
    return {
        "heading": HEADING,
        "export": {"id": export.name, "manifest_sha256": _sha256((export / "manifest.json").read_bytes()),
                   "input_pdf_sha256": pdf_sha},
        "params": dict(R.ROOM_PARAMS),
        "calibration_recomputed": {"sheets": CALIBRATION_SHEETS, "percentile": PERCENTILE, "n": n,
                                   "p95_d1_pt": round(float(np.percentile(d1s, PERCENTILE)), 3),
                                   "max_label_distance_pt": value, "max_d1_pt": round(max(d1s), 3)},
        "groups": {g: {"sheets": GROUPS[g], **groups[g]} for g in GROUPS},
        "per_sheet": per_sheet,
        "notes": [
            "dev (A1, A2) is the calibration set: its numbers are in-sample. held_out (A3, A4) did not influence the parameters.",
            "Accuracy is measured over devices whose ground-truth room is a labeled room; a proposal is correct "
            "when kind, name and label box all match.",
            "Exterior is never proposed and Unlabeled is only proposed far from every label; ground-truth "
            "Exterior/Unlabeled devices are counted, not scored.",
            "'wrong and unflagged' proposals carry no ambiguity flag, so every room stays a reviewer decision; "
            "the proposal is a convenience default only.",
            "Room polygons are not extracted; this checks a nearest-label heuristic on one supplied plan set "
            "and is not evidence that room detection is reliable.",
        ],
    }


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def markdown(ev: dict) -> str:
    p, c = ev["params"], ev["calibration_recomputed"]
    lines = [
        "# Room proposal evaluation", "", f"## {HEADING}", "",
        f"Export `{ev['export']['id']}` (manifest SHA-256 `{ev['export']['manifest_sha256']}`).", "",
        f"Parameters in effect: `max_label_distance_pt` = {p['max_label_distance_pt']}, "
        f"`ambiguity_ratio` = {p['ambiguity_ratio']}.", "",
        f"Calibration recomputed on {', '.join(c['sheets'])}: n = {c['n']} devices with a labeled GT room and a "
        f"correct nearest label; 95th percentile of d1 = {c['p95_d1_pt']} pt (max {c['max_d1_pt']} pt), "
        f"rounded up to {c['max_label_distance_pt']} pt.", "",
        "| group | sheets | GT-labeled devices | correct | accuracy | ambiguous | ambiguous rate "
        "| correct but flagged | wrong and unflagged |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for g, s in ev["groups"].items():
        lines.append(f"| {g} | {', '.join(s['sheets'])} | {s['gt_labeled']} | {s['correct']} | {_pct(s['accuracy'])} "
                     f"| {s['ambiguous']} | {_pct(s['ambiguous_rate'])} "
                     f"| {s['correct_but_flagged']} ({_pct(s['correct_but_flagged_rate_of_correct'])} of correct) "
                     f"| {s['wrong_and_unflagged']} |")
    lines += ["", "| group | GT Exterior | of which given a labeled proposal | GT Unlabeled "
              "| of which given a labeled proposal | ambiguous reasons |", "|---|---:|---:|---:|---:|---|"]
    for g, s in ev["groups"].items():
        lines.append(f"| {g} | {s['gt_exterior']} | {s['gt_exterior_given_a_labeled_proposal']} | {s['gt_unlabeled']} "
                     f"| {s['gt_unlabeled_given_a_labeled_proposal']} | {s['ambiguous_reasons']} |")
    lines += ["", "| sheet | GT-labeled | correct | accuracy | ambiguous rate | GT Exterior | GT Unlabeled |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for sid, s in ev["per_sheet"].items():
        lines.append(f"| {sid} | {s['gt_labeled']} | {s['correct']} | {_pct(s['accuracy'])} "
                     f"| {_pct(s['ambiguous_rate'])} | {s['gt_exterior']} | {s['gt_unlabeled']} |")
    lines += ["", "Notes", ""] + [f"- {n}" for n in ev["notes"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    from takeoff.groundtruth.export import ExportError, require_verified

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="a verified export directory under data/ground_truth/exports")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="directory for room_proposal_eval.{json,md}")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="render cache directory (git-ignored)")
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    try:
        require_verified(export)
    except (ExportError, OSError, ValueError, KeyError) as e:
        print(f"refusing {export}: {e}", file=sys.stderr)
        return 2
    ev = evaluate(export, Path(args.pdf), Path(args.cache))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "room_proposal_eval.json").write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "room_proposal_eval.md").write_text(markdown(ev), encoding="utf-8")
    c = ev["calibration_recomputed"]
    print(f"calibration on {c['sheets']}: n={c['n']} p95(d1)={c['p95_d1_pt']} pt "
          f"-> max_label_distance_pt={c['max_label_distance_pt']}")
    print(f"wrote {out / 'room_proposal_eval.json'} and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
