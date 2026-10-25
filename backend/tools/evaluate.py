"""Evaluate the candidate generators against a verified ground-truth export (tracker B-11).

From backend/:
    uv run python tools/evaluate.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z

Writes metrics.json, matches.jsonl, examples/ and report.md to a new directory
(default reports/evaluation/<export id>__eval1); an existing directory is never
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


def main(argv=None) -> int:
    from takeoff.evaluation import evaluate as EV

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="a verified export directory under data/ground_truth/exports")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--out", default=None, help="new output directory (default: reports/evaluation/<export id>__eval1)")
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    tag = EV.EVALUATOR_VERSION.replace("/", "")
    out = Path(args.out) if args.out else ROOT / "reports" / "evaluation" / f"{export.name}__{tag}"
    if out.exists():
        print(f"{out} already exists; evaluations are never overwritten. Pass a new --out.", file=sys.stderr)
        return 2
    ev = EV.evaluate(export, Path(args.pdf))
    EV.write(ev, out, Path(args.pdf))
    summary = {gen: {k: g["primary"]["total"][k] for k in ("tp", "fp", "fn", "precision", "recall")}
               for gen, g in ev.metrics["generators"].items()}
    print(json.dumps({"out": str(out), "tolerance_pt": ev.metrics["tolerance_pt"], "generators": summary}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
