"""``ai-discover/1``: non-authoritative AI proposals of device locations det/0 may have missed (spec §5.9.2).

The sheet's electrical region is cut into ``TILE_PT`` square tiles overlapping by ``OVERLAP_PT`` (row-major; the
last row and column are clipped to the region, so every adjacent pair overlaps by exactly ``OVERLAP_PT``). Each
tile is cropped from the 300 DPI region image and sent with the sheet's composite legend image; the model answers
with points in tile pixels. Each tile gives one ``ModelVerdict(kind="discover")`` whose ``output`` is
``{"points": [...as returned...], "dropped": [{"index", "reason"}]}``.

Revision 3.1 rules:
- a point is dropped only when it is outside the electrical region or outside its own tile (a malformed
  coordinate); a point in a tile's overlap strip is kept, so a symbol only a neighbour's overlap view found
  survives;
- observations of the **same category** within ``tol_pt`` of each other, from any tiles, are one cluster (every
  such pair linked, connected components). The representative is the most central observation (largest distance
  to its own tile's nearest edge), ties to the smallest ``(tile_id, y_pt, x_pt)``; every member is kept as
  ``supporting`` evidence. Different categories are never merged: each stays its own detection, and the takeoff
  merge turns a different-category neighbour into a ``model_disagrees`` flag.

Failed or skipped tiles give no observations. A sheet without legend icons sends no request: every tile is
recorded with ``error={"code": NO_LEGEND}`` (``legend_composite({})`` would be a blank image, not a reference).
Nothing here changes a device or a total: candidates become proposals that a reviewer must accept one by one.

Region images are unrotated (``render_region_image`` refuses rotated pages), so normalized points are the linear
map of the mapping's ``covers_pt`` onto its ``covers``, which equals ``coords.unrotated_to_normalized`` there.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Mapping, Protocol, runtime_checkable

import cv2
import numpy as np
from pydantic import BaseModel, BeforeValidator, Field

from takeoff.ai import common as AC
from takeoff.ai import prompt as P
from takeoff.ai.verifier import load_gray
from takeoff.app.models import ModelVerdict, RunDetection, SupportingObservation
from takeoff.groundtruth import taxonomy as T
from takeoff.groundtruth.models import new_id

AI_VERSION = "ai-discover/1"
PROMPT_VERSION = P.DISCOVER_PROMPT_VERSION
TILE_PT = 200.0
OVERLAP_PT = 24.0
MAX_ITEMS = P.DISCOVER_MAX_ITEMS  # 60
MERGE_TOL_PT = 6.0
MAX_OUTPUT_TOKENS = 4096  # 60 points with 150-character rationales fit; a cut-off answer is a parse_error
MAX_IMAGE_PX = 2576  # current models' long-edge limit at which coordinates map 1:1 to image pixels
NO_LEGEND = "skipped_no_legend"
FAKE_MODEL = "fake-discoverer"
_EPS = 1e-6

AICategory = Literal[tuple(T.CATEGORIES)]  # type: ignore[valid-type]


def _bounded(v: Any) -> Any:
    return v[:P.DISCOVER_RATIONALE_MAX_CHARS] if isinstance(v, str) else v


class AIPoint(BaseModel):
    x_px: float
    y_px: float
    category: AICategory
    subtype: str | None
    uncertain: bool
    # A longer rationale is cut to the limit rather than failing the whole tile's validation.
    rationale: Annotated[str, BeforeValidator(_bounded), Field(max_length=P.DISCOVER_RATIONALE_MAX_CHARS)]


class AIPointList(BaseModel):
    points: list[AIPoint] = Field(max_length=MAX_ITEMS)


@dataclass(frozen=True)
class Tile:
    id: str
    sheet_id: str
    bbox_pt: tuple


@dataclass(frozen=True)
class TileObservation:
    tile_id: str
    verdict_id: str
    category: str
    subtype: str | None
    point_pt: tuple[float, float]
    centrality_pt: float
    uncertain: bool
    rationale: str


# --- tiles ---------------------------------------------------------------------------------------------------

def _starts(lo: float, hi: float) -> list[float]:
    length = hi - lo
    if length <= TILE_PT:
        return [lo]
    step = TILE_PT - OVERLAP_PT
    n = math.ceil((length - TILE_PT) / step - 1e-9) + 1
    return [lo + k * step for k in range(n)]


def tiles(region_bbox_pt, sheet_id: str) -> list[Tile]:
    """Row-major tiles ``"{sheet}:t{row:02d}{col:02d}"`` covering the region; the last row and column are clipped."""
    x0, y0, x1, y1 = (float(v) for v in region_bbox_pt)
    out = []
    for r, ty in enumerate(_starts(y0, y1)):
        for c, tx in enumerate(_starts(x0, x1)):
            out.append(Tile(f"{sheet_id}:t{r:02d}{c:02d}", sheet_id,
                            (tx, ty, min(tx + TILE_PT, x1), min(ty + TILE_PT, y1))))
    return out


def centrality_pt(tile: Tile, point_pt) -> float:
    """Distance from the point to the tile's nearest edge (negative outside the tile)."""
    x0, y0, x1, y1 = tile.bbox_pt
    x, y = point_pt
    return min(x - x0, x1 - x, y - y0, y1 - y)


def _scale(mapping: Mapping) -> float:
    return mapping["dpi"] / 72.0


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def tile_origin_px(mapping: Mapping, tile: Tile) -> tuple[int, int]:
    """The tile's top-left corner in region-image pixels."""
    s, (cx, cy) = _scale(mapping), mapping["covers_pt"][:2]
    return _round((tile.bbox_pt[0] - cx) * s), _round((tile.bbox_pt[1] - cy) * s)


def tile_size_px(mapping: Mapping, tile: Tile) -> tuple[int, int]:
    s, (cx, cy) = _scale(mapping), mapping["covers_pt"][:2]
    ox, oy = tile_origin_px(mapping, tile)
    return _round((tile.bbox_pt[2] - cx) * s) - ox, _round((tile.bbox_pt[3] - cy) * s) - oy


def tile_px_to_pt(mapping: Mapping, tile: Tile, px) -> tuple[float, float]:
    s, (cx, cy) = _scale(mapping), mapping["covers_pt"][:2]
    ox, oy = tile_origin_px(mapping, tile)
    return cx + (ox + px[0]) / s, cy + (oy + px[1]) / s


def pt_to_tile_px(mapping: Mapping, tile: Tile, pt) -> tuple[float, float]:
    s, (cx, cy) = _scale(mapping), mapping["covers_pt"][:2]
    ox, oy = tile_origin_px(mapping, tile)
    return (pt[0] - cx) * s - ox, (pt[1] - cy) * s - oy


def crop_tile(region_png, mapping: Mapping, tile: Tile) -> bytes:
    """The tile's pixels of the region image (white beyond its edges) as PNG. Deterministic.

    ``region_png`` may be a decoded array (decode once per sheet), PNG bytes or a path.
    """
    img = load_gray(region_png)
    ox, oy = tile_origin_px(mapping, tile)
    w, h = tile_size_px(mapping, tile)
    if w <= 0 or h <= 0 or max(w, h) > MAX_IMAGE_PX:
        raise ValueError(f"tile {tile.id} would be {w} x {h} px")
    canvas = np.full((h, w), 255, np.uint8)
    ih, iw = img.shape
    sx0, sy0, sx1, sy1 = max(0, ox), max(0, oy), min(iw, ox + w), min(ih, oy + h)
    if sx1 > sx0 and sy1 > sy0:
        canvas[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox] = img[sy0:sy1, sx0:sx1]
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def composite_or_none(icons: Mapping[str, dict]) -> bytes | None:
    """The sheet's composite legend image, or ``None`` when it has no icons (discovery then sends nothing)."""
    if not icons:
        return None
    from takeoff.app.sheets import legend_composite

    return legend_composite(dict(icons))


# --- the request ---------------------------------------------------------------------------------------------

def _png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG image")
    return struct.unpack(">II", data[16:24])


def build_discover_request(tile_png: bytes, composite_png: bytes, taxonomy_text: str) -> tuple[str, list[dict]]:
    """The system prompt and the user content: the composite legend, then the tile, then the instruction text.

    Byte-stable: no timestamps, no random ids, nothing sheet- or run-specific beyond the images and the tile size.
    """
    w, h = _png_size(tile_png)
    content = [
        {"type": "text", "text": "Reference legend (first image): this sheet's own legend icons, each with its legend wording."},
        AC.image_block(composite_png),
        {"type": "text", "text": "Plan tile (second image):"},
        AC.image_block(tile_png),
        {"type": "text", "text": "\n".join([
            f"The plan tile is {w} x {h} pixels. Mark every electrical device symbol in it, each once, at the centre "
            f"of the symbol, in tile pixel coordinates (0 <= x_px <= {w}, 0 <= y_px <= {h}).",
            "",
            taxonomy_text,
            "",
            f"Return at most {MAX_ITEMS} points, or an empty list when the tile shows no device symbol.",
        ])},
    ]
    return P.DISCOVER_SYSTEM, content


# --- points -> observations -> candidates --------------------------------------------------------------------

def _inside(box, p) -> bool:
    return box[0] - _EPS <= p[0] <= box[2] + _EPS and box[1] - _EPS <= p[1] <= box[3] + _EPS


def classify_points(tile: Tile, mapping: Mapping, region_bbox_pt, points) -> tuple[list, list[dict]]:
    """``([(index, point_pt), ...] kept, [{"index", "reason"}, ...] dropped)``; reasons ``outside_tile`` (a
    malformed coordinate, checked first) and ``outside_region``. Overlap-strip points are kept."""
    kept, dropped = [], []
    for i, p in enumerate(points):
        pt = tile_px_to_pt(mapping, tile, (p.x_px, p.y_px))
        if not (math.isfinite(pt[0]) and math.isfinite(pt[1])) or not _inside(tile.bbox_pt, pt):
            dropped.append({"index": i, "reason": "outside_tile"})
        elif not _inside(tuple(region_bbox_pt), pt):
            dropped.append({"index": i, "reason": "outside_region"})
        else:
            kept.append((i, pt))
    return kept, dropped


def _subtype(category: str, subtype: str | None) -> str | None:
    """The model's subtype when the taxonomy has it for that category, else ``None`` (the verdict keeps the raw
    answer)."""
    return subtype if subtype in {s.name for s in T.CATEGORIES.get(category, ())} else None


def points_to_observations(tile: Tile, mapping: Mapping, region_bbox_pt, points, verdict_id: str) -> list[TileObservation]:
    points = list(points)
    return _observations(tile, points, classify_points(tile, mapping, region_bbox_pt, points)[0], verdict_id)


def _observations(tile: Tile, points: list, kept: list, verdict_id: str) -> list[TileObservation]:
    return [TileObservation(tile_id=tile.id, verdict_id=verdict_id, category=points[i].category,
                            subtype=_subtype(points[i].category, points[i].subtype), point_pt=pt,
                            centrality_pt=centrality_pt(tile, pt), uncertain=points[i].uncertain,
                            rationale=points[i].rationale) for i, pt in kept]


def to_normalized(mapping: Mapping, pt) -> dict:
    """PDF points (unrotated) -> normalized page coordinates through the region image mapping."""
    x0, y0, x1, y1 = mapping["covers_pt"]
    c = mapping["covers"]
    u = c["u0"] + (pt[0] - x0) / (x1 - x0) * (c["u1"] - c["u0"])
    v = c["v0"] + (pt[1] - y0) / (y1 - y0) * (c["v1"] - c["v0"])
    return {"u": min(max(u, 0.0), 1.0), "v": min(max(v, 0.0), 1.0)}


def _member_key(o: TileObservation) -> tuple:
    return (o.tile_id, o.point_pt[1], o.point_pt[0], o.verdict_id, o.subtype or "", o.rationale)


def _clusters(observations: list[TileObservation], tol_pt: float) -> list[list[TileObservation]]:
    """Connected components of same-category observations, linking every pair within ``tol_pt`` (inclusive)."""
    out = []
    by_cat: dict[str, list[TileObservation]] = {}
    for o in sorted(observations, key=lambda o: (o.category, *_member_key(o))):
        by_cat.setdefault(o.category, []).append(o)
    for cat in sorted(by_cat):
        group = by_cat[cat]
        parent = list(range(len(group)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i].point_pt, group[j].point_pt
                if math.hypot(a[0] - b[0], a[1] - b[1]) <= tol_pt + 1e-9:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)
        comps: dict[int, list[TileObservation]] = {}
        for i, o in enumerate(group):
            comps.setdefault(find(i), []).append(o)
        out += list(comps.values())
    return out


def merge_across_tiles(run_id: str, document_id: str, sheet_id: str, observations, tol_pt: float = MERGE_TOL_PT, *,
                       mapping: Mapping) -> list[RunDetection]:
    """One ``ai_discover`` detection per same-category cluster; deterministic for any input order.

    ``mapping`` (the region image mapping) places the representative's normalized point. Detection ids are
    ``"{run_id}:{sheet_id}:ai-discover-NNNN"``, numbered in reading order of the representatives.
    """
    reps = []
    for members in _clusters(list(observations), tol_pt):
        rep = min(members, key=lambda o: (-o.centrality_pt, *_member_key(o)))
        reps.append((rep, sorted(members, key=_member_key)))
    reps.sort(key=lambda t: (t[0].point_pt[1], t[0].point_pt[0], t[0].category, *_member_key(t[0])))
    out = []
    for k, (rep, members) in enumerate(reps, start=1):
        out.append(RunDetection(
            id=f"{run_id}:{sheet_id}:ai-discover-{k:04d}", run_id=run_id, document_id=document_id, sheet_id=sheet_id,
            source="ai_discover", category=rep.category, subtype=rep.subtype, point=to_normalized(mapping, rep.point_pt),
            point_pt=rep.point_pt, score=None, members=[], rationale=rep.rationale, verdict_id=rep.verdict_id,
            supporting=[SupportingObservation(tile_id=m.tile_id, verdict_id=m.verdict_id, point_pt=m.point_pt,
                                              centrality_pt=m.centrality_pt) for m in members]))
    return out


# --- discoverers ---------------------------------------------------------------------------------------------

@runtime_checkable
class Discoverer(Protocol):
    implementation: str
    model: str | None

    def available(self) -> tuple[bool, str | None]: ...

    def discover(self, run_id: str, document_id: str, sheet_id: str, region_png, mapping: Mapping, region_bbox_pt,
                 composite_png: bytes | None, budget: AC.RequestBudget | None
                 ) -> tuple[list[RunDetection], list[ModelVerdict]]: ...


def _usage(r: AC.CallResult) -> dict:
    return dict(stop_reason=r.stop_reason, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                cache_creation_input_tokens=r.cache_creation_input_tokens,
                cache_read_input_tokens=r.cache_read_input_tokens, latency_ms=r.latency_ms)


class _TileDiscoverer:
    implementation: str
    model: str | None

    def _call(self, system: str, content: list[dict], tile: Tile) -> AC.CallResult:
        raise NotImplementedError

    def discover(self, run_id, document_id, sheet_id, region_png, mapping, region_bbox_pt, composite_png,
                 budget=None) -> tuple[list[RunDetection], list[ModelVerdict]]:
        """One ``ModelVerdict`` per tile in row-major order, then the cross-tile merge of every observation.

        Budget is taken in tile order before any request is sent, so which tiles are skipped is deterministic; a
        skipped or failed tile is recorded, never dropped, and contributes no observation.
        """
        ts = tiles(region_bbox_pt, sheet_id)

        def base(t: Tile, shas: list[str]) -> dict:
            return dict(run_id=run_id, sheet_id=sheet_id, kind="discover", implementation=self.implementation,
                        model=self.model or "unknown", prompt_version=PROMPT_VERSION, ai_version=AI_VERSION,
                        image_sha256s=shas, tile_id=t.id)

        if not composite_png:
            return [], [ModelVerdict(**base(t, []), error={"code": NO_LEGEND}) for t in ts]
        composite_sha = hashlib.sha256(composite_png).hexdigest()
        try:
            region = load_gray(region_png)
            decode_error = None
        except Exception as e:  # recorded on every tile below
            region, decode_error = None, AC.unexpected_error(e)

        work = []
        for t in ts:
            tile_png, error = None, decode_error
            if error is None:
                try:
                    tile_png = crop_tile(region, mapping, t)
                except Exception as e:
                    error = AC.unexpected_error(e)
            shas = [composite_sha, hashlib.sha256(tile_png).hexdigest()] if tile_png is not None else []
            allowed = error is None and (budget is None or budget.take())
            work.append((t, tile_png, base(t, shas), error, allowed))

        def one(job) -> tuple[ModelVerdict, list[TileObservation]]:
            t, tile_png, b, error, allowed = job
            if error is not None:
                return ModelVerdict(**b, error=error), []
            if not allowed:
                return ModelVerdict(**b, error={"code": "skipped_budget"}), []
            try:
                system, content = build_discover_request(tile_png, composite_png, P.DISCOVER_TAXONOMY_TEXT)
                r = self._call(system, content, t)
                if r.error is not None:
                    return ModelVerdict(**b, **_usage(r), error=r.error), []
                points = list(r.parsed.points)
                vid = new_id()
                kept, dropped = classify_points(t, mapping, region_bbox_pt, points)
                found = _observations(t, points, kept, vid)
                return ModelVerdict(id=vid, **b, **_usage(r),
                                    output={"points": [p.model_dump() for p in points], "dropped": dropped}), found
            except Exception as e:  # one tile's failure never aborts the sheet: it is recorded on that tile
                return ModelVerdict(**b, error=AC.unexpected_error(e)), []

        results = AC.map_concurrent(one, work)
        verdicts = [v for v, _ in results]
        observations = [o for _, found in results for o in found]
        return merge_across_tiles(run_id, document_id, sheet_id, observations, mapping=mapping), verdicts


class ClaudeDiscoverer(_TileDiscoverer):
    implementation = "claude"

    def __init__(self, ai: AC.AIClient):
        self.ai = ai
        self.model = ai.model

    def available(self) -> tuple[bool, str | None]:
        return self.ai.available()

    def _call(self, system, content, tile):
        return self.ai.call(system, content, AIPointList, MAX_OUTPUT_TOKENS)


class FakeDiscoverer(_TileDiscoverer):
    """A deterministic test double. Never a model: every verdict says ``implementation="fake"``.

    ``points_by_tile`` maps a tile id to the ``AIPoint``s (or dicts) it answers; a tile not listed answers no points.
    ``{"_error": code}`` in place of a list makes that tile fail with that code.
    """

    implementation = "fake"

    def __init__(self, points_by_tile: Mapping[str, Any] | None = None,
                 available: tuple[bool, str | None] = (True, None)):
        self.points_by_tile = dict(points_by_tile or {})
        self._available = available
        self.model = FAKE_MODEL

    def available(self) -> tuple[bool, str | None]:
        return self._available

    def _call(self, system, content, tile):
        s = self.points_by_tile.get(tile.id, [])
        if isinstance(s, Mapping) and "_error" in s:
            return AC.CallResult(latency_ms=0.0, error={"code": s["_error"]})
        points = [p if isinstance(p, AIPoint) else AIPoint(**p) for p in s]
        return AC.CallResult(parsed=AIPointList(points=points), stop_reason="end_turn", latency_ms=0.0)
