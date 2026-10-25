"""``ai-verify/1``: a non-authoritative second opinion on det/0's classifications (spec §5.9.1).

Detections of one sheet are sent in reading order, up to ``VERIFY_BATCH_SIZE`` per request and up to
``MAX_CONCURRENCY`` requests at once. Each item is a 48 pt crop centred on the detection, its nearby text and
det/0's category and subtype; each request also carries the sheet's own legend icons for the categories in play.
Each request gives one ``ModelVerdict``: ``output = {"verdicts": [AIVerdict, ...]}`` exactly as the model returned
them, and ``agreement = {detection_id: {"category": bool, "subtype": bool | None}}`` for every item answered
exactly once. An item missing from the answer or answered twice gets no agreement entry, and the verdict's
``error`` is ``{"code": "parse_error", "detection_ids": [...]}`` for those ids. Nothing here changes a device or a
total; the merge decides what a disagreement means.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Protocol, runtime_checkable

import cv2
import numpy as np
from pydantic import BaseModel, BeforeValidator, Field

from takeoff.ai import common as AC
from takeoff.ai import prompt as P
from takeoff.app.models import ModelVerdict, RunDetection
from takeoff.groundtruth import taxonomy as T

AI_VERSION = "ai-verify/1"
PROMPT_VERSION = P.VERIFY_PROMPT_VERSION
VERIFY_BATCH_SIZE = 8
MAX_ICONS = 8
CROP_PT = 48.0
TOKEN_RADIUS_PT = 15.0
MAX_OUTPUT_TOKENS = 2048  # ruling R16
FAKE_MODEL = "fake-verifier"

CATEGORIES = tuple(T.CATEGORIES)
AICategory = Literal[CATEGORIES + (P.NOT_A_DEVICE,)]  # type: ignore[valid-type]


def _bounded(v: Any) -> Any:
    return v[:P.RATIONALE_MAX_CHARS] if isinstance(v, str) else v


class AIVerdict(BaseModel):
    item_id: str
    is_device: bool
    category: AICategory
    subtype: str | None
    # A longer rationale is cut to the limit rather than failing the whole batch's validation.
    rationale: Annotated[str, BeforeValidator(_bounded), Field(max_length=P.RATIONALE_MAX_CHARS)]
    uncertain: bool


class AIVerdictBatch(BaseModel):
    verdicts: list[AIVerdict]


# --- inputs --------------------------------------------------------------------------------------------------

def reading_order(detections: list[RunDetection]) -> list[RunDetection]:
    """Top to bottom, then left to right (PDF points), ties by id."""
    return sorted(detections, key=lambda d: (d.point_pt[1], d.point_pt[0], d.id))


def batches(detections: list[RunDetection], size: int = VERIFY_BATCH_SIZE) -> list[list[RunDetection]]:
    ordered = reading_order(detections)
    return [ordered[i:i + size] for i in range(0, len(ordered), max(1, size))]


def load_gray(src) -> np.ndarray:
    """A grayscale image from an array, PNG bytes or a PNG path."""
    if isinstance(src, np.ndarray):
        return src if src.ndim == 2 else cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)
    data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("region image could not be decoded")
    return img


def build_crop(region_png, mapping: Mapping, point_pt: tuple[float, float], size_pt: float = CROP_PT) -> bytes:
    """A ``size_pt`` square of the region image centred on ``point_pt``, white beyond the image edges, as PNG.

    ``mapping`` is the region image mapping of ``groundtruth.sheets.render_region_image`` (``dpi``, ``covers_pt``).
    ``region_png`` may be a decoded array (decode once per sheet), PNG bytes or a path. Deterministic.
    """
    img = load_gray(region_png)
    scale = mapping["dpi"] / 72.0
    side = int(round(size_pt * scale))
    x0 = int(math.floor((point_pt[0] - mapping["covers_pt"][0]) * scale - side / 2 + 0.5))
    y0 = int(math.floor((point_pt[1] - mapping["covers_pt"][1]) * scale - side / 2 + 0.5))
    canvas = np.full((side, side), 255, np.uint8)
    h, w = img.shape
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(w, x0 + side), min(h, y0 + side)
    if sx1 > sx0 and sy1 > sy0:
        canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = img[sy0:sy1, sx0:sx1]
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def tokens_near(detections: list[RunDetection], sheet_tokens: list[dict],
                radius_pt: float = TOKEN_RADIUS_PT) -> dict[str, list[str]]:
    """Detection id -> texts of the sheet tokens (``sheet_view(...)["tokens"]``) whose centre lies within
    ``radius_pt``, nearest first."""
    centres = [(((t["bbox_pt"][0] + t["bbox_pt"][2]) / 2, (t["bbox_pt"][1] + t["bbox_pt"][3]) / 2), t["text"])
               for t in sheet_tokens]
    out = {}
    for d in detections:
        near = sorted((math.hypot(x - d.point_pt[0], y - d.point_pt[1]), text) for (x, y), text in centres
                      if math.hypot(x - d.point_pt[0], y - d.point_pt[1]) <= radius_pt)
        out[d.id] = [text for _, text in near]
    return out


def relevant_icons(batch: list[RunDetection], icons: Mapping[str, dict]) -> list[dict]:
    """The sheet's legend icons for the batch: det/0's categories and its member proposals' categories.

    Deduplicated, at most ``MAX_ICONS``, ordered by category then subtype. When more qualify, the icons of det/0's
    own claims (category and subtype) are kept first, then the other categories its member templates proposed,
    then the remaining subtypes of det/0's categories.
    """
    claimed = {(d.category, d.subtype) for d in batch}
    det_cats = {d.category for d in batch}
    member_cats = {m.get("category") for d in batch for m in d.members if m.get("category")}

    def tier(i: dict) -> int | None:
        if (i["category"], i["subtype"]) in claimed:
            return 0
        if i["category"] in member_cats - det_cats:
            return 1
        if i["category"] in det_cats:
            return 2
        return None

    pool = []
    for f, i in icons.items():
        t = tier(i)
        if t is not None:
            pool.append((t, i["category"], i["subtype"] or "", f, i))
    chosen = sorted(pool)[:MAX_ICONS]
    chosen.sort(key=lambda x: (x[1], x[2], x[3]))
    return [{**i, "catalog_file": f} for _, _, _, f, i in chosen]


def item_text(d: RunDetection, tokens: list[str]) -> str:
    return f"Item {d.id}: detector says {d.category}/{d.subtype}; nearby text: {', '.join(tokens) if tokens else 'none'}"


def build_verify_request(batch: list[RunDetection], crops: Mapping[str, bytes], icons: Mapping[str, dict],
                         tokens_by_id: Mapping[str, list[str]]) -> tuple[str, list[dict]]:
    """The system prompt and the user content for one batch. Deterministic: no timestamps, no random ids."""
    batch = reading_order(batch)
    rel = relevant_icons(batch, icons)
    content: list[dict] = [{"type": "text", "text": (
        "Reference legend: icons from this sheet's own legend, each with its legend wording and its category/subtype."
        if rel else "Reference legend: none available for these items.")}]
    for i in rel:
        content.append({"type": "text", "text": f"Legend: {i['label']} = {i['category']}/{i['subtype']}"})
        content.append(AC.image_block(i["png"]))
    content.append({"type": "text", "text": f"Items to check ({len(batch)}), each followed by its crop:"})
    for d in batch:
        content.append({"type": "text", "text": item_text(d, list(tokens_by_id.get(d.id, [])))})
        content.append(AC.image_block(crops[d.id]))
    content.append({"type": "text", "text": "Return one verdict for each item id above, each id exactly once."})
    return P.VERIFY_SYSTEM, content


def _image_shas(batch: list[RunDetection], crops: Mapping[str, bytes], icons: Mapping[str, dict]) -> list[str]:
    """SHA-256 of every image the batch's request carries, in request order (empty if the inputs are broken;
    the request then fails and is recorded as an error verdict)."""
    try:
        pngs = [i["png"] for i in relevant_icons(batch, icons)] + [crops[d.id] for d in reading_order(batch)]
        return [hashlib.sha256(p).hexdigest() for p in pngs]
    except Exception:
        return []


# --- verdicts ------------------------------------------------------------------------------------------------

def agreement_of(det: RunDetection, row: AIVerdict) -> dict:
    """det/0 vs the model for one item: category, and subtype when the model gave one."""
    category = bool(row.is_device) and row.category == det.category
    return {"category": category, "subtype": None if row.subtype is None else row.subtype == det.subtype}


def verdict_from(base: dict, batch: list[RunDetection], r: AC.CallResult) -> ModelVerdict:
    usage = dict(stop_reason=r.stop_reason, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                 cache_creation_input_tokens=r.cache_creation_input_tokens,
                 cache_read_input_tokens=r.cache_read_input_tokens, latency_ms=r.latency_ms)
    if r.error is not None:
        return ModelVerdict(**base, **usage, error=r.error)
    rows: list[AIVerdict] = list(r.parsed.verdicts)
    seen: dict[str, list[AIVerdict]] = {}
    for row in rows:
        seen.setdefault(row.item_id, []).append(row)
    agreement, missing, duplicated = {}, [], []
    for d in batch:
        got = seen.get(d.id, [])
        if len(got) == 1:
            agreement[d.id] = agreement_of(d, got[0])
        elif not got:
            missing.append(d.id)
        else:
            duplicated.append(d.id)
    error = None
    if missing or duplicated:
        bad = [d.id for d in batch if d.id in set(missing) | set(duplicated)]
        error = {"code": "parse_error", "detection_ids": bad, "missing": missing, "duplicated": duplicated}
    return ModelVerdict(**base, **usage, output={"verdicts": [row.model_dump() for row in rows]},
                        agreement=agreement, error=error)


@runtime_checkable
class Verifier(Protocol):
    implementation: str
    model: str | None

    def available(self) -> tuple[bool, str | None]: ...

    def verify(self, run_id: str, sheet_id: str, detections: list[RunDetection], crops: Mapping[str, bytes],
               icons: Mapping[str, dict], tokens_by_id: Mapping[str, list[str]],
               budget: AC.RequestBudget | None) -> list[ModelVerdict]: ...


class _BatchVerifier:
    implementation: str
    model: str | None
    batch_size: int = VERIFY_BATCH_SIZE

    def _call(self, system: str, content: list[dict], batch: list[RunDetection]) -> AC.CallResult:
        raise NotImplementedError

    def verify(self, run_id, sheet_id, detections, crops, icons, tokens_by_id, budget=None) -> list[ModelVerdict]:
        """One ``ModelVerdict`` per batch, in reading order. Budget is taken in reading order before any request is
        sent, so which batches are skipped is deterministic; a skipped batch is recorded, never dropped."""
        work = []
        for batch in batches(detections, self.batch_size):
            base = dict(run_id=run_id, sheet_id=sheet_id, kind="verify", implementation=self.implementation,
                        model=self.model or "unknown", prompt_version=PROMPT_VERSION, ai_version=AI_VERSION,
                        image_sha256s=_image_shas(batch, crops, icons), detection_ids=[d.id for d in batch])
            work.append((batch, base, budget is None or budget.take()))

        def one(job):
            batch, base, allowed = job
            if not allowed:
                return ModelVerdict(**base, error={"code": "skipped_budget"})
            try:
                system, content = build_verify_request(batch, crops, icons, tokens_by_id)
                return verdict_from(base, batch, self._call(system, content, batch))
            except Exception as e:  # one batch's failure never aborts the sheet: it is recorded on that batch
                return ModelVerdict(**base, error=AC.unexpected_error(e))

        return AC.map_concurrent(one, work)


class ClaudeVerifier(_BatchVerifier):
    implementation = "claude"

    def __init__(self, ai: AC.AIClient, batch_size: int = VERIFY_BATCH_SIZE):
        self.ai = ai
        self.model = ai.model
        self.batch_size = batch_size

    def available(self) -> tuple[bool, str | None]:
        return self.ai.available()

    def _call(self, system, content, batch):
        return self.ai.call(system, content, AIVerdictBatch, MAX_OUTPUT_TOKENS)


class FakeVerifier(_BatchVerifier):
    """A deterministic test double. Never a model: every verdict says ``implementation="fake"``.

    ``script`` maps a detection id to fields overriding the default answer (agree with det/0). The special keys
    ``"_error": code`` (the whole batch fails with that code) and ``"_omit": True`` (the item is left out of the
    answer) simulate failures.
    """

    implementation = "fake"

    def __init__(self, script: Mapping[str, dict] | None = None, available: tuple[bool, str | None] = (True, None),
                 batch_size: int = VERIFY_BATCH_SIZE):
        self.script = dict(script or {})
        self._available = available
        self.model = FAKE_MODEL
        self.batch_size = batch_size

    def available(self) -> tuple[bool, str | None]:
        return self._available

    def _call(self, system, content, batch):
        rows = []
        for d in batch:
            s = self.script.get(d.id, {})
            if "_error" in s:
                return AC.CallResult(latency_ms=0.0, error={"code": s["_error"]})
            if s.get("_omit"):
                continue
            row = {"item_id": d.id, "is_device": True, "category": d.category, "subtype": d.subtype,
                   "rationale": "fake verifier: scripted answer", "uncertain": False}
            row.update({k: v for k, v in s.items() if not k.startswith("_")})
            if row["category"] == P.NOT_A_DEVICE and "is_device" not in s:
                row["is_device"] = False
            rows.append(AIVerdict(**row))
        return AC.CallResult(parsed=AIVerdictBatch(verdicts=rows), stop_reason="end_turn", latency_ms=0.0)
