"""det/0: a rule-based fusion of the Milestone 1 candidate generators (tracker T-01 experiment).

Proposals from ``vector_signature`` and ``raster_thickened`` that lie within
``site_radius_pt`` of a site's first proposal form one site; the site becomes
one device of the highest-priority category among its proposals:

* templates of the same category (the three light templates on one fixture,
  two receptacle hits) merge, so each device is counted once;
* category conflicts at one spot follow a fixed priority (ceiling fan > fan >
  detector > switch > receptacle > light > junction box), so a light template
  firing on a fan glyph or a junction-box template on a receptacle loses;
* a site with only junction-box proposals is kept only when a ``J`` text token
  sits at its centre, as the legend draws it (circle with J, no blades).

The point comes from a vector proposal of the winning category when there is
one (vector geometry is exact), otherwise from the best-scoring raster one.
Subtypes come from adjacent text tokens (``taxonomy.TOKEN_RULES``): each token
goes to the nearest device of a category it applies to, within
``token_radius_pt``, and a device's tokens are intersected (``MW`` + ``GFI`` →
``gfi_microwave``). Lights without a token take the best-scoring raster light
template; everything else falls back to a fixed default.

Rules and parameters were chosen by looking only at sheets A1-111 and A2-111
(development sheets); A3-111 and A4-111 are held out. Changing anything here
means a new DETECTOR_VERSION and a re-run of the evaluation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from takeoff.groundtruth import candidates as CA
from takeoff.groundtruth import models as M
from takeoff.groundtruth import sheets as S
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.protocol import TOKEN_SUGGEST_RADIUS_PT
from takeoff.pdf import coords as C
from takeoff.pdf import layout as L

DETECTOR_VERSION = "det/0"
PARAMS = {
    "proposal_generators": ["raster_thickened", "vector_signature"],
    # Dev sheets: proposals lie within 2.6 pt of their device; the closest two
    # same-category devices are 7.9 pt apart (ganged switches) and the closest
    # two of different categories 9.3 pt.
    "site_radius_pt": 4.0,
    "category_priority": ["ceiling_fan", "fan", "detector", "switch", "receptacle", "light", "junction_box"],
    "junction_box_token": "J",
    "junction_box_token_radius_pt": 2.0,  # dev: J tokens sit 0.4-0.6 pt from the hub
    "token_radius_pt": TOKEN_SUGGEST_RADIUS_PT,  # dev: linked tokens sit 8.3-12.3 pt from their device
    "default_subtype": {"switch": "standard", "receptacle": "afi_duplex", "light": "recessed_downlight",
                        "fan": "exhaust_fan", "detector": "smoke_detector"},
}
_FIXED_SUBTYPE = {"ceiling_fan": "optional_pre_wire", "junction_box": "electrical_junction_box"}
_GEN_ORDER = {"vector_signature": 0, "raster_thickened": 1}


def class_hint(class_file: str) -> tuple[str | None, str | None]:
    return CA.class_hint(class_file)


@dataclass(frozen=True)
class Proposal:
    id: str
    generator: str
    sheet_id: str
    class_hypothesis: str
    category: str | None
    subtype_hint: str | None
    score: float | None
    x_pt: float
    y_pt: float


@dataclass(frozen=True)
class Token:
    sheet_id: str
    text: str
    x_pt: float
    y_pt: float


@dataclass
class Detection:
    id: str
    sheet_id: str
    category: str
    subtype: str
    status: str
    x_pt: float
    y_pt: float
    score: float | None
    members: list[dict]
    subtype_source: str  # token | template | default | fixed
    tokens: list[str] = field(default_factory=list)


def _dist(ax, ay, bx, by) -> float:
    return math.hypot(ax - bx, ay - by)


def _sites(props: list[Proposal], params: dict) -> list[list[Proposal]]:
    prio = {c: i for i, c in enumerate(params["category_priority"])}
    order = sorted(props, key=lambda p: (prio[p.category], _GEN_ORDER.get(p.generator, 9), -(p.score or 0.0), p.id))
    sites: list[list[Proposal]] = []
    for p in order:
        home = next((s for s in sites if _dist(s[0].x_pt, s[0].y_pt, p.x_pt, p.y_pt) <= params["site_radius_pt"]), None)
        if home is None:
            sites.append([p])
        else:
            home.append(p)
    return sites


def _token_subtype(category: str, texts: list[str]) -> str | None:
    sets = [T.TOKEN_RULES[t][category] for t in texts]
    if not sets:
        return None
    base = min(sets, key=len)
    common = [s for s in base if all(s in other for other in sets)]
    return common[0] if common else sets[0][0]


def detect_sheet(proposals: list[Proposal], tokens: list[Token], params: dict = PARAMS) -> list[Detection]:
    """Detections for one sheet from its proposals and text tokens."""
    props = [p for p in proposals if p.generator in params["proposal_generators"] and p.category in params["category_priority"]]
    prio = {c: i for i, c in enumerate(params["category_priority"])}
    kept = []
    for site in _sites(props, params):
        category = min((p.category for p in site), key=prio.__getitem__)
        winners = [p for p in site if p.category == category]  # already in priority / vector-first / score order
        anchor = winners[0]
        if category == "junction_box" and not any(
                t.text == params["junction_box_token"] and _dist(t.x_pt, t.y_pt, anchor.x_pt, anchor.y_pt) <= params["junction_box_token_radius_pt"]
                for t in tokens):
            continue
        scores = [p.score for p in winners if p.score is not None]
        kept.append((category, anchor, winners, site, max(scores) if scores else None))

    # each subtype-bearing token goes to the nearest device of a category it applies to
    assigned: dict[int, list[tuple[float, str]]] = {}
    for t in sorted(tokens, key=lambda t: (t.y_pt, t.x_pt, t.text)):
        rule = T.TOKEN_RULES.get(t.text)
        if not rule:
            continue
        near = [(_dist(t.x_pt, t.y_pt, a.x_pt, a.y_pt), i) for i, (cat, a, *_rest) in enumerate(kept)
                if cat in rule and _dist(t.x_pt, t.y_pt, a.x_pt, a.y_pt) <= params["token_radius_pt"]]
        if near:
            d, i = min(near)
            assigned.setdefault(i, []).append((d, t.text))

    out = []
    for i, (category, anchor, winners, site, score) in enumerate(kept):
        texts = [x for _, x in sorted(assigned.get(i, []))]
        if category in _FIXED_SUBTYPE:
            subtype, source = _FIXED_SUBTYPE[category], "fixed"
        elif (sub := _token_subtype(category, texts)) is not None:
            subtype, source = sub, "token"
        elif category == "light" and any(p.score is not None for p in winners):
            subtype, source = max((p for p in winners if p.score is not None), key=lambda p: (p.score, p.id)).subtype_hint, "template"
        else:
            subtype, source = params["default_subtype"][category], "default"
        members = [{"id": p.id, "generator": p.generator, "class_hypothesis": p.class_hypothesis, "category": p.category,
                    "score": p.score} for p in sorted(site, key=lambda p: p.id)]
        status = T.REQUIRED_STATUS.get(category, T.STATUS_AS_DRAWN)
        out.append(Detection("", anchor.sheet_id, category, subtype, status, anchor.x_pt, anchor.y_pt, score, members, source, texts))
    out.sort(key=lambda d: (d.y_pt, d.x_pt, d.category))
    for n, d in enumerate(out):
        d.id = f"{d.sheet_id}:det0-{n:04d}"
    return out


def _sheet_tokens(page: pymupdf.Page) -> tuple[str, list[Token]]:
    lines = L.text_lines(page)
    sid = S.sheet_id(page, lines)
    region = S.electrical_region(page, lines)
    g = C.PageGeometry.from_page(page)
    toks = []
    for t in S.text_tokens(page, g, region["bbox_pt"]):
        x0, y0, x1, y1 = t["bbox_pt"]
        toks.append(Token(sid, t["text"], (x0 + x1) / 2, (y0 + y1) / 2))
    return sid, toks


def detect(pdf_path: Path, results: list[tuple[M.CandidateRun, list[M.Candidate]]], params: dict = PARAMS,
           pages: list[int] | None = None) -> dict:
    """Run det/0 over every sheet, or only the given pages (1-based). ``results`` are candidate runs
    (``candidates.generate``). ``pages`` only limits which pages are read; the rules are unchanged, so a sheet's
    detections are the same either way (the analyzer reads one page, so another page it cannot read does not fail
    it)."""
    doc = pymupdf.open(stream=Path(pdf_path).read_bytes(), filetype="pdf")  # never writes the file
    numbers = range(doc.page_count) if pages is None else [n - 1 for n in pages]
    tokens = dict(_sheet_tokens(doc[n]) for n in numbers)
    doc.close()
    used = [(run, cands) for run, cands in results if run.generator in params["proposal_generators"]]
    props = [Proposal(c.id, run.generator, c.sheet_id, c.class_hypothesis, c.category_hint, c.subtype_hint, c.score, *c.point_pt)
             for run, cands in used for c in cands]
    detections = []
    for sid in sorted(tokens):
        detections += detect_sheet([p for p in props if p.sheet_id == sid], tokens[sid], params)
    return {"detector_version": DETECTOR_VERSION, "params": params,
            "input_runs": [{"generator": run.generator, "run_hash": run.run_hash} for run, _ in sorted(used, key=lambda x: x[0].generator)],
            "detections": [d.__dict__ for d in detections]}
