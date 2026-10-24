"""AC7: computed warnings fire on constructed cases and not otherwise."""

from __future__ import annotations

import pytest

from gt_helpers import import_candidates, npt, workflow


@pytest.fixture
def wf(tmp_path):
    w = workflow(tmp_path / "gt.sqlite")
    yield w
    w.store.close()


def types(wf):
    return sorted(w["type"] for w in wf.warnings("S1"))


def make(wf, x, y, category="switch", subtype="standard", **kw):
    return wf.create_annotation("S1", "rev", category, subtype, npt(x, y), **kw)


def test_clean_state_has_no_warnings(wf):
    make(wf, 150, 150)
    make(wf, 160, 150)  # 10 pt apart: two switches side by side are fine
    assert types(wf) == []


def test_same_category_within_tolerance_is_possible_duplicate(wf):
    make(wf, 150, 150)
    make(wf, 152, 151)
    assert types(wf) == ["possible_duplicate"]


def test_different_category_within_tolerance_is_combined_or_duplicate(wf):
    make(wf, 150, 150, "ceiling_fan", "optional_pre_wire")
    make(wf, 151, 150, "light", "pendant")
    assert types(wf) == ["possible_combined_or_duplicate"]


def test_just_outside_tolerance_is_quiet(wf):
    make(wf, 150, 150)
    make(wf, 153.5, 150)
    assert types(wf) == []


def test_outside_region(wf):
    make(wf, 600, 150)
    assert types(wf) == ["outside_region"]


def test_token_conflict(wf):
    make(wf, 150, 150, "switch", "dimmer", text_tokens=[{"text": "F", "bbox_pt": (153, 148, 156, 152)}])
    (w,) = wf.warnings("S1")
    assert w["type"] == "token_conflict" and "fan_switch" in w["message"]


def test_shared_token(wf):
    tok = [{"text": "D", "bbox_pt": (153, 148, 156, 152)}]
    make(wf, 150, 150, "switch", "dimmer", text_tokens=tok)
    make(wf, 160, 150, "switch", "dimmer", text_tokens=tok)
    assert types(wf) == ["token_shared"]


def test_orphan_candidate_and_its_resolution(wf):
    import_candidates(wf, "S1", [(250.0, 250.0)])
    assert types(wf) == ["orphan_candidate"]
    (cl,) = wf.clusters("S1")
    wf.accept_cluster("S1", cl["id"], "rev")
    assert types(wf) == []


def test_acknowledgement_is_recorded_and_attached(wf):
    make(wf, 150, 150)
    make(wf, 151, 150)
    (w,) = wf.warnings("S1")
    assert not w["acknowledged"]
    wf.acknowledge_warning("S1", w["id"], "rev", "overprint: two separate devices verified on zoom")
    (w2,) = wf.warnings("S1")
    assert w2["id"] == w["id"] and w2["acknowledged"] and "overprint" in w2["ack_reason"]


def test_resolving_a_warning_removes_it(wf):
    a = make(wf, 150, 150)
    make(wf, 151, 150)
    wf.delete_annotation(a.id, "rev", "overprinted duplicate")
    assert types(wf) == []


def test_progress_counts(wf):
    import_candidates(wf, "S1", [(150.0, 150.0), (350.0, 350.0)])
    make(wf, 200, 200, "light", "pendant")
    p = wf.progress("S1")
    assert p["phase"] == "pass1" and p["cells_total"] == 12 and p["cells_pass1_done"] == 0
    assert p["candidates_unreviewed"] == 2 and p["candidate_clusters"] == 2
    assert p["annotations"] == 1 and p["tallies"] == {"light/pendant": 1}
