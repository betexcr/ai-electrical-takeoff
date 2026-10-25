"""One-to-one, category-aware matching of predictions to ground truth (``eval/1``).

Positions are PDF points in the unrotated page space; Euclidean distance there
equals the displayed distance. A prediction can only match a ground-truth
device of the same category on the same sheet. Candidate pairs within the
tolerance are taken nearest first (ties by ground-truth id, then prediction
id), each item used at most once, so the result does not depend on input
order. The tolerance boundary is inclusive.

Changing any rule here changes every metric: bump EVALUATOR_VERSION in
evaluate.py and re-run the fixed evaluation set.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

# A matched prediction this close to its ground-truth point almost certainly had
# its position copied into the ground truth when the reviewer accepted it.
COPIED_EPS_PT = 0.05


@dataclass(frozen=True)
class Item:
    id: str
    sheet_id: str
    category: str
    x_pt: float
    y_pt: float
    score: float | None = None


@dataclass(frozen=True)
class Pair:
    gt_id: str
    pred_id: str
    distance_pt: float


@dataclass
class Result:
    tp: list[Pair] = field(default_factory=list)
    fp: list[str] = field(default_factory=list)
    fn: list[str] = field(default_factory=list)


@dataclass
class Kinds:
    fp: dict[str, str] = field(default_factory=dict)  # duplicate | wrong_category | background
    fn: dict[str, str] = field(default_factory=dict)  # found_as_other_category | not_found


def _dist(a: Item, b: Item) -> float:
    return math.hypot(a.x_pt - b.x_pt, a.y_pt - b.y_pt)


def match(gt: list[Item], preds: list[Item], tol_pt: float) -> Result:
    groups: dict[tuple[str, str], tuple[list[Item], list[Item]]] = defaultdict(lambda: ([], []))
    for g in gt:
        groups[(g.sheet_id, g.category)][0].append(g)
    for p in preds:
        groups[(p.sheet_id, p.category)][1].append(p)
    out = Result()
    for key in sorted(groups):
        gs, ps = groups[key]
        pairs = sorted((d, g.id, p.id) for g in gs for p in ps if (d := _dist(g, p)) <= tol_pt)
        used_g, used_p = set(), set()
        for d, gid, pid in pairs:
            if gid not in used_g and pid not in used_p:
                used_g.add(gid), used_p.add(pid)
                out.tp.append(Pair(gid, pid, round(d, 4)))
        out.fp += sorted(p.id for p in ps if p.id not in used_p)
        out.fn += sorted(g.id for g in gs if g.id not in used_g)
    out.tp.sort(key=lambda t: (t.gt_id, t.pred_id))
    out.fp.sort()
    out.fn.sort()
    return out


def classify(gt: list[Item], preds: list[Item], r: Result, tol_pt: float) -> Kinds:
    """Why each false positive and each miss happened (priority: duplicate > wrong category > background)."""
    gt_by_id, pred_by_id = {g.id: g for g in gt}, {p.id: p for p in preds}
    matched_gt = {t.gt_id for t in r.tp}
    kinds = Kinds()
    for pid in r.fp:
        p = pred_by_id[pid]
        near = [g for g in gt if g.sheet_id == p.sheet_id and _dist(g, p) <= tol_pt]
        if any(g.category == p.category and g.id in matched_gt for g in near):
            kinds.fp[pid] = "duplicate"
        elif any(g.category != p.category for g in near):
            kinds.fp[pid] = "wrong_category"
        else:
            kinds.fp[pid] = "background"
    for gid in r.fn:
        g = gt_by_id[gid]
        other = any(p.sheet_id == g.sheet_id and p.category != g.category and _dist(g, p) <= tol_pt for p in preds)
        kinds.fn[gid] = "found_as_other_category" if other else "not_found"
    return kinds


def rates(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = None
    if precision is not None and recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    r4 = lambda v: None if v is None else round(v, 4)  # noqa: E731
    return {"precision": r4(precision), "recall": r4(recall), "f1": r4(f1)}


def _stats(ds: list[float]) -> dict | None:
    if not ds:
        return None
    s = sorted(ds)
    p90 = s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)]
    return {"n": len(s), "mean": round(statistics.fmean(s), 3), "median": round(statistics.median(s), 3),
            "p90": round(p90, 3), "max": round(s[-1], 3)}


def localization(tp: list[Pair]) -> dict:
    """Distance of matched pairs (pt); 'independent' leaves out positions copied into the ground truth."""
    ds = [t.distance_pt for t in tp]
    indep = [d for d in ds if d > COPIED_EPS_PT]
    return {"all": _stats(ds), "position_copied": len(ds) - len(indep), "independent": _stats(indep)}


def _cell(gts, preds, tp, fp_ids, fn_ids, kinds: Kinds) -> dict:
    fpk = defaultdict(int)
    for pid in fp_ids:
        fpk[kinds.fp[pid]] += 1
    fnk = defaultdict(int)
    for gid in fn_ids:
        fnk[kinds.fn[gid]] += 1
    return {"n_gt": len(gts), "n_pred": len(preds), "tp": len(tp), "fp": len(fp_ids), "fn": len(fn_ids),
            "count_error": len(preds) - len(gts), **rates(len(tp), len(fp_ids), len(fn_ids)),
            "fp_kinds": dict(sorted(fpk.items())), "fn_kinds": dict(sorted(fnk.items())), "localization": localization(tp)}


def summarize(gt: list[Item], preds: list[Item], r: Result, kinds: Kinds) -> dict:
    gt_by_id, pred_by_id = {g.id: g for g in gt}, {p.id: p for p in preds}

    def select(keep):
        gs = [g for g in gt if keep(g)]
        ps = [p for p in preds if keep(p)]
        tp = [t for t in r.tp if keep(gt_by_id[t.gt_id])]
        fp = [i for i in r.fp if keep(pred_by_id[i])]
        fn = [i for i in r.fn if keep(gt_by_id[i])]
        return _cell(gs, ps, tp, fp, fn, kinds)

    cats = sorted({x.category for x in gt} | {x.category for x in preds})
    sheets = sorted({x.sheet_id for x in gt} | {x.sheet_id for x in preds})
    return {
        "total": select(lambda x: True),
        "by_category": {c: select(lambda x, c=c: x.category == c) for c in cats},
        "by_sheet_category": {s: {c: select(lambda x, s=s, c=c: x.sheet_id == s and x.category == c) for c in cats}
                              for s in sheets},
    }
