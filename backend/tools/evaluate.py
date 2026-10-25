"""Evaluate the candidate generators against a verified ground-truth export (tracker B-11).

From backend/:
    uv run python tools/evaluate.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z
    uv run python tools/evaluate.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --detector det0 --groups "dev=A1-111,A2-111;held_out=A3-111,A4-111"

Writes metrics.json, matches.jsonl, examples/ and report.md to a new directory
(default reports/evaluation/<export id>__<evaluator version>[__<detector>]); an existing directory is never
overwritten. The supplied PDF is only ever read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"


def _det0():
    from takeoff.detection import det0
    return det0.detect


DETECTORS = {"det0": _det0}


def main(argv=None) -> int:
    from takeoff.evaluation import evaluate as EV

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="a verified export directory under data/ground_truth/exports")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--out", default=None, help="new output directory (default: reports/evaluation/<export id>__<evaluator>[__<detector>])")
    ap.add_argument("--detector", choices=sorted(DETECTORS), action="append", default=[],
                    help="also evaluate this detector, built from the same checked candidates")
    ap.add_argument("--groups", default=None, help='sheet groups, e.g. "dev=A1-111,A2-111;held_out=A3-111,A4-111"')
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    tag = EV.EVALUATOR_VERSION.replace("/", "")
    name = "__".join([export.name, tag, *args.detector])
    out = Path(args.out) if args.out else ROOT / "reports" / "evaluation" / name
    if out.exists():
        print(f"{out} already exists; evaluations are never overwritten. Pass a new --out.", file=sys.stderr)
        return 2
    groups = None
    if args.groups:
        groups = {k: v.split(",") for k, v in (part.split("=", 1) for part in args.groups.split(";"))}
    ev = EV.evaluate(export, Path(args.pdf), detectors={d: DETECTORS[d]() for d in args.detector}, groups=groups)
    cmd = " ".join(["uv run python tools/evaluate.py", f"--export ../data/ground_truth/exports/{export.name}",
                    *[f"--detector {d}" for d in args.detector], *([f'--groups "{args.groups}"'] if args.groups else [])])
    EV.write(ev, out, Path(args.pdf), reproduce=cmd)
    systems = {**ev.metrics["generators"], **ev.metrics.get("detectors", {})}
    summary = {n: {k: g["primary"]["total"][k] for k in ("tp", "fp", "fn", "precision", "recall", "f1")}
               for n, g in systems.items()}
    if groups:
        summary["groups"] = {n: {grp: {k: v[k] for k in ("precision", "recall", "f1")} for grp, v in per.items()}
                             for n, per in ev.metrics["groups"]["systems"].items()}
    print(json.dumps({"out": str(out), "tolerance_pt": ev.metrics["tolerance_pt"], "generators": summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
