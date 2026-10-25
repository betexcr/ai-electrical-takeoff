"""Takeoff records, ``takeoff-schema/1`` (Pydantic).

Rows are stored as validated JSON by ``takeoff.app.store.TakeoffStore``. Detector and AI
outputs (``RunDetection``, ``ModelVerdict``), match ``Evidence`` and ``BulkAction`` are
immutable; ``Device`` is the only record a reviewer changes, and every change is an event.
Official totals come only from accepted ``Device`` records.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

from takeoff.groundtruth.models import NormPoint, PtRect, RoomRef, _Model, new_id, utcnow

SCHEMA_VERSION = "takeoff-schema/1"
MERGE_VERSION = "takeoff-merge/2"  # /2: one-to-one nearest-first det/0 assignment (final review)

ReviewState = Literal["proposed", "accepted", "rejected"]
Origin = Literal["detector", "ai", "manual"]
AcceptKind = Literal["accepted", "corrected_and_accepted", "bulk_accepted", "manual"]
RoomSource = Literal["proposed_nearest_label", "reviewer"]
SheetStatus = Literal["queued", "running", "done", "failed", "skipped_no_legend"]
AIStatus = Literal["pending", "done", "partial", "unavailable", "off"]
DetectionSource = Literal["det0", "ai_discover"]
EvidenceOutcome = Literal["created", "confirmed", "conflict", "ambiguous", "rejected_redetected", "ai_corroborates"]
FlagType = Literal["classification_conflict", "ambiguous_match", "not_redetected", "model_disagrees",
                   "ai_only_candidate", "duplicate_warning", "room_ambiguous", "no_room"]
Severity = Literal["review", "info"]
VerdictKind = Literal["verify", "discover"]
HumanField = Literal["category", "subtype", "room", "point", "review_state"]

_RUN_ID = re.compile(r"^run-\d{3,}$")


class Document(_Model):
    """A PDF, identified by its SHA-256. Immutable."""

    id: str = Field(min_length=64, max_length=64)  # sha256 hex
    file_name: str = Field(min_length=1)
    page_count: int = Field(ge=1)
    source: Literal["supplied", "upload"]
    stored_path: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)


class SheetInfo(_Model):
    """One page of a document; recomputed only by a run."""

    id: str = Field(min_length=1)  # "{document_id}:{page_number}"
    document_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    sheet_id: str = Field(min_length=1)  # drawing sheet number, e.g. "A1-111"
    region_bbox_pt: PtRect | None = None
    legend_found: bool = False
    note: str | None = None


class Tokens(_Model):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_creation: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)


class Latency(_Model):
    p50: float | None = Field(default=None, ge=0)
    p95: float | None = Field(default=None, ge=0)


class AIStageStatus(_Model):
    """Per-sheet summary of one AI stage (verify or discover) within a run."""

    status: AIStatus = "off"
    reason: str | None = None  # why the stage is unavailable (the UI's "unavailable: {reason}")
    implementation: str | None = None  # "claude" | "fake"
    requests: int = Field(default=0, ge=0)
    errors_by_code: dict[str, int] = Field(default_factory=dict)
    tokens: Tokens = Field(default_factory=Tokens)
    latency_ms: Latency = Field(default_factory=Latency)
    n_items: int = Field(default=0, ge=0)


class SheetRunStatus(_Model):
    status: SheetStatus = "queued"
    message: str | None = None
    n_detections: int = Field(default=0, ge=0)
    input_runs: list[str] = Field(default_factory=list)  # the candidate run hashes det/0 used on this sheet
    started_at: str | None = None
    finished_at: str | None = None
    ai_verify: AIStageStatus = Field(default_factory=AIStageStatus)
    ai_discover: AIStageStatus = Field(default_factory=AIStageStatus)


class Run(_Model):
    """One Analyze. Immutable apart from per-sheet status and ``finished_at`` until finished."""

    id: str  # run-NNN
    document_id: str = Field(min_length=1)
    detector: str = Field(min_length=1)
    detector_version: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    merge_version: str = MERGE_VERSION
    ai_model: str | None = None
    ai_versions: dict[str, str] = Field(default_factory=dict)
    input_runs: list[str] = Field(default_factory=list)
    sheets: dict[str, SheetRunStatus] = Field(default_factory=dict)
    created_by: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None

    @field_validator("id")
    @classmethod
    def _run_id(cls, v: str) -> str:
        if not _RUN_ID.match(v):
            raise ValueError("run id must look like run-001")
        return v


class SupportingObservation(_Model):
    """One tile observation merged into an AI-discovered detection."""

    tile_id: str
    verdict_id: str
    point_pt: tuple[float, float]
    centrality_pt: float


class RunDetection(_Model):
    """One detector or AI output. Never mutated; never a count by itself."""

    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    sheet_id: str = Field(min_length=1)
    source: DetectionSource = "det0"
    category: str = Field(min_length=1)
    subtype: str | None = None
    status: str = ""
    point: NormPoint
    point_pt: tuple[float, float]
    score: float | None = None
    members: list[dict[str, Any]] = Field(default_factory=list)
    subtype_source: str | None = None  # token | template | default | fixed
    tokens: list[str] = Field(default_factory=list)
    rationale: str | None = None
    verdict_id: str | None = None
    supporting: list[SupportingObservation] = Field(default_factory=list)  # empty for det/0


class Device(_Model):
    """One physical device in the takeoff. The only count-bearing record (when accepted)."""

    id: str = Field(default_factory=new_id)
    document_id: str = Field(min_length=1)
    sheet_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    subtype: str | None = None
    status: str = ""
    point: NormPoint
    point_pt: tuple[float, float] | None = None
    room: RoomRef | None = None
    room_source: RoomSource | None = None
    review_state: ReviewState = "proposed"
    origin: Origin
    accept_kind: AcceptKind | None = None
    created_by_run: str | None = None
    human_set: list[HumanField] = Field(default_factory=list)
    deleted: bool = False
    delete_reason: str | None = None
    notes: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    revision: int = Field(default=1, ge=1)


class Evidence(_Model):
    """Links a RunDetection to the Device it created or matched. Immutable."""

    id: str = Field(default_factory=new_id)
    device_id: str = Field(min_length=1)
    detection_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    outcome: EvidenceOutcome
    distance_pt: float | None = Field(default=None, ge=0)


class Flag(_Model):
    """A review issue on a device. Only its resolution changes."""

    id: str = Field(default_factory=new_id)
    device_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    sheet_id: str = Field(min_length=1)
    type: FlagType
    severity: Severity
    details: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    open: bool = True
    resolution: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    created_at: str = Field(default_factory=utcnow)


class BulkBefore(_Model):
    review_state: ReviewState
    accept_kind: AcceptKind | None = None
    revision: int = Field(ge=1)


class BulkAction(_Model):
    """A bulk accept or its undo (a new linked action). Immutable."""

    id: str = Field(default_factory=new_id)
    document_id: str = Field(min_length=1)
    sheet_id: str = Field(min_length=1)
    kind: Literal["bulk_accept", "undo"]
    device_ids: list[str] = Field(default_factory=list)
    before: dict[str, BulkBefore] = Field(default_factory=dict)
    undo_of: str | None = None
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    actor: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)


class Agreement(_Model):
    category: bool
    subtype: bool | None = None  # None when the model gave no subtype to compare


class ModelVerdict(_Model):
    """One AI request's result (verify batch or discover tile), or its error. Immutable."""

    id: str = Field(default_factory=new_id)
    run_id: str = Field(min_length=1)
    sheet_id: str = Field(min_length=1)
    kind: VerdictKind
    implementation: Literal["claude", "fake"]
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    ai_version: str = Field(min_length=1)
    image_sha256s: list[str] = Field(default_factory=list)
    detection_ids: list[str] = Field(default_factory=list)  # verify
    tile_id: str | None = None  # discover
    output: dict[str, Any] | None = None  # verify: {"verdicts": [...]}
    agreement: dict[str, Agreement] | None = None  # keyed by detection id (verify)
    stop_reason: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    error: dict[str, Any] | None = None
    created_at: str = Field(default_factory=utcnow)
