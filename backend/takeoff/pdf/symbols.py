"""Symbol-matching primitives shared by the feasibility probe and the
ground-truth candidate exporter (moved verbatim from tools/symbol_probe.py
0.1.0 so both use one implementation).

Every output of these functions is a CANDIDATE LOCATION, never a detection
or a count.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

import cv2
import numpy as np
import pymupdf

from takeoff.pdf import layout as L

PRIMITIVES_VERSION = "symbols/1"  # bump when any matching primitive changes behaviour
MATCH_DPI = 300
ZOOM = MATCH_DPI / 72.0

# Hairline (0.24 pt ~ 1 px at 300 dpi) symbols make NCC sensitive to sub-pixel
# offsets; an ad-hoc test raised ceiling-fan hub scores (sheet 1: 0.40 -> 0.61)
# after eroding (darkening/thickening) both image and template with this kernel.
THICKEN_KERNEL = np.ones((5, 5), np.uint8)

# Reference file -> exact legend label (whitespace-normalised, upper case) as
# printed on the sheets. Read off the sheet text, not invented.
LEGEND_LABELS = {
    "switch_standard.png": "ELECTRICAL SWITCH",
    "switch_three_way_dimmer.png": "ELECTRICAL SWITCH-THREE WAY-DIMMER",
    "switch_vacancy_sensor.png": "ELECTRICAL SWITCH- VACANCY SENSOR",
    "switch_dimmer.png": "ELECTRICAL SWITCH-DIMMER",
    "switch_fan.png": "ELECTRICAL SWITCH-FAN",
    "switch_astronomical_time_or_motion_sensor.png": "ASTRONOMICAL TIME OR MOTION SENSOR SWITCH (AT LEAST 1 REQ.)",
    "light_pendant_high_efficacy.png": "PENDANT LIGHT HIGH-EFFICACY",
    "light_wall_mounted_high_efficacy.png": "WALL MOUNTED HIGH-EFFICACY LIGHT",
    "light_exterior_wall_photosensor.png": "EXTERIOR WALL MOUNTED HIGH- EFFICACY LIGHT W/ PHOTO SENSOR",
    "light_recessed_downlight.png": "RECESSED HIGH- EFFICACY DOWNLIGHT",
    "light_recessed_vapor_proof_downlight.png": "RECESSED HIGH- EFFICACY DOWNLIGHT-VAPOR PROOF",
    "ceiling_fan_optional_pre_wire.png": "CEILING FAN OPTIONAL (PRE WIRE FOR CEILING FAN ONLY)",
    "receptacle_afi_duplex_arc_fault.png": "DUPLEX OUTLET ARC-FAULT CIRCUIT INTERRUPTER",
    "receptacle_gfi_240v_duplex.png": "DUPLEX OUTLET 240 VOLTS",
    "receptacle_gfi_duplex.png": "DUPLEX OUTLET GROUND FAULT INTERRUPTER",
    "receptacle_gfi_weatherproof_duplex.png": "DUPLEX OUTLET WATERPROOF GROUND FAULT INTERRUPTER",
    "receptacle_afci_half_hot_duplex.png": "DUPLEX OUTLET AFCI-HALF HOT",
    "receptacle_gfi_microwave_duplex.png": "DUPLEX OUTLET MICROWAVE GROUND FAULT INTERRUPTER",
    "receptacle_gfi_dishwasher_duplex.png": "DUPLEX OUTLET DISH WASHER GROUND FAULT INTERRUPTER",
    "receptacle_range_hood_duplex.png": "DUPLEX OUTLET RANGE HOOD",
    "fan_exhaust.png": "EXHAUST FAN",
    "fan_indoor_air_quality_iaq.png": "INDOOR AIR QUALITY FAN",
    "detector_smoke.png": "SMOKE DETECTOR/ALARM",
    "detector_combined_smoke_carbon_monoxide.png": "COMBINATION SMOKE/CARBON MONOXIDE",
    "junction_box_electrical.png": "ELECTRICAL JUNCTION BOX",
    "wiring_run_electrical_REFERENCE_BOM.png": "ELECTRICAL WIRING",
    "fan_coil_unit_dedicated_outlet_REFERENCE.png": 'FAN COIL UNIT +80" TO BOTTOM OF UNIT PROVIDE DEDICATED OUTLET',
}

# Where the icon sits relative to its label block: (side, extent, lateral margin)
# in points. Default: left. The ceiling-fan icon is ~117 pt tall (measured from
# the reference crop at ~1.48 px/pt); a first run with an 80 pt window truncated it.
ICON_WINDOWS = {
    "default": ("left", 34.0, 0.0),
    # Lateral margin kept small: 40 pt pulled in the next legend column's receptacle icons.
    "ceiling_fan_optional_pre_wire.png": ("above", 135.0, 8.0),
    "wiring_run_electrical_REFERENCE_BOM.png": ("above", 50.0, 10.0),
    "fan_coil_unit_dedicated_outlet_REFERENCE.png": ("left", 40.0, 0.0),
}

# Classes used for the matching experiments (one representative per symbol shape).
PROBE_CLASSES = [
    "switch_standard.png",
    "receptacle_gfi_duplex.png",
    "light_recessed_downlight.png",
    "light_pendant_high_efficacy.png",
    "light_wall_mounted_high_efficacy.png",
    "ceiling_fan_optional_pre_wire.png",
    "fan_exhaust.png",
    "detector_smoke.png",
    "junction_box_electrical.png",
]


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper()


def legend_entries(lines: list[L.TextLine]) -> list[dict]:
    """Group 9-10 pt horizontal lines into label blocks (same x0, small vertical gaps)."""
    cand = sorted([l for l in lines if L.is_horizontal(l) and 9.0 <= l.size <= 10.0],
                  key=lambda l: (round(l.bbox[0]), l.bbox[1]))
    blocks: list[list[L.TextLine]] = []
    for l in cand:
        b = blocks[-1] if blocks else None
        if b and abs(b[-1].bbox[0] - l.bbox[0]) < 1.5 and 0 <= l.bbox[1] - b[-1].bbox[3] < 3.0:
            b.append(l)
        else:
            blocks.append([l])
    out = []
    for b in blocks:
        x0 = min(l.bbox[0] for l in b); y0 = min(l.bbox[1] for l in b)
        x1 = max(l.bbox[2] for l in b); y1 = max(l.bbox[3] for l in b)
        # display_text undoes line wrapping: a line ending in "-" continues the
        # word on the next line ("HIGH-" + "EFFICACY" -> "HIGH-EFFICACY").
        display = ""
        for l in b:
            display += ("" if not display or display.endswith("-") else " ") + l.text.strip()
        out.append({"text": norm_text(" ".join(l.text for l in b)), "display_text": norm_text(display),
                    "bbox": (x0, y0, x1, y1)})
    return out


def find_label(entries, label: str):
    hits = [e for e in entries if e["text"] == norm_text(label)]
    return hits


def icon_window(fname: str, label_bbox) -> tuple[float, float, float, float]:
    side, extent, margin = ICON_WINDOWS.get(fname, ICON_WINDOWS["default"])
    x0, y0, x1, y1 = label_bbox
    if side == "left":
        return (x0 - extent, y0 - 8.0, x0 - 1.5, y1 + 2.0)
    return (x0 - margin, y0 - extent, x1 + margin, y0 - 1.0)


def ink_bbox_in(page, win, drawings, include_text=True) -> tuple | None:
    """Tight bbox of vector strokes (and optionally glyphs) inside ``win`` (points)."""
    xs, ys = [], []
    for d in drawings:
        r = d["rect"]
        if win[0] <= r.x0 and r.x1 <= win[2] and win[1] <= r.y0 and r.y1 <= win[3]:
            xs += [r.x0, r.x1]; ys += [r.y0, r.y1]
    for w in page.get_text("words") if include_text else []:
        if win[0] <= w[0] and w[2] <= win[2] and win[1] <= w[1] and w[3] <= win[3]:
            xs += [w[0], w[2]]; ys += [w[1], w[3]]
    if not xs:
        return None
    return (min(xs) - 0.6, min(ys) - 0.6, max(xs) + 0.6, max(ys) + 0.6)


def render_gray(page, clip, dpi=MATCH_DPI) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(clip), colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def peaks(score: np.ndarray, thr: float, tw: int, th: int) -> list[tuple[int, int, float]]:
    k = max(3, (min(tw, th) // 2) | 1)
    dil = cv2.dilate(score, np.ones((k, k), np.uint8))
    ys, xs = np.nonzero((score >= thr) & (score >= dil - 1e-7))
    return [(int(x), int(y), float(score[y, x])) for x, y in zip(xs, ys)]


def nms(cands: list[dict], radius: float) -> list[dict]:
    out = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if all(math.hypot(c["cx"] - o["cx"], c["cy"] - o["cy"]) > radius for o in out):
            out.append(c)
    return out


def match_template(img: np.ndarray, tmpl: np.ndarray, min_thr: float) -> list[dict]:
    cands = []
    for k in range(4):
        t = np.ascontiguousarray(np.rot90(tmpl, k))
        th, tw = t.shape
        if th >= img.shape[0] or tw >= img.shape[1] or t.std() < 1e-6:
            continue
        score = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        for x, y, s in peaks(score, min_thr, tw, th):
            cands.append({"cx": x + tw / 2, "cy": y + th / 2, "w": tw, "h": th, "score": s, "rot": 90 * k})
    r = 0.5 * min(tmpl.shape)
    return nms(cands, r)


def segments_in(drawings, bbox) -> list[tuple[float, float, float, float, float]]:
    """(x1, y1, x2, y2, length) for strokes fully inside bbox; curves by chord + polygon length."""
    segs = []
    for d in drawings:
        r = d["rect"]
        if not (bbox[0] <= r.x0 and r.x1 <= bbox[2] and bbox[1] <= r.y0 and r.y1 <= bbox[3]):
            continue
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                segs.append((a.x, a.y, b.x, b.y, abs(b - a)))
            elif it[0] == "c":
                p = it[1:5]
                ln = sum(abs(p[i + 1] - p[i]) for i in range(3))
                segs.append((p[0].x, p[0].y, p[3].x, p[3].y, ln))
            elif it[0] == "re":
                q = it[1]
                pts = [q.tl, q.tr, q.br, q.bl, q.tl]
                segs += [(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y, abs(pts[i + 1] - pts[i])) for i in range(4)]
            elif it[0] == "qu":
                q = it[1]
                pts = [q.ul, q.ur, q.lr, q.ll, q.ul]
                segs += [(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y, abs(pts[i + 1] - pts[i])) for i in range(4)]
    return segs


def dedupe_segments(segs) -> list:
    """Drop exactly overprinted segments (observed: 21-36% of paths in the
    electrical regions are exact duplicates). Endpoint order is ignored."""
    seen, out = set(), []
    for s in segs:
        a, b = (round(s[0], 2), round(s[1], 2)), (round(s[2], 2), round(s[3], 2))
        key = (min(a, b), max(a, b), round(s[4], 2))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def components(segs, eps=0.3) -> list[dict]:
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid = defaultdict(list)
    for i, s in enumerate(segs):
        for (x, y) in ((s[0], s[1]), (s[2], s[3])):
            gx, gy = int(math.floor(x / eps)), int(math.floor(y / eps))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j, (ox, oy) in grid.get((gx + dx, gy + dy), []):
                        if math.hypot(ox - x, oy - y) <= eps:
                            parent[find(i)] = find(j)
            grid[(gx, gy)].append((i, (x, y)))
    groups = defaultdict(list)
    for i in range(len(segs)):
        groups[find(i)].append(segs[i])
    out = []
    for g in groups.values():
        xs = [v for s in g for v in (s[0], s[2])]
        ys = [v for s in g for v in (s[1], s[3])]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        out.append({"n": len(g), "length": sum(s[4] for s in g), "dims": sorted((w, h)),
                    "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def signature_matches(sig: dict, comps: list[dict]) -> list[dict]:
    hits = []
    for c in comps:
        if abs(c["n"] - sig["n"]) > max(1, 0.1 * sig["n"]):
            continue
        if abs(c["dims"][0] - sig["dims"][0]) > 0.6 or abs(c["dims"][1] - sig["dims"][1]) > 0.6:
            continue
        if abs(c["length"] - sig["length"]) > 0.08 * sig["length"]:
            continue
        hits.append(c)
    return hits
