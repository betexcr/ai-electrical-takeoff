"""B-11 AC-E2: one-to-one, category-aware matching and the error taxonomy (synthetic cases)."""

from __future__ import annotations

import random

from takeoff.evaluation import matching as EM

TOL = 6.0


def gt(i, x, y, cat="switch", sheet="A1-111"):
    return EM.Item(id=f"g{i}", sheet_id=sheet, category=cat, x_pt=x, y_pt=y)


def pred(i, x, y, cat="switch", sheet="A1-111", score=0.9):
    return EM.Item(id=f"p{i}", sheet_id=sheet, category=cat, x_pt=x, y_pt=y, score=score)


def test_one_to_one_nearest_pair_first():
    # p1 is nearer to g1 than p2 is; p2 must not steal g1 and stays unmatched (one GT only)
    r = EM.match([gt(1, 0, 0)], [pred(1, 1, 0), pred(2, 0.5, 0)], TOL)
    assert [(t.gt_id, t.pred_id) for t in r.tp] == [("g1", "p2")]
    assert r.fp == ["p1"] and r.fn == []


def test_global_nearest_first_maximises_sensible_pairs():
    # g1 at 0, g2 at 10; p1 at 4 (4 from g1, 6 from g2), p2 at 9 (1 from g2)
    r = EM.match([gt(1, 0, 0), gt(2, 10, 0)], [pred(1, 4, 0), pred(2, 9, 0)], TOL)
    assert sorted((t.gt_id, t.pred_id) for t in r.tp) == [("g1", "p1"), ("g2", "p2")]


def test_tolerance_boundary_is_inclusive():
    assert len(EM.match([gt(1, 0, 0)], [pred(1, 6.0, 0)], TOL).tp) == 1
    r = EM.match([gt(1, 0, 0)], [pred(1, 6.001, 0)], TOL)
    assert r.tp == [] and r.fp == ["p1"] and r.fn == ["g1"]


def test_matching_is_category_and_sheet_aware():
    r = EM.match([gt(1, 0, 0, cat="light"), gt(2, 0, 0, sheet="A2-111")], [pred(1, 0, 0, cat="switch")], TOL)
    assert r.tp == [] and r.fp == ["p1"] and sorted(r.fn) == ["g1", "g2"]


def test_result_is_independent_of_input_order():
    rng = random.Random(7)
    gts = [gt(i, rng.uniform(0, 60), rng.uniform(0, 60), cat=rng.choice(["switch", "light"])) for i in range(30)]
    preds = [pred(i, rng.uniform(0, 60), rng.uniform(0, 60), cat=rng.choice(["switch", "light"])) for i in range(40)]
    a = EM.match(gts, preds, TOL)
    b = EM.match(list(reversed(gts)), rng.sample(preds, len(preds)), TOL)
    assert a == b


def test_false_positive_kinds():
    gts = [gt(1, 0, 0, cat="switch"), gt(2, 100, 0, cat="light")]
    preds = [pred(1, 0, 0, cat="switch"),     # TP
             pred(2, 2, 0, cat="switch"),     # second hit on the same device: duplicate
             pred(3, 101, 0, cat="switch"),   # on a light: wrong category
             pred(4, 300, 0, cat="switch")]   # nowhere near a device: background
    r = EM.match(gts, preds, TOL)
    kinds = EM.classify(gts, preds, r, TOL)
    assert kinds.fp == {"p2": "duplicate", "p3": "wrong_category", "p4": "background"}
    assert kinds.fn == {"g2": "found_as_other_category"}


def test_duplicate_wins_over_wrong_category_and_misses_can_be_not_found():
    gts = [gt(1, 0, 0, cat="switch"), gt(2, 3, 0, cat="light"), gt(3, 500, 0, cat="light")]
    preds = [pred(1, 0, 0, cat="switch"), pred(2, 1, 0, cat="switch")]  # p2 near both g1 (matched) and g2
    r = EM.match(gts, preds, TOL)
    kinds = EM.classify(gts, preds, r, TOL)
    assert kinds.fp == {"p2": "duplicate"}
    assert kinds.fn == {"g2": "found_as_other_category", "g3": "not_found"}


def test_rates_are_null_when_undefined():
    s = EM.rates(tp=0, fp=0, fn=0)
    assert s == {"precision": None, "recall": None, "f1": None}
    s = EM.rates(tp=0, fp=3, fn=0)
    assert s["precision"] == 0.0 and s["recall"] is None and s["f1"] is None
    s = EM.rates(tp=3, fp=1, fn=1)
    assert (s["precision"], s["recall"], s["f1"]) == (0.75, 0.75, 0.75)


def test_localization_stats_and_copied_split():
    r = EM.match([gt(1, 0, 0), gt(2, 50, 0), gt(3, 100, 0)], [pred(1, 0, 0), pred(2, 53, 4), pred(3, 101, 0)], TOL)
    loc = EM.localization(r.tp)
    assert loc["all"]["n"] == 3 and loc["all"]["max"] == 5.0 and loc["all"]["median"] == 1.0
    assert loc["position_copied"] == 1  # p1 sits exactly on g1: its position was copied into the ground truth
    assert loc["independent"]["n"] == 2 and loc["independent"]["mean"] == 3.0
    assert EM.localization([])["all"] is None


def test_summary_counts_add_up_per_category_and_sheet():
    gts = [gt(1, 0, 0), gt(2, 100, 0), gt(3, 0, 0, cat="light"), gt(4, 0, 0, sheet="A2-111")]
    preds = [pred(1, 0, 0), pred(2, 300, 0), pred(3, 0, 0, sheet="A2-111"), pred(4, 0, 0, cat="light")]
    r = EM.match(gts, preds, TOL)
    s = EM.summarize(gts, preds, r, EM.classify(gts, preds, r, TOL))
    sw = s["by_category"]["switch"]
    assert (sw["n_gt"], sw["n_pred"], sw["tp"], sw["fp"], sw["fn"]) == (3, 3, 2, 1, 1)
    assert s["by_sheet_category"]["A2-111"]["switch"]["tp"] == 1
    t = s["total"]
    assert t["tp"] + t["fn"] == len(gts) and t["tp"] + t["fp"] == len(preds)
