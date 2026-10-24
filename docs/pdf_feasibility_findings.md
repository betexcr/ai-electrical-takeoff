# PDF Feasibility Findings — Milestone 1

**Date:** 2026-10-23 · **Status:** Milestone 1 complete. No detection method is locked in yet.
**Input:** `plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf`, SHA-256 `714af7da3154a1152c21656a7ef515daa1eb985998cdaaadcd7ac3f642fe2587`. The hash is unchanged before and after every tool run, and all 31 original assets were re-hashed at the end of the session with 0 mismatches.
**Tools:** PyMuPDF 2.0 (MuPDF 1.28.5), OpenCV 5.0.0, NumPy 2.5.4, Python 3.12 (uv-managed `backend/.venv`).

> Nothing in this document is a component count or an accuracy figure. There is no ground truth yet (tracker B-03). Every matcher output is a **candidate location**. Numbers here are either facts about the PDF or candidate counts from a stated method at a stated threshold.

The document has four parts: §1 measured facts, §2 visual-inspection observations, §3 hypotheses, §4 failures, §5 unresolved questions. §6 gives the evidence-bounded recommendation and §7 how to reproduce.

---

## 1. Measured facts

### 1.1 Document and page geometry
| Sheet | Page | Size (pt) | Size (in) | /Rotate | CropBox = MediaBox | Text lines / words | Vector paths | Images |
|---|---|---|---|---|---|---|---|---|
| A1-111 | 1 | 2592 × 1728 | 36 × 24 | 0 | yes, origin (0,0) | 537 / 2192 | 12,474 | 2 |
| A2-111 | 2 | 2592 × 1728 | 36 × 24 | 0 | yes | 557 / 2203 | 12,802 | 2 |
| A3-111 | 3 | 2592 × 1728 | 36 × 24 | 0 | yes | 593 / 2253 | 17,815 | 2 |
| A4-111 | 4 | 2592 × 1728 | 36 × 24 | 0 | yes | 603 / 2266 | 21,416 | 2 |

- The sheet IDs come from the PDF text (37.5 pt title-block text), not from page order.
- The 2 images on each page are JPEGs (313×301 and 531×264 px) in the title block at x ≈ 2313–2569 pt: the city seal and logo. They are not part of the drawing.
- There are no optional-content layers (OCGs). The drawing content sits in nested Form XObjects.
- Extracting text and drawings takes 0.13–0.22 s per page; rendering a page at 150 DPI takes about 0.3 s.

### 1.2 Coordinate spaces (`backend/takeoff/pdf/coords.py`, 109 tests)
- **Canonical storage:** normalized `(u, v) ∈ [0,1]²` in the **displayed** orientation, top-left origin. The four spaces native PDF, PyMuPDF unrotated, raster and PDF.js viewport are separate, each with explicit conversions.
- `page.rect` is the **displayed** (rotated) rectangle. A synthetic 200×100 page with `/Rotate 90` reports `rect` as 100×200. `page.cropbox` is y-flipped relative to the MediaBox.
- **PyMuPDF 2.0 quirk:** on rotated pages, `page.transformation_matrix` **drops the CropBox offset**, reporting `e=0, f=90` instead of `e=-10, f=95`. Rendering and `get_drawings()` keep the offset. `PageGeometry.from_page` therefore reads the raw `/CropBox` and `/MediaBox` keys, with inheritance and clipping. A characterisation test pins the quirk.
- Measured rather than assumed: `get_drawings()` and `get_text()` report in **unrotated** space at rotations of 0/90/180/270.
- **Render alignment:** a marker written in native space renders within **≤ 1 px** of its canonical → raster position. This was checked at rotations of 0/90/180/270, at 72/150/300 DPI, with and without a CropBox offset (24 cases), and the normalized position is DPI-invariant.
- The PDF.js `PageViewport` transform is **re-implemented** from PDF.js' formula and agrees with displayed × scale at all rotations and scales. PDF.js itself has not been run yet; see §5.
- The supplied sheets are unrotated with a zero-origin CropBox, so on these sheets canonical = unrotated / (2592, 1728).

### 1.3 Text is real and rich
- All labels are extractable text in embedded Arial and Century Gothic: room names, legend labels, notes, keynote tags, titles and scale.
- Room-label text found inside each electrical-plan region hypothesis:
  - **A1:** BEDROOM / STUDIO, LIVING, KITCHEN / DINING, BATH, CL ×2, LNDRY.
  - **A2:** LIVING, BED 1, BED 2, CL ×2, LNDRY, BATH, DINING, HALLWAY, KITCHEN. Kitchen and Dining are **labelled separately**.
  - **A3:** BED 1, BED 2, BATH 1, BATH 2, HALL, LNDRY, CL ×4, KITCHEN / DINING, LIVING.
  - **A4:** OPTIONAL BED 3, BED 1, BED 2, BATH 1, BATH 2, WALK-IN CLOSET, LNDRY, CL ×2, KITCHEN, LIVING, HALLWAY.
- **Switch letters and receptacle qualifiers are text tokens next to the symbols.** Tokens found in the sheet-1 electrical region include GFI ×14, AFI ×9, 3D ×4, WP ×3, F ×3, D ×2, FC ×2, VS, A, MW, DW, EXT, SD, IAQ and J. These token frequencies are **not** component counts; one receptacle can carry both `WP` and `GFI`. Some tokens are rotated 90°. Full lists: `reports/feasibility/diagnostics/page_N/meta.json` → `short_token_counts`.
- The `$` switch symbol is **vector**: there are no `$` text glyphs in any electrical region. The `J` at each ceiling-fan hub **is** text (Arial 6.6 pt).
- **Watermark:** exactly one text span per sheet, "FOR USE IN THE CITY OF ESCONDIDO", about 129 pt, at 30°, with a bbox covering almost the whole sheet. It is stored in 2 content streams.
- **Duplicated text on sheet A3:** 7 words appear twice at identical positions (`J` ×2, `SD` ×2, `SD/MA` ×2, `IAQ`). Sheets 1, 2 and 4 have none.

### 1.4 Vector geometry is exploded and partly duplicated
- In the electrical regions, **every path holds a single item**. Circles, arcs and the dashed wiring are pre-flattened into many short `l` segments; on sheet 1, 3,121 of 4,407 lines are shorter than 3 pt. Symbols are therefore **not grouped** as objects.
- The dash attribute is never used (`"[] 0"` or `None` on every path). Wiring "dashes" are separate segments, so no wiring-run object exists in the PDF.
- **21–36% of paths in the electrical regions are exact duplicates** (overprint): 938 / 4,462, 982 / 5,129, 2,891 / 8,041 and 2,639 / 9,024 on sheets 1–4. Deduplication shrinks the sheet 3 segments from 8,326 to 4,923.
- **Stroke widths:** symbols and wiring are 0.24 pt; walls are 0.48/0.72 pt; panel frames and view-title boxes are **0.84 pt**; the sheet border is 1.56 pt.
- **Pitfall:** PyMuPDF's `Rect.intersects` returns False for zero-area rects, and **2,624 of 4,464** sheet-1 electrical paths have a zero-area rect (axis-aligned lines). The tools filter by coordinates instead.

### 1.5 Sheet layout and region hypotheses (`backend/takeoff/pdf/layout.py`)
- **Method:** find title text (≥ 20 pt, horizontal), then cast rays from the title centre to the nearest 0.84/1.56 pt axis-aligned frame line (≥ 300 pt long) in each direction.
- The panel layout **differs per sheet**:

| Sheet | Electrical-plan hypothesis bbox (pt) | Normalized (u0, v0, u1, v1) | Position on the sheet |
|---|---|---|---|
| A1 | 854.4, 37.1, 1439.0, 870.1 | 0.330, 0.021, 0.555, 0.504 | top centre |
| A2 | 143.9, 37.1, 1439.0, 556.7 | 0.056, 0.021, 0.555, 0.322 | top left, wide |
| A3 | 818.4, 37.1, 1439.0, 870.0 | 0.316, 0.021, 0.555, 0.503 | top centre |
| A4 | 1007.3, 37.1, 1870.7, 711.8 | 0.389, 0.021, 0.722, 0.412 | top centre-right |

- Every sheet has one electrical, one mechanical and one finish plan. Sheet A4's title reads "**MECHNICAL** PLAN - PLAN 4" (typo in the original); the title pattern tolerates it.
- On every sheet all four sides of the electrical region came from frame lines, with no fallback to the page edge.

### 1.6 The legend is machine-readable on every sheet
- All **27/27** catalogue entries are found **exactly once on each of the 4 sheets** by exact label text, after grouping 9.6 pt lines into blocks. This does not depend on the legend region box, which is ambiguous on A2 (§4).
- A sheet-native, stroke-only icon template is cropped for each entry. Template images: `reports/feasibility/symbols/templates/`.

### 1.7 Supplied reference crops
- They are icon-plus-label screenshots at a median **≈ 1.48 px/pt (≈ 107 DPI)**, measured from label width in pixels against the same label's width in points. Icon cores are therefore only about 10–20 px tall.
- Normalised correlation between each resized reference core and the sheet-native stroke-only icon is mostly 0.48–0.73 for switches, receptacles, exhaust fan and recessed light. It is ≤ 0.1 for pendant, exterior, vapor-proof, smoke, combined and ceiling fan, where letters contaminate the core (§4). Source: `symbol_probe.json` → `references`.

### 1.8 Matching experiments (candidate counts only)
- **Setup:** 300 DPI renders of the electrical region, excluding the title strip, matched with sheet-native stroke-only templates, TM_CCOEFF_NORMED, rotations 0/90/180/270 and NMS. Each template takes about 1.0 s per sheet (max 1.5 s).
- **Full table:** `reports/feasibility/symbols/summary_table.txt`. Selected rows, giving candidates at NCC ≥ 0.7 for sheets A1/A2/A3/A4:

| Template | Plain render | Strokes thickened (5×5 erode) | Vector-signature hits (A1/A2/A3/A4) |
|---|---|---|---|
| switch `$` | 11 / 7 / 10 / 7 | 13 / 13 / 18 / 18 | 12 / 13 / 20 / 22 |
| duplex receptacle | 11 / 17 / 13 / 20 | 19 / 24 / 22 / 28 | 25 / 32 / 33 / 40 |
| recessed downlight | 8 / 12 / 14 / 19 | 9 / 12 / 13 / 18 | 7 / 11 / 13 / 18 |
| ceiling fan | **0** / 0 / 2 / 3 | 0 / 2 / 2 / 3 | **0** / 2 / 2 / 3 |
| smoke detector | 2 / 3 / 3 / 5 | 2 / 3 / 3 / 5 | 2 / 3 / 4 / 5 |
| junction box | 0 / 0 / 0 / 0 | 12 / 16 / 14 / 17 | 0 / 0 / 0 / 0 |

- The switch template is **very threshold-sensitive**: 85–282 candidates at 0.6 on the plain render, against 7–11 at 0.7.
- Fan scores at the `J` hub glyphs:

| Sheet | Plain | Thickened |
|---|---|---|
| A1 | 0.396 | 0.61 |
| A2 | 0.63 / 0.61 | 0.82 / 0.80 |
| A3 | 0.91 / 0.90 | 0.93 / 0.91 |
| A4 | 0.79–0.80 | 0.86–0.89 |

### 1.9 Watermark effect (in-memory copy; original untouched)
- Blanking the watermark changes **1.9% / 0% / 1.8% / 3.3%** of electrical-region pixels on sheets 1–4, with a maximum grey difference of 25/255. The watermark is light and does not cross A2's electrical region.
- Candidate counts at NCC ≥ 0.6 are **identical with and without the watermark** for every template on every sheet.
- Differences appear only at 0.5. Exhaust-fan candidates fall from 39→14, 12→12, 50→16 and 62→20, and switch candidates rise from 420→432 on A4. Diff images: `p{N}_watermark_diff_x4.png`.

## 2. Visual inspection (qualitative; no ground truth)
The overlays inspected were `page_{1..4}_overlay_60dpi.png`, `page_1_electrical_crop_150dpi.png`, `contact_sheet.png`, `p1_raster_candidates.png`, `p1_vector_candidates.png` and `p4_raster_candidates.png`.

- **Region hypotheses:** on all four sheets the red electrical box encloses the electrical floor plan visually and excludes the mechanical and finish plans, the legend and the notes. The view title straddles the region's bottom edge, because the ray stops at the title underline.
- **Icon extraction:**
  - The switch family splits cleanly into the `$` core and its letter (3D/VS/D/F/A), and the duplex receptacle core separates from GFI/AFI/WP.
  - Pendant `P`, vapor-proof `VP`, exterior `EXT`, IAQ and smoke `SD` letters stay inside the extracted core.
  - The wiring-run crop yields only an arc.
  - The first native fan crop was truncated by an 80 pt window; after the fix it is complete.
- **Sheet 1, raster ≥ 0.7:**
  - Rotated switches are boxed, but only one of the three `$` symbols in the front-door `A 3D 3D` group is.
  - Visible receptacle misses: the 240V variants, rotated AFI receptacles on the right wall, and the lower-right WP/GFI.
  - The receptacle template also boxes the `FC` fan-coil outlet.
  - `SD` and `SD/MA` both fire the smoke template.
  - The exhaust-fan template boxes the `IAQ` fans.
  - The pendant and wall-light templates box the same circles.
  - **The bedroom ceiling fan is not boxed.**
- **Sheet 1, vector signatures:**
  - Visually more receptacles and all three front-door switches are boxed.
  - Circle-based light classes produce identical hit sets, so they don't discriminate.
  - The ceiling fan is missed. A dashed wire touches a blade and merges into its component: 71 vs 64 segments measured before deduplication. After deduplication the legend key is 50 segments, and A1 is still unmatched.
- **Sheet 4, raster ≥ 0.7:**
  - The three visible ceiling fans (BED 2, OPTIONAL BED 3, BED 1) and the smoke detectors are boxed.
  - A false pendant candidate sits on the OPTIONAL BED 3 fan hub.
  - Many switches in groups and several AFI/FC receptacles are missed.

## 3. Hypotheses (supported by §1–2, not validated)
1. **Text drives subtype.** The symbol shape gives the family (switch `$`, duplex receptacle, circle-type light, detector circle, fan glyph). The adjacent text token gives the subtype: the switch letter (D/F/3D/VS/A), the receptacle tag (GFI/AFI/WP/240V/DW/MW/H), or SD vs SD/MA, IAQ vs exhaust. This follows the assessment's counting rule that switch types come from the letter.
2. **Candidates need a fusion step.** Raster matching and vector signatures fail differently: raster misses rotated or 3-line receptacles and hairline fans; vector misses symbols touching wiring and can't separate circle classes. A union followed by per-candidate classification and deduplication may recover more than either alone.
3. **Each sheet's own legend is the best template source.** Its icons share the plan's scale and stroke style. The supplied crops are better used as human-readable references than as templates.
4. **The `J` glyph is a strong ceiling-fan anchor.** It sat at every fan hub that was inspected. It may also appear in junction-box symbols, so it can't define a fan on its own.
5. **A multimodal model could help** as a verifier of ambiguous candidates (for example pendant vs wall vs recessed, or symbols crossed by labels), given a crop plus the legend entry. **No model was called in this milestone, so this is untested.**
6. **The electrical region hypotheses are usable** as the processing window for these four sheets. The frame-width assumption (0.84/1.56 pt) is specific to this plan set.

## 4. Failures and limitations observed
- **Ceiling fan on A1:** raster found 0 candidates even at 0.5 (best score at the hub 0.396), and the vector signature found 0. Thickening raised the hub score to 0.61. Fans are a priority class, so this matters most.
- **Switch template:** noisy at ≤ 0.6 (85–432 candidates) and missing visible switches at ≥ 0.7. No single threshold works.
- **Cross-class confusion:** pendant, wall-mounted and recessed lights overlap; exhaust and IAQ share a glyph; smoke and combined share a glyph; candidates stack at the same location under several classes.
- **Thickening is not uniformly good:** the junction-box template goes from 0 to 12–17 candidates at 0.7 on circles.
- **Vector signature on the largest component** doesn't discriminate circle classes and never matches the filled exhaust-fan glyph (n ≈ 263 segments).
- **Reference crops:** low resolution, contaminated cores for P, VP, EXT, IAQ and SD, an unusable wiring crop, and no label to estimate the fan crop's scale.
- **Region hypotheses:**
  - `floor_finish_legend` shares a cell with keynotes on A1 and with the finish plan on A2.
  - `legend` shares a cell with keynotes on A2.
  - These are reported as warnings in `meta.json`. They don't affect the electrical regions.
- **Bugs found and fixed during the milestone:**
  - Watermark false positives on labels 0.2° off-axis.
  - The PyMuPDF matrix quirk (§1.2).
  - The `Rect.intersects` trap (§1.4).
  - A misaligned column in my first results table, regenerated before use.

## 5. Unresolved questions
1. **Ground truth (B-03).** No hand-verified counts exist. Who annotates and verifies them? Every accuracy claim is blocked on this.
2. **Room boundaries.** Room-label text is reliable, but polygons for the rooms, the building outline and the exterior are not yet extracted. Walls appear as 0.48/0.72 pt strokes and grey fills; this has not been investigated.
3. **Combined labels.** How should "KITCHEN / DINING" be assigned on A1 and A3, where it is a single label, against A2, which labels KITCHEN and DINING separately? Per the assessment, they are separate rooms only when labelled separately.
4. **Junction box vs fan hub `J`,** and whether any stand-alone junction boxes exist on these sheets.
5. **Wiring runs** (optional bill of materials) must be rebuilt from exploded, duplicated 0.24 pt segments that carry no dash attribute.
6. **PDF.js parity** has only been proven against a re-implementation of its transform. It must be confirmed in the browser with a Playwright overlay test (Milestone 3).
7. **Generality.** The frame-width and title heuristics are tuned to this plan set; new plan styles need their own intake (SUP-04).
8. **Multimodal model value** is untested: there is no cost, latency or accuracy evidence yet.

## 6. Recommendation (bounded by the evidence)
- **Adopt now:**
  - The canonical normalized coordinate system and `coords.py`.
  - Per-sheet processing keyed by text-derived sheet IDs.
  - Text-anchored legend parsing.
  - Text-token extraction for room labels, switch letters and receptacle tags.
  - Deduplication of overprinted text and geometry.
  - The electrical-region hypotheses as processing windows, surfaced on the plan for review.
- **Keep as candidate generators only, to be evaluated against ground truth:** raster matching with sheet-native templates (plain and thickened) and vector-component signatures. Neither is reliable enough alone (§4).
- **Not yet justified:** choosing a final detector, choosing a confidence threshold, using a multimodal model, or training a custom detector (T-02). These decisions wait for B-03 ground truth and a measured candidate-generation baseline.
- **Next milestone:** build the B-03 ground-truth workflow. An annotation helper pre-fills candidates and a person verifies every label. Then measure per-class recall and precision of the candidate generators, starting with ceiling fans and lights.

## 7. Reproduce
From `backend/`:
```
uv sync
uv run pytest -q
uv run python tools/pdf_diagnostics.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --out ../reports/feasibility/diagnostics
uv run python tools/symbol_probe.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --symbols ../electrical_takeoff_symbols --diag ../reports/feasibility/diagnostics --out ../reports/feasibility/symbols
```
On Windows consoles, set `PYTHONIOENCODING=utf-8`; the page text contains `ϕ`. Last recorded results: **127 tests passed** (`test_coords.py` 109, `test_pdf_diagnostics.py` 9, `test_symbol_probe.py` 9), both tools exited 0, and `input_unchanged: true`.

**Outputs:**
- `reports/feasibility/diagnostics/`: `summary.json`, plus per page `meta.json`, `text.json`, 60 DPI overlay, 150 DPI electrical crop, and a 150 DPI render (git-ignored).
- `reports/feasibility/symbols/`: `symbol_probe.json`, `summary_table.txt`, `contact_sheet.png`, `templates/`, `p{N}_raster_candidates.png`, `p{N}_vector_candidates.png`, `p{N}_watermark_diff_x4.png`, and the watermark-free render (git-ignored).
