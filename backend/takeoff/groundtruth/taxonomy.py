"""Device taxonomy v1 and its validation rules.

Seeded from electrical_takeoff_symbols/symbol_catalog.csv. Subtypes marked
``in_legend=False`` were observed as text tokens on the plans but have no
legend entry (Milestone 1 findings section 1.3); they exist so a reviewer can
record what is drawn instead of forcing it into a legend type.

Many subtypes share one drawn symbol and differ only by an adjacent text token
(switch letters, receptacle tags, SD vs SD/MA, IAQ). ``TOKEN_RULES`` records
which subtypes each token allows, so a linked token that contradicts the
chosen subtype is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass

TAXONOMY_VERSION = "taxonomy/1"

STATUS_AS_DRAWN = "as_drawn"
STATUS_OPTIONAL_PRE_WIRE = "optional_pre_wire"
STATUSES = (STATUS_AS_DRAWN, STATUS_OPTIONAL_PRE_WIRE)

OTHER = "other"  # allowed in every category; requires a note


@dataclass(frozen=True)
class Subtype:
    name: str  # stable id, matches symbol_catalog.csv; used in storage and exports
    in_legend: bool = True
    catalog_file: str | None = None
    # Exact wording of the sheets' legend entry (line wrapping undone). Shown to
    # reviewers; tests/test_gt_taxonomy.py checks it against all four sheets.
    legend_label: str | None = None


CATEGORIES: dict[str, tuple[Subtype, ...]] = {
    "switch": (
        Subtype("standard", catalog_file="switch_standard.png", legend_label="ELECTRICAL SWITCH"),
        Subtype("three_way_dimmer", catalog_file="switch_three_way_dimmer.png",
                legend_label="ELECTRICAL SWITCH-THREE WAY-DIMMER"),
        Subtype("vacancy_sensor", catalog_file="switch_vacancy_sensor.png",
                legend_label="ELECTRICAL SWITCH-VACANCY SENSOR"),
        Subtype("dimmer", catalog_file="switch_dimmer.png", legend_label="ELECTRICAL SWITCH-DIMMER"),
        Subtype("fan_switch", catalog_file="switch_fan.png", legend_label="ELECTRICAL SWITCH-FAN"),
        Subtype("astronomical_time_or_motion_sensor", catalog_file="switch_astronomical_time_or_motion_sensor.png",
                legend_label="ASTRONOMICAL TIME OR MOTION SENSOR SWITCH (AT LEAST 1 REQ.)"),
        # The assessment says "3 = 3-way"; the sheets' legend only shows 3D.
        Subtype("three_way", in_legend=False),
    ),
    "light": (
        Subtype("pendant", catalog_file="light_pendant_high_efficacy.png", legend_label="PENDANT LIGHT HIGH-EFFICACY"),
        Subtype("wall_mounted", catalog_file="light_wall_mounted_high_efficacy.png",
                legend_label="WALL MOUNTED HIGH-EFFICACY LIGHT"),
        Subtype("exterior_wall_photosensor", catalog_file="light_exterior_wall_photosensor.png",
                legend_label="EXTERIOR WALL MOUNTED HIGH-EFFICACY LIGHT W/ PHOTO SENSOR"),
        Subtype("recessed_downlight", catalog_file="light_recessed_downlight.png",
                legend_label="RECESSED HIGH- EFFICACY DOWNLIGHT"),
        Subtype("recessed_vapor_proof_downlight", catalog_file="light_recessed_vapor_proof_downlight.png",
                legend_label="RECESSED HIGH- EFFICACY DOWNLIGHT-VAPOR PROOF"),
    ),
    "ceiling_fan": (
        Subtype("optional_pre_wire", catalog_file="ceiling_fan_optional_pre_wire.png",
                legend_label="CEILING FAN OPTIONAL (PRE WIRE FOR CEILING FAN ONLY)"),
    ),
    "receptacle": (
        Subtype("afi_duplex", catalog_file="receptacle_afi_duplex_arc_fault.png",
                legend_label="DUPLEX OUTLET ARC-FAULT CIRCUIT INTERRUPTER"),
        Subtype("gfi_240v", catalog_file="receptacle_gfi_240v_duplex.png", legend_label="DUPLEX OUTLET 240 VOLTS"),
        Subtype("gfi", catalog_file="receptacle_gfi_duplex.png", legend_label="DUPLEX OUTLET GROUND FAULT INTERRUPTER"),
        Subtype("gfi_weatherproof", catalog_file="receptacle_gfi_weatherproof_duplex.png",
                legend_label="DUPLEX OUTLET WATERPROOF GROUND FAULT INTERRUPTER"),
        Subtype("afci_half_hot", catalog_file="receptacle_afci_half_hot_duplex.png",
                legend_label="DUPLEX OUTLET AFCI-HALF HOT"),
        Subtype("gfi_microwave", catalog_file="receptacle_gfi_microwave_duplex.png",
                legend_label="DUPLEX OUTLET MICROWAVE GROUND FAULT INTERRUPTER"),
        Subtype("gfi_dishwasher", catalog_file="receptacle_gfi_dishwasher_duplex.png",
                legend_label="DUPLEX OUTLET DISH WASHER GROUND FAULT INTERRUPTER"),
        Subtype("range_hood", catalog_file="receptacle_range_hood_duplex.png", legend_label="DUPLEX OUTLET RANGE HOOD"),
        # Catalogue: "count the dedicated outlet where shown" for the fan-coil unit.
        Subtype("fan_coil_dedicated", catalog_file="fan_coil_unit_dedicated_outlet_REFERENCE.png",
                legend_label='FAN COIL UNIT +80" TO BOTTOM OF UNIT PROVIDE DEDICATED OUTLET'),
    ),
    "fan": (
        Subtype("exhaust_fan", catalog_file="fan_exhaust.png", legend_label="EXHAUST FAN"),
        Subtype("iaq_fan", catalog_file="fan_indoor_air_quality_iaq.png", legend_label="INDOOR AIR QUALITY FAN"),
    ),
    "detector": (
        Subtype("smoke_detector", catalog_file="detector_smoke.png", legend_label="SMOKE DETECTOR/ALARM"),
        Subtype("combined_smoke_co", catalog_file="detector_combined_smoke_carbon_monoxide.png",
                legend_label="COMBINATION SMOKE/CARBON MONOXIDE"),
    ),
    "junction_box": (
        Subtype("electrical_junction_box", catalog_file="junction_box_electrical.png",
                legend_label="ELECTRICAL JUNCTION BOX"),
    ),
    # Electrical devices drawn on the plan but absent from the legend.
    "other_electrical": (),
}

# Never device annotations: wiring is a WiringRun; reference_only crops are
# not components (catalogue handling notes).
NON_DEVICE_CATEGORIES = ("wiring_run", "reference_only")

# Default status by category; ceiling fans are always optional pre-wire
# locations (README: the symbol is not proof that a fan is installed).
REQUIRED_STATUS = {"ceiling_fan": STATUS_OPTIONAL_PRE_WIRE}

# Token text -> {category: allowed subtypes}. Tokens not listed (e.g. "+42"",
# keynote tags) carry no subtype meaning.
_GFI_FAMILY = ("gfi", "gfi_240v", "gfi_weatherproof", "gfi_microwave", "gfi_dishwasher")
TOKEN_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "3D": {"switch": ("three_way_dimmer",)},
    "3": {"switch": ("three_way",)},
    "D": {"switch": ("dimmer",)},
    "F": {"switch": ("fan_switch",)},
    "VS": {"switch": ("vacancy_sensor",)},
    "A": {"switch": ("astronomical_time_or_motion_sensor",)},
    "AFI": {"receptacle": ("afi_duplex",)},
    "GFI": {"receptacle": _GFI_FAMILY},
    "WP": {"receptacle": ("gfi_weatherproof",)},
    "240V": {"receptacle": ("gfi_240v",)},
    "MW": {"receptacle": ("gfi_microwave",)},
    "DW": {"receptacle": ("gfi_dishwasher",)},
    "H": {"receptacle": ("range_hood",)},
    "FC": {"receptacle": ("fan_coil_dedicated",)},
    "SD": {"detector": ("smoke_detector",)},
    "SD/MA": {"detector": ("combined_smoke_co",)},
    "IAQ": {"fan": ("iaq_fan",)},
    "P": {"light": ("pendant",)},
    "VP": {"light": ("recessed_vapor_proof_downlight",)},
    "EXT": {"light": ("exterior_wall_photosensor",)},
}


class TaxonomyError(ValueError):
    pass


def subtypes(category: str) -> tuple[str, ...]:
    if category not in CATEGORIES:
        raise TaxonomyError(f"unknown device category {category!r}")
    return tuple(s.name for s in CATEGORIES[category]) + (OTHER,)


def default_status(category: str) -> str:
    return REQUIRED_STATUS.get(category, STATUS_AS_DRAWN)


def validate_device(category: str, subtype: str, status: str, notes: str | None) -> None:
    """Raise TaxonomyError if the combination is not a valid device record."""
    if category in NON_DEVICE_CATEGORIES:
        raise TaxonomyError(f"{category!r} is not a device; wiring runs and reference-only items are stored separately")
    allowed = subtypes(category)
    if subtype not in allowed:
        raise TaxonomyError(f"subtype {subtype!r} is not valid for {category!r}; allowed: {allowed}")
    if subtype == OTHER and not (notes and notes.strip()):
        raise TaxonomyError("subtype 'other' requires a note describing what is drawn")
    if category == "other_electrical" and not (notes and notes.strip()):
        raise TaxonomyError("category 'other_electrical' requires a note")
    if status not in STATUSES:
        raise TaxonomyError(f"unknown status {status!r}")
    required = REQUIRED_STATUS.get(category)
    if required and status != required:
        raise TaxonomyError(f"{category!r} must have status {required!r}")
    if not required and status != STATUS_AS_DRAWN:
        raise TaxonomyError(f"status {status!r} is only valid for {sorted(REQUIRED_STATUS)}")


def token_conflicts(category: str, subtype: str, tokens: list[str]) -> list[str]:
    """Messages for linked tokens that imply a different subtype (warnings, not errors)."""
    out = []
    for t in tokens:
        rule = TOKEN_RULES.get(t.strip().upper())
        if not rule:
            continue
        if category not in rule:
            out.append(f"token {t!r} usually belongs to {sorted(rule)}, not {category!r}")
        elif subtype != OTHER and subtype not in rule[category]:
            out.append(f"token {t!r} implies {category} subtype in {rule[category]}, got {subtype!r}")
    return out


def label(category: str, subtype: str) -> str:
    """Reviewer-facing name: the exact legend wording, or a plain description."""
    if subtype == OTHER:
        return "OTHER (describe in notes)"
    for s in CATEGORIES.get(category, ()):
        if s.name == subtype:
            return s.legend_label or f"{subtype.replace('_', ' ').upper()} (not in legend)"
    return subtype


def as_dict() -> dict:
    """Serialisable taxonomy for the reviewer UI and export manifests."""
    return {
        "version": TAXONOMY_VERSION,
        "categories": {c: [{"name": s.name, "label": label(c, s.name), "in_legend": s.in_legend,
                            "catalog_file": s.catalog_file} for s in subs]
                       + [{"name": OTHER, "label": label(c, OTHER), "in_legend": False, "catalog_file": None}]
                       for c, subs in CATEGORIES.items()},
        "statuses": list(STATUSES),
        "required_status": REQUIRED_STATUS,
        "non_device_categories": list(NON_DEVICE_CATEGORIES),
        "token_rules": {k: {c: list(v) for c, v in r.items()} for k, r in TOKEN_RULES.items()},
    }
