"""Ground-truth schema v1 (Pydantic).

Coordinates are canonical normalized (u, v) in displayed page space
(takeoff.pdf.coords). PDF points (PyMuPDF unrotated space) may be stored
alongside for traceability. Devices and wiring runs are separate models; a
wiring run can never be stored as an Annotation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from takeoff.groundtruth import taxonomy as T

SCHEMA_VERSION = "gt-schema/1"

Origin = Literal["manual", "candidate", "blind_scan"]
ReviewState = Literal["draft", "verified", "needs_adjudication"]
BlindConfirmation = Literal["confirmed", "not_seen_in_blind_scan", "added_from_blind_scan"]
Phase = Literal["pass1", "blind_scan", "reconcile", "verified"]
Generator = Literal["raster_plain", "raster_thickened", "vector_signature"]
CandidateOutcome = Literal["unreviewed", "accepted", "corrected", "rejected"]
Reconciliation = Literal["pending", "matched", "promoted", "dismissed"]
RoomKind = Literal["labeled", "exterior", "unlabeled"]


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class NormPoint(_Model):
    u: float = Field(ge=0.0, le=1.0)
    v: float = Field(ge=0.0, le=1.0)


class NormRect(_Model):
    u0: float = Field(ge=0.0, le=1.0)
    v0: float = Field(ge=0.0, le=1.0)
    u1: float = Field(ge=0.0, le=1.0)
    v1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self):
        if self.u0 > self.u1 or self.v0 > self.v1:
            raise ValueError("rect must satisfy u0 <= u1 and v0 <= v1")
        return self


PtRect = tuple[float, float, float, float]


class TextTokenRef(_Model):
    """A PDF text word the reviewer linked to a device (e.g. 'GFI', '3D')."""

    text: str = Field(min_length=1)
    bbox_pt: PtRect


class RoomRef(_Model):
    kind: RoomKind
    name: str = Field(min_length=1)
    # The room-label text box this assignment refers to, so the two "CL"
    # labels on a sheet stay distinguishable.
    label_bbox_pt: PtRect | None = None

    @model_validator(mode="after")
    def _label_needed(self):
        if self.kind == "labeled" and self.label_bbox_pt is None:
            raise ValueError("a labeled room must reference its label's bbox")
        return self


class Provenance(_Model):
    origin: Origin
    candidate_ids: list[str] = Field(default_factory=list)
    generator: Generator | None = None
    generator_version: str | None = None
    score: float | None = None
    observation_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.origin == "candidate":
            if not self.candidate_ids or not self.generator or not self.generator_version:
                raise ValueError("candidate origin requires candidate_ids, generator and generator_version")
            if self.observation_id:
                raise ValueError("candidate origin cannot carry an observation_id")
        elif self.origin == "blind_scan":
            if not self.observation_id:
                raise ValueError("blind_scan origin requires observation_id")
            if self.candidate_ids or self.generator:
                raise ValueError("blind_scan origin cannot carry candidate fields")
        else:
            if self.candidate_ids or self.generator or self.observation_id or self.score is not None:
                raise ValueError("manual origin cannot carry candidate or observation fields")
        return self


class Annotation(_Model):
    """One electrical device as drawn on a sheet."""

    id: str = Field(default_factory=new_id)
    sheet_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    category: str
    subtype: str
    status: str = ""
    point: NormPoint
    point_pt: tuple[float, float] | None = None
    bbox: NormRect | None = None
    rotation: Literal[0, 90, 180, 270] | None = None
    text_tokens: list[TextTokenRef] = Field(default_factory=list)
    room: RoomRef | None = None
    notes: str | None = None
    provenance: Provenance
    review_state: ReviewState = "draft"
    blind_confirmation: BlindConfirmation | None = None
    blind_decision_reason: str | None = None
    deleted: bool = False
    created_by: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _default_status(cls, data: Any):
        if isinstance(data, dict) and not data.get("status") and data.get("category"):
            data = {**data, "status": T.default_status(data["category"])}
        return data

    @model_validator(mode="after")
    def _taxonomy(self):
        try:
            T.validate_device(self.category, self.subtype, self.status, self.notes)
        except T.TaxonomyError as e:
            raise ValueError(str(e)) from e
        if self.provenance.origin == "blind_scan" and self.blind_confirmation not in (None, "added_from_blind_scan"):
            raise ValueError("an annotation promoted from the blind scan has blind_confirmation 'added_from_blind_scan'")
        if self.blind_confirmation == "not_seen_in_blind_scan" and self.review_state == "verified" \
                and not (self.blind_decision_reason and self.blind_decision_reason.strip()):
            raise ValueError("verifying an annotation not seen in the blind scan requires a reason")
        return self

    def token_conflicts(self) -> list[str]:
        return T.token_conflicts(self.category, self.subtype, [t.text for t in self.text_tokens])


class WiringRun(_Model):
    """A wiring/connection run; never a device (no UI in B-03)."""

    id: str = Field(default_factory=new_id)
    sheet_id: str = Field(min_length=1)
    page_number: int = Field(ge=1)
    polyline: list[NormPoint] = Field(min_length=2)
    from_annotation_id: str | None = None
    to_annotation_id: str | None = None
    cable_type: Literal["unknown", "2-wire", "3-wire"] = "unknown"
    provenance: Provenance
    notes: str | None = None
    deleted: bool = False
    created_by: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)


class CandidateRun(_Model):
    id: str = Field(default_factory=new_id)
    generator: Generator
    generator_version: str = Field(min_length=1)
    params: dict[str, Any]
    input_sha256: str = Field(min_length=64, max_length=64)
    run_hash: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)


class Candidate(_Model):
    """An unvalidated candidate location. Never a detection or a count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_id)
    run_id: str
    sheet_id: str
    page_number: int = Field(ge=1)
    class_hypothesis: str  # catalogue filename stem, e.g. "receptacle_gfi_duplex"
    category_hint: str | None = None
    subtype_hint: str | None = None
    score: float | None = None  # NCC score for raster generators; None for vector signatures
    point: NormPoint
    point_pt: tuple[float, float]  # PyMuPDF unrotated points, for traceability
    bbox: NormRect | None = None
    # Template rotation (counter-clockwise, degrees) that produced the match.
    rotation: Literal[0, 90, 180, 270] | None = None
    # Clusters are computed at read time across all runs (candidates.cluster_candidates),
    # because a run imported later can join an existing cluster and these records are immutable.


class CandidateReview(_Model):
    candidate_id: str
    outcome: CandidateOutcome = "unreviewed"
    annotation_id: str | None = None
    reason: str | None = None
    actor: str | None = None
    at: str | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.outcome in ("accepted", "corrected") and not self.annotation_id:
            raise ValueError(f"{self.outcome} candidates must reference the annotation they became")
        if self.outcome == "rejected" and self.annotation_id:
            raise ValueError("a rejected candidate cannot reference an annotation")
        return self


class ReviewCell(_Model):
    id: str
    sheet_id: str
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    bbox: NormRect
    bbox_pt: PtRect
    pass1_done: bool = False
    pass1_by: str | None = None
    pass1_at: str | None = None
    blind_scan_done: bool = False
    blind_scan_by: str | None = None
    blind_scan_at: str | None = None


class SheetReview(_Model):
    sheet_id: str
    page_number: int = Field(ge=1)
    region_bbox_pt: PtRect
    region_confirmed: bool = False
    region_flag_reason: str | None = None
    phase: Phase = "pass1"
    blind_scan_started_at: str | None = None
    blind_scan_closed_at: str | None = None
    reconcile_completed_at: str | None = None


class BlindObservation(_Model):
    id: str = Field(default_factory=new_id)
    sheet_id: str
    cell_id: str | None = None
    point: NormPoint
    category: str
    subtype: str | None = None
    note: str | None = None
    created_by: str = Field(min_length=1)
    created_at: str = Field(default_factory=utcnow)
    reconciliation: Reconciliation = "pending"
    annotation_id: str | None = None
    reason: str | None = None
    deleted: bool = False  # removable only while the blind scan is open

    @field_validator("category")
    @classmethod
    def _device_category(cls, v: str) -> str:
        if v not in T.CATEGORIES:
            raise ValueError(f"unknown device category {v!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.subtype is not None and self.subtype not in T.subtypes(self.category):
            raise ValueError(f"subtype {self.subtype!r} is not valid for {self.category!r}")
        if self.reconciliation in ("matched", "promoted") and not self.annotation_id:
            raise ValueError(f"{self.reconciliation} observations must reference an annotation")
        if self.reconciliation == "dismissed" and not (self.reason and self.reason.strip()):
            raise ValueError("dismissing an observation requires a reason")
        return self


class WarningAck(_Model):
    """A reviewer's acknowledgement that a computed warning is not an error."""

    id: str  # the warning id, e.g. "dup:<a>:<b>"
    sheet_id: str
    reason: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    at: str = Field(default_factory=utcnow)
    deleted: bool = False


class Event(_Model):
    id: int | None = None
    entity: str
    entity_id: str
    action: str
    before: dict | None = None
    after: dict | None = None
    actor: str
    timestamp: str = Field(default_factory=utcnow)
    schema_version: str = SCHEMA_VERSION
