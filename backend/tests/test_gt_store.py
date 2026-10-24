"""AC5 (history, soft delete, persistence) and store-level immutability."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from takeoff.groundtruth import models as M
from takeoff.groundtruth.store import NotFound, Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gt.sqlite")
    yield s
    s.close()


def make_ann(**kw):
    base = dict(sheet_id="A1-111", page_number=1, category="switch", subtype="standard",
                point={"u": 0.4, "v": 0.3}, provenance={"origin": "manual"}, created_by="rev")
    base.update(kw)
    return M.Annotation(**base)


def make_run(store, n=2):
    run = M.CandidateRun(generator="raster_plain", generator_version="symbol_probe/0.2.0",
                         params={"threshold": 0.5}, input_sha256="0" * 64, run_hash="h1")
    cands = [M.Candidate(run_id=run.id, sheet_id="A1-111", page_number=1, class_hypothesis="switch_standard",
                         score=0.6 + i / 10, point={"u": 0.1 * (i + 1), "v": 0.2},
                         point_pt=(259.2 * (i + 1), 345.6)) for i in range(n)]
    return run, cands


def test_create_logs_event_and_persists_across_reopen(tmp_path):
    path = tmp_path / "gt.sqlite"
    s = Store(path)
    a = s.create_annotation(make_ann(), actor="rev")
    s.close()
    s2 = Store(path)  # simulates a server restart
    assert s2.get("annotations", a.id) == a
    (ev,) = s2.events("annotation", a.id)
    assert ev.action == "create" and ev.before is None and ev.after["id"] == a.id and ev.actor == "rev"
    s2.close()


def test_update_records_before_and_after_and_bumps_revision(store):
    a = store.create_annotation(make_ann(), actor="rev")
    b = store.update_annotation(a.id, actor="rev", subtype="dimmer",
                                text_tokens=[{"text": "D", "bbox_pt": (1, 2, 3, 4)}])
    assert b.revision == 2 and b.subtype == "dimmer"
    evs = store.events("annotation", a.id)
    assert [e.action for e in evs] == ["create", "update"]
    assert evs[1].before["subtype"] == "standard" and evs[1].after["subtype"] == "dimmer"


def test_invalid_update_writes_nothing(store):
    a = store.create_annotation(make_ann(), actor="rev")
    with pytest.raises(ValidationError):
        store.update_annotation(a.id, actor="rev", subtype="chandelier")
    assert store.get("annotations", a.id).subtype == "standard"
    assert len(store.events("annotation", a.id)) == 1


def test_identity_fields_cannot_change(store):
    a = store.create_annotation(make_ann(), actor="rev")
    with pytest.raises(StoreError, match="cannot be changed"):
        store.update_annotation(a.id, actor="rev", sheet_id="A2-111")


def test_delete_is_soft_and_logged(store):
    a = store.create_annotation(make_ann(), actor="rev")
    store.delete_annotation(a.id, actor="rev", reason="overprinted duplicate")
    assert store.list("annotations", "A1-111") == []
    (d,) = store.list("annotations", "A1-111", include_deleted=True)
    assert d.deleted and "[deleted] overprinted duplicate" in d.notes
    assert [e.action for e in store.events("annotation", a.id)] == ["create", "delete"]
    with pytest.raises(StoreError):
        store.update_annotation(a.id, actor="rev", subtype="dimmer")
    with pytest.raises(StoreError):
        store.delete_annotation(a.id, actor="rev")


def test_duplicate_insert_refused(store):
    a = store.create_annotation(make_ann(), actor="rev")
    with pytest.raises(StoreError, match="already exists"):
        store.create_annotation(a, actor="rev")


def test_missing_row(store):
    with pytest.raises(NotFound):
        store.get("annotations", "nope")
    assert store.find("annotations", "nope") is None


def test_events_are_append_only_at_sql_level(store):
    store.create_annotation(make_ann(), actor="rev")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE events SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM events")


def test_candidate_import_idempotent_and_immutable(store):
    run, cands = make_run(store)
    r1, inserted = store.import_candidate_run(run, cands, actor="importer")
    assert inserted and len(store.list("candidates", "A1-111")) == 2
    reviews = store.list("candidate_reviews")
    assert {r.outcome for r in reviews} == {"unreviewed"}
    # same run_hash again: no-op
    run2, cands2 = make_run(store)
    r2, inserted2 = store.import_candidate_run(run2, cands2, actor="importer")
    assert not inserted2 and r2.id == r1.id and len(store.list("candidates")) == 2
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store._conn.execute("UPDATE candidates SET data='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store._conn.execute("DELETE FROM candidate_runs")


def test_failed_import_rolls_back_completely(store):
    run, cands = make_run(store)
    bad = cands + [M.Candidate(run_id="other", sheet_id="A1-111", page_number=1, class_hypothesis="x",
                               score=0.5, point={"u": 0.5, "v": 0.5}, point_pt=(1296.0, 864.0))]
    with pytest.raises(StoreError):
        store.import_candidate_run(run, bad, actor="importer")
    assert store.list("candidates") == [] and store.list("candidate_runs") == [] and store.events() == []


def test_wiring_runs_stored_separately(store):
    w = M.WiringRun(sheet_id="A1-111", page_number=1, polyline=[{"u": 0.1, "v": 0.1}, {"u": 0.2, "v": 0.3}],
                    provenance={"origin": "manual"}, created_by="rev")
    store.insert("wiring_runs", w, actor="rev")
    assert store.list("wiring_runs", "A1-111") == [w]
    assert store.list("annotations", "A1-111") == []
