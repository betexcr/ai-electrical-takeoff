# Project Tracking — AI Electrical Takeoff

This is a living tracker for the assessment's **design, delivery, release, and support** requirements. Keep it updated throughout implementation. The working application is not the only deliverable: the design document, evaluation evidence, release strategy, support plan, and demonstration materials are part of the assessment.

## Tracking rules

- Update this file whenever a milestone, architecture decision, scope decision, evaluation, or release gate changes.
- Do not mark a task `Done` without observable evidence. Record the file, report, test command/output, or demo step that proves completion.
- Use `Blocked` when work cannot proceed and write the reason and next action.
- Keep known limitations and failed detections visible; never fabricate accuracy numbers or hide known errors.
- Keep the submitted design document to 1–2 pages, as requested, and keep this fuller tracker as the working record.
- Treat changes to models, prompts, schemas, detection rules, and post-processing as versioned changes that require regression evaluation.
- Preserve completed takeoffs and user corrections when detection logic changes. New analysis should create a new, versioned run rather than silently rewriting historical results.

Status values: `Not started` · `In progress` · `Blocked` · `Done` · `Deferred (with reason)`.

## 1. Breakdown and implementation order

| ID | Workstream / deliverable | Acceptance evidence | Status | Evidence / notes |
|---|---|---|---|---|
| B-01 | Inspect source assessment, all four PDF sheets, symbol images, README, and CSV catalogue | Asset inventory and requirements list checked against the assessment | Done | 2026-10-23. Read the assessment's text (python-docx), all 4 sheets, README, and CSV. The 31 original assets were SHA-256-hashed at the start and end of the session with 0 mismatches. All 27 catalogue files map to a legend label found exactly once on each sheet (`backend/tests/test_symbol_probe.py::test_every_catalogued_legend_label_found_once_per_sheet`, `::test_catalog_covers_every_reference_crop`). The assessment's 5 minimum requirements map to §2 MVP scope; its 5 design-document questions map to §§1–5 and SUB-01. |
| B-02 | Inspect PDF geometry, text, page dimensions, and sheet layout | Reproducible inspection output; electrical-plan region identified on each sheet | Done | 2026-10-23. `backend/tools/pdf_diagnostics.py` writes to `reports/feasibility/diagnostics/` (exit 0, input hash unchanged). Pages are 2592×1728 pt, rotation 0; the layout differs per sheet. The electrical-region **hypothesis** was produced for all 4 sheets and visually inspected on the 60 DPI overlays; each encloses the electrical plan. Ambiguous non-electrical cells are flagged as warnings. Tests: `test_pdf_diagnostics.py` (9 passed). Findings: `docs/pdf_feasibility_findings.md` §1.1–1.5, §2. |
| B-03 | Establish hand-verified ground truth for the four plans | Reviewed annotations with component type, position, room, status, and expected totals | Done | **2026-10-25. All 4 sheets are `verified`. Export: `data/ground_truth/exports/gt_001_20261025T020009Z/` (`gt-export/1`, level `verified`, reviewer Alberto).**<br>• 300 annotations: A1 55, A2 66, A3 82, A4 97. Per sheet, category, subtype and room in `totals.json`, derived from `annotations.jsonl`.<br>• Checked on 2026-10-25: `export.require_verified` passes; all 6 manifest file hashes match; the input PDF SHA-256 equals the original (`714af7da…`); each sheet has 0 open gate reasons.<br>• Blind-scan consistency: every observation matched on all 4 sheets, with 0 promoted, 0 dismissed and 0 unseen. The blind scans took 4.5–8 min per sheet, and the same reviewer did both passes, so this is weak evidence of completeness (memory leakage; manifest limitation). It is not detector accuracy.<br>• 0 `junction_box` and 0 `other_electrical` on all sheets; not independently spot-checked.<br>• Reconcile keyboard shortcuts added on 2026-10-24 (see B-03 evidence).<br>• Tool: `backend/takeoff/groundtruth/`, `backend/reviewer/static/`, `backend/tools/gt_review.py`; protocol `docs/ground_truth_protocol.md`.<br>• Workflow per sheet: pass 1 (annotate, with candidates as hints) → blind scan (candidates and annotations hidden, enforced by the server) → reconcile (match / promote / dismiss; unmatched annotations need an explicit decision) → verified.<br>• Tests: 129 new (unit, API and 14 Playwright/Chromium end-to-end); full suite 256 passed.<br>• The working DB `data/ground_truth/working.sqlite` holds only imported candidates (1,158 in 3 runs) and the sheet and cell rows: **0 annotations, 0 observations** as of 2026-10-23. Superseded by the reviewer's work; not re-counted.<br>• Manual run of the full A1 workflow on a **scratch** DB only (screenshots in `reports/ground_truth/`); those test labels are not ground truth.<br>**Done when:** a reviewer completes all 4 sheets and a `verified` export exists under `data/ground_truth/exports/`. Met on 2026-10-25; this unblocks R1-04 and R1-05. A second-reviewer check is still not done. |
| B-04 | Define canonical component taxonomy and legend interpretation | Versioned schema; all included symbol references mapped; ambiguity rules documented | In progress | • `taxonomy/1` in `backend/takeoff/groundtruth/taxonomy.py`: 8 device categories; every catalogue row mapped (test); wiring and reference-only items are never devices.<br>• Subtypes observed on the plans but missing from the legend are flagged (`three_way` for `3`; `fan_coil_dedicated` for `FC`).<br>• Token rules keep shared-symbol subtypes apart; a conflict raises a warning.<br>• Ambiguity rules: `docs/ground_truth_protocol.md` §3.<br>• Reviewer-facing subtype names are the sheets' exact legend wording (line wrapping undone). `test_gt_taxonomy.py::test_legend_labels_match_every_sheet_exactly` checks them on all 4 sheets. Stored ids are unchanged (they match `symbol_catalog.csv`); the export CSV adds a `subtype_label` column.<br>• The ground-truth schema `gt-schema/1` (Pydantic) is versioned. The takeoff app's detection schema (Milestone 2) is still to do. |
| B-05 | Implement PDF ingestion and per-sheet processing | Four pages processed independently with stable page references | In progress | The diagnostics process each page independently and read sheet IDs A1-111…A4-111 from text. There is no ingestion service or persistence yet. Overprinted text and paths (21–36% exact duplicate paths; 7 duplicate words on A3) must be deduplicated at ingestion. |
| B-06 | Implement room labels, room boundaries, and Exterior assignment | Room assignments reviewable on plan; separate Kitchen/Dining where labeled | Not started | Evidence for the approach: room-label text is extractable inside every electrical region (lists in findings §1.3); A2 labels KITCHEN and DINING separately, while A1 and A3 use a single "KITCHEN / DINING" label. Room polygons, the building outline and the Exterior are not yet investigated. |
| B-07 | Implement component detection and deduplication | Each proposed detection has a type, position, plan, confidence/evidence, and validation outcome | In progress | Feasibility only: `backend/tools/symbol_probe.py` → `reports/feasibility/symbols/`. Raster (sheet-native templates, plain or thickened) and vector-signature **candidate** generators were measured; neither is sufficient alone. Example: the A1 ceiling fan was missed by both, scoring 0.396 at its hub. No detector chosen; findings §1.8, §2, §4. |
| B-08 | Implement coordinate mapping and visual overlays | Markers remain registered while zooming, panning, and resizing; underlying drawing remains visible | In progress | `backend/takeoff/pdf/coords.py` defines a canonical normalized space in displayed orientation and keeps the native, PyMuPDF, raster and PDF.js spaces explicit. 109 tests pass, including render alignment ≤1 px at 0/90/180/270° and 72/150/300 DPI, and PDF.js transform parity. Found that PyMuPDF 2.0's `transformation_matrix` drops the CropBox offset on rotated pages.<br>• Reviewer overlay (raster plus SVG, not PDF.js): in Chromium, markers stay within 1.5 px across 4 zoom levels and a pan on all 4 sheets, and clicks store points within 0.5 pt (`test_gt_e2e.py`).<br>• A served region image maps to the PDF within ≤1 px (`test_gt_sheets.py`). This required using the pixmap's snapped pixel origin rather than the requested clip.<br>• Still to do: PDF.js viewer parity in the app (Milestone 3). |
| B-09 | Implement per-room and per-plan counts | Counts are derived only from stored, validated detection records and trace to markers | Not started | |
| B-10 | Implement Create / Read / Update / Delete on the plan | CRUD updates counts immediately and changes persist after refresh | Not started | |
| B-11 | Implement evaluation and error review | Metrics and examples include false positives, missed detections, localization errors, and room errors | Not started | |
| B-12 | Optional bill of materials | Boxes, gang sizes, runs, cable estimates/assumptions, prices, overlays, and edit behavior documented and tested | Deferred (after core MVP) | Optional assessment differentiator; do not jeopardize required functionality. |

## 2. First release and release gate

### Proposed MVP scope

- Load and process the four provided sheets separately.
- Detect at minimum ceiling-fan symbols/pre-wire locations, lights, switches, and receptacles; include other electrical legend types where feasible and show omissions explicitly.
- Assign detections to labeled rooms or Exterior as applicable.
- Draw every counted detection on the plan.
- Show counts per room, subtype, and home total.
- Support correction CRUD and persist corrections.
- Include a repeatable evaluation against manually verified ground truth.

### Explicitly out of the initial release

- Production multi-tenant deployment, advanced user/role management, and complex cloud infrastructure unless needed for the demonstration.
- A full priced bill of materials until the mandatory detection, overlay, count, and correction workflows are reliable.
- Claims that model confidence is a calibrated probability unless calibration is measured.

### Release-readiness checklist

| ID | Gate | Acceptance evidence | Status | Evidence / notes |
|---|---|---|---|---|
| R1-01 | Required workflow works end to end | Demo: upload PDF → see detections → inspect marker → correct/add/delete → counts update → refresh preserves changes | Not started | |
| R1-02 | Four plans supported | Each provided sheet can be processed and reviewed separately | Not started | |
| R1-03 | Counts trace to visual evidence | Automated invariant/test proves every counted record has a rendered marker or an explicitly explained exclusion | Not started | |
| R1-04 | Detection quality measured | Precision, recall, localization, room assignment, and per-category count comparisons against ground truth | Not started | |
| R1-05 | Ship thresholds selected transparently | Proposed thresholds documented after baseline results; approval/justification recorded; no invented success claims | Not started | |
| R1-06 | Errors and limitations reviewed | Report contains representative correct and incorrect detections and known limitations | Not started | |
| R1-07 | Setup is reproducible | README setup/run/test instructions verified from a clean environment | Not started | |

Do not declare the MVP ready solely because the UI looks polished or aggregate totals happen to match. Inspect per-class performance, especially fans and lights, and preserve visible evidence of errors.

## 3. Release strategy and change safety

The assessment demonstration can be local and single-user. Document how this would evolve into a controlled estimator pilot if the product were deployed to users.

| ID | Release practice | Required decision / evidence | Status | Evidence / notes |
|---|---|---|---|---|
| REL-01 | Pilot rollout | Define a small estimator pilot, feedback channel, success metrics, and rollback owner/process | Not started | |
| REL-02 | Feature flags | Identify risky/new capabilities that can be enabled or disabled independently | Not started | |
| REL-03 | Versioning | Record application, detection pipeline, model, prompt, schema, and rules versions for each analysis run | Not started | |
| REL-04 | Immutable completed takeoffs | Keep the input reference/hash and analysis version; changed models/rules create a new run and do not silently alter completed takeoffs | In progress | Implemented for ground truth:<br>• Exports are new immutable directories, never overwritten.<br>• The manifest carries the input PDF SHA-256, schema, taxonomy and protocol versions, candidate-run hashes and the hash of every file.<br>• Candidate runs and events are immutable (SQL triggers).<br>• Tests: `test_gt_export.py`, `test_gt_store.py`.<br>Takeoff analysis runs (Milestone 2+) still to do. |
| REL-05 | Safe model/prompt rollout | Run the fixed evaluation set against candidate changes; compare results by class and plan; promote only if gates pass | Not started | |
| REL-06 | Rollback | Document how to revert the active model/prompt/rules version without corrupting saved results | Not started | |

## 4. Support, monitoring, and learning from corrections

| ID | Support practice | Required decision / evidence | Status | Evidence / notes |
|---|---|---|---|---|
| SUP-01 | Production-quality metrics | Define measurement of precision/recall from reviewed samples, correction rate, low-confidence rate, failures, and processing latency/cost | Not started | |
| SUP-02 | Correction audit trail | Store original and corrected value, action, timestamp, and relevant pipeline version; preserve history rather than silently replacing it | In progress | Implemented for ground-truth review:<br>• An append-only event log records entity, action, before/after JSON, actor (`X-Reviewer`), timestamp and schema version for every write. Deletes are soft. Candidate provenance keeps the generator and version.<br>• SQL triggers forbid updating or deleting events.<br>• Tests: `test_gt_store.py`, `test_gt_api.py`.<br>Takeoff-app corrections (B-10) will reuse this pattern. |
| SUP-03 | Safe learning loop | Review corrections, anonymize/remove sensitive information if applicable, curate and validate training/evaluation examples before reuse | Not started | |
| SUP-04 | New plan styles and legends | Define an intake workflow: detect unfamiliar legend/style → flag uncertain items → review/annotate → add evaluation cases → regression-test before rollout | Not started | |
| SUP-05 | Bug triage | Define severity, reproducibility requirements, owner, impact, workaround, fix verification, and regression-test addition | Not started | |
| SUP-06 | Operational observability | Record per-run status, stage failures, model/prompt/schema versions, and diagnostics without logging secrets | Not started | |

## 5. Trade-offs and future work

Maintain a short decision record for each material trade-off. Include what was chosen, alternatives considered, why, impact, and what evidence would trigger reconsideration.

| ID | Trade-off / follow-up | Decision or trigger | Status | Evidence / notes |
|---|---|---|---|---|
| T-01 | Vector extraction vs image matching vs multimodal AI vs hybrid | Compare on the supplied PDF and ground truth before locking the detector | In progress | Evidence so far, on the PDF only and without ground truth:<br>• Vector alone cannot isolate symbols: geometry is exploded into single-segment paths with no dash attribute and heavy overprint, and largest-component signatures don't discriminate circle classes.<br>• Raster NCC with sheet-native templates finds many compact symbols but is threshold-sensitive (switches) and misses hairline fans (A1).<br>• Text tokens carry subtype.<br>• Multimodal: not tested.<br>Decision deferred until B-03 ground truth exists (findings §3, §6). |
| T-02 | Off-the-shelf vision model vs custom detector | Consider custom training only if measured errors justify it and suitable annotated data exists | Not started | |
| T-03 | MVP vs optional BOM | Stabilize required workflow first; add BOM only after core release gates pass | Not started | |
| T-04 | Prototype persistence/hosting vs production infrastructure | Keep demo architecture simple; document production changes separately | Not started | |
| T-05 | More time/team | Prioritize more annotated plan diversity, detector improvements, reliability, estimator feedback, and BOM depth based on measured errors | Not started | |

## 6. Assessment submission and interview readiness

| ID | Deliverable | Acceptance evidence | Status | Evidence / notes |
|---|---|---|---|---|
| SUB-01 | 1–2 page design document | Covers breakdown, MVP and ship criteria, release/versioning, support/feedback, and trade-offs | Not started | |
| SUB-02 | Repository and README | Code repository includes validated install/run/test instructions | In progress | `README.md` stub with the setup, test and ground-truth reviewer commands, each run in this session. Not yet verified from a clean clone (R1-07). The folder is a git repository (`main`) as of 2026-10-24. |
| SUB-03 | Architecture and data-flow diagrams | Diagrams match the implemented system | Not started | |
| SUB-04 | Evaluation report and plan overlays | Includes each plan and honest examples of incorrect/missed detections | Not started | |
| SUB-05 | Working live demo | PDF load, visible detections, CRUD corrections, updated counts, persistence | Not started | |
| SUB-06 | Technical notes | Model, prompt, schema, coordinate, detection, and known-limit decisions are explainable | In progress | `docs/pdf_feasibility_findings.md` covers the coordinate, PDF-structure and detection-feasibility decisions and their limits. Model, prompt and schema notes are pending. |
| SUB-07 | Final review | No unreported failures; tests/build/demo rerun; known limitations disclosed | Not started | |

## 7. Decision log

Append dated entries for architecture changes, model/prompt/schema releases, evaluation threshold decisions, and scope changes.

| Date | Decision | Alternatives / evidence | Impact and follow-up |
|---|---|---|---|
| 2026-10-23 | Track assessment delivery/support requirements as first-class work alongside coding | Assessment requires breakdown, first release, release strategy, support, and trade-offs | Keep this file updated throughout development; include a concise 1–2 page design document in final submission. |
| 2026-10-23 | Backend is Python 3.12 managed by `uv` (`backend/pyproject.toml`, `.venv`); PDF library is PyMuPDF 2.0 (MuPDF 1.28.5), with OpenCV 5.0 | No PDF library was installed; PyMuPDF gives text, vectors and rendering in one library | Pin versions in outputs (both tools record them). Re-run the regression evaluation on any PyMuPDF upgrade: 2.0's rotated-page `transformation_matrix` quirk is pinned by a test. |
| 2026-10-23 | Canonical coordinates: normalized (u, v) in the **displayed** orientation, top-left origin; native, PyMuPDF, raster and PDF.js spaces converted explicitly; geometry read from the raw /CropBox and /MediaBox | `page.rect` is the rotated rectangle; `page.cropbox` is y-flipped; `transformation_matrix` drops the CropBox offset when rotated (tests) | Detections will store normalized coordinates plus PDF points. Browser parity is verified only against a re-implemented PDF.js transform; confirm with Playwright in Milestone 3. |
| 2026-10-23 | Electrical-plan regions come from a text-anchored ray cast to 0.84/1.56 pt frame lines, labelled `hypothesis` with evidence and warnings | Layout differs per sheet, so fixed coordinates fail; visual inspection on all 4 sheets enclosed the electrical plan | Show the region on the plan for review. The frame-width assumption is specific to this plan set (SUP-04 intake). |
| 2026-10-23 | Legend parsed from each sheet by exact label text; sheet-native stroke-only icons preferred over the supplied crops as templates | 27/27 labels found once per sheet; supplied crops are ≈1.48 px/pt with contaminated cores | Supplied crops are kept as human references. No detector, threshold or model is locked in (T-01). |
| 2026-10-23 | B-03 reviewer: static HTML/JS page served by FastAPI, showing a PyMuPDF 300 DPI region image with an SVG overlay drawn in screen space | Chosen by you over starting Next.js/PDF.js now; no Node build; alignment uses the tested coords module | PDF.js parity stays a Milestone 3 item. The page sends `Cache-Control: no-cache` because stale scripts were observed during the manual check. |
| 2026-10-23 | Ground-truth store: stdlib SQLite with validated JSON rows plus an append-only event log; working DB git-ignored, exports committed | Smallest durable option; triggers make events and candidates immutable | Exports are the record of truth; the working DB can be rebuilt from an export plus events. |
| 2026-10-23 | Two-pass protocol `protocol/1`: pass 1 → blind scan (server returns 409 for annotations, candidates and warnings) → reconcile (match / promote / dismiss; unmatched annotations decided explicitly) → verified | Chosen by you; reduces anchoring on candidates and catches pass-1 misses | Single-reviewer memory leakage is a recorded limitation in every manifest. |
| 2026-10-23 | Coverage grid of ≤100 pt cells tiling the region hypothesis; `pass1_done` and `blind_scan_done` tracked separately | Systematic sweep so candidate lists are never treated as exhaustive | Phase gates refuse to advance until every cell is done. |
| 2026-10-23 | Wiring runs: separate model and export file, no drawing tool in B-03 | Chosen by you; device precision and recall don't need wiring | Revisit for the BOM (B-12). |
| 2026-10-23 | Candidate pre-fill thresholds: raster plain ≥ 0.7, raster thickened ≥ 0.6, plus every vector-signature match. This deviates from the plan's ≥ 0.5 | Milestone 1 measured 85–432 switch candidates per sheet at plain ≥ 0.5, mostly noise. These thresholds set review burden only, not detector operating points | Missed devices are caught by the cell sweep and the blind scan. Thresholds are recorded in each candidate run. |
| 2026-10-23 | Candidate clusters computed at read time across runs; the planned `Candidate.cluster_id` field was dropped | Candidates are immutable, and a run imported later can join an existing cluster | Clustering is deterministic (score-ordered, 6 pt tolerance) and tested. |
| 2026-10-23 | Probe primitives moved verbatim from `tools/symbol_probe.py` to `takeoff/pdf/symbols.py` (`symbols/1`); probe bumped to 0.2.0 | One implementation for the probe and the candidate exporter | Regression: a re-run is identical to Milestone 1 except 29 wall-clock `seconds` fields. |

## Milestone 1 evidence (PDF feasibility) — 2026-10-23

- **Commands, from `backend/`:**
  - `uv sync`
  - `uv run pytest -q` → **127 passed** (`test_coords.py` 109, `test_pdf_diagnostics.py` 9, `test_symbol_probe.py` 9). The first run had 15 failures from the CropBox-offset and rotation quirk; fixed and re-run.
  - `uv run python tools/pdf_diagnostics.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --out ../reports/feasibility/diagnostics` → exit 0, `input_unchanged: true`.
  - `uv run python tools/symbol_probe.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --symbols ../electrical_takeoff_symbols --diag ../reports/feasibility/diagnostics --out ../reports/feasibility/symbols` → exit 0, `input_unchanged: true`, about 90 s.
- **Visual artifacts inspected:** 60 DPI overlays for all 4 sheets, the A1 electrical crop, the template contact sheet, raster candidates (A1, A4) and vector candidates (A1).
- **Report:** `docs/pdf_feasibility_findings.md`, with measured facts, visual observations, hypotheses, failures and unresolved questions kept separate.
- **Original assets:** 31 files hashed at the start and end, 0 mismatches.

## B-03 evidence (ground-truth review tool) — 2026-10-23

- **Acceptance criteria and their tests:**

| AC | What it covers | Tests |
|---|---|---|
| AC1 | Alignment | `test_gt_e2e.py` marker and click tests on 4 sheets; `test_gt_sheets.py` |
| AC2 | Manual create, then reload persists | `test_gt_e2e.py` |
| AC3 | Candidate actions | `test_gt_review.py` |
| AC4 | Schema rules | `test_gt_taxonomy.py` |
| AC5 | History | `test_gt_store.py` |
| AC6 | Coverage and gates | `test_gt_sheets.py`, `test_gt_review.py` |
| AC7 | Warnings | `test_gt_checks.py` |
| AC8 | Exports | `test_gt_export.py` |
| AC9 | Candidate import | `test_gt_candidates.py`, which also regression-checks against the Milestone 1 probe report |
| AC10 | Originals untouched | e2e hash test; 31-file hash check at session end |
| AC11 | Blind scan hides pass-1 hints | `test_gt_api.py` (409s, payload contents) and `test_gt_e2e.py` (0 markers in every cell, no forbidden requests) |
| AC12 | Reconciliation without duplicates | `test_gt_review.py`, `test_gt_e2e.py` |
| AC13 | Persistence and separate tracking | `test_gt_review.py` (restart), `test_gt_api.py` (app restart), `test_gt_e2e.py` (reload) |

- **Commands, from `backend/`:**
  - `uv run pytest -q` → **256 passed** (1 Starlette TestClient deprecation warning).
  - `uv run python tools/gt_review.py import-candidates` → 3 runs (`raster_plain`, `raster_thickened`, `vector_signature`), about 88 s.
  - `uv run python tools/gt_review.py serve`.
- **Failures found and fixed during the work:**
  - `executescript` implicit COMMIT.
  - A 1.12 px image offset, caused by the snapped clip origin.
  - A UI bootstrap null reference.
  - Stale cached `app.js`.
  - Page overflow when the top bar wraps.
  - Transient empty overlay during refresh, found by an e2e test.
  - 55 orphan-candidate warnings burying the others, now grouped by type.
  - One wrong test expectation (two lights 1 pt apart is a duplicate, not a combined fixture).
- **Not done:** the human annotation itself, and a second-reviewer check. These limitations are recorded in the protocol and the manifest.
- **Reconciliation keyboard shortcuts — 2026-10-24** (reviewer request during the first real reconcile; UI only, no schema, protocol rule or API change):
  - Acceptance: in `reconcile` only, **G** selects and centres the next blocking item (pending observations → unmatched annotations → unverified annotations; wraps; "Nothing left" toast when empty). **M**/**O**/**D** match, promote, dismiss the selected observation. **K** keeps an unmatched annotation (reason prompt). **V** saves unsaved panel edits, then verifies. Each action and its button auto-advance to the next item. Keys are inert in other phases and while a form field has focus.
  - Tests: `test_gt_e2e.py::test_reconcile_shortcuts_match_promote_dismiss_keep_verify` and `::test_reconcile_shortcuts_are_inert_outside_reconcile_and_while_typing`. A mutation check (removing the phase guard, then the in-field guard) made the second test fail both times.
  - `uv run pytest -q` → **260 passed** (1 Starlette deprecation warning).
  - Behaviour change: the Verify button became **Save & verify**, so an unsaved room choice is no longer lost.

## Current project pulse

- **Overall status:** In progress
- **Current phase:** B-03 is done: a `verified` ground-truth export (`gt_001_20261025T020009Z`, 300 devices across 4 sheets) is committed. Next is measuring the existing candidate generators against it (B-11 / R1-04). No accuracy metrics exist yet.
- **Next action:** Build the evaluator (B-11) behind `export.require_verified`.
  - Match candidates to ground truth one-to-one within a stated tolerance.
  - Report per-category and per-subtype precision, recall and localization error for `raster_plain`, `raster_thickened` and `vector_signature`.
  - Write the report to `reports/evaluation/`, with false-positive and missed-device examples rendered on the plan.
  - Then use it to decide T-01, the detector approach.
- **Top risks:**
  - Single-reviewer ground truth: the blind scan matched 100% in 4.5–8 min per sheet, so pass-1 misses may survive. A second-reviewer spot check is not done.
  - Ceiling-fan recall: the A1 fan was missed by both candidate generators.
  - Switch and receptacle grouping and rotation misses.
  - Cross-class confusion among circle-type lights and detectors.
  - Overprinted duplicate geometry and text causing double counts.
  - Room polygons and Exterior not yet extracted.
  - Heuristics tuned to this plan set.
  - A multimodal verifier is untested.
  - The supplied symbol crops' low resolution.
