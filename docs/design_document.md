# AI Electrical Takeoff: design document

This covers the Stage 3 assessment, using the 4 Escondido ADU sheets A1-111 to A4-111.

**Status on 2026-10-25:**

- The required workflow runs end to end, with 577 automated tests, including tests in a real browser.
- The AI stages are implemented. They have **not yet run against a live model**, because no API credentials are configured.
- Figures come from saved reports in this repository. Plans not yet built are marked *(proposed)*.

**Governing rule:** *detectors propose, reviewers decide, saved device records determine the official totals.* Every number is a count of saved devices, each drawn as a marker on the plan; a detection counts only once a reviewer accepts it, and re-analysis never overwrites a reviewer's work.

## 1. Breakdown and build order

In build order; each piece was tested before the next started.

| # | Piece | What it does | Status |
|---|---|---|---|
| 1 | PDF ingestion and coordinates | Splits the PDF into sheets and reads each sheet's id. Maps PDF, raster and screen coordinates, including rotation. | Done; markers within 1.5 px at each zoom level tested |
| 2 | Ground truth, built before any detector | A review tool with coverage cells, a blind re-scan and reconciliation. Its verified export, `gt_001`, holds 300 devices. | Done; single reviewer |
| 3 | Evaluator | One-to-one matching within 6 pt. Precision, recall, subtype and localization per category and sheet group. | Done (`eval/1.1`) |
| 4 | Symbol detection (`det/0`) | Templates are cut from **each sheet's own legend**. Raster and vector-geometry matches merge into one device per site. Subtype comes from the letters beside a symbol (`D`, `3`, `GFI`, `WP`, …). | Done; tuned on A1 and A2, with A3 and A4 held out |
| 5 | AI stages (Claude vision) | **Verify:** a second opinion on each detection, checked against the legend icons; a disagreement opens a flag. **Discover:** reads the plan in tiles and proposes devices det/0 missed. Neither stage can change a device or a total. | Built, with a request budget; **not run live yet** |
| 6 | Room assignment | Nearest room label, flagged when ambiguous; the reviewer corrects it on the plan. | Weak: 48.8% correct on the held-out sheets |
| 7 | Store and merge | Runs are immutable; device changes go to an append-only event log. A re-run only adds evidence and flags, never changes a reviewer's field, and adds nothing on an unchanged plan. | Done |
| 8 | Review UI | Markers and room leader lines. Create, read, update and delete on the plan, plus reject, bulk accept of unflagged proposals, and undo. Counts per room and per home update on every save. | Done |
| 9 | Export | Device CSVs and `totals.json` that reconcile exactly with the app. Marked `complete: false` while items are unresolved. | Done |
| 10 | Bill of materials (optional) | Gang boxes, cable runs, priced total. | Deferred: the required workflow comes first |

## 2. First release

**What it does:** the estimator uploads a plan set, runs analysis, and reviews the proposals on the drawing: fixing category, subtype, room or position, adding what was missed and rejecting false positives.

The result is an export of per-room and per-home counts, where each count traces to markers and each correction has a history.

**What it leaves out:** room boundaries and automatic Exterior assignment, calibrated confidence and auto-accept, the bill of materials, accounts and hosting.

**Today:** on the held-out sheets, det/0 scores precision **0.92**, recall **0.98** and subtype accuracy **0.95**. Across all four sheets it finds ceiling fans 8/8 and lights 61/63 (`reports/evaluation/gt_001_20261025T020009Z__eval1.1__det0/report.md`). These numbers are **biased upward**: 298 of the 300 ground-truth devices began as hints from the generators det/0 is built on, and two held-out sheets are too few to decide a release.

**Ship criteria** *(adopted 2026-10-25, tracker R1-05; not yet measured)*. Measure on at least 10 new sheets, hand-counted blind and never used for tuning. The release needs all of these:

- Ceiling fans and lights: recall ≥ 0.98 and precision ≥ 0.95 (the priority categories).
- Every other category: recall ≥ 0.95.
- After a normal review, per-room counts are exact on at least 95% of sheets.
- Review takes under 10 minutes per sheet. The blind scan took 4.5 to 8 minutes.
- Zero integrity failures: no count without a marker, no correction lost to a re-run, no export that differs from the app.

## 3. Release

- **Pilot** *(proposed)*. 2 or 3 estimators use it on live bids alongside their usual manual takeoff. For each bid, compare the counts and the review time.
- **Versioning** *(built)*. Each analysis creates a new immutable run that records the detector version and parameters, the merge-rule version, the AI stage, prompt and model versions, and the PDF's SHA-256; the export manifest repeats them. Still missing: an application version.
- **Shipping a model or prompt change without breaking finished takeoffs.**
    - A finished takeoff is never recomputed in place. A new model produces a new run, which can only add evidence and flags.
    - The AI cannot change a total by itself, so a worse model mostly adds review work; a reviewer still decides every AI candidate individually.
    - *(proposed)* Before each change ships, re-run the fixed evaluation set and block the change if it falls below the §2 gates.
- **Feature flags.** `--ai on|off` and a request budget per run exist today. *(proposed)* Add per-user flags for each AI stage and each detector version, enable them for pilot users first, and roll back by turning the flag off. Old runs stay readable.

## 4. Support

- **Accuracy in production.** Each reviewer action is already a labelled outcome, stored with its run and versions: an accept confirms the detection, a reject is a false positive, a manual add is a miss, a category or subtype change is a confusion. *(proposed)* Chart these by version, category and customer, and alert when the reject rate or the manual-add rate drifts.
- **Using corrections** *(proposed)*. Corrections are *candidate* ground truth, never automatic training data: a second reviewer audits a sample, audited sheets join the evaluation set, and a new detector or prompt (e.g. `det/1`) ships only if it beats the current one there.
- **New plan styles and legends.** Templates come from each sheet's own legend, so a new symbol style is picked up when its legend wording matches; a sheet without a recognisable legend is skipped with a message and can still be counted by hand. *(proposed)* An unmatched legend entry opens a "new symbol" ticket, and a firm's new drawing style gets a small hand-counted set before it is enabled.
- **Bug triage** *(proposed)*. A "this is wrong" report captures the run id, the device and its history, so the immutable records reproduce it exactly. Fix order: wrong totals or lost corrections, then missed devices, then UI problems. Every confirmed bug becomes a regression test, as the defects from this project's final review did.

## 5. Trade-offs, and what I would do with more time or a team

- **Rules before a trained detector.** With 4 sheets, rules can be inspected, versioned and honestly evaluated on a held-out pair. With more plans, I would train a detector on audited corrections and keep det/0 as the baseline it must beat.
- **AI as second opinion, not decision-maker.** Totals never depend on a model whose accuracy is unmeasured. Once the live evaluations exist, the per-category evidence decides whether the AI's role grows.
- **Rooms are the weakest piece.** The next investment is room polygons from walls and door gaps, plus a building outline for automatic Exterior assignment.
- **Ground truth from one reviewer.** With a team, two blind annotators would measure agreement and hand-count new plans, giving an unbiased recall figure.
- **Then the bill of materials:** gang boxes, cable runs measured at 1/4" = 1'-0" with a stated routing allowance, and a priced total, all drawn on the plan and traceable like every count.
- **Infrastructure:** a hosted, multi-user service with per-firm data and the §4 monitoring.
