"""Generic store and takeoff-schema/1 records (Task 1 of the takeoff workflow)."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from takeoff.app import models as TM
from takeoff.app.store import TakeoffStore
from takeoff.groundtruth import models as M
from takeoff.groundtruth import store as gts
from takeoff.groundtruth.store import Store

DOC = "a" * 64
SHEET = "A1-111"


@pytest.fixture
def store(tmp_path):
    s = TakeoffStore(tmp_path / "tk.sqlite")
    yield s
    s.close()


def make_device(**kw):
    base = dict(document_id=DOC, sheet_id=SHEET, category="switch", subtype="standard",
                point={"u": 0.4, "v": 0.3}, point_pt=(100.0, 200.0), origin="detector",
                created_by_run="run-001")
    base.update(kw)
    return TM.Device(**base)


def make_detection(**kw):
    base = dict(run_id="run-001", document_id=DOC, sheet_id=SHEET, source="det0", category="switch",
                subtype="standard", point={"u": 0.4, "v": 0.3}, point_pt=(100.0, 200.0), score=0.9)
    base.update(kw)
    return TM.RunDetection(**base)


def make_verdict(**kw):
    base = dict(run_id="run-001", sheet_id=SHEET, kind="verify", implementation="fake", model="m",
                prompt_version="ai-verify-prompt/1", ai_version="ai-verify/1")
    base.update(kw)
    return TM.ModelVerdict(**base)


def test_device_round_trip_and_event_schema_version(store):
    d = store.insert("devices", make_device(room={"kind": "labeled", "name": "CL", "label_bbox_pt": (1, 2, 3, 4)},
                                            room_source="proposed_nearest_label"), actor="det0")
    assert store.get("devices", d.id) == d
    assert store.list("devices", sheet_id=SHEET) == [d]
    assert d.review_state == "proposed" and d.human_set == [] and d.revision == 1 and not d.deleted
    ev = store.events("device", d.id)
    assert len(ev) == 1 and ev[0].action == "create"
    assert ev[0].schema_version == TM.SCHEMA_VERSION == "takeoff-schema/1"
    assert TM.MERGE_VERSION == "takeoff-merge/2"
    meta = dict(store._conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta == {"store_version": "takeoff-store/1", "schema_version": "takeoff-schema/1"}
    assert store.STORE_VERSION == "takeoff-store/1"
    # replace logs before/after with the takeoff schema version too
    changed = d.model_copy(update={"review_state": "accepted", "accept_kind": "accepted",
                                   "human_set": ["review_state"], "revision": 2})
    store.replace("devices", changed, actor="rev", action="accept")
    ev = store.events("device", d.id)
    assert [e.action for e in ev] == ["create", "accept"]
    assert ev[1].before["review_state"] == "proposed" and ev[1].after["review_state"] == "accepted"
    assert {e.schema_version for e in ev} == {"takeoff-schema/1"}


def test_other_tables_round_trip(store):
    doc = store.insert("documents", TM.Document(id=DOC, file_name="a.pdf", page_count=4, source="supplied",
                                                 stored_path="data/a.pdf"), actor="app")
    sheet = store.insert("sheets", TM.SheetInfo(id=f"{DOC}:1", document_id=DOC, page_number=1, sheet_id=SHEET,
                                                 region_bbox_pt=(0, 0, 100, 100), legend_found=True), actor="app")
    run = store.insert("runs", TM.Run(id="run-001", document_id=DOC, detector="det/0", detector_version="det/0.1",
                                      params={"a": 1}, created_by="rev",
                                      sheets={SHEET: TM.SheetRunStatus(status="queued")}), actor="rev")
    assert run.merge_version == TM.MERGE_VERSION
    assert run.sheets[SHEET].ai_verify.status == "off" and run.sheets[SHEET].ai_verify.tokens.cache_read == 0
    assert store.get("documents", doc.id) == doc and store.get("sheets", sheet.id) == sheet
    assert store.get("runs", "run-001") == run
    assert store.list("sheets", sheet_id=SHEET) == [sheet]
    assert [e.entity for e in store.events()] == ["document", "sheet", "run"]


def test_detections_evidence_bulk_actions_and_verdicts_are_immutable(store):
    det = store.insert("detections", make_detection(), actor="det0")
    dev = store.insert("devices", make_device(), actor="det0")
    ev = store.insert("evidence", TM.Evidence(device_id=dev.id, detection_id=det.id, run_id="run-001",
                                              outcome="created", distance_pt=0.0), actor="merge")
    bulk = store.insert("bulk_actions", TM.BulkAction(
        document_id=DOC, sheet_id=SHEET, kind="bulk_accept", device_ids=[dev.id], actor="rev",
        before={dev.id: {"review_state": "proposed", "accept_kind": None, "revision": 1}}), actor="rev")
    ver = store.insert("verdicts", make_verdict(), actor="ai")
    for table, rec in (("detections", det), ("evidence", ev), ("bulk_actions", bulk), ("verdicts", ver)):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store.replace(table, rec, actor="x", action="edit")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store._conn.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._conn.execute("UPDATE events SET actor='x' WHERE entity_id=?", (rec.id,))
        assert store.get(table, rec.id) == rec
    # devices stay mutable
    store.replace("devices", dev.model_copy(update={"notes": "n", "revision": 2}), actor="rev", action="update")
    assert store.get("devices", dev.id).notes == "n"


def test_ground_truth_store_still_uses_gt_schema(tmp_path):
    s = Store(tmp_path / "gt.sqlite")
    try:
        assert s.SCHEMA_VERSION == M.SCHEMA_VERSION == "gt-schema/1"
        assert s.STORE_VERSION == gts.STORE_VERSION == "gt-store/1"
        assert s.TABLES is gts.TABLES and s.KEY is gts.KEY and s.ENTITY is gts.ENTITY
        assert set(s.IMMUTABLE) == {"candidates", "candidate_runs"}
        a = s.create_annotation(M.Annotation(sheet_id=SHEET, page_number=1, category="switch", subtype="standard",
                                              point={"u": 0.4, "v": 0.3}, provenance={"origin": "manual"},
                                              created_by="rev"), actor="rev")
        assert [e.schema_version for e in s.events("annotation", a.id)] == ["gt-schema/1"]
        meta = dict(s._conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta == {"store_version": "gt-store/1", "schema_version": "gt-schema/1"}
        tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "devices" not in tables and "annotations" in tables
    finally:
        s.close()


def test_takeoff_store_rejects_ground_truth_database(tmp_path):
    Store(tmp_path / "x.sqlite").close()
    with pytest.raises(gts.StoreError, match="store version"):
        TakeoffStore(tmp_path / "x.sqlite")


def test_device_rejects_invalid_review_state_and_origin():
    with pytest.raises(ValidationError):
        make_device(review_state="verified")
    with pytest.raises(ValidationError):
        make_device(origin="candidate")
    with pytest.raises(ValidationError):
        make_device(accept_kind="auto")
    with pytest.raises(ValidationError):
        make_device(room_source="guess")
    with pytest.raises(ValidationError):
        make_device(human_set=["status"])
    with pytest.raises(ValidationError):
        make_device(point={"u": 1.5, "v": 0.3})
    with pytest.raises(ValidationError):
        make_device(surprise=1)
    d = make_device()
    with pytest.raises(ValidationError):
        d.review_state = "done"


def test_ai_origin_flag_types_and_verdict_fields_validate():
    d = make_device(origin="ai")
    assert d.origin == "ai"
    for t in ("classification_conflict", "ambiguous_match", "not_redetected", "model_disagrees",
              "ai_only_candidate", "duplicate_warning", "room_ambiguous", "no_room"):
        f = TM.Flag(device_id=d.id, document_id=DOC, sheet_id=SHEET, type=t, severity="review")
        assert f.open and f.resolution is None
    with pytest.raises(ValidationError):
        TM.Flag(device_id=d.id, document_id=DOC, sheet_id=SHEET, type="other", severity="review")
    with pytest.raises(ValidationError):
        TM.Flag(device_id=d.id, document_id=DOC, sheet_id=SHEET, type="no_room", severity="high")

    # R1: verify output holds {"verdicts": [...]}; agreement is keyed by detection id
    v = make_verdict(detection_ids=["d1", "d2"], image_sha256s=["b" * 64],
                     output={"verdicts": [{"item_id": "d1", "is_device": True}]},
                     agreement={"d1": {"category": True, "subtype": None}, "d2": {"category": False, "subtype": False}},
                     stop_reason="end_turn", input_tokens=10, output_tokens=5,
                     cache_creation_input_tokens=0, cache_read_input_tokens=0, latency_ms=120.5)
    assert v.agreement["d1"].subtype is None and v.agreement["d2"].category is False
    assert v.output["verdicts"][0]["item_id"] == "d1"
    e = make_verdict(kind="discover", tile_id="t-3", error={"code": "timeout", "message": "60s"})
    assert e.output is None and e.agreement is None and e.error["code"] == "timeout"
    for bad in (dict(kind="other"), dict(implementation="gpt"), dict(agreement={"d1": {"category": "maybe"}})):
        with pytest.raises(ValidationError):
            make_verdict(**bad)

    ai = make_detection(source="ai_discover", verdict_id="v1", rationale="looks like a switch",
                        supporting=[dict(tile_id="t1", verdict_id="v1", point_pt=(1.0, 2.0), centrality_pt=40.0)])
    assert ai.supporting[0].tile_id == "t1"
    assert make_detection().supporting == []
    with pytest.raises(ValidationError):
        make_detection(source="det1")
    for k in ("created", "confirmed", "conflict", "ambiguous", "rejected_redetected", "ai_corroborates"):
        TM.Evidence(device_id="d", detection_id="x", run_id="run-001", outcome=k)
    with pytest.raises(ValidationError):
        TM.Evidence(device_id="d", detection_id="x", run_id="run-001", outcome="matched")
    for k in ("accepted", "corrected_and_accepted", "bulk_accepted", "manual"):
        make_device(accept_kind=k)
    for s in ("queued", "running", "done", "failed", "skipped_no_legend"):
        TM.SheetRunStatus(status=s)
    for s in ("pending", "done", "partial", "unavailable", "off"):
        TM.AIStageStatus(status=s)
    with pytest.raises(ValidationError):
        TM.SheetRunStatus(status="skipped")
    with pytest.raises(ValidationError):
        TM.Run(id="run-1x", document_id=DOC, detector="det/0", detector_version="1", params={}, created_by="r")


@pytest.mark.parametrize("outcome", ["rollback", "commit"])
def test_reads_wait_for_another_threads_open_transaction(store, outcome):
    """The connection is shared across threads: a read must not see another thread's uncommitted insert. It waits
    for the commit (then sees the row) or the rollback (then never sees it)."""
    import threading

    d = make_device()
    inserted, finish, done = threading.Event(), threading.Event(), threading.Event()

    class Rollback(Exception):
        pass

    def writer():
        try:
            with store.transaction() as conn:
                store.insert("devices", d, "w", conn=conn)
                inserted.set()
                assert finish.wait(10)
                if outcome == "rollback":
                    raise Rollback
        except Rollback:
            pass

    seen = {}

    def reader():
        seen["find"] = store.find("devices", d.id)
        seen["list"] = store.list("devices")
        seen["events"] = store.events("device", d.id)
        done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert inserted.wait(10)
    r = threading.Thread(target=reader)
    r.start()
    try:
        assert not done.wait(0.3)  # blocked while the transaction is open
        assert seen == {}
    finally:
        finish.set()  # never leave the writer holding the transaction
    w.join(10)
    r.join(10)
    assert done.is_set()
    if outcome == "rollback":
        assert seen == {"find": None, "list": [], "events": []}
    else:
        assert seen["find"] == d and seen["list"] == [d] and len(seen["events"]) == 1
