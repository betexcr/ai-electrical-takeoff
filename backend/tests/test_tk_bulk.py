"""Bulk accept and undo (spec §4.9): unflagged proposals only, one transaction, audited, undoable as a whole.

Undo restores a device only while its revision is still the one the bulk accept left it at; a device edited since
is skipped and reported, never overwritten.
"""

from __future__ import annotations

import pytest

from takeoff.app import flags as F
from takeoff.app.service import ALL_SHEETS, TakeoffError
from tk_helpers import ACTOR, DOC, SHEET, flags_of, make, seed_device, seed_flag

OTHER_DOC = "e" * 64
BULK_ACTOR = "bulk@example.com"


def pt(i):  # well apart (> the 3 pt duplicate tolerance), all clearly in the Kitchen
    return (110.0 + 20.0 * i, 150.0)


@pytest.fixture
def env(tmp_path):
    store, svc = make(tmp_path)
    for i in range(4):
        seed_device(store, f"d{i}", pt=pt(i))
    yield store, svc
    store.close()


def states(store, ids=("d0", "d1", "d2", "d3")):
    return {i: (d.review_state, d.accept_kind, d.revision) for i in ids for d in [store.get("devices", i)]}


def test_bulk_accepts_only_unflagged_proposals(env):
    store, svc = env
    seed_flag(store, store.get("devices", "d1"), "classification_conflict", severity="review")
    seed_flag(store, store.get("devices", "d2"), "duplicate_warning", severity="review")
    seed_device(store, "rej", pt=pt(5), state="rejected")
    seed_device(store, "acc", pt=pt(6), state="accepted")
    seed_device(store, "gone", pt=pt(7), deleted=True, delete_reason="not a device")
    ids = svc.bulk_eligible_ids(DOC)
    assert sorted(ids) == ["d0", "d3"]
    action = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=2)
    assert sorted(action.device_ids) == ["d0", "d3"]
    for i in ("d0", "d3"):
        d = store.get("devices", i)
        assert (d.review_state, d.accept_kind) == ("accepted", "bulk_accepted")
        assert "review_state" in d.human_set
    for i in ("d1", "d2"):  # flagged: untouched
        assert store.get("devices", i).review_state == "proposed"
    assert store.get("devices", "rej").review_state == "rejected"
    assert store.get("devices", "acc").accept_kind == "accepted"  # an already accepted device keeps its kind
    assert store.get("devices", "gone").deleted
    assert svc.counts(DOC)["review"]["awaiting_unflagged"] == 0


def test_bulk_never_includes_ai_only_or_model_disagrees(env):
    store, svc = env
    ai = seed_device(store, "ai1", pt=pt(5), origin="ai")
    seed_flag(store, ai, "ai_only_candidate", severity="info")  # info severity, still never bulk
    seed_flag(store, store.get("devices", "d1"), "model_disagrees", severity="info", ai_category="receptacle",
              ai_subtype="gfi")
    assert sorted(svc.bulk_eligible_ids(DOC)) == ["d0", "d2", "d3"]
    action = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=3)
    assert "ai1" not in action.device_ids and "d1" not in action.device_ids
    assert store.get("devices", "ai1").review_state == "proposed"
    assert store.get("devices", "d1").review_state == "proposed"


def test_info_flags_remain_eligible(env):
    store, svc = env
    seed_flag(store, store.get("devices", "d1"), "not_redetected", severity="info")
    assert "d1" in svc.bulk_eligible_ids(DOC)
    action = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    assert "d1" in action.device_ids
    assert store.get("devices", "d1").accept_kind == "bulk_accepted"
    assert [f.type for f in flags_of(store, "d1")] == ["not_redetected"]  # bulk does not close the info flag


def test_stale_expected_count_accepts_nothing(env):
    store, svc = env
    before = states(store)
    n_actions, n_events = len(store.list("bulk_actions")), len(store.events())
    with pytest.raises(TakeoffError) as e:
        svc.bulk_accept(DOC, BULK_ACTOR, expected_count=3)  # the reviewer saw 3; 4 are eligible now
    assert e.value.code == "bulk_count_changed" and e.value.status == 409
    assert e.value.details == {"expected": 3, "current": 4}
    assert states(store) == before
    assert len(store.list("bulk_actions")) == n_actions and len(store.events()) == n_events
    # a flag that appeared after the confirmation dialog was shown shrinks the set the same way
    seed_flag(store, store.get("devices", "d0"), "classification_conflict", severity="review")
    with pytest.raises(TakeoffError) as e:
        svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    assert e.value.code == "bulk_count_changed" and e.value.details["current"] == 3
    assert states(store) == before


def test_failure_mid_transaction_rolls_back(env, monkeypatch):
    store, svc = env
    before = states(store)
    totals = svc.counts(DOC)
    n_actions, n_events = len(store.list("bulk_actions")), len(store.events())
    real, calls = store.replace, []

    def flaky(table, model, *a, **kw):
        if table == "devices":
            calls.append(model.id)
            if len(calls) == 3:
                raise RuntimeError("disk full")
        return real(table, model, *a, **kw)

    monkeypatch.setattr(store, "replace", flaky)
    with pytest.raises(RuntimeError):
        svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    assert len(calls) == 3  # two devices were written before the failure
    monkeypatch.setattr(store, "replace", real)
    assert states(store) == before
    assert store.list("bulk_actions") == [] and len(store.list("bulk_actions")) == n_actions
    assert len(store.events()) == n_events
    assert svc.counts(DOC) == totals


def test_undo_restores_states_and_keeps_history(env):
    store, svc = env
    bulk = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    assert bulk.kind == "bulk_accept" and bulk.actor == BULK_ACTOR and bulk.sheet_id == ALL_SHEETS
    assert bulk.before["d0"].review_state == "proposed" and bulk.before["d0"].accept_kind is None
    assert svc.counts(DOC)["review"]["official_total"] == 4
    undo = svc.undo_bulk(bulk.id, "undo@example.com")
    assert undo.kind == "undo" and undo.undo_of == bulk.id and undo.actor == "undo@example.com"
    assert sorted(undo.device_ids) == ["d0", "d1", "d2", "d3"] and undo.skipped == []
    for i in range(4):
        d = store.get("devices", f"d{i}")
        assert (d.review_state, d.accept_kind) == ("proposed", None)
        assert "review_state" not in d.human_set
        assert d.revision == 3  # created at 1, bulk accept 2, undo 3: history moves forward, never back
    review = svc.counts(DOC)["review"]
    assert (review["official_total"], review["awaiting_unflagged"]) == (0, 4)
    # nothing was lost: both actions are stored, and each device's event log has the accept and the undo
    assert [b.kind for b in store.list("bulk_actions")] == ["bulk_accept", "undo"]
    assert store.get("bulk_actions", bulk.id) == bulk  # the original is untouched
    assert [e.action for e in store.events("device", "d0")] == ["create", "bulk_accept", "bulk_undo"]
    assert store.events("bulk_action", undo.id)[0].actor == "undo@example.com"


def test_undo_skips_devices_changed_after_bulk_and_reports_them(env):
    store, svc = env
    bulk = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    svc.reject("d1", ACTOR, "wrong")  # changed after the bulk accept
    svc.update_device("d2", ACTOR, {"notes": "checked on site"})  # even a notes-only save bumps the revision
    seed_flag(store, store.get("devices", "d3"), "duplicate_warning", id="cond:d3:duplicate_warning")
    svc.delete("d3", ACTOR, "duplicate")  # a detected device is deleted only as a duplicate (final review I4)
    undo = svc.undo_bulk(bulk.id, ACTOR)
    assert undo.device_ids == ["d0"]
    assert sorted(s["device_id"] for s in undo.skipped) == ["d1", "d2", "d3"]
    skipped = {s["device_id"]: s for s in undo.skipped}
    assert skipped["d1"]["reason"] == "changed_after_bulk"
    assert skipped["d1"]["recorded_revision"] == 2 and skipped["d1"]["current_revision"] == 3
    assert store.get("devices", "d0").review_state == "proposed"
    assert store.get("devices", "d1").review_state == "rejected"  # the later decision is kept
    d2 = store.get("devices", "d2")
    assert (d2.review_state, d2.accept_kind, d2.notes) == ("accepted", "bulk_accepted", "checked on site")
    assert store.get("devices", "d3").deleted
    assert store.get("bulk_actions", undo.id).skipped == undo.skipped  # persisted, not just returned


def test_undo_twice_is_refused(env):
    store, svc = env
    bulk = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    svc.undo_bulk(bulk.id, ACTOR)
    n = len(store.list("bulk_actions"))
    with pytest.raises(TakeoffError) as e:
        svc.undo_bulk(bulk.id, ACTOR)
    assert e.value.code == "invalid"
    assert len(store.list("bulk_actions")) == n


def test_undo_rejects_unknown_and_non_bulk_ids(env):
    store, svc = env
    with pytest.raises(TakeoffError) as e:
        svc.undo_bulk("nope", ACTOR)
    assert e.value.code == "not_found"
    bulk = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    undo = svc.undo_bulk(bulk.id, ACTOR)
    with pytest.raises(TakeoffError) as e:  # an undo cannot itself be undone
        svc.undo_bulk(undo.id, ACTOR)
    assert e.value.code == "invalid"


def test_bulk_count_equals_awaiting_unflagged(env):
    store, svc = env
    seed_flag(store, store.get("devices", "d0"), "ambiguous_match", severity="review")
    seed_flag(store, store.get("devices", "d1"), "not_redetected", severity="info")
    ai = seed_device(store, "ai1", pt=pt(5), origin="ai")
    seed_flag(store, ai, "ai_only_candidate", severity="info")
    seed_device(store, "rej", pt=pt(6), state="rejected")
    review = svc.counts(DOC)["review"]
    assert len(svc.bulk_eligible_ids(DOC)) == review["awaiting_unflagged"] == 3
    assert len(svc.bulk_eligible_ids(DOC, SHEET)) == 3
    svc.bulk_accept(DOC, BULK_ACTOR, expected_count=review["awaiting_unflagged"])
    after = svc.counts(DOC)["review"]
    assert after["awaiting_unflagged"] == 0 and after["awaiting_flagged"] == 2
    assert after["official_total"] == 3


def test_bulk_is_scoped_to_the_document_and_sheet(env):
    store, svc = env
    seed_device(store, "x1", pt=pt(1), document_id=OTHER_DOC)  # another document's proposal, same sheet id
    assert svc.bulk_eligible_ids(DOC) == ["d0", "d1", "d2", "d3"]
    action = svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4, sheet_id=SHEET)
    assert action.sheet_id == SHEET and action.document_id == DOC
    assert store.get("devices", "x1").review_state == "proposed"
    with pytest.raises(TakeoffError) as e:
        svc.bulk_eligible_ids(DOC, "no-such-sheet")
    assert e.value.code == "not_found"
    with pytest.raises(TakeoffError) as e:
        svc.bulk_accept(OTHER_DOC, BULK_ACTOR, expected_count=1, sheet_id=SHEET)  # that document has no such sheet
    assert e.value.code == "not_found"


def test_bulk_refuses_empty_and_malformed_requests(env):
    store, svc = env
    for bad in ("4", None, 4.0, True, -1):
        with pytest.raises(TakeoffError) as e:
            svc.bulk_accept(DOC, BULK_ACTOR, expected_count=bad)
        assert e.value.code == "invalid"
    with pytest.raises(TakeoffError) as e:
        svc.bulk_accept(DOC, "  ", expected_count=4)
    assert e.value.code == "invalid"
    svc.bulk_accept(DOC, BULK_ACTOR, expected_count=4)
    with pytest.raises(TakeoffError) as e:  # nothing left: no empty audit record
        svc.bulk_accept(DOC, BULK_ACTOR, expected_count=0)
    assert e.value.code == "invalid"
    assert len(store.list("bulk_actions")) == 1


def test_bulk_gate_is_the_flags_module_gate(env):
    store, svc = env
    for i in range(4):
        d = store.get("devices", f"d{i}")
        assert (d.id in svc.bulk_eligible_ids(DOC)) == F.bulk_eligible(d, flags_of(store, d.id))
