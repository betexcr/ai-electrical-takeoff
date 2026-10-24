# Ground-truth annotation protocol (`protocol/1`)

This document defines how a human reviewer records hand-verified ground truth for the four supplied sheets (tracker **B-03**). The ground truth is the reference against which candidate detectors will later be measured, so it must be complete, consistent and independent of the detectors.

**Versions:** protocol `protocol/1` · taxonomy `taxonomy/1` (`backend/takeoff/groundtruth/taxonomy.py`) · schema `gt-schema/1` (`backend/takeoff/groundtruth/models.py`). Tolerances live in `backend/takeoff/groundtruth/protocol.py`. Changing a rule here means bumping the version and recording the change in `PROJECT_TRACKING.md`.

## 1. Scope
- Annotate only inside the **electrical plan** of each sheet (A1-111 … A4-111). Ignore the legend, notes, mechanical plan and finish plan.
- The electrical-region box comes from Milestone 1 and is a **hypothesis**. Before starting a sheet, confirm the box covers the whole electrical plan, including exterior items near the building, or flag it with a reason. A flagged region blocks completion until it is fixed with a recorded override.
- Annotate **every electrical device symbol you can see**. Candidate locations from the detectors are hints only; they are **never** exhaustive and never proof.
- **Not annotated in B-03:** wiring runs (dashed lines). They have their own data model for the optional bill of materials, but no tool yet. Also not annotated: plumbing symbols, the attic access panel, the fan-coil HVAC unit itself, keynote tags, and dimensions.

## 2. One annotation per device
- Place the point at the **symbol's centre**:
  - Switch: the middle of the `$`.
  - Receptacle: the circle's centre.
  - Light, detector or fan: the circle's centre.
  - Ceiling fan: the hub (the `J`).
- **Combined fixtures** count **once**, under the combined type. A circle with `SD/MA` is one `detector/combined_smoke_co`, not a smoke detector plus a CO detector.
- **Ganged devices** (switches side by side) are **separate annotations**, one per `$`. Box grouping belongs to the bill-of-materials work.
- **Overprinted duplicates** (the same symbol drawn twice in one place, common on A3) are **one** device. The duplicate warning helps catch these.

## 3. Category, subtype and status
- Choose the **category** from the drawn shape and the **subtype** from the **adjacent text token**, then link the token(s) to the annotation. The assessment requires switch types to follow the letter next to the switch.

| Category | Shape | Token → subtype |
|---|---|---|
| switch | `$` | none → `standard`; `D` → `dimmer`; `F` → `fan_switch`; `3D` → `three_way_dimmer`; `3` → `three_way` (not in the legend); `VS` → `vacancy_sensor`; `A` → `astronomical_time_or_motion_sensor` |
| receptacle | duplex circle | `AFI` → `afi_duplex`; `GFI` → `gfi` (or a more specific GFI subtype: `WP` → `gfi_weatherproof`, `240V` → `gfi_240v`, `MW` → `gfi_microwave`, `DW` → `gfi_dishwasher`); `H` → `range_hood`; half-filled → `afci_half_hot`; `FC` → `fan_coil_dedicated` |
| light | circle variants | `P` → `pendant`; wall-mounted symbol → `wall_mounted`; `EXT` → `exterior_wall_photosensor`; circle in square → `recessed_downlight`; with `VP` → `recessed_vapor_proof_downlight` |
| ceiling_fan | four blades + `J` hub | `optional_pre_wire` |
| fan | filled yin-yang glyph | none → `exhaust_fan`; `IAQ` → `iaq_fan` |
| detector | circle with dot | `SD` → `smoke_detector`; `SD/MA` → `combined_smoke_co` |
| junction_box | circle with `J`, no blades | `electrical_junction_box` |

- **Status:**
  - Every **ceiling fan** is `optional_pre_wire`. The symbol marks an optional pre-wire location; it does **not** prove a fan is installed. No "installed" status exists.
  - Every other device is `as_drawn`.
- **Junction boxes:** annotate them all. Whether they count in the takeoff is decided later; ground truth records what is drawn.
- **Not in the legend:** use `other` with a note describing the drawing, or `other_electrical` for a device that fits no category. If you're unsure, set **`needs_adjudication`** and add a note. Don't guess.
- Mounting heights (`+42"`, `+84`, `+30"`) are informational; link them only if useful.

## 4. Room assignment
- Assign the room whose label covers the area containing the device, and pick the specific label instance. A1 has two `CL` labels, and each is a separate room.
- **Kitchen and Dining are separate rooms only when the plan labels them separately** (A2 has `KITCHEN` and `DINING`). A single `KITCHEN / DINING` label (A1, A3) is one room.
- A device **outside the building** (porch light, weatherproof receptacle) is **Exterior**.
- If no label clearly applies, choose `unlabeled` and add a note, or set `needs_adjudication`.
- Room polygons are a later task (B-06); here the reviewer's choice is the ground truth.

## 5. Procedure (per sheet)
The workflow moves each sheet through four phases. Each gate is enforced by the tool.

1. **Pass 1, annotate.** Confirm the region. Then step through **every coverage cell** (about 100 × 100 pt; `N`/`P`). In each cell:
   - Review the candidates: accept, correct or reclassify, or reject.
   - **Add a manual annotation for every device the candidates missed.**
   - Mark the cell done.

   Gate to continue: region confirmed, every cell done, 0 unreviewed candidates.
2. **Pass 2a, blind scan.** Candidates **and** your pass-1 annotations are hidden, and the server refuses to send them. Re-scan every cell, mark every device you see as an observation with a category, and mark each cell blind-scanned.

   Gate to continue: every cell blind-scanned.
3. **Pass 2b, reconcile.** Opening reconciliation closes the blind scan permanently, then reveals the annotations.
   - For each observation:
     - **Match** it to the existing annotation of the same device. No new record is created.
     - **Promote** it if pass 1 missed the device. This creates exactly one annotation, and is refused if a same-category annotation lies within 3 pt.
     - **Dismiss** it with a reason, for example a mis-click.
   - Every annotation that no observation matched is `not_seen_in_blind_scan`. Look again, then verify it (a reason is required), correct it, or delete it.
   - Complete every annotation (subtype, tokens, room) and set it to `verified`.

   Gate to finish: 0 pending observations, every unmatched annotation decided, 0 drafts, 0 `needs_adjudication`, all warnings resolved or acknowledged.
4. **Verified.** Export. A `verified` export is possible only when all four sheets are verified. The evaluator will refuse anything else.

## 6. Tolerances (`protocol.py`)
| Parameter | Value | Use |
|---|---|---|
| `DUPLICATE_TOLERANCE_PT` | 3 pt | duplicate / combined-fixture warning; Promote refusal |
| `MATCH_TOLERANCE_PT` | 6 pt | suggested reconciliation match; orphan-candidate warning |
| `TOKEN_SUGGEST_RADIUS_PT` | 15 pt | nearby text tokens suggested for linking |
| `CELL_SIZE_PT` | 100 pt | coverage-grid cell size |

## 7. Integrity rules
- Nothing is silently overwritten. Every create, update, delete and decision is an append-only event; deletes are soft.
- Imported candidate records never change. Review outcomes are stored separately.
- Totals are **derived** from annotations at export time and never typed in.
- Exports are immutable, versioned directories with file hashes. An existing export is never overwritten.

## 8. Known limitations
- **Single reviewer.** The same person does both passes, so memory of pass 1 can leak into the blind scan. Where possible, blind-scan a different sheet before returning to one you just annotated. No inter-annotator agreement can be measured. This limitation is recorded in every export manifest.
- The blind-scan consistency counts in the manifest (matched / promoted / dismissed / not seen) describe the review process. They are **not** detector accuracy.
