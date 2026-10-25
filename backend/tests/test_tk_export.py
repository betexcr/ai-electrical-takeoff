"""Export: an immutable, reconciling export. Official records and totals come only from accepted and manual
devices; review items go to a separate file; the manifest states completeness and AI accounting honestly."""

from __future__ import annotations

import csv
import hashlib
import json
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from tk_helpers import (ACTOR, CL_EAST, CL_WEST, DOC, HALL, NEAR_HALL, NEAR_KITCHEN, RUN_ID, SHEET, make,
                        seed_detection, seed_device, seed_flag)

from takeoff.app import export as X
from takeoff.app import models as TM

BRIEF_COLUMNS = ["id", "sheet_id", "category", "subtype", "subtype_label", "status", "room_kind", "room_name",
                 "u", "v", "x_pt", "y_pt", "review_state", "origin_reported", "accept_kind", "created_by_run",
                 "detector_version", "match_score", "subtype_source", "ai_summary", "ai_implementation",
                 "ai_model"]
OFFICIAL_COLUMNS = BRIEF_COLUMNS + ["room_key"]
REVIEW_COLUMNS = BRIEF_COLUMNS + ["deleted", "delete_reason", "open_flags", "room_key"]
AI_NOTE = "AI verdicts and AI-only candidates are suggestions; totals come from accepted and manual device records"
FAKE_LABEL = "(test double, not a model)"


@pytest.fixture
def env(tmp_path):
    store, svc = make(tmp_path)
    return store, svc, tmp_path / "exports"


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def header(path):
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def load(d, name):
    return json.loads((d / name).read_text(encoding="utf-8"))


def seed_mixed(store):
    """Accepted detector x2 (one with evidence), accepted ai, manual, proposed, rejected, deleted-accepted."""
    det = seed_detection(store, "det-a1", score=0.91, subtype_source="token")
    seed_device(store, "acc-1", state="accepted", detection=det)
    seed_device(store, "acc-2", subtype="dimmer", state="accepted", pt=NEAR_HALL, room=HALL)
    seed_device(store, "acc-ai", state="accepted", origin="ai", category="receptacle", subtype="gfi",
                pt=(200.0, 200.0))
    seed_device(store, "man-1", state="accepted", origin="manual", category="receptacle", subtype="gfi",
                accept_kind="manual", pt=(140.0, 165.0))
    seed_device(store, "prop-1", state="proposed", pt=(300.0, 120.0))
    seed_device(store, "rej-1", state="rejected", pt=(350.0, 150.0))
    seed_device(store, "del-1", state="accepted", deleted=True, delete_reason="not a device", pt=(400.0, 150.0))


def test_official_csv_holds_only_accepted_and_manual(env):
    store, svc, out = env
    seed_mixed(store)
    seed_flag(store, svc.store.find("devices", "prop-1"), "duplicate_warning")
    d = X.export_takeoff(svc, DOC, out, ACTOR)

    assert header(d / "devices_official.csv") == OFFICIAL_COLUMNS
    official = rows(d / "devices_official.csv")
    assert {r["id"] for r in official} == {"acc-1", "acc-2", "acc-ai", "man-1"}
    assert {r["review_state"] for r in official} == {"accepted"}
    by_id = {r["id"]: r for r in official}
    assert by_id["acc-1"]["origin_reported"] == "detector_accepted"
    assert by_id["acc-ai"]["origin_reported"] == "ai_accepted"
    assert by_id["man-1"]["origin_reported"] == "manual"
    assert by_id["man-1"]["accept_kind"] == "manual"
    assert by_id["acc-1"]["created_by_run"] == RUN_ID
    assert by_id["acc-1"]["detector_version"] == "1"
    assert by_id["acc-1"]["match_score"] == "0.91"
    assert by_id["acc-1"]["subtype_source"] == "token"
    assert by_id["acc-1"]["room_kind"] == "labeled" and by_id["acc-1"]["room_name"] == "Kitchen"
    assert by_id["acc-1"]["subtype_label"]  # the legend wording, never empty
    assert by_id["man-1"]["created_by_run"] == "" and by_id["man-1"]["detector_version"] == ""
    assert by_id["acc-1"]["ai_summary"].startswith("AI second opinion")

    assert header(d / "devices_review.csv") == REVIEW_COLUMNS
    review = {r["id"]: r for r in rows(d / "devices_review.csv")}
    assert set(review) == {"prop-1", "rej-1", "del-1"}
    assert review["prop-1"]["review_state"] == "proposed" and review["prop-1"]["deleted"] == "false"
    assert review["prop-1"]["open_flags"] == "duplicate_warning"
    assert review["rej-1"]["review_state"] == "rejected"
    # review rows carry the internal origin; only accepted official rows use the reported vocabulary
    assert {r["origin_reported"] for r in review.values()} == {"detector"}
    vocab = load(d, "manifest.json")["origin_vocabulary"]
    assert "detector_accepted | ai_accepted | manual" in vocab["devices_official.csv"]
    assert "detector | ai | manual" in vocab["devices_review.csv"]
    assert review["del-1"]["deleted"] == "true" and review["del-1"]["delete_reason"] == "not a device"
    # nothing in both files, nothing lost
    assert len(official) + len(review) == len(svc.devices(DOC, include_deleted=True))


def test_totals_reconcile_with_csv_and_service(env):
    store, svc, out = env
    seed_mixed(store)
    # two same-named "CL" labels on one sheet are two separate rows keyed by label position
    seed_device(store, "cl-w1", state="accepted", pt=(300.0, 880.0), room=CL_WEST)
    seed_device(store, "cl-w2", state="accepted", pt=(310.0, 860.0), room=CL_WEST)
    seed_device(store, "cl-e1", state="accepted", pt=(700.0, 880.0), room=CL_EAST)
    seed_device(store, "cl-r", state="rejected", pt=(720.0, 860.0), room=CL_EAST)
    d = X.export_takeoff(svc, DOC, out, ACTOR)

    # recompute from the CSV alone, rooms by its room_key column
    recomputed: dict = {}
    for r in rows(d / "devices_official.csv"):
        s = recomputed.setdefault(r["sheet_id"], {"by_room": {}, "by_category": {}, "total": 0,
                                                  "by_origin": {"detector_accepted": 0, "ai_accepted": 0,
                                                                "manual": 0}})
        room = s["by_room"].setdefault(r["room_key"], {}).setdefault(r["category"], Counter())
        room[r["subtype"]] += 1
        s["by_category"].setdefault(r["category"], Counter())[r["subtype"]] += 1
        s["total"] += 1
        s["by_origin"][r["origin_reported"]] += 1
    plain = json.loads(json.dumps(recomputed))  # Counters -> dicts
    totals = load(d, "totals.json")
    assert plain[SHEET]["total"] == totals[SHEET]["total"] == 7
    assert plain[SHEET]["by_origin"] == totals[SHEET]["by_origin"] == {
        "detector_accepted": 5, "ai_accepted": 1, "manual": 1}
    assert totals[SHEET]["by_room"]["CL @(300,900)"] == {"switch": {"standard": 2}}
    assert totals[SHEET]["by_room"]["CL @(700,900)"] == {"switch": {"standard": 1}}
    review = {r["id"]: r for r in rows(d / "devices_review.csv")}
    assert review["cl-r"]["room_key"] == "CL @(700,900)"  # the review file carries the same key
    assert plain[SHEET]["by_category"] == totals[SHEET]["by_category"]
    assert plain[SHEET]["by_room"] == totals[SHEET]["by_room"]
    assert totals == svc.counts(DOC)["official"]
    assert load(d, "manifest.json")["review"]["official_total"] == totals[SHEET]["total"]

    # the files are what the manifest hashed
    manifest = load(d, "manifest.json")
    assert set(manifest["files"]) == {"devices_official.csv", "devices_review.csv", "totals.json"}
    for name, meta in manifest["files"].items():
        data = (d / name).read_bytes()
        assert meta["sha256"] == hashlib.sha256(data).hexdigest() and meta["bytes"] == len(data)


def test_incomplete_takeoff_is_marked_with_reasons_including_ai_flags(env):
    store, svc, out = env
    seed_device(store, "acc-1", state="accepted")
    seed_device(store, "p1", state="proposed", pt=NEAR_KITCHEN)
    p2 = seed_device(store, "p2", state="proposed", pt=NEAR_HALL, room=HALL)
    p3 = seed_device(store, "p3", state="proposed", pt=(900.0, 900.0), room=None)
    ai = seed_device(store, "ai-1", state="proposed", origin="ai", pt=(170.0, 150.0))
    seed_flag(store, p2, "model_disagrees", verdict_id="v", ai_category="receptacle", ai_subtype=None)
    seed_flag(store, p3, "model_disagrees", verdict_id="v", ai_category="receptacle", ai_subtype=None)
    seed_flag(store, ai, "ai_only_candidate", severity="info", verdict_id="v", tile_id="t1")
    # an informational flag never blocks completeness by itself
    seed_flag(store, p3, "no_room", severity="info")
    run = store.find("runs", RUN_ID)
    store.replace("runs", run.model_copy(update={"sheets": {SHEET: TM.SheetRunStatus(
        status="done", ai_discover=TM.AIStageStatus(status="unavailable", reason="no credentials"))}}),
        "system", "update")

    m = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    assert m["complete"] is False
    reasons = m["incomplete_reasons"]
    assert "4 detected devices awaiting review" in reasons
    assert "1 AI-only candidate awaiting review" in reasons
    assert "2 open model_disagrees flags" in reasons
    assert f"AI discovery unavailable on {SHEET}: no credentials" in reasons
    assert m["open_flags_by_type"] == {"ai_only_candidate": 1, "model_disagrees": 2, "no_room": 1}
    assert not any("no_room" in r for r in reasons)  # info flags are counted, not blocking


def test_complete_when_nothing_unresolved(env):
    store, svc, out = env
    seed_mixed(store)
    # only informational flags remain open; the proposal in seed_mixed is resolved by rejecting it
    svc.reject("prop-1", ACTOR)
    seed_flag(store, store.find("devices", "acc-1"), "no_room", severity="info")
    m = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    assert m["complete"] is True
    assert m["incomplete_reasons"] == []
    assert m["review"]["awaiting"] == 0

    # unavailable AI discovery is informational: it is listed but does not make the takeoff incomplete
    run = store.find("runs", RUN_ID)
    store.replace("runs", run.model_copy(update={"sheets": {SHEET: TM.SheetRunStatus(
        status="done", ai_discover=TM.AIStageStatus(status="unavailable", reason="sdk missing"))}}),
        "system", "update")
    m2 = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    assert m2["complete"] is True
    assert m2["incomplete_reasons"] == [f"AI discovery unavailable on {SHEET}: sdk missing"]


def test_export_never_overwrites(env):
    store, svc, out = env
    seed_mixed(store)
    (out / "tk_007_20260101T000000Z").mkdir(parents=True)  # an existing export, whatever its content
    first = X.export_takeoff(svc, DOC, out, ACTOR)
    assert first.name.startswith("tk_008_") and first.name.endswith("Z")
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    svc.reject("prop-1", ACTOR)
    second = X.export_takeoff(svc, DOC, out, ACTOR)
    assert second != first and second.name.startswith("tk_009_")
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before  # the first is untouched
    assert load(first, "manifest.json")["complete"] is False
    assert len([p for p in out.iterdir() if p.name.startswith("tk_")]) == 3
    assert list((out / "tk_007_20260101T000000Z").iterdir()) == []


def test_manifest_review_figures_satisfy_identities(env):
    store, svc, out = env
    seed_mixed(store)
    for i in range(3):
        seed_device(store, f"w{i}", state="proposed", origin="ai" if i == 0 else "detector")
    seed_flag(store, store.find("devices", "w0"), "ai_only_candidate", severity="info", verdict_id="v", tile_id="t")
    seed_flag(store, store.find("devices", "w1"), "duplicate_warning")
    m = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    r = m["review"]
    assert r == svc.counts(DOC)["review"]
    assert r["detected"] == r["accepted_detected"] + r["rejected"] + r["awaiting"]
    assert r["awaiting"] == r["awaiting_unflagged"] + r["awaiting_flagged"]
    assert r["official_total"] == r["accepted_detected"] + r["manual"]
    assert r["detected_ai_only"] <= r["detected"]
    assert (r["awaiting"], r["awaiting_flagged"]) == (4, 2)
    assert m["export_format"] == X.EXPORT_FORMAT == "takeoff-export/1"
    assert m["schema_version"] == TM.SCHEMA_VERSION and m["merge_version"] == TM.MERGE_VERSION
    assert m["document_sha256"] == DOC
    assert m["created_by"] == ACTOR and m["created_at"]
    assert m["runs"][0]["id"] == RUN_ID
    assert (m["runs"][0]["detector"], m["runs"][0]["detector_version"]) == ("det/0", "1")


def _ai_run(store, impl, verify_tokens):
    """Give the run per-sheet AI stage status, a model and verdicts carrying real usage numbers."""
    run = store.find("runs", RUN_ID)
    stage = lambda status, **kw: TM.AIStageStatus(status=status, implementation=impl, **kw)  # noqa: E731
    store.replace("runs", run.model_copy(update={
        "ai_model": "claude-opus-5-5",
        "ai_versions": {"verify": "ai-verify/1", "verify_prompt": "ai-verify-prompt/1",
                        "verify_implementation": impl, "verify_model": "claude-opus-5-5",
                        "discover": "ai-discover/1", "discover_prompt": "ai-discover-prompt/1"},
        "sheets": {SHEET: TM.SheetRunStatus(
            status="done", ai_verify=stage("partial", requests=2, errors_by_code={"timeout": 1}),
            ai_discover=TM.AIStageStatus(status="off"))}}), "system", "update")
    common = dict(run_id=RUN_ID, sheet_id=SHEET, kind="verify", implementation=impl, model="claude-opus-5-5",
                  prompt_version="ai-verify-prompt/1", ai_version="ai-verify/1")
    det = seed_detection(store, "det-v")
    store.insert("verdicts", TM.ModelVerdict(
        id="v-1", detection_ids=[det.id], input_tokens=verify_tokens, output_tokens=40,
        cache_creation_input_tokens=5, cache_read_input_tokens=7, latency_ms=120.0, stop_reason="end_turn",
        output={"verdicts": [{"item_id": det.id, "category": "switch", "subtype": "standard",
                              "is_device": True, "uncertain": False}]},
        agreement={det.id: TM.Agreement(category=True, subtype=True)}, **common), "system")
    store.insert("verdicts", TM.ModelVerdict(id="v-2", detection_ids=["x"], error={"code": "timeout"},
                                             latency_ms=60000.0, **common), "system")
    return det


def test_manifest_states_ai_status_usage_and_fake_label(env):
    store, svc, out = env
    det = _ai_run(store, "fake", 1000)
    seed_device(store, "acc-1", state="accepted", detection=det)
    d = X.export_takeoff(svc, DOC, out, ACTOR)
    m = load(d, "manifest.json")

    verify = m["runs"][0]["ai"]["verify"]
    assert verify["status"] == "partial"
    assert verify["implementation"] == "fake"
    assert verify["model"] == "claude-opus-5-5"
    assert verify["prompt_version"] == "ai-verify-prompt/1" and verify["ai_version"] == "ai-verify/1"
    assert verify["requests"] == 2
    assert verify["errors_by_code"] == {"timeout": 1}
    assert verify["tokens"] == {"input": 1000, "output": 40, "cache_creation": 5, "cache_read": 7}
    assert verify["latency_ms"]["p50"] is not None
    assert verify["sheets"][SHEET]["status"] == "partial"
    assert m["runs"][0]["ai"]["discover"]["status"] == "off"
    assert m["runs"][0]["sheets"][SHEET]["status"] == "done"
    assert m["ai_note"] == f"{AI_NOTE} {FAKE_LABEL}"
    assert m["ai_test_double"] is True
    assert rows(d / "devices_official.csv")[0]["ai_implementation"] == "fake"
    assert rows(d / "devices_official.csv")[0]["ai_model"] == "claude-opus-5-5"

    # a run made with the real implementation carries the plain note
    (d.parent / "second").mkdir()
    store2, svc2 = make(d.parent / "second")
    _ai_run(store2, "claude", 900)
    seed_device(store2, "acc-1", state="accepted")
    m2 = load(X.export_takeoff(svc2, DOC, d.parent / "second-out", ACTOR), "manifest.json")
    assert m2["ai_note"] == AI_NOTE and m2["ai_test_double"] is False
    assert m2["runs"][0]["ai"]["verify"]["implementation"] == "claude"
    assert m2["runs"][0]["ai"]["verify"]["tokens"]["input"] == 900


def test_complete_is_false_for_an_open_review_flag_on_an_accepted_device(env):
    store, svc, out = env
    seed_mixed(store)
    svc.reject("prop-1", ACTOR)
    seed_flag(store, store.find("devices", "acc-1"), "duplicate_warning")  # accepted, flag open, none awaiting
    m = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    assert m["review"]["awaiting"] == 0 and m["review"]["accepted_with_open_flags"] == 1
    assert m["complete"] is False
    assert m["incomplete_reasons"] == ["1 open duplicate_warning flag"]


def test_open_flags_by_type_excludes_flags_on_deleted_devices(env):
    store, svc, out = env
    seed_mixed(store)
    svc.reject("prop-1", ACTOR)
    seed_flag(store, store.find("devices", "del-1"), "duplicate_warning")  # a deleted device's stale flag
    seed_flag(store, store.find("devices", "acc-1"), "no_room", severity="info")
    m = load(X.export_takeoff(svc, DOC, out, ACTOR), "manifest.json")
    assert m["open_flags_by_type"] == {"no_room": 1}
    assert m["complete"] is True and m["incomplete_reasons"] == []


def test_failed_write_removes_only_this_calls_directory(env, monkeypatch):
    store, svc, out = env
    seed_mixed(store)
    first = X.export_takeoff(svc, DOC, out, ACTOR)
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    real = Path.write_bytes

    def failing(self, data):
        if self.name == "manifest.json":
            raise OSError("disk full")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    with pytest.raises(OSError, match="disk full"):
        X.export_takeoff(svc, DOC, out, ACTOR)
    monkeypatch.undo()
    assert [p.name for p in out.iterdir()] == [first.name]  # the failed attempt left nothing behind
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before  # the earlier export is untouched
    assert X.export_takeoff(svc, DOC, out, ACTOR).name.startswith("tk_002_")


def test_export_reads_one_snapshot_while_a_writer_waits(env):
    """A write from another thread cannot land between the export's reads: it waits for the read phase."""
    store, svc, out = env
    seed_mixed(store)
    seed_device(store, "late", state="proposed", pt=(130.0, 185.0))
    real_list = store.list
    state = {"fired": False, "writer": None, "done_at": None}

    def writer():
        svc.accept("late", ACTOR)  # a reviewer acting mid-export
        state["done_at"] = time.monotonic()

    def spying_list(*args, **kwargs):
        if not state["fired"]:  # the export's first read: another thread tries to write right now
            state["fired"] = True
            state["writer"] = threading.Thread(target=writer)
            state["writer"].start()
            time.sleep(0.3)  # long enough for an unlocked writer to commit before the later reads
        return real_list(*args, **kwargs)

    store.list = spying_list
    try:
        d = X.export_takeoff(svc, DOC, out, ACTOR)
    finally:
        del store.list  # back to the class method
    state["writer"].join(timeout=10)
    assert state["fired"] and state["done_at"] is not None

    official = {r["id"] for r in rows(d / "devices_official.csv")}
    review = {r["id"] for r in rows(d / "devices_review.csv")}
    assert "late" in review and "late" not in official  # the snapshot predates the accept
    m = load(d, "manifest.json")
    assert m["review"]["awaiting"] == 2  # prop-1 and late, all from the same snapshot
    assert load(d, "totals.json")[SHEET]["total"] == len(official) == m["review"]["official_total"]
    assert m["complete"] is False
    # the write landed once the read phase was over
    assert store.find("devices", "late").review_state == "accepted"
    assert svc.counts(DOC)["review"]["awaiting"] == 1
