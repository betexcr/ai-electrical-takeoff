"""Explicit coordinate spaces for plan pages.

Every position the system stores or draws passes through one of these spaces.
Keeping them distinct (and converting through the canonical space) is what keeps
overlay markers registered with the drawing at any zoom, pan, DPI or rotation.

Spaces
------
native     PDF user space as written in the content stream: points, origin at the
           bottom-left, y up, *before* the page's /Rotate is applied.
unrotated  PyMuPDF page space: points, origin at the top-left of the CropBox,
           y down, before /Rotate. ``get_text`` / ``get_drawings`` report here
           (verified by tests/test_coords.py, not assumed).
displayed  The page as a viewer shows it: ``unrotated`` with /Rotate applied
           (clockwise), points, top-left origin. ``page.rect`` is this size.
normalized CANONICAL storage space: ``displayed`` divided by the displayed width
           and height, so (u, v) is in [0, 1] with (0, 0) at the displayed
           top-left. Independent of DPI, zoom and viewport size.
raster     Pixels of a render of the displayed page; continuous coordinates
           where pixel (i, j) covers [i, i+1) x [j, j+1).
pdfjs      PDF.js viewport space (CSS px), reproduced from PDF.js'
           ``PageViewport`` transform so the backend and the viewer agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

VALID_ROTATIONS = (0, 90, 180, 270)


@dataclass(frozen=True)
class PageGeometry:
    """Everything needed to convert between spaces for one page.

    ``crop_x0, crop_y0, crop_x1, crop_y1`` are the CropBox in native PDF space.
    ``rotation`` is the page /Rotate value (clockwise display rotation).
    """

    crop_x0: float
    crop_y0: float
    crop_x1: float
    crop_y1: float
    rotation: int = 0

    def __post_init__(self) -> None:
        if self.rotation not in VALID_ROTATIONS:
            raise ValueError(f"rotation must be one of {VALID_ROTATIONS}, got {self.rotation}")
        if self.crop_x1 <= self.crop_x0 or self.crop_y1 <= self.crop_y0:
            raise ValueError("CropBox must have positive width and height")

    @property
    def unrotated_width(self) -> float:
        return self.crop_x1 - self.crop_x0

    @property
    def unrotated_height(self) -> float:
        return self.crop_y1 - self.crop_y0

    @property
    def displayed_width(self) -> float:
        return self.unrotated_height if self.rotation in (90, 270) else self.unrotated_width

    @property
    def displayed_height(self) -> float:
        return self.unrotated_width if self.rotation in (90, 270) else self.unrotated_height

    @classmethod
    def from_page(cls, page) -> "PageGeometry":
        """Build from a PyMuPDF page without assuming ``page.rect`` is unrotated.

        Reads the raw /CropBox and /MediaBox (with page-tree inheritance) in
        native space. PyMuPDF's convenience values are avoided on purpose:
        ``page.rect`` is the displayed (rotated) size, ``page.cropbox`` is
        y-flipped against the MediaBox, and in PyMuPDF 2.0
        ``page.transformation_matrix`` drops the CropBox offset on rotated pages
        (see tests/test_coords.py::test_pymupdf_transformation_matrix_quirk).
        """
        doc = page.parent
        media = _inherited_box(doc, page.xref, "MediaBox")
        if media is None:
            raise ValueError("page has no MediaBox")
        crop = _inherited_box(doc, page.xref, "CropBox") or media
        # A CropBox larger than the MediaBox is clipped to it (PDF 32000-1, 14.11.2).
        x0, y0 = max(crop[0], media[0]), max(crop[1], media[1])
        x1, y1 = min(crop[2], media[2]), min(crop[3], media[3])
        return cls(x0, y0, x1, y1, int(page.rotation) % 360)


def _inherited_box(doc, xref: int, key: str) -> Rect | None:
    """Read a box key from a page object, following /Parent inheritance."""
    seen = set()
    while xref and xref not in seen:
        seen.add(xref)
        kind, value = doc.xref_get_key(xref, key)
        if kind == "array":
            nums = [float(v) for v in value.strip("[]").split()]
            if len(nums) == 4:
                return (min(nums[0], nums[2]), min(nums[1], nums[3]),
                        max(nums[0], nums[2]), max(nums[1], nums[3]))
        kind, parent = doc.xref_get_key(xref, "Parent")
        xref = int(parent.split()[0]) if kind == "xref" else 0
    return None


# --- native <-> unrotated ---------------------------------------------------

def native_to_unrotated(g: PageGeometry, p: Point) -> Point:
    x, y = p
    return (x - g.crop_x0, g.crop_y1 - y)


def unrotated_to_native(g: PageGeometry, p: Point) -> Point:
    x, y = p
    return (x + g.crop_x0, g.crop_y1 - y)


# --- unrotated <-> displayed -------------------------------------------------

def _rotate_cw(rotation: int, w: float, h: float, p: Point) -> Point:
    """Rotate a point of a w x h top-left-origin page clockwise by ``rotation``."""
    x, y = p
    if rotation == 0:
        return (x, y)
    if rotation == 90:
        return (h - y, x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        return (y, w - x)
    raise ValueError(rotation)


def unrotated_to_displayed(g: PageGeometry, p: Point) -> Point:
    return _rotate_cw(g.rotation, g.unrotated_width, g.unrotated_height, p)


def displayed_to_unrotated(g: PageGeometry, p: Point) -> Point:
    # Inverse of a clockwise rotation r is a clockwise rotation (360 - r)
    # applied to the displayed page, whose size is swapped for 90/270.
    inverse = (360 - g.rotation) % 360
    return _rotate_cw(inverse, g.displayed_width, g.displayed_height, p)


# --- displayed <-> normalized (canonical) ------------------------------------

def displayed_to_normalized(g: PageGeometry, p: Point) -> Point:
    x, y = p
    return (x / g.displayed_width, y / g.displayed_height)


def normalized_to_displayed(g: PageGeometry, p: Point) -> Point:
    u, v = p
    return (u * g.displayed_width, v * g.displayed_height)


# --- convenience compositions through the canonical space --------------------

def unrotated_to_normalized(g: PageGeometry, p: Point) -> Point:
    return displayed_to_normalized(g, unrotated_to_displayed(g, p))


def normalized_to_unrotated(g: PageGeometry, p: Point) -> Point:
    return displayed_to_unrotated(g, normalized_to_displayed(g, p))


def native_to_normalized(g: PageGeometry, p: Point) -> Point:
    return unrotated_to_normalized(g, native_to_unrotated(g, p))


def normalized_to_native(g: PageGeometry, p: Point) -> Point:
    return unrotated_to_native(g, normalized_to_unrotated(g, p))


# --- normalized <-> raster -----------------------------------------------------

def raster_size(g: PageGeometry, dpi: float) -> tuple[int, int]:
    """Nominal pixel size of a render at ``dpi``.

    Prefer the actual image's width/height when converting; PyMuPDF rounds
    the pixmap size, so this may differ from a real render by one pixel.
    """
    zoom = dpi / 72.0
    return (round(g.displayed_width * zoom), round(g.displayed_height * zoom))


def normalized_to_raster(p: Point, width_px: int, height_px: int) -> Point:
    u, v = p
    return (u * width_px, v * height_px)


def raster_to_normalized(p: Point, width_px: int, height_px: int) -> Point:
    x, y = p
    return (x / width_px, y / height_px)


# --- PDF.js viewport -----------------------------------------------------------

def pdfjs_viewport_transform(
    view_box: Rect, scale: float, rotation: int, offset_x: float = 0.0, offset_y: float = 0.0
) -> tuple[float, float, float, float, float, float]:
    """Reproduce PDF.js ``PageViewport.transform`` (dontFlip = false).

    ``view_box`` is the native-space [x0, y0, x1, y1] PDF.js uses (the CropBox)
    and ``rotation`` the *total* viewport rotation (PDF.js defaults it to the
    page /Rotate). Returns the affine [a, b, c, d, e, f] applied as
    ``x' = a*x + c*y + e``, ``y' = b*x + d*y + f`` to native points.
    """
    rotation %= 360
    if rotation not in VALID_ROTATIONS:
        raise ValueError(rotation)
    x0, y0, x1, y1 = view_box
    cx, cy = (x1 + x0) / 2, (y1 + y0) / 2
    ra, rb, rc, rd = {
        180: (-1, 0, 0, 1),
        90: (0, 1, 1, 0),
        270: (0, -1, -1, 0),
        0: (1, 0, 0, -1),
    }[rotation]
    if ra == 0:
        off_cx = abs(cy - y0) * scale + offset_x
        off_cy = abs(cx - x0) * scale + offset_y
    else:
        off_cx = abs(cx - x0) * scale + offset_x
        off_cy = abs(cy - y0) * scale + offset_y
    return (
        ra * scale,
        rb * scale,
        rc * scale,
        rd * scale,
        off_cx - ra * scale * cx - rc * scale * cy,
        off_cy - rb * scale * cx - rd * scale * cy,
    )


def pdfjs_viewport_size(view_box: Rect, scale: float, rotation: int) -> tuple[float, float]:
    x0, y0, x1, y1 = view_box
    if rotation % 180 == 90:
        return ((y1 - y0) * scale, (x1 - x0) * scale)
    return ((x1 - x0) * scale, (y1 - y0) * scale)


def apply_affine(t: tuple[float, float, float, float, float, float], p: Point) -> Point:
    a, b, c, d, e, f = t
    x, y = p
    return (a * x + c * y + e, b * x + d * y + f)


def native_to_pdfjs(g: PageGeometry, p: Point, scale: float, rotation: int | None = None) -> Point:
    rot = g.rotation if rotation is None else rotation
    view_box = (g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1)
    return apply_affine(pdfjs_viewport_transform(view_box, scale, rot), p)


def normalized_to_pdfjs(p: Point, viewport_width: float, viewport_height: float) -> Point:
    """Canonical -> viewer CSS px, valid when the viewport uses the page /Rotate."""
    u, v = p
    return (u * viewport_width, v * viewport_height)


def pdfjs_to_normalized(p: Point, viewport_width: float, viewport_height: float) -> Point:
    x, y = p
    return (x / viewport_width, y / viewport_height)


# --- rectangles ------------------------------------------------------------------

def transform_rect(fn, rect: Rect) -> Rect:
    """Map a rect through a point function; returns the axis-aligned bounds.

    Exact for the 90-degree rotations used here because corners map to corners.
    """
    x0, y0, x1, y1 = rect
    pts = [fn((x0, y0)), fn((x1, y0)), fn((x0, y1)), fn((x1, y1))]
    return bounds(pts)


def bounds(points: Iterable[Point]) -> Rect:
    pts = list(points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def unrotated_rect_to_normalized(g: PageGeometry, rect: Rect) -> Rect:
    return transform_rect(lambda p: unrotated_to_normalized(g, p), rect)


def normalized_rect_to_unrotated(g: PageGeometry, rect: Rect) -> Rect:
    return transform_rect(lambda p: normalized_to_unrotated(g, p), rect)
