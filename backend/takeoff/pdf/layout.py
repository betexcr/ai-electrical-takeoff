"""Sheet layout analysis: text, frame lines and panel-region *hypotheses*.

All coordinates here are PyMuPDF unrotated page space (points, top-left
origin); callers convert to the canonical normalized space with
``takeoff.pdf.coords``. Region boxes are hypotheses produced by a stated
method; they are not ground truth and carry their evidence and warnings.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

import pymupdf

# Observed on the Escondido PAADU sheets: panel separators and view-title boxes
# are stroked at 0.84 pt and the sheet border at 1.56 pt; walls and symbols use
# 0.24/0.48/0.72 pt. These are assumptions about this plan set, recorded in the
# diagnostic output, not universal CAD conventions.
FRAME_STROKE_WIDTHS = (0.84, 1.56)
FRAME_WIDTH_TOLERANCE = 0.02
FRAME_MIN_LENGTH = 300.0
AXIS_TOLERANCE = 0.5

TITLE_MIN_FONT_SIZE = 20.0
WATERMARK_MIN_FONT_SIZE = 100.0
WATERMARK_MIN_ANGLE_DEG = 5.0

# Title text -> region kind. Patterns tolerate observed typos ("MECHNICAL").
TITLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("electrical_plan", re.compile(r"\bELECTRICAL\s+PLAN\b(?!S)")),
    ("mechanical_plan", re.compile(r"\bMECH[A-Z]*CAL\s+PLAN\b")),
    ("finish_plan", re.compile(r"\bFINISH\s+PLAN\b")),
    ("floor_finish_legend", re.compile(r"\bFLOOR\s+FINISH\s+LEGEND\b")),
    ("legend", re.compile(r"^LEGEND$")),
    ("electrical_notes", re.compile(r"\bELECTRICAL\s+NOTES\b")),
    ("general_mep_notes", re.compile(r"\bGENERAL\s+MEP\s+NOTES\b")),
    ("plumbing_notes", re.compile(r"\bPLUMBING\s+NOTES\b")),
    ("keynotes", re.compile(r"^KEYNOTES$")),
    ("ventilation_summaries", re.compile(r"\bVENTILATION\s+SUMMARIES\b")),
]

ROOM_WORDS = re.compile(
    r"^(LIVING|KITCHEN|DINING|BEDROOM|BED|BATH|LNDRY|LAUNDRY|CL|CLOSET|HALL|HALLWAY|"
    r"STUDIO|WALK-IN|OPTIONAL|ENTRY|PORCH|PATIO|PANTRY|OFFICE|DEN|GARAGE|STORAGE|UTILITY)\b"
)
SHORT_TOKEN = re.compile(r"^[A-Z0-9]{1,3}$")


@dataclass
class TextLine:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    font: str
    direction: tuple[float, float]


@dataclass
class Segment:
    """An axis-aligned frame-line candidate."""

    orient: str  # "H" or "V"
    pos: float  # y for H, x for V
    lo: float
    hi: float
    width: float


@dataclass
class RegionHypothesis:
    kind: str
    title_text: str
    title_bbox: tuple[float, float, float, float]
    bbox: tuple[float, float, float, float]
    method: str
    status: str = "hypothesis"
    sides: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def text_lines(page: pymupdf.Page) -> list[TextLine]:
    """Line-level text with the dominant span's size and font."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in line["spans"]).strip()
            main = max(spans, key=lambda s: len(s["text"]))
            out.append(TextLine(text, tuple(line["bbox"]), main["size"], main["font"], tuple(line["dir"])))
    return out


def is_horizontal(line: TextLine) -> bool:
    return abs(line.direction[0] - 1.0) < 1e-3 and abs(line.direction[1]) < 1e-3


def off_axis_degrees(line: TextLine) -> float:
    """Angle between the text direction and the nearest page axis."""
    angle = math.degrees(math.atan2(line.direction[1], line.direction[0])) % 90.0
    return min(angle, 90.0 - angle)


def find_watermarks(lines: list[TextLine]) -> list[TextLine]:
    # Small labels can sit a fraction of a degree off-axis (observed: 0.2 deg),
    # so only very large or clearly diagonal text is treated as a watermark.
    return [l for l in lines if l.size >= WATERMARK_MIN_FONT_SIZE or off_axis_degrees(l) > WATERMARK_MIN_ANGLE_DEG]


def find_titles(lines: list[TextLine]) -> list[tuple[str, TextLine]]:
    out = []
    for l in lines:
        if not is_horizontal(l) or l.size < TITLE_MIN_FONT_SIZE or l.size >= WATERMARK_MIN_FONT_SIZE:
            continue
        for kind, pat in TITLE_PATTERNS:
            if pat.search(l.text.upper()):
                out.append((kind, l))
                break
    return out


def frame_segments(page: pymupdf.Page, drawings=None) -> list[Segment]:
    """Long axis-aligned strokes at the frame widths (see module constants)."""
    segs = []
    for d in drawings if drawings is not None else page.get_drawings():
        w = d.get("width") or 0.0
        if not any(abs(w - fw) <= FRAME_WIDTH_TOLERANCE for fw in FRAME_STROKE_WIDTHS):
            continue
        for item in d["items"]:
            if item[0] == "l":
                pts = [item[1], item[2]]
            elif item[0] == "re":
                r = item[1]
                pts = [r.tl, r.tr, r.br, r.bl, r.tl]
            else:
                continue
            for a, b in zip(pts, pts[1:]):
                if abs(a.x - b.x) <= AXIS_TOLERANCE and abs(a.y - b.y) >= FRAME_MIN_LENGTH:
                    segs.append(Segment("V", (a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y), w))
                elif abs(a.y - b.y) <= AXIS_TOLERANCE and abs(a.x - b.x) >= FRAME_MIN_LENGTH:
                    segs.append(Segment("H", (a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x), w))
    return segs


def ray_cast_cell(segs: list[Segment], px: float, py: float, page_rect) -> tuple[tuple, dict]:
    """Nearest frame line in each direction from (px, py) whose span covers the ray."""
    eps = 1.0
    left = [s.pos for s in segs if s.orient == "V" and s.pos < px - eps and s.lo - eps <= py <= s.hi + eps]
    right = [s.pos for s in segs if s.orient == "V" and s.pos > px + eps and s.lo - eps <= py <= s.hi + eps]
    up = [s.pos for s in segs if s.orient == "H" and s.pos < py - eps and s.lo - eps <= px <= s.hi + eps]
    down = [s.pos for s in segs if s.orient == "H" and s.pos > py + eps and s.lo - eps <= px <= s.hi + eps]
    sides = {
        "left": {"value": max(left), "source": "frame_line"} if left else {"value": page_rect.x0, "source": "page_edge"},
        "right": {"value": min(right), "source": "frame_line"} if right else {"value": page_rect.x1, "source": "page_edge"},
        "top": {"value": max(up), "source": "frame_line"} if up else {"value": page_rect.y0, "source": "page_edge"},
        "bottom": {"value": min(down), "source": "frame_line"} if down else {"value": page_rect.y1, "source": "page_edge"},
    }
    bbox = (sides["left"]["value"], sides["top"]["value"], sides["right"]["value"], sides["bottom"]["value"])
    return bbox, sides


def lines_inside(lines: list[TextLine], bbox) -> list[TextLine]:
    x0, y0, x1, y1 = bbox
    return [l for l in lines if x0 <= (l.bbox[0] + l.bbox[2]) / 2 <= x1 and y0 <= (l.bbox[1] + l.bbox[3]) / 2 <= y1]


def region_hypotheses(page: pymupdf.Page, lines=None, drawings=None) -> list[RegionHypothesis]:
    lines = lines if lines is not None else text_lines(page)
    segs = frame_segments(page, drawings)
    titles = find_titles(lines)
    kinds_seen: dict[str, int] = {}
    for kind, _ in titles:
        kinds_seen[kind] = kinds_seen.get(kind, 0) + 1
    out = []
    # Unrotated page bounds for edge fallbacks (rect of the unrotated page).
    W, H = page.cropbox.width, page.cropbox.height
    bounds = pymupdf.Rect(0, 0, W, H)
    for kind, tl in titles:
        px = (tl.bbox[0] + tl.bbox[2]) / 2
        py = (tl.bbox[1] + tl.bbox[3]) / 2
        bbox, sides = ray_cast_cell(segs, px, py, bounds)
        h = RegionHypothesis(
            kind=kind,
            title_text=tl.text,
            title_bbox=tuple(round(v, 2) for v in tl.bbox),
            bbox=tuple(round(v, 2) for v in bbox),
            method="ray_cast_from_title_centre_to_frame_lines",
            sides=sides,
        )
        for name, side in sides.items():
            if side["source"] != "frame_line":
                h.warnings.append(f"{name} side fell back to the page edge")
        if kinds_seen[kind] > 1:
            h.warnings.append(f"{kinds_seen[kind]} titles of kind {kind} on this sheet")
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area < 0.02 * W * H:
            h.warnings.append(f"region is small ({area / (W * H):.1%} of the sheet)")
        if kind.endswith("_plan"):
            rooms = [l for l in lines_inside(lines, bbox) if ROOM_WORDS.match(l.text.upper())]
            if not rooms:
                h.warnings.append("no room-label text found inside the region")
        out.append(h)
    # Two different titles resolving to the same cell means a missing frame
    # line: the boundary between those panels is unknown.
    for h in out:
        others = [o.kind for o in out if o is not h and o.bbox == h.bbox]
        if others:
            h.warnings.append(f"cell shared with {', '.join(others)} (no frame line between them)")
    return out


def room_label_candidates(lines: list[TextLine], bbox) -> list[TextLine]:
    return [l for l in lines_inside(lines, bbox) if is_horizontal(l) and ROOM_WORDS.match(l.text.upper())]


def short_tokens(page: pymupdf.Page, bbox) -> list[dict]:
    """1-3 character upper-case/digit words inside ``bbox`` (e.g. switch letters, tags)."""
    x0, y0, x1, y1 = bbox
    out = []
    for w in page.get_text("words"):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1 and SHORT_TOKEN.match(w[4]):
            out.append({"text": w[4], "bbox": [round(v, 2) for v in w[:4]]})
    return out
