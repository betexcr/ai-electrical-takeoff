"""ai-discover/1 (spec §5.9.2, revision 3.1): tiling, tile crops, pixel -> point conversion, the drop rule, the
cross-tile merge (overlap-only finds kept, same-category duplicates merged with all supporting evidence, never
across categories), request stability, failures, budget, the fake and the evaluation script's refusals and
offline success path. Stub clients only; nothing here makes a network request."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import random
import re
import struct
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from takeoff.ai import common as AC
from takeoff.ai import discover as D
from takeoff.ai import prompt as P
from takeoff.app import models as TM
from takeoff.app.merge import merge_sheet
from takeoff.app.rooms import RoomProposal

from test_ai_common import StubClient, response, usage

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "data" / "ground_truth" / "exports" / "gt_001_20261025T020009Z"
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
SHEET = "A1-111"
DOC = "d" * 64
A1_REGION = (854.4, 37.08, 1439.04, 870.12)  # the gt_001 export's A1-111 electrical region
SCALE = 300 / 72


def synthetic(region=(0.0, 0.0, 376.0, 200.0), origin=(0, 0), value=255):
    """A white region image covering ``region`` (pixel-snapped like render_region_image) and its mapping."""
    x0, y0, x1, y1 = region
    ox, oy = math.floor(x0 * SCALE) + origin[0], math.floor(y0 * SCALE) + origin[1]
    w, h = math.ceil(x1 * SCALE) - ox, math.ceil(y1 * SCALE) - oy
    covers = [ox / SCALE, oy / SCALE, (ox + w) / SCALE, (oy + h) / SCALE]
    page_w, page_h = 2000.0, 1200.0
    mapping = {"width_px": w, "height_px": h, "dpi": 300, "origin_px": [ox, oy], "region_pt": list(region),
               "covers_pt": covers, "covers": {"u0": covers[0] / page_w, "v0": covers[1] / page_h,
                                               "u1": covers[2] / page_w, "v1": covers[3] / page_h}}
    return np.full((h, w), value, np.uint8), mapping


def png(value: int, size=(10, 10)) -> bytes:
    return cv2.imencode(".png", np.full(size, value, np.uint8))[1].tobytes()


def point(x_px, y_px, category="ceiling_fan", subtype=None, uncertain=False, rationale="fan symbol"):
    return D.AIPoint(x_px=x_px, y_px=y_px, category=category, subtype=subtype, uncertain=uncertain,
                     rationale=rationale)


def obs(tile_id, x, y, category="ceiling_fan", centrality=10.0, verdict_id=None, subtype=None):
    return D.TileObservation(tile_id=tile_id, verdict_id=verdict_id or f"v-{tile_id}", category=category,
                             subtype=subtype, point_pt=(x, y), centrality_pt=centrality, uncertain=False,
                             rationale=f"seen in {tile_id}")


def tile_size(kw) -> tuple[int, int]:
    """The tile image's pixel size from the request (the second image block)."""
    images = [b for b in kw["messages"][0]["content"] if b["type"] == "image"]
    data = base64.standard_b64decode(images[1]["source"]["data"])
    return struct.unpack(">II", data[16:24])


def claude(stub):
    return D.ClaudeDiscoverer(AC.AIClient(model="claude-test", client=stub))


def dump(dets):
    return [d.model_dump(exclude={"created_at"}) for d in dets]


# --- tiles ---------------------------------------------------------------------------------------------------

def test_tiles_cover_region_with_overlap():
    assert (D.AI_VERSION, D.PROMPT_VERSION) == ("ai-discover/1", "ai-discover-prompt/1")
    assert (D.TILE_PT, D.OVERLAP_PT, D.MAX_ITEMS) == (200.0, 24.0, 60)
    ts = D.tiles(A1_REGION, SHEET)
    assert len(ts) == 20  # "about 20 tiles per sheet" (spec §5.9.2): 4 columns x 5 rows on A1
    assert ts[0].id == "A1-111:t0000" and ts[-1].id == "A1-111:t0403" and all(t.sheet_id == SHEET for t in ts)
    assert [t.id for t in ts] == sorted(t.id for t in ts)  # row-major
    x0, y0, x1, y1 = A1_REGION
    for t in ts:
        a, b, c, d = t.bbox_pt
        assert x0 <= a < c <= x1 and y0 <= b < d <= y1 and c - a <= 200.0 + 1e-9 and d - b <= 200.0 + 1e-9
    grid = {(int(t.id[-4:-2]), int(t.id[-2:])): t.bbox_pt for t in ts}
    for (r, c), box in grid.items():  # adjacent tiles overlap by exactly 24 pt
        if (r, c + 1) in grid:
            assert grid[(r, c + 1)][0] == pytest.approx(box[2] - 24.0)
        if (r + 1, c) in grid:
            assert grid[(r + 1, c)][1] == pytest.approx(box[3] - 24.0)
    rng = random.Random(3)
    pts = [(x0, y0), (x1, y1), (x0, y1), (x1, y0)] + [(rng.uniform(x0, x1), rng.uniform(y0, y1)) for _ in range(2000)]
    for x, y in pts:  # every region point lies in at least one tile
        assert any(t.bbox_pt[0] <= x <= t.bbox_pt[2] and t.bbox_pt[1] <= y <= t.bbox_pt[3] for t in ts), (x, y)
    # a region no bigger than one tile is one tile
    assert [t.bbox_pt for t in D.tiles((10.0, 20.0, 110.0, 220.0), "X")] == [(10.0, 20.0, 110.0, 220.0)]
    # centrality: distance to the nearest tile edge
    t = D.Tile("X:t0000", "X", (0.0, 0.0, 200.0, 200.0))
    assert D.centrality_pt(t, (100.0, 100.0)) == 100.0 and D.centrality_pt(t, (190.0, 30.0)) == 10.0


def test_tile_image_within_2576px_and_deterministic():
    region_box = (854.4, 37.08, 1439.04, 870.12)
    rng = np.random.default_rng(5)
    img, mapping = synthetic(region_box)
    img = rng.integers(0, 256, img.shape, dtype=np.uint8)
    ts = D.tiles(region_box, SHEET)
    for t in ts:
        data = D.crop_tile(img, mapping, t)
        tile = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
        h, w = tile.shape
        assert max(h, w) <= 2576
        assert abs(w - (t.bbox_pt[2] - t.bbox_pt[0]) * SCALE) <= 1 and abs(h - (t.bbox_pt[3] - t.bbox_pt[1]) * SCALE) <= 1
        assert D.crop_tile(img, mapping, t) == data  # deterministic
        ox, oy = D.tile_origin_px(mapping, t)
        inside = img[oy:oy + h, ox:ox + w]
        assert np.array_equal(tile[:inside.shape[0], :inside.shape[1]], inside)  # the region's own pixels
    full = cv2.imdecode(np.frombuffer(D.crop_tile(img, mapping, ts[0]), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert full.shape in {(833, 833), (834, 834), (833, 834), (834, 833)}  # a full 200 pt tile at 300 DPI
    # the same tile from PNG bytes
    region_png = cv2.imencode(".png", img)[1].tobytes()
    assert D.crop_tile(region_png, mapping, ts[5]) == D.crop_tile(img, mapping, ts[5])


# --- points --------------------------------------------------------------------------------------------------

def test_points_convert_pixels_to_pdf_points():
    region_box = (100.3, 50.7, 500.9, 400.2)  # off-pixel region edges, as on the real sheets
    img, mapping = synthetic(region_box)
    marks = [(150.0, 80.0), (277.77, 199.1), (480.4, 390.6), (290.0, 233.3)]
    for x, y in marks:  # a dark 5 px mark at each known point
        px = (x - mapping["covers_pt"][0]) * SCALE
        py = (y - mapping["covers_pt"][1]) * SCALE
        img[int(round(py)) - 2:int(round(py)) + 3, int(round(px)) - 2:int(round(px)) + 3] = 0
    for t in D.tiles(region_box, "S"):
        tile = cv2.imdecode(np.frombuffer(D.crop_tile(img, mapping, t), np.uint8), cv2.IMREAD_GRAYSCALE)
        n, labels, stats, cents = cv2.connectedComponentsWithStats((tile < 128).astype(np.uint8))
        found = [point(float(cx), float(cy)) for cx, cy in cents[1:]]
        got = D.points_to_observations(t, mapping, region_box, found, "v-1")
        assert len(got) == len(found)
        for o in got:
            nearest = min(marks, key=lambda m: math.hypot(m[0] - o.point_pt[0], m[1] - o.point_pt[1]))
            assert math.hypot(nearest[0] - o.point_pt[0], nearest[1] - o.point_pt[1]) <= 0.5
            assert o.tile_id == t.id and o.verdict_id == "v-1"
            assert o.centrality_pt == pytest.approx(D.centrality_pt(t, o.point_pt))
    # tile pixels round-trip through points
    t = D.tiles(region_box, "S")[3]
    for p in [(t.bbox_pt[0] + 3.3, t.bbox_pt[1] + 7.9), (t.bbox_pt[2] - 1.0, t.bbox_pt[3] - 2.0)]:
        assert D.tile_px_to_pt(mapping, t, D.pt_to_tile_px(mapping, t, p)) == pytest.approx(p, abs=1e-9)


def test_only_points_outside_region_or_tile_are_dropped():
    img, mapping = synthetic((0.0, 0.0, 600.0, 600.0))
    region = (0.0, 0.0, 376.0, 376.0)  # the region the points are judged against
    tile = D.Tile("S:t0001", "S", (176.0, 0.0, 376.0, 200.0))
    beyond = D.Tile("S:t0002", "S", (352.0, 0.0, 552.0, 200.0))  # extends past the region's right edge
    px = lambda t, x, y: D.pt_to_tile_px(mapping, t, (x, y))  # noqa: E731
    pts = [point(*px(tile, 276.0, 100.0)),                # centre: kept
           point(*px(tile, 180.0, 100.0)),                # overlap strip with the left neighbour: kept
           point(*px(tile, 370.0, 190.0)),                # overlap strip, near a corner: kept
           point(-5.0, 40.0),                             # left of the tile image: malformed, dropped
           point(40.0, 9000.0),                           # below the tile image: dropped
           point(float("nan"), 10.0)]                     # not a coordinate: dropped
    got = D.points_to_observations(tile, mapping, region, pts, "v-7")
    assert [round(o.point_pt[0], 3) for o in got] == [276.0, 180.0, 370.0]
    kept, dropped = D.classify_points(tile, mapping, region, pts)
    assert [i for i, _ in kept] == [0, 1, 2]
    assert dropped == [{"index": 3, "reason": "outside_tile"}, {"index": 4, "reason": "outside_tile"},
                       {"index": 5, "reason": "outside_tile"}]
    # inside the tile but outside the region: dropped as outside the region
    got = D.points_to_observations(beyond, mapping, region, [point(*px(beyond, 370.0, 50.0)),
                                                             point(*px(beyond, 400.0, 50.0))], "v-8")
    assert [round(o.point_pt[0], 3) for o in got] == [370.0]
    assert D.classify_points(beyond, mapping, region, [point(*px(beyond, 400.0, 50.0))])[1] == [
        {"index": 0, "reason": "outside_region"}]
    # a subtype outside the taxonomy is not carried over (the verdict keeps the model's answer as given)
    (o,) = D.points_to_observations(tile, mapping, region, [point(*px(tile, 276.0, 100.0), category="light",
                                                                  subtype="disco_ball")], "v-9")
    assert o.category == "light" and o.subtype is None
    (o,) = D.points_to_observations(tile, mapping, region, [point(*px(tile, 276.0, 100.0), category="light",
                                                                  subtype="pendant")], "v-9")
    assert o.subtype == "pendant"


# --- merge across tiles (revision 3.1) -----------------------------------------------------------------------

def two_tiles():
    img, mapping = synthetic((0.0, 0.0, 376.0, 200.0))
    a, b = D.tiles((0.0, 0.0, 376.0, 200.0), SHEET)
    assert (a.bbox_pt, b.bbox_pt) == ((0.0, 0.0, 200.0, 200.0), (176.0, 0.0, 376.0, 200.0))
    return img, mapping, a, b


def discover_with(points_by_tile, budget=None):
    img, mapping, a, b = two_tiles()
    fake = D.FakeDiscoverer(points_by_tile)
    return fake.discover("run-001", DOC, SHEET, img, mapping, (0.0, 0.0, 376.0, 200.0), png(255), budget), mapping, a, b


def test_symbol_found_only_in_neighbouring_tiles_overlap_survives():
    img, mapping, a, b = two_tiles()
    fan = (190.0, 100.0)  # inside both tiles: B's overlap strip with A
    (dets, verdicts), _, _, _ = discover_with({a.id: [], b.id: [point(*D.pt_to_tile_px(mapping, b, fan))]})
    assert [v.tile_id for v in verdicts] == [a.id, b.id] and all(v.error is None for v in verdicts)
    (d,) = dets
    assert d.source == "ai_discover" and d.category == "ceiling_fan" and d.point_pt == pytest.approx(fan)
    vb = verdicts[1]
    assert d.verdict_id == vb.id and d.rationale == "fan symbol" and d.members == [] and d.score is None
    assert [(s.tile_id, s.verdict_id) for s in d.supporting] == [(b.id, vb.id)]
    assert d.supporting[0].centrality_pt == pytest.approx(14.0)  # 190 - 176
    assert (d.point.u, d.point.v) == pytest.approx((fan[0] / 2000.0, fan[1] / 1200.0))


def test_duplicate_observations_become_one_candidate_with_all_supporting_evidence():
    img, mapping, a, b = two_tiles()
    in_a, in_b = (188.0, 100.0), (190.0, 100.0)  # the same fan, 2 pt apart; B's view is more central (14 vs 12)
    (dets, verdicts), _, _, _ = discover_with({a.id: [point(*D.pt_to_tile_px(mapping, a, in_a), rationale="A")],
                                               b.id: [point(*D.pt_to_tile_px(mapping, b, in_b), rationale="B")]})
    va, vb = verdicts
    (d,) = dets
    assert d.point_pt == pytest.approx(in_b) and d.verdict_id == vb.id and d.rationale == "B"
    assert [(s.tile_id, s.verdict_id) for s in d.supporting] == [(a.id, va.id), (b.id, vb.id)]
    assert [s.point_pt for s in d.supporting] == [pytest.approx(in_a), pytest.approx(in_b)]
    assert [s.centrality_pt for s in d.supporting] == [pytest.approx(12.0), pytest.approx(14.0)]


def test_centrality_ties_break_deterministically():
    m = synthetic((0.0, 0.0, 600.0, 600.0))[1]
    group = [obs("S:t0001", 101.0, 100.0, centrality=20.0), obs("S:t0000", 103.0, 101.0, centrality=20.0),
             obs("S:t0000", 102.0, 99.0, centrality=20.0), obs("S:t0002", 100.0, 100.0, centrality=5.0)]
    (d,) = D.merge_across_tiles("run-001", DOC, "S", group, mapping=m)
    assert d.point_pt == (102.0, 99.0)  # equal centrality: min (tile id, y, x)
    assert [(s.tile_id, s.point_pt) for s in d.supporting] == [
        ("S:t0000", (102.0, 99.0)), ("S:t0000", (103.0, 101.0)), ("S:t0001", (101.0, 100.0)),
        ("S:t0002", (100.0, 100.0))]
    others = [obs("S:t0003", 300.0, 300.0, category="light"), obs("S:t0004", 400.0, 120.0, category="switch")]
    first = D.merge_across_tiles("run-001", DOC, "S", group + others, mapping=m)
    rng = random.Random(11)
    for _ in range(10):
        shuffled = group + others
        rng.shuffle(shuffled)
        assert dump(D.merge_across_tiles("run-001", DOC, "S", shuffled, mapping=m)) == dump(first)
    assert len({d.id for d in first}) == 3 and all(d.id.startswith("run-001:S:") for d in first)


def test_different_categories_are_never_merged():
    img, mapping, a, b = two_tiles()
    (dets, verdicts), _, _, _ = discover_with({
        a.id: [point(*D.pt_to_tile_px(mapping, a, (188.0, 100.0)), category="ceiling_fan", rationale="fan")],
        b.id: [point(*D.pt_to_tile_px(mapping, b, (190.0, 100.0)), category="light", subtype="pendant",
                     rationale="pendant")]})
    assert sorted(d.category for d in dets) == ["ceiling_fan", "light"]
    assert all(len(d.supporting) == 1 for d in dets)

    run = TM.Run(id="run-001", document_id=DOC, detector="det/0", detector_version="1", created_by="tester")
    propose = lambda pt: RoomProposal(room=None, ambiguous=False, reason=None, nearest=[])  # noqa: E731
    res = merge_sheet(run, dets, [], propose, verdicts, now="2026-10-25T12:00:00.000+00:00")
    (dev,) = res.new_devices  # one AI proposal...
    assert dev.origin == "ai" and dev.review_state == "proposed" and dev.category == "ceiling_fan"
    by_type = {f.type: f for f in res.flags}
    assert set(by_type) == {"ai_only_candidate", "model_disagrees"}
    assert by_type["ai_only_candidate"].details["tile_id"] == a.id
    md = by_type["model_disagrees"]  # ...plus a model_disagrees flag carrying the other observation
    assert md.device_id == dev.id and md.details["ai_category"] == "light" and md.details["ai_subtype"] == "pendant"
    assert md.details["source"] == "discover" and md.details["verdict_id"] == verdicts[1].id


def test_chained_same_category_points_form_one_cluster():
    m = synthetic((0.0, 0.0, 600.0, 600.0))[1]
    chain = [obs("S:t0000", 100.0, 50.0, centrality=3.0), obs("S:t0001", 105.0, 50.0, centrality=9.0),
             obs("S:t0002", 110.0, 50.0, centrality=4.0)]  # 0-10 are 10 pt apart, linked through 5
    alone = obs("S:t0003", 116.5, 50.0, centrality=50.0)   # 6.5 pt from the chain's end: its own cluster
    exact = obs("S:t0004", 300.0, 300.0), obs("S:t0005", 306.0, 300.0)  # exactly 6 pt: linked (inclusive)
    dets = D.merge_across_tiles("run-001", DOC, "S", chain + [alone, *exact], mapping=m)
    assert len(dets) == 3
    by_x = {round(d.point_pt[0], 1): d for d in dets}
    assert len(by_x[105.0].supporting) == 3 and len(by_x[116.5].supporting) == 1
    assert sum(len(d.supporting) for d in dets) == 6


# --- requests ------------------------------------------------------------------------------------------------

def test_request_order_composite_then_tile_and_stable_bytes():
    tile_png, composite = png(10, (834, 834)), png(20, (300, 600))
    system, content = D.build_discover_request(tile_png, composite, P.DISCOVER_TAXONOMY_TEXT)
    again = D.build_discover_request(tile_png, composite, P.DISCOVER_TAXONOMY_TEXT)
    assert json.dumps([system, content], sort_keys=True) == json.dumps(list(again), sort_keys=True)
    assert system == P.DISCOVER_SYSTEM and P.DISCOVER_PROMPT_VERSION == D.PROMPT_VERSION
    images = [i for i, b in enumerate(content) if b["type"] == "image"]
    assert len(images) == 2
    assert base64.standard_b64decode(content[images[0]]["source"]["data"]) == composite
    assert base64.standard_b64decode(content[images[1]]["source"]["data"]) == tile_png
    assert content[-1]["type"] == "text" and images[1] < len(content) - 1  # the instruction text comes last
    last = content[-1]["text"]
    assert P.DISCOVER_TAXONOMY_TEXT in last and "834 x 834 pixels" in last and "ceiling_fan" in last
    blob = system + "".join(b.get("text", "") for b in content)
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", blob) and not re.search(r"[0-9a-f]{32}", blob)
    # the schema bounds
    assert len(point(1, 1, rationale="x" * 400).rationale) == P.DISCOVER_RATIONALE_MAX_CHARS == 150
    with pytest.raises(Exception):
        D.AIPointList(points=[point(1, 1)] * 61)
    assert len(D.AIPointList(points=[point(1, 1)] * 60).points) == D.MAX_ITEMS


def test_failed_tile_yields_verdict_with_error_and_no_detections():
    img, mapping = synthetic((0.0, 0.0, 300.0, 200.0))
    a, b = D.tiles((0.0, 0.0, 300.0, 200.0), SHEET)  # A is 200 pt wide, B 124 pt: told apart by size

    def answer(kw):
        w, _ = tile_size(kw)
        if w > 600:  # tile A
            return response(None, stop_reason="refusal", u=usage(900, 3))
        return response(D.AIPointList(points=[point(50.0, 60.0)]), u=usage(1200, 90, 0, 0))

    stub = StubClient(default=answer)
    dets, verdicts = claude(stub).discover("run-001", DOC, SHEET, img, mapping, (0.0, 0.0, 300.0, 200.0), png(200),
                                           AC.RequestBudget(10))
    assert len(stub.calls) == 2 and [v.tile_id for v in verdicts] == [a.id, b.id]
    va, vb = verdicts
    assert va.error == {"code": "refusal"} and va.output is None and va.stop_reason == "refusal"
    assert (va.input_tokens, va.output_tokens) == (900, 3)
    assert vb.error is None and vb.output["points"][0]["x_px"] == 50.0 and vb.output["dropped"] == []
    assert (vb.input_tokens, vb.output_tokens, vb.cache_creation_input_tokens) == (1200, 90, 0)
    assert all(v.kind == "discover" and v.implementation == "claude" and v.model == "claude-test" for v in verdicts)
    assert all((v.prompt_version, v.ai_version) == ("ai-discover-prompt/1", "ai-discover/1") for v in verdicts)
    assert len(vb.image_sha256s) == 2 and vb.image_sha256s[0] == hashlib.sha256(png(200)).hexdigest()
    (d,) = dets  # only the successful tile contributes
    assert d.verdict_id == vb.id and [s.tile_id for s in d.supporting] == [b.id]
    assert stub.calls[0]["max_tokens"] == D.MAX_OUTPUT_TOKENS and stub.calls[0]["output_format"] is D.AIPointList

    # an unexpected exception on one tile is recorded on that tile and the others still run
    def boom(kw):
        if tile_size(kw)[0] > 600:
            raise RuntimeError("boom")
        return response(D.AIPointList(points=[]))

    dets, verdicts = claude(StubClient(default=boom)).discover("run-001", DOC, SHEET, img, mapping,
                                                               (0.0, 0.0, 300.0, 200.0), png(200), None)
    assert dets == [] and verdicts[0].error["code"] == "error" and verdicts[1].error is None


def test_budget_exhaustion_marks_tiles_skipped_budget():
    region = (0.0, 0.0, 376.0, 376.0)
    img, mapping = synthetic(region)
    ts = D.tiles(region, SHEET)
    assert len(ts) == 4
    stub = StubClient(default=lambda kw: response(D.AIPointList(points=[point(100.0, 100.0)])))
    budget = AC.RequestBudget(1)
    dets, verdicts = claude(stub).discover("run-001", DOC, SHEET, img, mapping, region, png(200), budget)
    assert len(stub.calls) == 1 and budget.used == 1 and budget.skipped == 3
    assert [v.tile_id for v in verdicts] == [t.id for t in ts]
    assert verdicts[0].error is None
    assert [v.error for v in verdicts[1:]] == [{"code": "skipped_budget"}] * 3
    assert len(dets) == 1 and dets[0].supporting[0].tile_id == ts[0].id
    s = AC.summarize(verdicts)
    assert s["requests"] == 1 and s["errors_by_code"] == {"skipped_budget": 3}


def test_sheet_without_legend_sends_no_request():
    """legend_composite({}) is a blank image: a sheet without legend icons is skipped, never sent."""
    region = (0.0, 0.0, 376.0, 376.0)
    img, mapping = synthetic(region)
    stub = StubClient(default=lambda kw: response(D.AIPointList(points=[point(100.0, 100.0)])))
    budget = AC.RequestBudget(10)
    dets, verdicts = claude(stub).discover("run-001", DOC, SHEET, img, mapping, region, None, budget)
    assert dets == [] and stub.calls == [] and budget.used == 0
    assert [v.error for v in verdicts] == [{"code": D.NO_LEGEND}] * 4 and D.NO_LEGEND == "skipped_no_legend"
    assert AC.summarize(verdicts)["requests"] == 0
    assert D.composite_or_none({}) is None
    assert claude(stub).discover("run-001", DOC, SHEET, img, mapping, region, b"", budget)[0] == []


def test_fake_discoverer_marks_implementation_fake():
    img, mapping, a, b = two_tiles()
    script = {a.id: [point(100.0, 100.0)], b.id: {"_error": "timeout"}}
    fake = D.FakeDiscoverer(script)
    assert fake.implementation == "fake" and fake.model == D.FAKE_MODEL and fake.available() == (True, None)
    assert D.FakeDiscoverer({}, available=(False, "off")).available() == (False, "off")
    run = lambda: fake.discover("run-001", DOC, SHEET, img, mapping, (0.0, 0.0, 376.0, 200.0), png(9), None)  # noqa: E731
    (dets, verdicts), (dets2, verdicts2) = run(), run()
    assert all(v.implementation == "fake" and v.model == D.FAKE_MODEL for v in verdicts)
    assert verdicts[1].error == {"code": "timeout"} and len(dets) == 1
    strip = lambda vs: [v.model_dump(exclude={"id", "created_at"}) for v in vs]  # noqa: E731
    assert strip(verdicts) == strip(verdicts2)
    assert [d.model_dump(exclude={"verdict_id", "supporting"}) for d in dets] == \
        [d.model_dump(exclude={"verdict_id", "supporting"}) for d in dets2]
    assert AC.summarize(verdicts)["implementation"] == "fake"
    assert isinstance(fake, D.Discoverer) and isinstance(claude(StubClient()), D.Discoverer)
    # an unscripted tile reports nothing
    dets, verdicts = D.FakeDiscoverer({}).discover("run-001", DOC, SHEET, img, mapping, (0.0, 0.0, 376.0, 200.0),
                                                   png(9), None)
    assert dets == [] and [v.output["points"] for v in verdicts] == [[], []]


# --- the evaluation script -----------------------------------------------------------------------------------

def _eval_module():
    path = ROOT / "backend" / "tools" / "eval_ai_discovery.py"
    spec = importlib.util.spec_from_file_location("eval_ai_discovery", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_ai_discovery"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not EXPORT.exists(), reason="gt_001 export missing")
def test_eval_discovery_refuses_fake_and_unverified_export(tmp_path, capsys):
    ev = _eval_module()
    out = tmp_path / "out"
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], discoverer=D.FakeDiscoverer({})) == 3
    assert "fake" in capsys.readouterr().err and not out.exists()
    import anthropic, httpx2
    gone = anthropic.NotFoundError("no", response=httpx2.Response(404, request=httpx2.Request("GET", "https://x")), body=None)
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], discoverer=claude(StubClient(model_error=gone))) == 3
    assert "not found" in capsys.readouterr().err and not out.exists()
    bad = tmp_path / "gt_bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"level": "incomplete", "files": {}}), encoding="utf-8")
    stub = StubClient()
    assert ev.main(["--export", str(bad), "--out", str(out)], discoverer=claude(stub)) == 2
    assert "verified" in capsys.readouterr().err and not out.exists() and stub.retrieved == []
    # an existing output directory is refused before any model check
    out.mkdir()
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], discoverer=claude(stub)) == 2
    assert "already exists" in capsys.readouterr().err and stub.retrieved == []
    assert ev.out_dir(EXPORT, None).name == f"{EXPORT.name}__ai-discover1"


def test_eval_discovery_scoring_on_synthetic_items():
    ev = _eval_module()
    gt = [{"id": "g1", "sheet_id": "A1-111", "category": "light", "subtype": "pendant", "point_pt": [100.0, 100.0]},
          {"id": "g2", "sheet_id": "A1-111", "category": "switch", "subtype": "standard", "point_pt": [200.0, 100.0]},
          {"id": "g3", "sheet_id": "A3-111", "category": "light", "subtype": "pendant", "point_pt": [100.0, 100.0]},
          {"id": "g4", "sheet_id": "A3-111", "category": "fan", "subtype": "exhaust_fan", "point_pt": [300.0, 100.0]}]

    def ai(id, sheet, cat, x, y):
        return TM.RunDetection(id=id, run_id="r", document_id=DOC, sheet_id=sheet, source="ai_discover", category=cat,
                               point={"u": 0.1, "v": 0.1}, point_pt=(x, y))

    dets = [ai("k1", "A1-111", "light", 103.0, 104.0),   # TP at 5 pt
            ai("k2", "A1-111", "light", 400.0, 400.0),   # false candidate
            ai("k3", "A3-111", "light", 100.0, 101.0),   # TP at 1 pt
            ai("k4", "A3-111", "light", 300.0, 100.0)]   # wrong category at g4: FP, g4 missed
    v = TM.ModelVerdict(run_id="r", sheet_id="A1-111", kind="discover", implementation="claude", model="m",
                        prompt_version="p", ai_version="a", tile_id="A1-111:t0000", input_tokens=50, latency_ms=10.0)
    m = ev.score(dets, gt, [v], {"dev": ["A1-111"], "held_out": ["A3-111"]}, det0_fn_ids={"g2", "g3"})
    dev, held = m["dev"], m["held_out"]
    assert (dev["tp"], dev["fp"], dev["fn"]) == (1, 1, 1) and dev["recall"] == 0.5 and dev["precision"] == 0.5
    assert dev["localization_pt"]["median"] == 5.0 and dev["false_candidates_per_sheet"] == 1.0
    assert dev["det0_misses"] == {"n": 1, "found": 0, "share": 0.0, "ids_found": []}
    assert held["det0_misses"] == {"n": 1, "found": 1, "share": 1.0, "ids_found": ["g3"]}
    assert (held["tp"], held["fp"], held["fn"]) == (1, 1, 1) and held["localization_pt"]["p90"] == 1.0
    assert dev["usage"]["tokens"]["input"] == 50 and held["usage"]["requests"] == 0
    assert m["total"]["tp"] == 2 and m["total"]["false_candidates_per_sheet"] == 1.0


@pytest.mark.skipif(not (EXPORT.exists() and PDF.exists()), reason="gt_001 export or supplied PDF missing")
def test_eval_discovery_success_path_offline(tmp_path):
    """main -> run -> score -> write on the real A1 sheet with a stub Claude client that answers each tile with
    the ground-truth devices inside it (it recognises the tile by its image bytes)."""
    import pymupdf

    from takeoff.app import sheets as TS
    from takeoff.groundtruth import sheets as S
    from takeoff.pdf import coords as C

    ev = _eval_module()
    cache = tmp_path / "cache"
    sha = hashlib.sha256(PDF.read_bytes()).hexdigest()
    info = next(s for s in TS.intake(PDF, sha) if s.sheet_id == SHEET)
    view = TS.sheet_view(PDF, info, sha, cache)
    doc = pymupdf.open(PDF)
    try:
        path, mapping = S.render_region_image(doc[info.page_number - 1], view["region"]["bbox_pt"], sha, cache)
    finally:
        doc.close()
    region = view["region"]["bbox_pt"]
    rows = [json.loads(line) for line in (EXPORT / "annotations.jsonl").read_text(encoding="utf-8").splitlines() if line]
    gt = [r for r in rows if r["sheet_id"] == SHEET and not r.get("deleted")]
    by_sha = {}
    for t in D.tiles(region, SHEET):
        inside = [r for r in gt if t.bbox_pt[0] <= r["point_pt"][0] <= t.bbox_pt[2]
                  and t.bbox_pt[1] <= r["point_pt"][1] <= t.bbox_pt[3]]
        pts = [point(*D.pt_to_tile_px(mapping, t, tuple(r["point_pt"])), category=r["category"], subtype=r["subtype"])
               for r in inside]
        by_sha[hashlib.sha256(D.crop_tile(path, mapping, t)).hexdigest()] = pts

    def answer(kw):
        images = [b for b in kw["messages"][0]["content"] if b["type"] == "image"]
        data = base64.standard_b64decode(images[1]["source"]["data"])
        return response(D.AIPointList(points=by_sha[hashlib.sha256(data).hexdigest()]), u=usage(2000, 100, 0, 0))

    stub = StubClient(default=answer)
    out = tmp_path / "out"
    # det/0 missed nothing on A1 at 6 pt, so two A1 devices stand in as its misses here
    missed = sorted(r["id"] for r in gt)[:2]
    fns = tmp_path / "matches.jsonl"
    lines = [{"generator": "det0", "outcome": "fn", "tolerance_pt": 6.0, "gt_id": g} for g in missed]
    lines.append({"generator": "det0", "outcome": "fn", "tolerance_pt": 3.0, "gt_id": "elsewhere"})  # not 6 pt
    fns.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    code = ev.main(["--export", str(EXPORT), "--sheets", SHEET, "--out", str(out), "--cache", str(cache),
                    "--det0-matches", str(fns)], discoverer=claude(stub))
    assert code == 0 and len(stub.calls) == 20
    assert sorted(p.name for p in out.iterdir()) == ["candidates.jsonl", "metrics.json", "report.md", "verdicts.jsonl"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# ai-discover/1 evaluation") and ev.HEADING in report
    assert "measured separately; candidates need human approval; not used by totals" in report
    m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    dev = m["results"]["dev"]
    assert m["implementation"] == "claude" and m["sheets"] == [SHEET] and m["tiles"] == 20
    in_region = [r for r in gt if region[0] <= r["point_pt"][0] <= region[2] and region[1] <= r["point_pt"][1] <= region[3]]
    assert dev["tp"] == len(in_region) and dev["fp"] == 0 and dev["recall"] == round(len(in_region) / len(gt), 4)
    assert dev["precision"] == 1.0 and dev["localization_pt"]["p90"] <= 0.5
    assert dev["usage"]["requests"] == 20 and dev["usage"]["tokens"]["input"] == 40000
    assert dev["det0_misses"] == {"n": 2, "found": 2, "share": 1.0, "ids_found": missed}
    assert m["det0_misses_source"]["available"] is True and m["det0_misses_source"]["n"] == 2
    cands = [json.loads(line) for line in (out / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(cands) == dev["tp"] and all(c["source"] == "ai_discover" for c in cands)
    # the mapping-based normalized point equals the page-geometry conversion
    g = C.PageGeometry(*view["geometry"]["crop_native"], rotation=view["geometry"]["rotation"])
    for c in cands[:10]:
        assert (c["point"]["u"], c["point"]["v"]) == pytest.approx(C.unrotated_to_normalized(g, tuple(c["point_pt"])),
                                                                   abs=1e-9)
