"""SQLite store for takeoff records: the ground-truth store's row/event machinery, other tables."""

from __future__ import annotations

from pydantic import BaseModel

from takeoff.app import models as TM
from takeoff.groundtruth.store import NotFound, Store, StoreError  # noqa: F401  (re-exported)

STORE_VERSION = "takeoff-store/1"

TABLES: dict[str, type[BaseModel]] = {
    "documents": TM.Document,
    "sheets": TM.SheetInfo,
    "runs": TM.Run,
    "detections": TM.RunDetection,
    "devices": TM.Device,
    "evidence": TM.Evidence,
    "flags": TM.Flag,
    "bulk_actions": TM.BulkAction,
    "verdicts": TM.ModelVerdict,
}
ENTITY = {"documents": "document", "sheets": "sheet", "runs": "run", "detections": "detection",
          "devices": "device", "evidence": "evidence", "flags": "flag", "bulk_actions": "bulk_action",
          "verdicts": "verdict"}
IMMUTABLE = ("detections", "evidence", "bulk_actions", "verdicts")


class TakeoffStore(Store):
    TABLES = TABLES
    KEY: dict[str, str] = {}
    ENTITY = ENTITY
    IMMUTABLE = IMMUTABLE
    STORE_VERSION = STORE_VERSION
    SCHEMA_VERSION = TM.SCHEMA_VERSION
