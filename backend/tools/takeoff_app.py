"""Takeoff app CLI (spec §5.1).

From backend/:
    uv run python tools/takeoff_app.py serve                 # http://127.0.0.1:8766, AI on (Claude)
    uv run python tools/takeoff_app.py serve --ai off        # det/0 only; no Anthropic request is ever made

The supplied PDF is registered at startup and only ever read. The working store is data/takeoff/takeoff.sqlite
(git-ignored), uploads go to data/takeoff/documents/<sha256>.pdf and exports to data/takeoff/exports/.
With --ai on nothing is requested at startup: availability is checked when /api/config or the first run asks.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "data": ROOT / "data" / "takeoff",
    "pdf": ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf",
}


def cmd_serve(args) -> int:
    import uvicorn

    from takeoff.app.api import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pdf = Path(args.pdf or DEFAULTS["pdf"])
    if not pdf.exists():
        print(f"supplied PDF not found: {pdf}", file=sys.stderr)
        return 2
    app = create_app(Path(args.data or DEFAULTS["data"]), pdf, default_reviewer=args.reviewer, background=True,
                     ai="claude" if args.ai == "on" else "off", max_requests=args.ai_max_requests)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    srv = sub.add_parser("serve", help="run the takeoff app")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8766)
    srv.add_argument("--reviewer", default="reviewer", help="actor when a request has no X-Reviewer header")
    srv.add_argument("--data", default=None, help=f"default: {DEFAULTS['data']}")
    srv.add_argument("--pdf", default=None, help=f"the supplied PDF; default: {DEFAULTS['pdf']}")
    srv.add_argument("--ai", choices=("on", "off"), default="on", help="on: Claude verify + discover; off: det/0 only")
    srv.add_argument("--ai-max-requests", type=int, default=None,
                     help="per-run AI request cap (default: TAKEOFF_AI_MAX_REQUESTS or 200)")
    args = ap.parse_args(argv)
    return {"serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
