"""Ground-truth review CLI (tracker B-03).

From backend/:
    uv run python tools/gt_review.py import-candidates      # pre-fill hints (never ground truth)
    uv run python tools/gt_review.py serve                  # http://127.0.0.1:8765
    uv run python tools/gt_review.py export                 # immutable export under data/ground_truth/exports

The supplied PDF is only ever read. The working database is
data/ground_truth/working.sqlite (git-ignored); exports are the durable record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "pdf": ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf",
    "db": ROOT / "data" / "ground_truth" / "working.sqlite",
    "cache": ROOT / "data" / "ground_truth" / "cache",
    "exports": ROOT / "data" / "ground_truth" / "exports",
}


def _paths(args) -> dict:
    return {k: Path(getattr(args, k) or v) for k, v in DEFAULTS.items()}


def cmd_import(args) -> int:
    import pymupdf

    from takeoff.groundtruth import candidates as CA
    from takeoff.groundtruth import sheets as S
    from takeoff.groundtruth.review import Workflow
    from takeoff.groundtruth.store import Store

    p = _paths(args)
    pages = [int(x) for x in args.pages.split(",")] if args.pages else None
    results = CA.generate(p["pdf"], pages=pages)
    sha = hashlib.sha256(p["pdf"].read_bytes()).hexdigest()
    doc = pymupdf.open(stream=p["pdf"].read_bytes(), filetype="pdf")
    specs = {}
    for n in pages or range(1, doc.page_count + 1):
        d = S.describe_sheet(doc[n - 1], sha, p["cache"])
        specs[d["sheet_id"]] = S.SheetSpec.from_description(d)
    store = Store(p["db"])
    Workflow(store, specs)  # ensures sheet and cell rows exist
    summary = {}
    for run, cands in results:
        _, inserted = store.import_candidate_run(run, cands, actor=args.reviewer)
        summary[run.generator] = {"run": run.id, "inserted": inserted, "params": run.params,
                                  "candidates_per_sheet": dict(Counter(c.sheet_id for c in cands))}
    store.close()
    print(json.dumps({"note": "candidate locations only; not detections or counts", "runs": summary}, indent=1))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from takeoff.groundtruth.api import create_app

    p = _paths(args)
    app = create_app(p["pdf"], p["db"], p["cache"], p["exports"], default_reviewer=args.reviewer)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_export(args) -> int:
    import pymupdf

    from takeoff.groundtruth import export as E
    from takeoff.groundtruth import sheets as S
    from takeoff.groundtruth.review import Workflow
    from takeoff.groundtruth.store import Store

    p = _paths(args)
    sha = hashlib.sha256(p["pdf"].read_bytes()).hexdigest()
    doc = pymupdf.open(stream=p["pdf"].read_bytes(), filetype="pdf")
    specs = {}
    for n in range(1, doc.page_count + 1):
        d = S.describe_sheet(doc[n - 1], sha, p["cache"])
        specs[d["sheet_id"]] = S.SheetSpec.from_description(d)
    store = Store(p["db"])
    out = E.export(Workflow(store, specs), p["exports"], args.reviewer, sha)
    store.close()
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"path": str(out), "level": m["level"], "sheets": {k: v["phase"] for k, v in m["sheets"].items()}},
                     indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for k in DEFAULTS:
        ap.add_argument(f"--{k}", default=None, help=f"default: {DEFAULTS[k]}")
    ap.add_argument("--reviewer", default="reviewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-candidates")
    imp.add_argument("--pages", default=None, help="comma-separated 1-based pages (default: all)")
    srv = sub.add_parser("serve")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    sub.add_parser("export")
    args = ap.parse_args(argv)
    return {"import-candidates": cmd_import, "serve": cmd_serve, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
