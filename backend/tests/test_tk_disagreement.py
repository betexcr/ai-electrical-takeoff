"""The ``model_disagrees`` lifecycle, one test per row of spec §5.9.3. The model is never authoritative:
keeping a classification it disagrees with needs a reviewer's reason, and every transition is audited."""

from __future__ import annotations

import pytest

from takeoff.app import flags as F
from takeoff.app.service import TakeoffError
from tk_helpers import ACTOR, DOC, HALL, flags_of, make, seed_device, seed_flag

AI = {"verdict_id": "v-1", "ai_category": "receptacle", "ai_subtype": "gfi", "rationale": "GFI tag next to it",
      "source": "verify"}


@pytest.fixture
def env(tmp_path):
    store, svc = make(tmp_path)
    dev = seed_device(store, "d1")  # switch / standard, proposed, clear Kitchen room
    fl = seed_flag(store, dev, "model_disagrees", **AI)
    yield store, svc, fl
    store.close()


def state(store):
    d = store.get("devices", "d1")
    return d.review_state, d.accept_kind


def test_correction_matching_ai_closes_flag_and_accepts(env):
    store, svc, fl = env
    out = svc.update_device("d1", ACTOR, {"category": "receptacle", "subtype": "gfi"})
    assert out["blocking_flags"] == []
    assert state(store) == ("accepted", "corrected_and_accepted")
    closed = store.get("flags", fl.id)
    assert (closed.open, closed.resolution, closed.resolved_by) == (False, "resolved_by_correction", ACTOR)
    assert closed.resolved_at is not None


def test_correction_matching_ai_category_when_ai_subtype_none_closes_flag(tmp_path):
    store, svc = make(tmp_path)
    dev = seed_device(store, "d1")
    fl = seed_flag(store, dev, "model_disagrees", **{**AI, "ai_subtype": None})
    out = svc.update_device("d1", ACTOR, {"category": "receptacle", "subtype": "gfi_240v"})
    assert out["blocking_flags"] == []
    assert store.get("flags", fl.id).resolution == "resolved_by_correction"
    assert state(store) == ("accepted", "corrected_and_accepted")
    store.close()


def test_conflicting_correction_without_reason_stays_proposed_and_names_flag(env):
    store, svc, fl = env
    # same category as the model but another subtype still conflicts when the model gave a subtype
    out = svc.update_device("d1", ACTOR, {"category": "receptacle", "subtype": "afi_duplex"})
    assert [f.id for f in out["blocking_flags"]] == [fl.id]
    assert out["blocking_flags"][0].type == "model_disagrees"
    assert out["device"].review_state == "proposed"
    saved = store.get("devices", "d1")
    assert (saved.category, saved.subtype) == ("receptacle", "afi_duplex")  # the correction is saved
    assert store.get("flags", fl.id).open
    assert svc.counts(DOC)["review"]["official_total"] == 0


def test_conflicting_correction_with_reason_closes_kept_over_ai_and_accepts(env):
    store, svc, fl = env
    out = svc.update_device("d1", ACTOR, {"subtype": "dimmer"}, reason="legend shows D")
    assert out["blocking_flags"] == []
    assert state(store) == ("accepted", "corrected_and_accepted")
    closed = store.get("flags", fl.id)
    assert (closed.open, closed.resolution) == (False, "kept_over_ai: legend shows D")


def test_accept_without_reason_refused_ai_disagreement_reason_required(env):
    store, svc, fl = env
    for reason in (None, "", "   "):
        with pytest.raises(TakeoffError) as e:
            svc.accept("d1", ACTOR, reason=reason)
        assert (e.value.code, e.value.status) == ("ai_disagreement_reason_required", 409)
        assert [f["id"] for f in e.value.details["blocking_flags"]] == [fl.id]
    assert state(store) == ("proposed", None)
    assert store.get("flags", fl.id).open
    assert [e.action for e in store.events("device", "d1")] == ["create"]  # a refusal writes nothing


def test_accept_with_reason_closes_flag_and_records_audit(env):
    store, svc, fl = env
    dev = svc.accept("d1", ACTOR, reason="legend symbol is a switch")
    assert (dev.review_state, dev.accept_kind) == ("accepted", "accepted")

    flag_ev = store.events("flag", fl.id)[-1]
    assert flag_ev.actor == ACTOR and flag_ev.action == "resolve"
    assert flag_ev.before["open"] is True and flag_ev.after["open"] is False
    assert flag_ev.after["resolution"] == "kept_over_ai: legend symbol is a switch"
    assert flag_ev.after["resolved_by"] == ACTOR
    assert flag_ev.after["details"]["verdict_id"] == "v-1"

    dev_ev = store.events("device", "d1")[-1]
    assert dev_ev.actor == ACTOR and dev_ev.action == "accept"
    assert dev_ev.before["review_state"] == "proposed" and dev_ev.after["review_state"] == "accepted"
    assert svc.counts(DOC)["review"]["official_total"] == 1


def test_acknowledge_requires_reason_and_keeps_device_state(env):
    store, svc, fl = env
    for reason in ("", "  ", None):
        with pytest.raises(TakeoffError) as e:
            svc.acknowledge_flag(fl.id, ACTOR, reason)
        assert (e.value.code, e.value.status) == ("reason_required", 400)
    assert store.get("flags", fl.id).open

    closed = svc.acknowledge_flag(fl.id, ACTOR, "AI misread the GFI tag of the outlet below")
    assert (closed.open, closed.resolution) == (False, "acknowledged: AI misread the GFI tag of the outlet below")
    assert store.get("flags", fl.id) == closed
    assert state(store) == ("proposed", None)
    assert store.get("devices", "d1").revision == 1
    assert [e.action for e in store.events("device", "d1")] == ["create"]

    with pytest.raises(TakeoffError) as e:
        svc.acknowledge_flag(fl.id, ACTOR, "again")
    assert e.value.code == "invalid"  # already closed


def test_acknowledged_device_then_accepts_without_reason(env):
    store, svc, fl = env
    svc.acknowledge_flag(fl.id, ACTOR, "checked against the legend")
    dev = svc.accept("d1", ACTOR)
    assert (dev.review_state, dev.accept_kind) == ("accepted", "accepted")
    assert store.get("flags", fl.id).resolution == "acknowledged: checked against the legend"


def test_reject_closes_flag_without_reason(env):
    store, svc, fl = env
    dev = svc.reject("d1", ACTOR)
    assert dev.review_state == "rejected"
    closed = store.get("flags", fl.id)
    assert (closed.open, closed.resolution, closed.resolved_by) == (False, "rejected_by_reviewer", ACTOR)
    counts = svc.counts(DOC)["review"]
    assert (counts["rejected"], counts["official_total"]) == (1, 0)


def test_open_flag_keeps_device_out_of_bulk(env):
    store, svc, fl = env
    device = store.get("devices", "d1")
    assert F.bulk_eligible(device, flags_of(store, "d1")) is False
    review = svc.counts(DOC)["review"]
    assert (review["awaiting_flagged"], review["awaiting_unflagged"]) == (1, 0)

    svc.acknowledge_flag(fl.id, ACTOR, "checked against the legend")
    assert F.bulk_eligible(store.get("devices", "d1"), flags_of(store, "d1")) is True
    review = svc.counts(DOC)["review"]
    assert (review["awaiting_flagged"], review["awaiting_unflagged"]) == (0, 1)


def test_notes_only_save_keeps_uncertain_disagreement_open(tmp_path):
    store, svc = make(tmp_path)
    dev = seed_device(store, "d1")  # switch / standard
    # an "uncertain" verify answer whose class equals the device's own
    fl = seed_flag(store, dev, "model_disagrees", **{**AI, "ai_category": "switch", "ai_subtype": "standard"})
    for save in ({"notes": "looked at it"}, {"category": "switch", "subtype": "standard"}):
        out = svc.update_device("d1", ACTOR, save)
        assert out["device"].review_state == "proposed"
        assert store.get("flags", fl.id).open  # only a classification change can resolve it by correction
    # a real correction that leaves the classification alone still needs a reason
    out = svc.update_device("d1", ACTOR, {"room": HALL})
    assert [f.id for f in out["blocking_flags"]] == [fl.id]
    assert out["device"].review_state == "proposed" and store.get("flags", fl.id).open
    store.close()


def test_room_only_save_without_reason_names_flag(env):
    store, svc, fl = env
    out = svc.update_device("d1", ACTOR, {"room": HALL})
    assert [f.id for f in out["blocking_flags"]] == [fl.id]
    assert out["device"].review_state == "proposed" and out["device"].room.name == "Hall"
    assert store.get("flags", fl.id).open


def test_room_only_save_with_reason_closes_kept_over_ai(env):
    store, svc, fl = env
    out = svc.update_device("d1", ACTOR, {"room": HALL}, reason="legend symbol is a switch")
    assert out["blocking_flags"] == []
    assert state(store) == ("accepted", "corrected_and_accepted")
    assert store.get("flags", fl.id).resolution == "kept_over_ai: legend symbol is a switch"
