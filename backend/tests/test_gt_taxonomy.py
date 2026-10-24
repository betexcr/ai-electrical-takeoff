"""AC4: taxonomy v1 and ground-truth schema rules."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from pydantic import ValidationError

from takeoff.groundtruth import models as M
from takeoff.groundtruth import taxonomy as T

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "electrical_takeoff_symbols" / "symbol_catalog.csv"


def ann(**kw):
    base = dict(sheet_id="A1-111", page_number=1, category="light", subtype="recessed_downlight",
                point={"u": 0.5, "v": 0.5}, provenance={"origin": "manual"}, created_by="reviewer")
    base.update(kw)
    return M.Annotation(**base)


# --- taxonomy vs the supplied catalogue ----------------------------------------------

def test_every_countable_catalogue_row_maps_to_a_taxonomy_subtype():
    by_file = {s.catalog_file: (c, s.name) for c, subs in T.CATEGORIES.items() for s in subs if s.catalog_file}
    for row in csv.DictReader(open(CATALOG, encoding="utf-8")):
        f = row["filename"]
        if row["subtype"] == "electrical_wiring_run":
            assert f not in by_file  # wiring is never a device
            continue
        assert f in by_file, f
        cat, sub = by_file[f]
        if row["category"] != "reference_only":
            assert (cat, sub) == (row["category"], row["subtype"]), f
    # The fan-coil reference maps to its dedicated outlet, not the HVAC unit.
    assert by_file["fan_coil_unit_dedicated_outlet_REFERENCE.png"] == ("receptacle", "fan_coil_dedicated")


@pytest.mark.skipif(not (ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf").exists(), reason="PDF missing")
def test_legend_labels_match_every_sheet_exactly():
    import pymupdf

    from takeoff.pdf import layout as L
    from takeoff.pdf import symbols as SY

    doc = pymupdf.open(ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf")
    subs = [s for subs in T.CATEGORIES.values() for s in subs if s.in_legend]
    assert all(s.legend_label and s.catalog_file for s in subs)
    for page in doc:
        entries = SY.legend_entries(L.text_lines(page))
        for s in subs:
            (hit,) = SY.find_label(entries, SY.LEGEND_LABELS[s.catalog_file])
            assert hit["display_text"] == s.legend_label, (page.number + 1, s.name)


def test_labels_for_display():
    assert T.label("switch", "dimmer") == "ELECTRICAL SWITCH-DIMMER"
    assert T.label("switch", "three_way") == "THREE WAY (not in legend)"
    assert T.label("light", "other") == "OTHER (describe in notes)"
    assert {s["label"] for s in T.as_dict()["categories"]["detector"]} >= {"SMOKE DETECTOR/ALARM",
                                                                          "COMBINATION SMOKE/CARBON MONOXIDE"}


def test_observed_non_legend_subtypes_are_flagged():
    flags = {s.name: s.in_legend for subs in T.CATEGORIES.values() for s in subs}
    assert flags["three_way"] is False
    assert flags["dimmer"] is True


# --- category / subtype / status rules -------------------------------------------------

def test_valid_annotation_gets_default_status():
    a = ann()
    assert a.status == "as_drawn" and a.review_state == "draft"


def test_unknown_subtype_rejected():
    with pytest.raises(ValidationError, match="not valid"):
        ann(subtype="chandelier")


def test_unknown_category_rejected():
    with pytest.raises(ValidationError, match="unknown device category"):
        ann(category="lamp", subtype="other", notes="x")


@pytest.mark.parametrize("cat", T.NON_DEVICE_CATEGORIES)
def test_wiring_and_reference_only_are_never_devices(cat):
    with pytest.raises(ValidationError, match="not a device"):
        ann(category=cat, subtype="other", notes="x")


def test_ceiling_fan_is_always_optional_pre_wire():
    a = ann(category="ceiling_fan", subtype="optional_pre_wire")
    assert a.status == "optional_pre_wire"
    with pytest.raises(ValidationError, match="must have status"):
        ann(category="ceiling_fan", subtype="optional_pre_wire", status="as_drawn")


def test_optional_pre_wire_not_allowed_elsewhere():
    with pytest.raises(ValidationError, match="only valid for"):
        ann(status="optional_pre_wire")


def test_other_requires_note():
    with pytest.raises(ValidationError, match="requires a note"):
        ann(subtype="other")
    assert ann(subtype="other", notes="circle with 'X', not in legend").subtype == "other"
    with pytest.raises(ValidationError, match="requires a note"):
        ann(category="other_electrical", subtype="other", notes="  ")


# --- shared symbols kept apart by text tokens -----------------------------------------------

@pytest.mark.parametrize("token,category,ok,bad", [
    ("3D", "switch", "three_way_dimmer", "dimmer"),
    ("D", "switch", "dimmer", "standard"),
    ("F", "switch", "fan_switch", "dimmer"),
    ("VS", "switch", "vacancy_sensor", "standard"),
    ("WP", "receptacle", "gfi_weatherproof", "gfi"),
    ("240V", "receptacle", "gfi_240v", "afi_duplex"),
    ("SD", "detector", "smoke_detector", "combined_smoke_co"),
    ("SD/MA", "detector", "combined_smoke_co", "smoke_detector"),
    ("IAQ", "fan", "iaq_fan", "exhaust_fan"),
])
def test_token_conflicts_distinguish_shared_symbols(token, category, ok, bad):
    tok = [{"text": token, "bbox_pt": (0, 0, 5, 5)}]
    assert ann(category=category, subtype=ok, text_tokens=tok).token_conflicts() == []
    assert ann(category=category, subtype=bad, text_tokens=tok).token_conflicts()


def test_gfi_allows_its_family_and_informational_tokens_are_ignored():
    toks = [{"text": "GFI", "bbox_pt": (0, 0, 5, 5)}, {"text": "+42\"", "bbox_pt": (0, 6, 5, 9)}]
    for sub in ("gfi", "gfi_microwave", "gfi_dishwasher", "gfi_240v", "gfi_weatherproof"):
        assert ann(category="receptacle", subtype=sub, text_tokens=toks).token_conflicts() == []
    assert ann(category="receptacle", subtype="afi_duplex", text_tokens=toks).token_conflicts()


def test_token_on_wrong_category_is_flagged():
    tok = [{"text": "GFI", "bbox_pt": (0, 0, 5, 5)}]
    assert "usually belongs" in ann(text_tokens=tok).token_conflicts()[0]


# --- coordinates, provenance, rooms -----------------------------------------------------------

@pytest.mark.parametrize("pt", [{"u": -0.01, "v": 0.5}, {"u": 0.5, "v": 1.01}])
def test_coordinates_must_be_normalized(pt):
    with pytest.raises(ValidationError):
        ann(point=pt)


def test_rect_must_be_ordered():
    with pytest.raises(ValidationError, match="u0 <= u1"):
        M.NormRect(u0=0.6, v0=0.1, u1=0.5, v1=0.2)


def test_provenance_rules():
    M.Provenance(origin="manual")
    M.Provenance(origin="candidate", candidate_ids=["c1"], generator="raster_plain", generator_version="x", score=0.8)
    M.Provenance(origin="blind_scan", observation_id="o1")
    for bad in [dict(origin="manual", candidate_ids=["c1"]), dict(origin="candidate"),
                dict(origin="blind_scan"), dict(origin="blind_scan", observation_id="o", generator="raster_plain")]:
        with pytest.raises(ValidationError):
            M.Provenance(**bad)


def test_labeled_room_must_reference_label():
    with pytest.raises(ValidationError, match="reference its label"):
        M.RoomRef(kind="labeled", name="CL")
    assert M.RoomRef(kind="exterior", name="Exterior").label_bbox_pt is None


def test_blind_scan_rules_on_annotation():
    with pytest.raises(ValidationError, match="added_from_blind_scan"):
        ann(provenance={"origin": "blind_scan", "observation_id": "o1"}, blind_confirmation="confirmed")
    with pytest.raises(ValidationError, match="requires a reason"):
        ann(blind_confirmation="not_seen_in_blind_scan", review_state="verified")
    ann(blind_confirmation="not_seen_in_blind_scan", review_state="verified", blind_decision_reason="re-checked; present")


def test_wiring_run_is_a_separate_model():
    w = M.WiringRun(sheet_id="A1-111", page_number=1, polyline=[{"u": 0.1, "v": 0.1}, {"u": 0.2, "v": 0.2}],
                    provenance={"origin": "manual"}, created_by="r")
    assert w.cable_type == "unknown"
    with pytest.raises(ValidationError):
        M.Annotation(**{**w.model_dump(), "category": "wiring_run", "subtype": "other"})


def test_candidate_records_are_frozen():
    c = M.Candidate(run_id="r", sheet_id="A1-111", page_number=1, class_hypothesis="switch_standard",
                    score=0.8, point={"u": 0.2, "v": 0.3}, point_pt=(518.4, 518.4))
    with pytest.raises(ValidationError):
        c.score = 0.9


def test_candidate_review_and_observation_consistency():
    with pytest.raises(ValidationError):
        M.CandidateReview(candidate_id="c", outcome="accepted")
    with pytest.raises(ValidationError):
        M.CandidateReview(candidate_id="c", outcome="rejected", annotation_id="a")
    with pytest.raises(ValidationError, match="requires a reason"):
        M.BlindObservation(sheet_id="A1-111", point={"u": 0.1, "v": 0.1}, category="switch",
                           created_by="r", reconciliation="dismissed")
    with pytest.raises(ValidationError, match="reference an annotation"):
        M.BlindObservation(sheet_id="A1-111", point={"u": 0.1, "v": 0.1}, category="switch",
                           created_by="r", reconciliation="matched")
    with pytest.raises(ValidationError, match="unknown device category"):
        M.BlindObservation(sheet_id="A1-111", point={"u": 0.1, "v": 0.1}, category="wiring_run", created_by="r")


def test_taxonomy_is_serialisable():
    d = T.as_dict()
    assert d["version"] == T.TAXONOMY_VERSION
    assert "other" in [s["name"] for s in d["categories"]["switch"]]
