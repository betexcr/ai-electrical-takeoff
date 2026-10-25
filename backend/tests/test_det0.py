"""det/0 rules on synthetic proposals and tokens (AC-D1, AC-D3)."""

from __future__ import annotations

import random

from takeoff.detection import det0 as D

SID = "A1-111"


def prop(i, cls, x, y, gen="raster_thickened", score=0.8):
    cat, sub = D.class_hint(cls + ".png")
    return D.Proposal(id=f"p{i}", generator=gen, sheet_id=SID, class_hypothesis=cls, category=cat, subtype_hint=sub,
                      score=None if gen == "vector_signature" else score, x_pt=x, y_pt=y)


def tok(text, x, y):
    return D.Token(sheet_id=SID, text=text, x_pt=x, y_pt=y)


def detect(props, toks=()):
    return D.detect_sheet(list(props), list(toks))


def test_templates_of_one_category_merge_into_one_device():
    ds = detect([prop(1, "light_recessed_downlight", 100, 100, score=0.84), prop(2, "light_pendant_high_efficacy", 100.1, 100),
                 prop(3, "light_wall_mounted_high_efficacy", 100.9, 100.2), prop(4, "light_pendant_high_efficacy", 100, 100,
                                                                              gen="vector_signature")])
    assert len(ds) == 1 and ds[0].category == "light"
    assert sorted(m["id"] for m in ds[0].members) == ["p1", "p2", "p3", "p4"]


def test_ganged_switches_stay_separate():
    ds = detect([prop(1, "switch_standard", 100, 100), prop(2, "switch_standard", 107.9, 100)])
    assert len(ds) == 2


def test_category_priority_resolves_shared_sites():
    ds = detect([prop(1, "light_pendant_high_efficacy", 50, 50, gen="vector_signature"), prop(2, "fan_exhaust", 50.5, 50)])
    assert [d.category for d in ds] == ["fan"]
    ds = detect([prop(1, "fan_exhaust", 0, 0), prop(2, "light_pendant_high_efficacy", 0, 0),
                 prop(3, "ceiling_fan_optional_pre_wire", 0.1, 0)])
    assert [(d.category, d.subtype, d.status) for d in ds] == [("ceiling_fan", "optional_pre_wire", "optional_pre_wire")]
    ds = detect([prop(1, "junction_box_electrical", 10, 10), prop(2, "receptacle_gfi_duplex", 11, 10)])
    assert [d.category for d in ds] == ["receptacle"]


def test_lone_junction_box_needs_a_j_token():
    assert detect([prop(1, "junction_box_electrical", 10, 10)]) == []
    ds = detect([prop(1, "junction_box_electrical", 10, 10)], [tok("J", 11, 10)])
    assert [(d.category, d.subtype) for d in ds] == [("junction_box", "electrical_junction_box")]
    assert detect([prop(1, "junction_box_electrical", 10, 10)], [tok("J", 13, 10)]) == []


def test_point_comes_from_the_vector_proposal_when_present():
    (d,) = detect([prop(1, "receptacle_gfi_duplex", 10, 10, score=0.95), prop(2, "receptacle_gfi_duplex", 12, 10, gen="vector_signature")])
    assert (d.x_pt, d.y_pt) == (12, 10)


def test_switch_subtype_from_the_nearest_token():
    ds = detect([prop(1, "switch_standard", 100, 100), prop(2, "switch_standard", 108, 100), prop(3, "switch_standard", 300, 300)],
                [tok("D", 100, 89), tok("F", 108, 89), tok("VS", 300, 316)])  # VS beyond 15 pt
    by_x = {d.x_pt: d for d in ds}
    assert (by_x[100].subtype, by_x[108].subtype, by_x[300].subtype) == ("dimmer", "fan_switch", "standard")
    assert by_x[100].subtype_source == "token" and by_x[300].subtype_source == "default"
    assert by_x[100].tokens == ["D"]


def test_receptacle_tokens_intersect_and_each_token_goes_to_one_device():
    ds = detect([prop(1, "receptacle_gfi_duplex", 0, 0), prop(2, "receptacle_gfi_duplex", 0, 40), prop(3, "receptacle_gfi_duplex", 0, 80),
                 prop(4, "receptacle_gfi_duplex", 0, 120)],
                [tok("MW", 10, -4), tok("GFI", 10, 4), tok("GFI", 10, 40), tok("240V", 10, 80), tok("AFI", 10, 110)])
    by_y = {d.y_pt: d.subtype for d in ds}
    assert by_y == {0: "gfi_microwave", 40: "gfi", 80: "gfi_240v", 120: "afi_duplex"}
    # the AFI token is nearer to the 120 receptacle than to the 80 one, so only that one takes it
    two = detect([prop(1, "receptacle_gfi_duplex", 0, 0), prop(2, "receptacle_gfi_duplex", 0, 14)], [tok("AFI", 9, 10)])
    assert {d.y_pt: d.subtype_source for d in two} == {0: "default", 14: "token"}


def test_light_subtype_from_the_best_raster_template_unless_a_token_says_otherwise():
    (d,) = detect([prop(1, "light_recessed_downlight", 0, 0, score=0.84), prop(2, "light_pendant_high_efficacy", 0, 0, score=0.69)])
    assert (d.subtype, d.subtype_source) == ("recessed_downlight", "template")
    (d,) = detect([prop(1, "light_pendant_high_efficacy", 0, 0, score=0.79), prop(2, "light_wall_mounted_high_efficacy", 0, 0, score=0.7)],
                  [tok("EXT", 0, 9)])
    assert (d.subtype, d.subtype_source) == ("exterior_wall_photosensor", "token")


def test_fan_and_detector_subtypes():
    ds = detect([prop(1, "fan_exhaust", 0, 0), prop(2, "fan_exhaust", 200, 0), prop(3, "detector_smoke", 400, 0),
                 prop(4, "detector_smoke", 600, 0)], [tok("IAQ", 210, 0), tok("SD/MA", 610, 0)])
    assert [d.subtype for d in sorted(ds, key=lambda d: d.x_pt)] == ["exhaust_fan", "iaq_fan", "smoke_detector", "combined_smoke_co"]


def test_output_is_deterministic_and_traceable():
    rng = random.Random(3)
    props = [prop(i, rng.choice(["switch_standard", "receptacle_gfi_duplex", "light_recessed_downlight", "fan_exhaust"]),
                  rng.uniform(0, 300), rng.uniform(0, 300), gen=rng.choice(["raster_thickened", "vector_signature"]),
                  score=round(rng.uniform(0.6, 0.9), 3)) for i in range(60)]
    toks = [tok(rng.choice(["D", "GFI", "AFI", "IAQ"]), rng.uniform(0, 300), rng.uniform(0, 300)) for _ in range(20)]
    a = detect(props, toks)
    b = detect(rng.sample(props, len(props)), rng.sample(toks, len(toks)))
    assert a == b
    for d in a:
        assert d.members and d.subtype_source in ("token", "template", "default", "fixed")
        assert {m["generator"] for m in d.members} <= {"raster_thickened", "vector_signature"}


def test_only_the_configured_generators_feed_proposals():
    assert D.PARAMS["proposal_generators"] == ["raster_thickened", "vector_signature"]
    ds = detect([prop(1, "switch_standard", 0, 0, gen="raster_plain")])
    assert ds == []
