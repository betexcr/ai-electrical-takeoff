"""Coordinate-space tests.

Synthetic pages are built in memory with raw /CropBox, /Rotate and content
streams, so the tests control native PDF space directly rather than relying on
PyMuPDF's conventions. The original plan PDF is never written.
"""

from __future__ import annotations

import itertools

import numpy as np
import pymupdf
import pytest

from takeoff.pdf import coords as c

ROTATIONS = c.VALID_ROTATIONS
DPIS = (72, 150, 300)
# Deliberately asymmetric so a wrong rotation direction cannot pass by symmetry.
MEDIA = (0.0, 0.0, 200.0, 100.0)
# Asymmetric in y too, so a y-flipped CropBox reading is caught.
CROP_OFFSET = (10.0, 8.0, 190.0, 95.0)
MARKER_NATIVE = (47.0, 71.0)


def make_page(rotation: int, crop=None, media=MEDIA, markers=(MARKER_NATIVE,), half=3.0):
    """In-memory one-page PDF with filled squares centred on native points."""
    doc = pymupdf.open()
    page = doc.new_page(width=media[2] - media[0], height=media[3] - media[1])
    doc.xref_set_key(page.xref, "MediaBox", "[%g %g %g %g]" % media)
    if crop is not None:
        doc.xref_set_key(page.xref, "CropBox", "[%g %g %g %g]" % crop)
    ops = ["0 0 0 rg"]
    for x, y in markers:
        ops.append(f"{x - half} {y - half} {2 * half} {2 * half} re f")
    content_xref = doc.get_new_xref()
    doc.update_object(content_xref, "<<>>")
    doc.update_stream(content_xref, "\n".join(ops).encode())
    doc.xref_set_key(page.xref, "Contents", f"{content_xref} 0 R")
    doc.xref_set_key(page.xref, "Rotate", str(rotation))
    return doc, doc[0]


def dark_centroid(pix: pymupdf.Pixmap) -> tuple[float, float]:
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    ys, xs = np.nonzero(img[:, :, 0] < 128)
    assert len(xs) > 0, "marker not rendered"
    return (xs.mean() + 0.5, ys.mean() + 0.5)


# --- geometry construction -----------------------------------------------------

@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("crop", [None, CROP_OFFSET])
def test_from_page_does_not_assume_rect_is_unrotated(rotation, crop):
    doc, page = make_page(rotation, crop)
    g = c.PageGeometry.from_page(page)
    box = crop or MEDIA
    assert (g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1) == pytest.approx(box)
    assert g.rotation == rotation
    # page.rect is the *displayed* rectangle; check that our sizes agree with it.
    assert g.displayed_width == pytest.approx(page.rect.width)
    assert g.displayed_height == pytest.approx(page.rect.height)
    if rotation in (90, 270):
        assert page.rect.width == pytest.approx(box[3] - box[1])


def test_invalid_rotation_rejected():
    with pytest.raises(ValueError):
        c.PageGeometry(0, 0, 10, 10, rotation=45)


# --- agreement with PyMuPDF's own matrices -------------------------------------

@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("crop", [None, CROP_OFFSET])
def test_matches_pymupdf_rotation_matrix(rotation, crop):
    doc, page = make_page(rotation, crop)
    g = c.PageGeometry.from_page(page)
    for p in [(0, 0), (12.5, 33.0), (150.0, 80.0), MARKER_NATIVE]:
        unrot = c.native_to_unrotated(g, p)
        disp = c.unrotated_to_displayed(g, unrot)
        md = pymupdf.Point(unrot) * page.rotation_matrix
        assert disp == pytest.approx((md.x, md.y))


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_matches_pymupdf_transformation_matrix_without_crop_offset(rotation):
    doc, page = make_page(rotation)
    g = c.PageGeometry.from_page(page)
    for p in [(0, 0), (150.0, 80.0), MARKER_NATIVE]:
        mu = pymupdf.Point(p) * page.transformation_matrix
        assert c.native_to_unrotated(g, p) == pytest.approx((mu.x, mu.y))


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_pymupdf_transformation_matrix_quirk(rotation):
    """Characterisation: PyMuPDF 2.0 drops the CropBox offset from
    ``transformation_matrix`` on rotated pages, while ``get_drawings`` and
    rendering keep it. If this starts failing, PyMuPDF changed behaviour and
    ``PageGeometry.from_page``'s rationale should be revisited."""
    doc, page = make_page(rotation, CROP_OFFSET)
    tm = page.transformation_matrix
    assert (tm.e, tm.f) != pytest.approx((-CROP_OFFSET[0], CROP_OFFSET[3]))


# --- round trips ------------------------------------------------------------------

@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("crop", [None, CROP_OFFSET])
def test_round_trips_every_space(rotation, crop):
    box = crop or MEDIA
    g = c.PageGeometry(*box, rotation=rotation)
    for u, v in itertools.product([0.0, 0.25, 0.5, 0.9, 1.0], repeat=2):
        n = (u, v)
        assert c.native_to_normalized(g, c.normalized_to_native(g, n)) == pytest.approx(n)
        assert c.unrotated_to_normalized(g, c.normalized_to_unrotated(g, n)) == pytest.approx(n)
        d = c.normalized_to_displayed(g, n)
        assert c.displayed_to_normalized(g, d) == pytest.approx(n)
        assert c.unrotated_to_displayed(g, c.displayed_to_unrotated(g, d)) == pytest.approx(d)
        r = c.normalized_to_raster(n, 640, 480)
        assert c.raster_to_normalized(r, 640, 480) == pytest.approx(n)
        vw, vh = 1234.0, 567.0
        assert c.pdfjs_to_normalized(c.normalized_to_pdfjs(n, vw, vh), vw, vh) == pytest.approx(n)


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_corners_map_to_displayed_corners(rotation):
    """The displayed top-left is always (0, 0) and bottom-right is (1, 1)."""
    g = c.PageGeometry(*CROP_OFFSET, rotation=rotation)
    native_corners = [(g.crop_x0, g.crop_y0), (g.crop_x1, g.crop_y0),
                      (g.crop_x0, g.crop_y1), (g.crop_x1, g.crop_y1)]
    mapped = {tuple(round(x, 9) for x in c.native_to_normalized(g, p)) for p in native_corners}
    assert mapped == {(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)}
    centre = ((g.crop_x0 + g.crop_x1) / 2, (g.crop_y0 + g.crop_y1) / 2)
    assert c.native_to_normalized(g, centre) == pytest.approx((0.5, 0.5))


def test_expected_rotation_directions():
    """Spot-check /Rotate semantics (clockwise) on the 200x100 page, native (47, 71).

    unrotated = (47, 29). 90 cw: displayed 100x200 -> (100-29, 47) = (71, 47).
    """
    p = MARKER_NATIVE
    expect = {0: (47, 29), 90: (71, 47), 180: (153, 71), 270: (29, 153)}
    for rot, want in expect.items():
        g = c.PageGeometry(*MEDIA, rotation=rot)
        assert c.unrotated_to_displayed(g, c.native_to_unrotated(g, p)) == pytest.approx(want)


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_rect_transform_round_trip(rotation):
    g = c.PageGeometry(*MEDIA, rotation=rotation)
    rect = (20.0, 10.0, 60.0, 35.0)
    n = c.unrotated_rect_to_normalized(g, rect)
    assert all(0 <= x <= 1 for x in n)
    assert n[0] < n[2] and n[1] < n[3]
    assert c.normalized_rect_to_unrotated(g, n) == pytest.approx(rect)


# --- render alignment: the decisive test -------------------------------------------

@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("dpi", DPIS)
@pytest.mark.parametrize("crop", [None, CROP_OFFSET])
def test_marker_renders_where_canonical_coords_say(rotation, dpi, crop):
    doc, page = make_page(rotation, crop)
    g = c.PageGeometry.from_page(page)
    pix = page.get_pixmap(dpi=dpi)
    got = dark_centroid(pix)
    want = c.normalized_to_raster(c.native_to_normalized(g, MARKER_NATIVE), pix.width, pix.height)
    # One pixel at 72-300 DPI, plus the pixmap's own size rounding.
    assert abs(got[0] - want[0]) <= 1.0 and abs(got[1] - want[1]) <= 1.0, (got, want)


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_normalized_position_is_dpi_invariant(rotation):
    doc, page = make_page(rotation)
    found = []
    for dpi in DPIS:
        pix = page.get_pixmap(dpi=dpi)
        found.append(c.raster_to_normalized(dark_centroid(pix), pix.width, pix.height))
    for f in found[1:]:
        assert f == pytest.approx(found[0], abs=0.01)


# --- which space PyMuPDF extraction reports in (verified, not assumed) -----------

@pytest.mark.parametrize("rotation", ROTATIONS)
def test_get_drawings_reports_unrotated_space(rotation):
    doc, page = make_page(rotation, CROP_OFFSET)
    g = c.PageGeometry.from_page(page)
    (drawing,) = page.get_drawings()
    cx = (drawing["rect"].x0 + drawing["rect"].x1) / 2
    cy = (drawing["rect"].y0 + drawing["rect"].y1) / 2
    assert (cx, cy) == pytest.approx(c.native_to_unrotated(g, MARKER_NATIVE))


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_get_text_reports_unrotated_space(rotation):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((30, 40), "LIVING", fontsize=10)
    before = page.get_text("words")[0][:4]
    page.set_rotation(rotation)
    after = doc[0].get_text("words")[0][:4]
    assert after == pytest.approx(before)


# --- PDF.js viewport parity -------------------------------------------------------

@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("crop", [None, CROP_OFFSET])
@pytest.mark.parametrize("scale", [0.5, 1.0, 4.0 / 3.0, 2.75])
def test_pdfjs_transform_equals_displayed_times_scale(rotation, crop, scale):
    g = c.PageGeometry(*(crop or MEDIA), rotation=rotation)
    vw, vh = c.pdfjs_viewport_size((g.crop_x0, g.crop_y0, g.crop_x1, g.crop_y1), scale, rotation)
    assert (vw, vh) == pytest.approx((g.displayed_width * scale, g.displayed_height * scale))
    for p in [(g.crop_x0, g.crop_y0), MARKER_NATIVE, (150.0, 20.0)]:
        viewport = c.native_to_pdfjs(g, p, scale)
        disp = c.unrotated_to_displayed(g, c.native_to_unrotated(g, p))
        assert viewport == pytest.approx((disp[0] * scale, disp[1] * scale))
        # And canonical -> viewport agrees with PDF.js' own transform.
        assert c.normalized_to_pdfjs(c.native_to_normalized(g, p), vw, vh) == pytest.approx(viewport)
