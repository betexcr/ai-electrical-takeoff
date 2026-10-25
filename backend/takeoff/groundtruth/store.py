"""SQLite working store for ground truth (stdlib sqlite3).

Each table holds the current state of one model as validated JSON; every
write appends an Event in the same transaction, so history can be replayed.
Events and imported candidates are immutable, enforced by SQL triggers rather
than by convention. Deletes are soft (``deleted`` flag + event).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TypeVar

from pydantic import BaseModel

from takeoff.groundtruth import models as M

STORE_VERSION = "gt-store/1"

T = TypeVar("T", bound=BaseModel)

# table -> (model, has sheet_id column)
TABLES: dict[str, type[BaseModel]] = {
    "annotations": M.Annotation,
    "wiring_runs": M.WiringRun,
    "candidate_runs": M.CandidateRun,
    "candidates": M.Candidate,
    "candidate_reviews": M.CandidateReview,
    "cells": M.ReviewCell,
    "sheets": M.SheetReview,
    "observations": M.BlindObservation,
    "warning_acks": M.WarningAck,
}
KEY = {"candidate_reviews": "candidate_id", "sheets": "sheet_id"}
ENTITY = {"annotations": "annotation", "wiring_runs": "wiring_run", "candidate_runs": "candidate_run",
          "candidates": "candidate", "candidate_reviews": "candidate_review", "cells": "cell",
          "sheets": "sheet", "observations": "observation", "warning_acks": "warning_ack"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL, entity_id TEXT NOT NULL, action TEXT NOT NULL,
    before TEXT, after TEXT, actor TEXT NOT NULL, timestamp TEXT NOT NULL, schema_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_entity ON events(entity, entity_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
_TABLE = """
CREATE TABLE IF NOT EXISTS {t} (
    id TEXT PRIMARY KEY, sheet_id TEXT, deleted INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS {t}_sheet ON {t}(sheet_id);
"""
_IMMUTABLE = """
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
    BEGIN SELECT RAISE(ABORT, '{t} are immutable once imported'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
    BEGIN SELECT RAISE(ABORT, '{t} are immutable once imported'); END;
"""


class StoreError(RuntimeError):
    pass


class NotFound(StoreError):
    pass


class Store:
    """SQLite row/event store. Subclasses swap the class attributes to host another record family."""

    TABLES = TABLES
    KEY = KEY
    ENTITY = ENTITY
    IMMUTABLE: tuple[str, ...] = ("candidates", "candidate_runs")
    STORE_VERSION = STORE_VERSION
    SCHEMA_VERSION = M.SCHEMA_VERSION

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # executescript() commits implicitly, so the idempotent DDL runs outside
        # the explicit transaction used for the version check below.
        self._conn.executescript(_SCHEMA)
        for t in self.TABLES:
            self._conn.executescript(_TABLE.format(t=t))
        for t in self.IMMUTABLE:
            self._conn.executescript(_IMMUTABLE.format(t=t))
        with self.transaction():
            row = self._conn.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta VALUES ('store_version', ?)", (self.STORE_VERSION,))
                self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (self.SCHEMA_VERSION,))
            elif row[0] != self.STORE_VERSION:
                raise StoreError(f"store version {row[0]} != {self.STORE_VERSION}")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # --- generic rows ----------------------------------------------------------------

    @classmethod
    def _key(cls, table: str, model: BaseModel) -> str:
        return getattr(model, cls.KEY.get(table, "id"))

    def _event(self, conn, table, entity_id, action, before, after, actor) -> None:
        ev = M.Event(entity=self.ENTITY[table], entity_id=entity_id, action=action, before=before, after=after,
                     actor=actor, schema_version=self.SCHEMA_VERSION)
        conn.execute(
            "INSERT INTO events (entity, entity_id, action, before, after, actor, timestamp, schema_version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ev.entity, ev.entity_id, ev.action, json.dumps(before) if before is not None else None,
             json.dumps(after) if after is not None else None, ev.actor, ev.timestamp, ev.schema_version))

    def insert(self, table: str, model: BaseModel, actor: str, action: str = "create", conn=None) -> BaseModel:
        model = self.TABLES[table].model_validate(model.model_dump())
        key = self._key(table, model)
        data = model.model_dump(mode="json")

        def _do(c):
            if c.execute(f"SELECT 1 FROM {table} WHERE id=?", (key,)).fetchone():
                raise StoreError(f"{table} {key} already exists")
            c.execute(f"INSERT INTO {table} (id, sheet_id, deleted, data) VALUES (?,?,?,?)",
                      (key, data.get("sheet_id"), int(bool(data.get("deleted", False))), json.dumps(data)))
            self._event(c, table, key, action, None, data, actor)

        if conn is not None:
            _do(conn)
        else:
            with self.transaction() as c:
                _do(c)
        return model

    def replace(self, table: str, model: BaseModel, actor: str, action: str, conn=None) -> BaseModel:
        """Replace the current state of an existing row (validated) and log before/after."""
        model = self.TABLES[table].model_validate(model.model_dump())
        key = self._key(table, model)
        data = model.model_dump(mode="json")

        def _do(c):
            row = c.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
            if row is None:
                raise NotFound(f"{table} {key} not found")
            before = json.loads(row[0])
            c.execute(f"UPDATE {table} SET data=?, deleted=?, sheet_id=? WHERE id=?",
                      (json.dumps(data), int(bool(data.get("deleted", False))), data.get("sheet_id"), key))
            self._event(c, table, key, action, before, data, actor)

        if conn is not None:
            _do(conn)
        else:
            with self.transaction() as c:
                _do(c)
        return model

    def get(self, table: str, key: str) -> BaseModel:
        row = self._conn.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFound(f"{table} {key} not found")
        return self.TABLES[table].model_validate(json.loads(row[0]))

    def find(self, table: str, key: str) -> BaseModel | None:
        try:
            return self.get(table, key)
        except NotFound:
            return None

    def list(self, table: str, sheet_id: str | None = None, include_deleted: bool = False) -> list:
        sql = f"SELECT data FROM {table}"
        clauses, params = [], []
        if sheet_id is not None:
            clauses.append("sheet_id=?")
            params.append(sheet_id)
        if not include_deleted:
            clauses.append("deleted=0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid"
        return [self.TABLES[table].model_validate(json.loads(r[0])) for r in self._conn.execute(sql, params)]

    def events(self, entity: str | None = None, entity_id: str | None = None) -> list[M.Event]:
        sql, params = "SELECT id, entity, entity_id, action, before, after, actor, timestamp, schema_version FROM events", []
        clauses = []
        if entity:
            clauses.append("entity=?")
            params.append(entity)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        out = []
        for r in self._conn.execute(sql, params):
            out.append(M.Event(id=r[0], entity=r[1], entity_id=r[2], action=r[3],
                               before=json.loads(r[4]) if r[4] else None, after=json.loads(r[5]) if r[5] else None,
                               actor=r[6], timestamp=r[7], schema_version=r[8]))
        return out

    # --- annotations ---------------------------------------------------------------------

    def create_annotation(self, a: M.Annotation, actor: str, conn=None) -> M.Annotation:
        return self.insert("annotations", a, actor, "create", conn=conn)

    def update_annotation(self, annotation_id: str, actor: str, action: str = "update", conn=None,
                          **changes) -> M.Annotation:
        """Apply field changes; bumps revision and updated_at; validated before writing."""
        current = self.get("annotations", annotation_id)
        if current.deleted and action != "restore":
            raise StoreError("cannot update a deleted annotation")
        forbidden = {"id", "sheet_id", "page_number", "created_at", "created_by", "revision"} & set(changes)
        if forbidden:
            raise StoreError(f"fields cannot be changed: {sorted(forbidden)}")
        data = {**current.model_dump(), **changes, "revision": current.revision + 1, "updated_at": M.utcnow()}
        return self.replace("annotations", M.Annotation.model_validate(data), actor, action, conn=conn)

    def delete_annotation(self, annotation_id: str, actor: str, reason: str | None = None, conn=None) -> M.Annotation:
        current = self.get("annotations", annotation_id)
        if current.deleted:
            raise StoreError("annotation already deleted")
        notes = current.notes
        if reason:
            notes = f"{notes}\n[deleted] {reason}" if notes else f"[deleted] {reason}"
        return self.update_annotation(annotation_id, actor, action="delete", conn=conn, deleted=True, notes=notes)

    # --- candidates (immutable) ----------------------------------------------------------------

    def find_run_by_hash(self, run_hash: str) -> M.CandidateRun | None:
        for r in self._conn.execute("SELECT data FROM candidate_runs"):
            run = M.CandidateRun.model_validate(json.loads(r[0]))
            if run.run_hash == run_hash:
                return run
        return None

    def import_candidate_run(self, run: M.CandidateRun, candidates: list[M.Candidate], actor: str) -> tuple[M.CandidateRun, bool]:
        """Insert a run and its candidates once; re-importing the same run_hash is a no-op."""
        existing = self.find_run_by_hash(run.run_hash)
        if existing:
            return existing, False
        with self.transaction() as c:
            self.insert("candidate_runs", run, actor, "import", conn=c)
            for cand in candidates:
                if cand.run_id != run.id:
                    raise StoreError("candidate run_id does not match the run")
                self.insert("candidates", cand, actor, "import", conn=c)
                self.insert("candidate_reviews", M.CandidateReview(candidate_id=cand.id), actor, "import", conn=c)
        return run, True
