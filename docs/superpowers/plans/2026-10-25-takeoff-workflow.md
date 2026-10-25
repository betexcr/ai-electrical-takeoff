# Takeoff Workflow Implementation Plan (revision 3.1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Analyze a PDF into immutable runs, using `det/0` plus two non-authoritative Claude vision stages: verification of det/0's classifications, and discovery of locations det/0 missed. Merge each run into one living takeoff of reviewable devices, and show it on the plan with CRUD, bulk accept and undo. Official totals are derived only from saved accepted and manual devices, and the export reconciles with them.

**Architecture:**
- **Pure rule modules,** unit-tested in isolation: room proposal, merge, flags, counts, tiling.
- **A `TakeoffService`** that applies those rules through a SQLite store with an append-only event log. The store is the ground-truth store, generalized.
- **A background `Analyzer`,** per sheet: candidates, then det/0, then AI verify, then AI discover, then a merge.
- **A FastAPI app,** and a vanilla JS page (layout A) that shares the reviewer's tested plan-view code.

**Tech Stack:** Python 3.12 (uv), FastAPI, Pydantic 2, stdlib sqlite3, PyMuPDF 2.0, OpenCV, the Anthropic Python SDK (`anthropic`, new); vanilla JS; pytest and pytest-playwright (Chromium).

**Spec:** `docs/superpowers/specs/2026-10-25-takeoff-workflow-design.md`, revision 3.1.
- §4 holds the binding decisions: §4.15–4.19 from revision 2, §4.20–4.22 from revision 3.
- §5.9 specifies both AI stages, the `model_disagrees` lifecycle (§5.9.3), and the payload and accounting (§5.9.4).

**Revision 3 changes:**
- **Task 9 is new:** AI discovery.
- **Task 8 (verify):** batched, with relevant legend icons only and full usage accounting.
- **Tasks 5 and 6:** the `model_disagrees` lifecycle, and the `ai_only_candidate` flag.
- **Task 1:** origin `ai`.
- **Task 4:** merge rules for AI detections.
- **Fakes** are labelled everywhere.
- **Renumbering:** the old Tasks 9–14 are now 10–15.
- **Self-contained:** this document no longer refers to earlier revisions for content.

**Revision 3.1 changes:**
- **Task 9:** the tile-overlap dedupe is corrected. Every valid point is kept, including overlap-only finds. Same-category duplicates collapse to the most central observation, with supporting evidence, and different categories stay separate.
- **Task 1:** `RunDetection.supporting`.
- **Task 10:** the live-access preflight `tools/ai_preflight.py` (spec §5.9.5).
- **Task 15:** the demo uses live AI by default and cites the preflight. The user confirmed API budget on 2026-10-25.

## Global Constraints

- **Governing rule:** detectors propose, reviewers decide, saved device records determine the official totals.
- **Official totals:** `review_state == "accepted"` and `deleted == False` only. Proposed, rejected and deleted devices never count.
- **Origins:**
  - internal `detector`, `ai` and `manual`;
  - reported as `detector_accepted`, `ai_accepted` and `manual`;
  - never merged.

  Manual devices are deleted, never rejected.
- **Accept kinds:** `accepted`, `corrected_and_accepted`, `bulk_accepted`, `manual`.
- **A new run never writes** `category`, `subtype`, `room`, `point` or `review_state` on an existing device, and never deletes anything. It only adds `Evidence`, `Flag`, `ModelVerdict` and new proposed devices.
- **The model is never authoritative.**
  - **Verify** can only open `model_disagrees`.
  - **Discover** can only add evidence, open `model_disagrees`, or create an `ai`-origin *proposed* device flagged `ai_only_candidate`.
  - **Agreement** never auto-accepts.
  - **No AI output** feeds a total.
  - **`ai_only_candidate` and open `model_disagrees`** are never bulk-eligible.
- **The `model_disagrees` lifecycle** follows the spec's §5.9.3 table exactly. Keeping a conflicting classification needs a reason (`ai_disagreement_reason_required`).
- **Review-figure identities (spec §4.17):**
  - *D = A_det + R + W*, over automated origins (`detector` and `ai`);
  - *D_ai ⊆ D*;
  - *W = W_unflagged + W_flagged*;
  - *Official = A_det + M*;
  - per sheet (the bulk button is scoped to the sheet shown): the button's N = that sheet's unflagged awaiting *U*, and the sheets' N sum to the document's *W_unflagged*.
- **Tolerances:**
  - match and dedupe: `6.0` pt;
  - duplicate: `3.0` pt;
  - room ambiguity ratio: `1.25`;
  - verify crop: `48` pt, with tokens within `15` pt;
  - discover tile: `200` pt with `24` pt overlap.

  All images are rendered at 300 DPI.
- **AI request parameters:**
  - model `claude-opus-5-5` by default (`TAKEOFF_AI_MODEL`), checked with `client.models.retrieve`;
  - `client.messages.parse(..., output_format=…)`;
  - verify: `max_tokens=2048` per batch of ≤ 8 items, with ≤ 8 relevant legend icons;
  - discover: `max_tokens=4096` per tile, with one composite legend image;
  - `timeout=60`, the SDK's default retries, ≤ 4 concurrent requests per sheet;
  - a per-run cap `TAKEOFF_AI_MAX_REQUESTS=200`.
- **AI error handling:**
  - exceptions are caught most-specific first: `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `APIStatusError`;
  - `stop_reason == "refusal"` is checked before reading the output;
  - the error codes are `rate_limited`, `timeout`, `connection`, `http_<status>`, `refusal`, `parse_error`, `skipped_budget`.
- **AI accounting:** every request records `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `latency_ms`, `stop_reason`, `error` and `implementation` (`claude` or `fake`).
- **Live AI is the expected path.** The user confirmed Anthropic API budget on 2026-10-25. `tools/ai_preflight.py` must pass before the demo relies on AI. It reports the credential source and host without secrets, and flags a non-default `ANTHROPIC_BASE_URL` for confirmation, since another tool's session variables may be present in the shell.
- **Fakes are never evidence:** `FakeVerifier` and `FakeDiscoverer` results are labelled `fake` in the UI, the manifest and the demo README. The evaluation scripts refuse to run with them.
- **Versions:**
  - `takeoff-schema/1`, `takeoff-merge/1`, `takeoff-store/1`;
  - `det0` / `det/0`;
  - `ai-verify/1` (prompt `ai-verify-prompt/1`) and `ai-discover/1` (prompt `ai-discover-prompt/1`);
  - all recorded on every run.
- **Ground truth** is read only from an explicit path (`--export`, or `TAKEOFF_GT_EXPORT` in tests) that passes `export.require_verified`.
- **Paths:**
  - store: `data/takeoff/takeoff.sqlite`;
  - uploads: `data/takeoff/documents/<sha256>.pdf`;
  - render cache: `data/takeoff/cache/`;
  - exports: `data/takeoff/exports/tk_NNN_<UTC stamp>/`.

  All of `data/takeoff/` is git-ignored. Demo evidence goes to `reports/takeoff/demo/`, protected by `-text`.
- **Server:** `uv run python tools/takeoff_app.py serve`, port **8766**.
- **The PDF is only ever read.**
- **UI copy, verbatim.** "Proposed" never labels a count.
  - **Score label:** `match score (not a probability)`
  - **Review lines:**
    - `Detected by analysis {D} = {A} accepted + {R} rejected + {W} awaiting review` · `of which AI-only candidates {Dai}`
    - `Awaiting review {W} = {U} unflagged + {F} flagged`
    - `Added manually {M}`
    - `Official takeoff total {T} = {A} accepted + {M} manual`
    - `Accepted with open flags {K} (still counted)`
  - **Stage lines:** `AI verification: …` and `AI discovery: …`, each one of `done ({n})`, `partial ({k} errors)`, `unavailable: {reason}`, `off`, and with ` · test double, not a model` appended when the implementation is fake.
  - **Device AI line:** `AI second opinion: agrees | disagrees ({what}) | not verified ({reason})`
  - **AI-only device panel:** `AI-proposed location: check position before accepting`
  - **Actions:** `Accept selected`, `Accept all unflagged ({U})`, `Review flagged`, `Reject selected`, `Undo last bulk accept`
  - **Confirmation:** `Accept {U} unflagged proposals? Official totals will update.`
  - **Disagreement prompt:** `The AI classified this as {ai}. Give a reason for keeping {current}.`
  - **Totals heading:** `Official takeoff totals (accepted and manual devices only)`
- **The app shows no accuracy percentage.**
- **Git:** work happens on the branch `takeoff-workflow` (Task 0), with one commit per task. Nothing is pushed or merged by this plan.

## Review Focus

1. **Analyze clicked again while a run is active** → 409 `run_in_progress`, with no duplicate proposals. *(Task 10)*
2. **The same PDF uploaded twice** → one `Document`, with no second takeoff. *(Task 12)*
3. **A non-PDF or corrupted upload** → 400 `invalid_pdf`, and nothing written. *(Task 12)*
4. **A device created or moved outside its region** → 400 `outside_region`, and the device is unchanged. *(Task 6)*
5. **A stale bulk confirmation** → 409 `bulk_count_changed`, nothing accepted. *(Task 7)*

---

## File structure

| File | Responsibility |
|---|---|
| `backend/takeoff/groundtruth/store.py` (modify) | Generic store with class-level tables, versions and event schema |
| `backend/takeoff/app/models.py`, `store.py`, `__init__.py` | `takeoff-schema/1` records; `TakeoffStore` |
| `backend/takeoff/app/sheets.py` | Sheet intake for any PDF; sheet view; legend icons and the composite |
| `backend/takeoff/app/rooms.py` | `propose_room`, with calibrated parameters and their provenance |
| `backend/takeoff/app/merge.py` | Pure merge (`takeoff-merge/1`) for det/0 and AI detections |
| `backend/takeoff/app/flags.py` | Pure flag conditions, blocking and bulk eligibility |
| `backend/takeoff/app/counts.py` | Pure §4.17 review figures and official totals |
| `backend/takeoff/app/service.py` | `TakeoffService`: every state change, transactional and audited |
| `backend/takeoff/ai/__init__.py`, `prompt.py`, `common.py`, `verifier.py`, `discover.py` | The AI stages: shared client, accounting and errors; verify; discover; fakes |
| `backend/takeoff/app/analyze.py` | `Analyzer`: runs and per-sheet stages |
| `backend/takeoff/app/export.py` | Immutable export and the completeness rule |
| `backend/takeoff/app/api.py`, `backend/tools/takeoff_app.py` | API and CLI |
| `backend/tools/eval_rooms.py`, `eval_ai_verifier.py`, `eval_ai_discovery.py` | Separate measurements |
| `backend/tests/gt_config.py` | `gt_export()` for tests |
| `backend/shared/static/planview.js` | Plan-view code shared by both pages |
| `backend/reviewer/static/*` (modify), `backend/app/static/*` | The reviewer (now using shared code) and the takeoff page |
| `backend/tests/test_tk_*.py`, `test_ai_*.py` | Tests |

---

### Task 0: Git and input preflight (no product code)

- [ ] **Step 1: Check the git state.**
  - Run `git status --short`, `git branch --show-current`, `git config user.name`, `git config user.email`, `git config core.autocrlf`.
  - **Expected:** branch `ground-truth-v1`; identity set; `autocrlf=true`; the only changes are `.gitignore`, `PROJECT_TRACKING.md` and `docs/superpowers/`.
  - **Anything else** → stop and report.
- [ ] **Step 2: Create the branch:** `git switch -c takeoff-workflow`.
- [ ] **Step 3: Add the ignore and line-ending rules.**
  - Append `data/takeoff/` to `.gitignore`, and `reports/takeoff/demo/** -text` to `.gitattributes`.
  - Verify that `git check-ignore data/takeoff/takeoff.sqlite .superpowers/x` prints both paths, and that `git check-attr text -- reports/takeoff/demo/manifest.json` prints `text: unset`.
- [ ] **Step 4: Verify the ground-truth input.** Run `cd backend; uv run python -I -c "import pathlib; from takeoff.groundtruth.export import require_verified as r; print(r(pathlib.Path('../data/ground_truth/exports/gt_001_20261025T020009Z'))['level'])"`. Expected: `verified`.
- [ ] **Step 5: Get the test baseline:** `uv run pytest -q`. Expected: `295 passed`. Record it.
- [ ] **Step 6: Commit** `.gitignore`, `.gitattributes`, `PROJECT_TRACKING.md` and `docs/superpowers/`, with the message `docs: takeoff workflow design (rev 3.1) and implementation plan`. Then `git status --short` is empty.

### Task 1: Generic store and takeoff records

**Files:** modify `backend/takeoff/groundtruth/store.py`; create `backend/takeoff/app/{__init__,models,store}.py`; test `backend/tests/test_tk_store.py`

**Interfaces:**
- Produces:
  - **`Store`** gets class attributes `TABLES`, `KEY`, `ENTITY`, `IMMUTABLE`, `STORE_VERSION`, `SCHEMA_VERSION`. `_event` stamps `self.SCHEMA_VERSION`. The module-level aliases are kept, and the ground-truth behaviour is unchanged.
  - **`takeoff.app.models` constants:** `SCHEMA_VERSION = "takeoff-schema/1"`, `MERGE_VERSION = "takeoff-merge/1"`.
  - **Literals:**
    - `ReviewState = proposed|accepted|rejected`
    - `Origin = detector|ai|manual`
    - `AcceptKind = accepted|corrected_and_accepted|bulk_accepted|manual`
    - `RoomSource = proposed_nearest_label|reviewer`
    - `SheetStatus = queued|running|done|failed|skipped_no_legend`
    - `AIStatus = pending|done|partial|unavailable|off`
    - `DetectionSource = det0|ai_discover`
    - `EvidenceOutcome = created|confirmed|conflict|ambiguous|rejected_redetected|ai_corroborates`
    - `FlagType = classification_conflict|ambiguous_match|not_redetected|model_disagrees|ai_only_candidate|duplicate_warning|room_ambiguous|no_room`
    - `Severity = review|info`
    - `VerdictKind = verify|discover`
  - **Models** (fields per spec §5.2 and §5.9):
    - **`Document`:** `id` (= sha256), `file_name`, `page_count`, `source` (`supplied` or `upload`), `stored_path`, `created_at`.
    - **`SheetInfo`:** `id` (`{doc}:{page}`), `document_id`, `page_number`, `sheet_id`, `region_bbox_pt`, `legend_found`, `note`.
    - **`SheetRunStatus`:**
      - `status`, `message`, `n_detections`, `started_at`, `finished_at`;
      - `ai_verify` and `ai_discover`, each `{status: AIStatus, implementation: str | None, requests, errors_by_code: dict, tokens: {input, output, cache_creation, cache_read}, latency_ms: {p50, p95}, n_items}`.
    - **`Run`:** `id` (`run-NNN`), `document_id`, `detector`, `detector_version`, `params`, `merge_version`, `ai_model`, `ai_versions: dict`, `input_runs`, `sheets: dict[str, SheetRunStatus]`, `created_by`, `created_at`, `finished_at`.
    - **`RunDetection`:** `id`, `run_id`, `document_id`, `sheet_id`, `source: DetectionSource`, `category`, `subtype`, `status`, `point`, `point_pt`, `score`, `members`, `subtype_source`, `tokens`, `rationale`, `verdict_id`, `supporting: list[{tile_id, verdict_id, point_pt, centrality_pt}]` (empty for det/0).
    - **`Device`:** `id`, `document_id`, `sheet_id`, `category`, `subtype`, `status`, `point` (`NormPoint`), `point_pt`, `room` (`RoomRef | None`), `room_source`, `review_state`, `origin`, `accept_kind`, `created_by_run`, `human_set: list[Literal["category","subtype","room","point","review_state"]]`, `deleted`, `delete_reason`, `notes`, `created_at`, `updated_at`, `revision`.
    - **`Evidence`:** `id`, `device_id`, `detection_id`, `run_id`, `outcome`, `distance_pt`.
    - **`Flag`:** `id`, `device_id`, `document_id`, `sheet_id`, `type`, `severity`, `details`, `run_id`, `open`, `resolution`, `resolved_by`, `resolved_at`, `created_at`.
    - **`BulkAction`:** `id`, `document_id`, `sheet_id`, `kind` (`bulk_accept` or `undo`), `device_ids`, `before: dict[str, {"review_state", "accept_kind", "revision"}]`, `undo_of`, `skipped`, `actor`, `created_at`.
    - **`ModelVerdict`:** `id`, `run_id`, `sheet_id`, `kind`, `implementation`, `model`, `prompt_version`, `ai_version`, `image_sha256s: list[str]`, `detection_ids: list[str]`, `tile_id`, `output: dict | None`, `agreement: dict[str, {"category": bool, "subtype": bool | None}] | None`, `stop_reason`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `latency_ms`, `error: dict | None`, `created_at`.
  - **`TakeoffStore(path)`:** tables `documents`, `sheets`, `runs`, `detections`, `devices`, `evidence`, `flags`, `bulk_actions`, `verdicts`. `detections`, `evidence`, `bulk_actions` and `verdicts` are immutable. `STORE_VERSION = "takeoff-store/1"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_device_round_trip_and_event_schema_version`
  - `test_detections_evidence_bulk_actions_and_verdicts_are_immutable`: both `replace` and `UPDATE events` raise.
  - `test_ground_truth_store_still_uses_gt_schema`
  - `test_device_rejects_invalid_review_state_and_origin`
  - `test_ai_origin_flag_types_and_verdict_fields_validate`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_store.py`. Expected: FAIL (`ModuleNotFoundError: takeoff.app`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q tests/test_tk_store.py tests/test_gt_store.py tests/test_gt_review.py tests/test_gt_api.py`. Expected: all pass.
- [ ] **Step 5: Commit** with the message `takeoff: generic store and takeoff-schema/1 records`.

### Task 2: Sheet intake for any PDF, legend references

**Files:** `backend/takeoff/app/sheets.py`; test `backend/tests/test_tk_sheets.py`

**Interfaces:**
- Consumes: `groundtruth.sheets` (`sheet_id`, `electrical_region`, `text_tokens`, `room_labels`, `render_region_image`, `ROOM_EXTERIOR`, `ROOM_UNLABELED`, `SheetError`); `pdf.symbols` (`legend_entries`, `find_label`, `LEGEND_LABELS`, `icon_window`, `ink_bbox_in`, `render_gray`); `pdf.layout.text_lines`; `taxonomy`.
- Produces:
  - **`LEGEND_MIN_LABELS = 5`:** the minimum number of `LEGEND_LABELS` found exactly once for `legend_found`. Escondido finds 27/27.
  - **`intake(pdf_path, document_id) -> list[SheetInfo]`,** one per page. On success: the title-block sheet id and the `electrical_region` region. On a `SheetError`: sheet id `page-N`, the full-page region, `legend_found=False`, and a `note`. It never raises on a readable PDF.
  - **`sheet_view(pdf_path, sheet, pdf_sha, cache_dir) -> dict`:** keys `sheet_id`, `page_number`, `geometry`, `region`, `image` (the `render_region_image` mapping), `tokens`, `rooms` (labeled rooms plus Exterior and Unlabeled), `legend_found`, `note`. The same shapes as `groundtruth.sheets.describe_sheet`.
  - **`legend_icons(pdf_path, sheet) -> dict[str, dict]`:** catalogue file → `{"category", "subtype", "label", "png": bytes}`, from the sheet's own legend. Empty without a legend.
  - **`legend_composite(icons) -> bytes`:** one PNG grid of every icon with its legend wording, for discovery. Deterministic: the same input gives the same bytes.

- [ ] **Step 1: Write the failing tests:**
  - `test_escondido_intake_finds_four_sheets_with_legends`
  - `test_blank_pdf_page_is_page_n_without_legend`: a one-page PDF from `pymupdf.open(); new_page()`.
  - `test_sheet_view_matches_reviewer_description`: on A1, the image mapping and room names equal `describe_sheet`'s.
  - `test_legend_icons_found_on_a1_and_empty_without_legend`
  - `test_legend_composite_is_deterministic_and_labelled`: the SHA-256 is equal across calls, and the image width is ≤ 2576 px.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_sheets.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: sheet intake for any PDF and legend references`.

### Task 3: Room proposal, calibrated on A1/A2 from an explicit verified export

**Files:** `backend/takeoff/app/rooms.py`, `backend/tools/eval_rooms.py`, `backend/tests/gt_config.py`; test `backend/tests/test_tk_rooms.py`; output `reports/takeoff/room_proposal_eval.{json,md}`

**Interfaces:**
- Produces:
  - **`ROOM_PARAMS = {"ambiguity_ratio": 1.25, "max_label_distance_pt": <calibrated>, "calibrated_on": {"export_id", "manifest_sha256", "sheets": ["A1-111", "A2-111"], "percentile": 95, "n", "date"}}`**
  - **`label_distance_pt(point_pt, label_bbox_pt) -> float`:** 0 inside the box.
  - **`@dataclass RoomProposal: room: dict | None; ambiguous: bool; reason: str | None; nearest: list[tuple[str, float]]`**
  - **`propose_room(point_pt, rooms, params=ROOM_PARAMS) -> RoomProposal`.** Only `labeled` rooms compete, with nearest distance `d1` and second-nearest `d2`:
    - `d1 > max_label_distance_pt` → Unlabeled, ambiguous, `reason="far_from_labels"`;
    - `d2 <= ratio * d1` → the nearest label, ambiguous, `reason="two_labels_close"`;
    - otherwise the nearest label, not ambiguous;
    - no labels → Unlabeled, ambiguous;
    - Exterior is never proposed.
  - **`tests/gt_config.py: gt_export() -> Path`:**
    - uses `TAKEOFF_GT_EXPORT`, falling back to the repo's `data/ground_truth/exports/gt_001_20261025T020009Z`;
    - calls `require_verified`;
    - an explicit but invalid path raises;
    - a missing fallback gives `pytest.skip("no verified ground-truth export: set TAKEOFF_GT_EXPORT")`.
  - **`tools/eval_rooms.py --export PATH`:** `--export` is required. It calls `require_verified` first and exits 2 on failure.

- [ ] **Step 1: Write the failing tests:**
  - `test_inside_label_box_is_distance_zero`
  - `test_clear_nearest_label_is_not_ambiguous`: labels at 20 pt and 60 pt.
  - `test_two_close_labels_are_ambiguous`: 20 pt and 24 pt.
  - `test_far_from_every_label_proposes_unlabeled`
  - `test_two_cl_labels_are_distinct_rooms`
  - `test_exterior_is_never_proposed`
  - `test_calibration_provenance_matches_export`
  - `test_eval_rooms_refuses_unverified_export`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_rooms.py`. Expected: FAIL.
- [ ] **Step 3: Implement,** with a placeholder `max_label_distance_pt = 1e9`.
- [ ] **Step 4: Calibrate.** Run `uv run python tools/eval_rooms.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z`. On A1/A2 labeled-room annotations only, set `max_label_distance_pt` to the smallest multiple of 10 that is ≥ the 95th percentile of `d1` over the correct-nearest devices. Write the value and its provenance into `ROOM_PARAMS`. Re-run to write the 4-sheet report:
  - dev and held-out accuracy against GT rooms;
  - the ambiguous rate, and the correct-but-flagged rate;
  - the GT Exterior and Unlabeled counts;
  - the header "not shown in the app; not a room-detection accuracy claim".
- [ ] **Step 5: Run** `uv run pytest -q tests/test_tk_rooms.py`. Expected: PASS. Inspect the `.md`.
- [ ] **Step 6: Commit** with the message `takeoff: nearest-label room proposal calibrated on A1/A2 (explicit verified export)`.

### Task 4: Merge (`takeoff-merge/1`) for det/0 and AI detections

**Files:** `backend/takeoff/app/merge.py`; test `backend/tests/test_tk_merge.py`

**Interfaces:**
- Consumes: the models (1) and `RoomProposal` (3).
- Produces:
  - **`@dataclass MergeResult: new_devices: list[Device]; evidence: list[Evidence]; flags: list[Flag]`**
  - **`merge_sheet(run, detections, devices, propose, verdicts: list[ModelVerdict] | None = None, tol_pt=6.0) -> MergeResult`.** It is pure: no store, and the inputs are not mutated. It is deterministic: detections are sorted by `(source == "ai_discover", y_pt, x_pt, id)`, so det/0 comes first, and candidates by `(distance, id)`.
- **Rules for det/0 detections** (spec §5.3). Deleted devices are ignored throughout.
  1. `possible(d)` = devices on the same sheet within `tol_pt`.
  2. **Exactly one possible device, same category,** nearest-first one-to-one claims:
     - rejected device → `Evidence(rejected_redetected)`;
     - subtype equal → `Evidence(confirmed)`;
     - subtype different → `Evidence(conflict)` plus `Flag(classification_conflict, review, {run_category, run_subtype, detection_id})`.

     A later claim on the same device → treated as rule 4.
  3. **Exactly one possible device, different category** → `Evidence(conflict)` plus `Flag(classification_conflict, review, {…, kind: "category"})`, with no new device.
  4. **Two or more possible devices** → a new proposed device, `Evidence(created)`, plus `Flag(ambiguous_match, review, {possible_device_ids})`.
  5. **None** → a new proposed device and `Evidence(created)`.
  6. **A new device takes** the detection's category, subtype, status and point, plus `propose(point_pt).room` and `room_source="proposed_nearest_label"`. It is created with `review_state="proposed"`, `origin="detector"`, `created_by_run=run.id` and `human_set=[]`.
- **Rules for `ai_discover` detections.** Their possible devices are the existing devices, **plus** every device created earlier in this merge: det/0's (rules 5–6), and AI detections processed before this one in sort order. That way, two different-category AI candidates at one spot yield one `ai` proposal and a `model_disagrees` flag.
  - **Same category within 6 pt** → `Evidence(ai_corroborates)`, and nothing new.
  - **Different category within 6 pt** → evidence plus `Flag(model_disagrees, review, {verdict_id, ai_category, ai_subtype, rationale, source: "discover"})`, unless the device is rejected.
  - **None** → a new device with `origin="ai"` and `review_state="proposed"`, plus `Flag(ai_only_candidate, review, {verdict_id, tile_id, rationale})`.
- **Verify verdicts,** for a det/0 detection whose `agreement` is false for the category or subtype, or whose output has `is_device` false or `uncertain` true → `Flag(model_disagrees, review, {verdict_id, ai_category, ai_subtype, rationale, source: "verify"})` on the device that detection created or touched. Rejected devices are skipped, and verdicts with an `error` never flag.
- **Not re-detected:** any non-rejected device touched by no det/0 detection gets `Flag(not_redetected)`. Severity is `review` for origin `detector`, and `info` for `manual` and `ai`.

- [ ] **Step 1: Write the failing tests.** Synthetic factories; each test asserts the exact outcomes and flags, and that the inputs are deep-equal to their pre-call copies:
  - `test_first_run_creates_proposals_with_created_evidence`
  - `test_same_category_same_subtype_confirms`
  - `test_same_category_other_subtype_flags_conflict_and_keeps_record`
  - `test_other_category_flags_conflict_without_new_device`
  - `test_two_possible_devices_create_flagged_proposal`
  - `test_second_claim_on_same_device_becomes_ambiguous_proposal`
  - `test_rejected_device_redetected_stays_rejected_with_evidence`
  - `test_not_redetected_severity_by_origin`
  - `test_human_set_fields_never_change`
  - `test_deleted_devices_are_ignored`
  - `test_boundary_6pt_inclusive_and_order_independent`
  - `test_verify_disagreement_flags_without_changing_device`
  - `test_verify_agreement_adds_no_flag_and_no_acceptance`
  - `test_verdict_error_adds_no_flag`
  - `test_ai_point_near_same_category_corroborates`
  - `test_ai_point_near_other_category_flags_model_disagrees`
  - `test_ai_point_alone_creates_ai_origin_flagged_proposal`
  - `test_ai_point_near_det0_device_created_in_same_merge_is_not_duplicated`
  - `test_ai_point_near_rejected_device_only_links_evidence`
  - `test_second_ai_point_of_other_category_flags_first_ai_proposal`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_merge.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: merge rules for det/0 and AI detections (takeoff-merge/1)`.

### Task 5: Flags, blocking and bulk eligibility

**Files:** `backend/takeoff/app/flags.py`; test `backend/tests/test_tk_flags.py`

**Interfaces:**
- Produces:
  - **`VALIDATION_TYPES = ("duplicate_warning", "room_ambiguous", "no_room")`:** condition flags, severity `review`.
  - **`DECISION_TYPES = ("classification_conflict", "ambiguous_match", "not_redetected", "model_disagrees", "ai_only_candidate")`**
  - **`conditions(devices, rooms, params=ROOM_PARAMS) -> dict[str, dict[str, dict]]`,** over non-deleted devices:
    - `duplicate_warning`: another non-rejected device of the same category within 3.0 pt, flagged on both;
    - `room_ambiguous`: `room_source == "proposed_nearest_label"` and the proposal is ambiguous;
    - `no_room`: the room is None.
  - **`sync_validation(open_flags, current) -> tuple[list[Flag], list[Flag]]`:** returns `(to_open, to_close)`, for condition flags only.
  - **`bulk_eligible(device, open_flags) -> bool`:** `proposed`, not deleted, and no open `review` flag of any type.
  - **`acceptance_blockers(device_id, open_flags, reason: str | None) -> list[Flag]`:** the open `review` condition flags, plus an open `model_disagrees` when `reason` is empty. `classification_conflict`, `ambiguous_match`, `not_redetected` and `ai_only_candidate` don't block an *individual* acceptance; that acceptance closes them.

- [ ] **Step 1: Write the failing tests:**
  - `test_duplicate_within_3pt_flags_both`
  - `test_rejected_device_never_counts_as_duplicate_partner`
  - `test_reviewer_room_clears_room_ambiguous`
  - `test_sync_opens_and_closes_only_condition_flags`
  - `test_bulk_excludes_every_open_review_flag_including_model_disagrees_and_ai_only`
  - `test_info_flags_do_not_block_bulk`
  - `test_model_disagrees_blocks_acceptance_without_reason_only`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_flags.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: flags, acceptance blockers and bulk eligibility`.

### Task 6: TakeoffService — edits, acceptance and disagreement lifecycle, counts

**Files:** `backend/takeoff/app/service.py`, `backend/takeoff/app/counts.py`; tests `backend/tests/test_tk_service.py`, `backend/tests/test_tk_counts.py`, `backend/tests/test_tk_disagreement.py`

**Interfaces:**
- Consumes: `TakeoffStore` (1), `sheet_view` (2), `propose_room` (3), `flags` (5).
- Produces:
  - **`counts.derive(devices, open_flags) -> dict`:**
    - **`review`:** `{detected, detected_ai_only, accepted_detected, rejected, awaiting, awaiting_unflagged, awaiting_flagged, manual, official_total, accepted_with_open_flags}`, per spec §4.17. "Automated" means origin `detector` or `ai`.
    - **`official`:** `{sheet_id: {"by_room": {room_key: {category: {subtype: n}}}, "by_category": {category: {subtype: n}}, "total": n, "by_origin": {"detector_accepted", "ai_accepted", "manual"}}}`, over accepted, non-deleted devices. `room_key` disambiguates repeated label names as `groundtruth.export.room_key` does.
  - **`class TakeoffError(Exception)`,** with `code` and HTTP `status`: `not_found` 404, `invalid` 400, `outside_region` 400, `reason_required` 400, `ai_disagreement_reason_required` 409, `manual_cannot_be_rejected` 400, `blocked_by_flags` 409, `bulk_count_changed` 409, `run_in_progress` 409, `invalid_pdf` 400.
  - **`TakeoffService(store, views: Callable[[str, str], dict])`:**
    - `devices(document_id, sheet_id=None, include_deleted=False) -> list[Device]`
    - `device_detail(device_id) -> dict`: the device, evidence (with run and detection), flags, verdicts (linked through evidence), `ai_summary`, and history.
    - `create_device(document_id, sheet_id, actor, category, subtype, point, room=None, notes=None) -> Device`: origin `manual`, accepted, `accept_kind="manual"`, with a proposed room if none is given. Refused with `outside_region` when the point is outside the sheet's region.
    - **`update_device(device_id, actor, changes: dict, reason: str | None = None) -> dict`** (`{"device", "blocking_flags"}`). Keys are any of `category`, `subtype`, `room`, `point`, `notes`. The steps:
      1. validate against the taxonomy and the region;
      2. update `human_set`, plus `room_source="reviewer"` when the room changes;
      3. sync the validation flags;
      4. close `classification_conflict` when the category or subtype changed;
      5. **`model_disagrees`:** if the final category equals the flag's `ai_category`, and the subtype equals `ai_subtype` (or `ai_subtype` is None), close it as `resolved_by_correction`. Otherwise, if `reason` is given, close it as `kept_over_ai: <reason>`. Otherwise it stays open;
      6. **acceptance:** if the device was `proposed` and `acceptance_blockers(..., reason)` is empty, it becomes `accepted` / `corrected_and_accepted`, closing `ai_only_candidate`, `ambiguous_match` and `not_redetected` with `accepted by reviewer`. Otherwise it stays proposed, and `blocking_flags` lists the blockers.
    - **`accept(device_id, actor, reason=None) -> Device`:**
      - refused with `blocked_by_flags` if condition flags are open;
      - refused with `ai_disagreement_reason_required` if `model_disagrees` is open and `reason` is empty;
      - with a reason, the flag closes as `kept_over_ai: <reason>`;
      - closes the other decision flags; `accept_kind="accepted"`.
    - **`reject(device_id, actor, reason=None) -> Device`:** a manual device raises `manual_cannot_be_rejected`. Closes `model_disagrees` as `rejected_by_reviewer`, and closes the other decision flags.
    - **`delete(device_id, actor, reason) -> Device`:** soft; an empty reason raises `reason_required`.
    - **`acknowledge_flag(flag_id, actor, reason) -> Flag`:** a reason is required. Closes the flag as `acknowledged: <reason>`; the device state is unchanged.
    - **`resolve_conflict(flag_id, actor, choice: Literal["keep", "change_category", "separate_device"]) -> Device`** (for `classification_conflict`):
      - `keep` closes the flag;
      - `change_category` applies the flag's run category and subtype through `update_device`;
      - `separate_device` creates a proposed device from the flag's `detection_id`, with `Evidence(created)`.
    - **`counts(document_id) -> dict`:** `counts.derive`.
  - **Every mutation** runs in one `store.transaction()`, and every row change is an event.

- [ ] **Step 1: Write the failing tests** in `test_tk_counts.py`:
  - `test_only_accepted_non_deleted_devices_count`
  - `test_origins_reported_separately_including_ai_accepted`
  - `test_two_cl_rooms_are_separate_rows`
  - `test_review_partition_example`: 32 awaiting (5 flagged), 18 accepted automated, 4 rejected and 1 manual give `detected=54, awaiting=32, awaiting_unflagged=27, awaiting_flagged=5, manual=1, official_total=19`.
  - `test_review_identities_hold_for_random_states`: 500 random devices over 3 origins, 3 states, deleted, and flags. All identities hold, `detected_ai_only ≤ detected`, and `accepted_with_open_flags` counts only accepted devices.
- [ ] **Step 2: Write the failing tests** in `test_tk_service.py`, with a temporary store and a stub `views` (two labeled rooms, a 0–1000 pt region):
  - `test_saved_category_correction_of_proposal_is_corrected_and_accepted`
  - `test_correction_blocked_by_open_duplicate_stays_proposed`
  - `test_moved_position_is_a_correction_and_accepts`
  - `test_room_change_resolves_room_ambiguous_and_accepts`
  - `test_accept_refuses_when_condition_flag_open`
  - `test_reject_and_delete_never_count`
  - `test_manual_create_counts_as_manual`
  - `test_create_or_move_outside_region_is_refused` *(Review Focus 4)*
  - `test_resolve_conflict_three_choices`
  - `test_deleting_duplicate_partner_clears_the_other_flag`
  - `test_manual_device_cannot_be_rejected_only_deleted`
  - `test_ai_only_candidate_individual_accept_and_correction_close_flag`
  - `test_device_detail_ai_summary_variants`
- [ ] **Step 3: Write the failing tests** in `test_tk_disagreement.py`, one per §5.9.3 row:
  - `test_correction_matching_ai_closes_flag_and_accepts`
  - `test_correction_matching_ai_category_when_ai_subtype_none_closes_flag`
  - `test_conflicting_correction_without_reason_stays_proposed_and_names_flag`
  - `test_conflicting_correction_with_reason_closes_kept_over_ai_and_accepts`
  - `test_accept_without_reason_refused_ai_disagreement_reason_required`
  - `test_accept_with_reason_closes_flag_and_records_audit`: the event has the actor, reason, previous and new state, and the verdict id.
  - `test_acknowledge_requires_reason_and_keeps_device_state`
  - `test_acknowledged_device_then_accepts_without_reason`
  - `test_reject_closes_flag_without_reason`
  - `test_open_flag_keeps_device_out_of_bulk`
- [ ] **Step 4: Run** `uv run pytest -q tests/test_tk_counts.py tests/test_tk_service.py tests/test_tk_disagreement.py`. Expected: FAIL.
- [ ] **Step 5: Implement** `counts.py`, then `service.py`.
- [ ] **Step 6: Run** the same command. Expected: PASS.
- [ ] **Step 7: Commit** with the message `takeoff: edits, acceptance rule, model_disagrees lifecycle and §4.17 figures`.

### Task 7: Bulk accept and undo

**Files:** `backend/takeoff/app/service.py`; test `backend/tests/test_tk_bulk.py`

**Interfaces:**
- Produces:
  - **`bulk_eligible_ids(document_id, sheet_id=None) -> list[str]`:** with `sheet_id`, its length is that sheet's unflagged awaiting *U* (the page's bulk N); without it, the document's *W_unflagged* (the sum of U over the sheets).
  - **`bulk_accept(document_id, actor, expected_count, sheet_id=None) -> BulkAction`:**
    - one transaction, which recomputes eligibility;
    - a count ≠ `expected_count` raises `bulk_count_changed`;
    - sets `accepted` / `bulk_accepted`, and records `before[id] = {review_state, accept_kind, revision}`.
  - **`undo_bulk(bulk_id, actor) -> BulkAction`:**
    - restores `before` only for devices whose current revision equals the recorded one;
    - skips and reports the others;
    - writes `BulkAction(kind="undo", undo_of=…)`;
    - a second undo raises `invalid`.

- [ ] **Step 1: Write the failing tests:**
  - `test_bulk_accepts_only_unflagged_proposals`
  - `test_bulk_never_includes_ai_only_or_model_disagrees`
  - `test_info_flags_remain_eligible`
  - `test_stale_expected_count_accepts_nothing` *(Review Focus 5)*
  - `test_failure_mid_transaction_rolls_back`: monkeypatch `store.replace` to raise on the third device; nothing changes.
  - `test_undo_restores_states_and_keeps_history`
  - `test_undo_skips_devices_changed_after_bulk_and_reports_them`
  - `test_undo_twice_is_refused`
  - `test_bulk_count_equals_awaiting_unflagged`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_bulk.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: transactional, undoable bulk accept`.

### Task 8: AI common layer and verify (`ai-verify/1`)

**Files:** modify `backend/pyproject.toml`, `backend/uv.lock`; create `backend/takeoff/ai/{__init__,prompt,common,verifier}.py`, `backend/tools/eval_ai_verifier.py`; tests `backend/tests/test_ai_common.py`, `backend/tests/test_ai_verifier.py`, `backend/tests/test_ai_live.py` (register the `live` marker)

**Interfaces:**
- **`takeoff.ai.common`:**
  - **Constants:** `DEFAULT_MODEL = "claude-opus-5-5"`, `MAX_CONCURRENCY = 4`, `TIMEOUT_S = 60`, `DEFAULT_MAX_REQUESTS = 200`.
  - **`class AIClient(model=os.environ.get("TAKEOFF_AI_MODEL", DEFAULT_MODEL), client=None)`:**
    - **`available() -> tuple[bool, str | None]`:** False when the `anthropic` import fails, the client can't be built for lack of credentials, or `models.retrieve(model)` raises `NotFoundError` or reports no image input (when that field exists).
    - **`call(system, content_blocks, output_model, max_tokens) -> CallResult`.** It uses `client.with_options(timeout=TIMEOUT_S).messages.parse(model=…, max_tokens=…, system=…, messages=[{"role": "user", "content": content_blocks}], output_format=output_model)`. `CallResult` has `parsed`, `stop_reason`, the four usage fields, `latency_ms` and `error`.
    - **Mapping:** refusal → `refusal`; `parsed_output is None` → `parse_error`; `RateLimitError` → `rate_limited`; `APITimeoutError` → `timeout`; `APIConnectionError` → `connection`; `APIStatusError` → `http_<status>`.
  - **`class RequestBudget(max_requests)`,** thread-safe: `take() -> bool`. A refused take gives `error={"code": "skipped_budget"}` without calling.
  - **`summarize(verdicts) -> dict`:** requests, `errors_by_code`, summed tokens, latency p50/p95, and `implementation`.
  - **`image_block(png: bytes) -> dict`:** a base64 `image` content block, `media_type` `image/png`.
- **`takeoff.ai.verifier`:**
  - **Constants:** `AI_VERSION = "ai-verify/1"`, `PROMPT_VERSION = "ai-verify-prompt/1"`, `VERIFY_BATCH_SIZE = 8`, `MAX_ICONS = 8`.
  - **`class AIVerdict(BaseModel)`:** `item_id: str`, `is_device: bool`, `category: Literal[<taxonomy categories>, "not_a_device"]`, `subtype: str | None`, `rationale: str` (≤ 300 characters), `uncertain: bool`. Wrapped in `AIVerdictBatch(BaseModel): verdicts: list[AIVerdict]`.
  - **`build_crop(region_png, mapping, point_pt, size_pt=48.0) -> bytes`:** padded white at the edges, and deterministic.
  - **`relevant_icons(batch: list[RunDetection], icons) -> list[dict]`:** the icons whose category is any item's det/0 category or any of its `members`' categories, deduplicated, ordered by category and then subtype, at most `MAX_ICONS`.
  - **`build_verify_request(batch, crops, icons, tokens_by_id) -> tuple[str, list[dict]]`.** The system prompt is from `prompt.py`. The content is:
    - a "Reference legend" text, then each relevant icon (a label text plus an image);
    - then, per item: `Item <id>: detector says <category>/<subtype>; nearby text: …`, plus the crop image;
    - no timestamps or random ids.
  - **`class Verifier(Protocol)`:**
    - `implementation: str`, `model: str | None`;
    - `available()`;
    - `verify(run_id, sheet_id, detections, crops, icons, tokens_by_id, budget) -> list[ModelVerdict]`.
  - **`ClaudeVerifier(ai: AIClient)`:** batches in reading order and runs them concurrently, up to `MAX_CONCURRENCY`. Each batch gives one `ModelVerdict(kind="verify", detection_ids=[…], image_sha256s=[…], agreement={id: {...}})`. A missing or duplicated item id → that item's agreement is None, and the verdict's error is `parse_error` for those ids.
  - **`FakeVerifier(script, available=(True, None))`:** `implementation="fake"`, and deterministic.
- **`tools/eval_ai_verifier.py --export PATH [--batch-size 8|1] [--sheets …]`:**
  - `require_verified`, then refuses unless the verifier is `ClaudeVerifier` and available (exit 3 with the reason);
  - runs det/0 and then verify;
  - matches det/0 detections to the ground truth at 6 pt (`takeoff.evaluation.matching`);
  - writes `reports/evaluation/<export id>__ai-verify1[_b1]/` (`metrics.json`, `verdicts.jsonl`, `report.md`). Per dev and held-out group: category and subtype agreement with the ground truth; flag precision and recall against det/0's errors; refusals, errors and budget skips; summed tokens (all four fields); latency p50/p95; requests. The report says "measured separately; not used by the app's totals".

- [ ] **Step 1: Add the dependency:** `uv add anthropic` in `backend/`, then `uv run python -c "import anthropic; print(anthropic.__version__)"`. Record the version.
- [ ] **Step 2: Write the failing tests** in `test_ai_common.py`. A stub SDK client returns responses or raises exceptions built with the SDK's own classes:
  - `test_call_success_records_all_usage_fields_and_latency`
  - `test_each_exception_and_refusal_and_parse_failure_map_to_codes`
  - `test_unavailable_without_sdk_credentials_or_model`
  - `test_budget_skips_are_recorded_not_dropped`
  - `test_concurrency_never_exceeds_4`
  - `test_summarize_sums_tokens_and_counts_errors`
- [ ] **Step 3: Write the failing tests** in `test_ai_verifier.py`:
  - `test_crop_is_48pt_and_deterministic`
  - `test_batches_of_at_most_8_in_reading_order`
  - `test_relevant_icons_only_and_at_most_8_without_repetition`: a batch of 3 receptacles and 1 light, with members' categories, gives exactly those categories' icons, each once.
  - `test_request_has_no_timestamps_and_stable_bytes`: building it twice gives an identical serialized request.
  - `test_missing_item_id_is_parse_error_for_that_item_only`
  - `test_agreement_fields_computed_per_item`
  - `test_fake_verifier_marks_implementation_fake`
  - `test_eval_script_refuses_fake_and_unverified_export`
- [ ] **Step 4: Write the opt-in live test** `test_ai_live.py::test_live_verify_three_a1_items`. It is skipped unless `TAKEOFF_LIVE_AI=1`. It asserts schema-valid verdicts, non-zero `input_tokens` and `latency_ms`, and `implementation == "claude"`. It never asserts accuracy.
- [ ] **Step 5: Run** `uv run pytest -q tests/test_ai_common.py tests/test_ai_verifier.py`. Expected: FAIL.
- [ ] **Step 6: Implement** `common.py`, `prompt.py` (the verify prompt) and `verifier.py`.
- [ ] **Step 7: Run** `uv run pytest -q tests/test_ai_common.py tests/test_ai_verifier.py tests/test_ai_live.py`. Expected: PASS, with the live test SKIPPED unless enabled.
- [ ] **Step 8: Write `eval_ai_verifier.py`.** If credentials exist, run it with `--batch-size 8` and then `--batch-size 1` on gt_001. Otherwise run it once to capture the exit-3 reason. Record the actual outcome for Task 15. Never estimate.
- [ ] **Step 9: Commit** with the message `ai: common layer and ai-verify/1 (anthropic <version>)`. Include the reports only if they were produced.

### Task 9: AI discovery (`ai-discover/1`)

**Files:** `backend/takeoff/ai/discover.py`, `backend/takeoff/ai/prompt.py` (discover prompt), `backend/tools/eval_ai_discovery.py`; test `backend/tests/test_ai_discover.py`

**Interfaces:**
- Consumes: `AIClient`, `RequestBudget`, `image_block` (8); `sheet_view`, `legend_icons`, `legend_composite` (2).
- Produces:
  - **Constants:** `AI_VERSION = "ai-discover/1"`, `PROMPT_VERSION = "ai-discover-prompt/1"`, `TILE_PT = 200.0`, `OVERLAP_PT = 24.0`, `MAX_ITEMS = 60`.
  - **`@dataclass Tile: id: str; sheet_id: str; bbox_pt: tuple`.** There is no ownership area: overlapping tiles may both report a symbol, and both observations are kept.
  - **`tiles(region_bbox_pt, sheet_id) -> list[Tile]`:** row-major, ids `"{sheet}:t{row:02d}{col:02d}"`. Every region point lies in at least one tile.
  - **`centrality_pt(tile, point_pt) -> float`:** the distance from the point to the tile's nearest edge.
  - **`crop_tile(region_png, mapping, tile) -> bytes`**
  - **`class AIPoint(BaseModel)`:** `x_px: float`, `y_px: float`, `category`, `subtype: str | None`, `uncertain: bool`, `rationale: str` (≤ 150 characters). Wrapped in `AIPointList(BaseModel): points: list[AIPoint]` (max 60).
  - **`build_discover_request(tile_png, composite_png, taxonomy_text) -> tuple[str, list[dict]]`:** the composite legend image, then the tile image, then the instruction text. Byte-stable.
  - **`@dataclass TileObservation: tile_id: str; verdict_id: str; category: str; subtype: str | None; point_pt: tuple[float, float]; centrality_pt: float; uncertain: bool; rationale: str`**
  - **`points_to_observations(tile, mapping, region_bbox_pt, points, verdict_id) -> list[TileObservation]`:**
    - pixels → PDF points through the mapping;
    - dropped **only** if outside the region, or outside the tile's own bbox (a malformed coordinate);
    - overlap-strip points are kept.
  - **`merge_across_tiles(run_id, document_id, sheet_id, observations, tol_pt=6.0) -> list[RunDetection]`:**
    - cluster the **same category only**, linking every pair within `tol_pt` (connected components);
    - the representative is the max `centrality_pt`, ties to the min `(tile_id, y, x)`;
    - each cluster gives one `RunDetection(source="ai_discover", verdict_id=representative's, rationale=representative's, supporting=[every member, sorted by (tile_id, y, x)])`, with `members=[]` and `score=None`;
    - different categories are never merged. Each stays its own detection, and the takeoff merge (Task 4) turns a different-category neighbour into a `model_disagrees` flag.
  - **`class Discoverer(Protocol)`:**
    - `implementation`, `model`;
    - `available()`;
    - `discover(run_id, document_id, sheet_id, region_png, mapping, region_bbox_pt, composite_png, budget) -> tuple[list[RunDetection], list[ModelVerdict]]`.

    Each tile gives one `ModelVerdict(kind="discover", tile_id=…)`. A failed tile gives no observations. All observations from all tiles go through `merge_across_tiles`.
  - **`ClaudeDiscoverer(ai)`**, and **`FakeDiscoverer(points_by_tile, available=(True, None))`** with `implementation="fake"`.
- **`tools/eval_ai_discovery.py --export PATH`:**
  - `require_verified`, then refuses fakes or an unavailable AI (exit 3);
  - runs discovery on all sheets;
  - matches the AI points to the ground truth one-to-one at 6 pt, by category;
  - reports, per dev and held-out group: recall, precision, localization error (median and p90); the share of det/0's misses found (det/0's FNs from `reports/evaluation/gt_001_20261025T020009Z__eval1.1__det0/matches.jsonl`); false candidates per sheet; tokens, latency, errors and requests;
  - writes to `reports/evaluation/<export id>__ai-discover1/`, with "measured separately; candidates need human approval; not used by totals".

- [ ] **Step 1: Write the failing tests:**
  - `test_tiles_cover_region_with_overlap`: every region point lies in ≥ 1 tile, adjacent tiles overlap by 24 pt, and A1's region gives about 20 tiles.
  - `test_tile_image_within_2576px_and_deterministic`
  - `test_points_convert_pixels_to_pdf_points`: a synthetic mapping; ≤ 0.5 pt error.
  - `test_only_points_outside_region_or_tile_are_dropped`
  - `test_symbol_found_only_in_neighbouring_tiles_overlap_survives`: tile A reports nothing; tile B reports a ceiling fan inside its overlap strip with A. Expect exactly one `ai_discover` detection at that point, supported by tile B.
  - `test_duplicate_observations_become_one_candidate_with_all_supporting_evidence`: tiles A and B report the same fan 2 pt apart. Expect one detection whose point is the more central observation, and `supporting` lists both, with their tile and verdict ids.
  - `test_centrality_ties_break_deterministically`: equal centrality → the min `(tile_id, y, x)`; shuffled input → identical output.
  - `test_different_categories_are_never_merged`: a fan and a light 2 pt apart in two tiles → two detections. Then, through `merge_sheet`, one `ai` proposal is created, plus a `model_disagrees` flag carrying the other observation.
  - `test_chained_same_category_points_form_one_cluster`: points at 0, 5 and 10 pt → one cluster.
  - `test_request_order_composite_then_tile_and_stable_bytes`
  - `test_failed_tile_yields_verdict_with_error_and_no_detections`
  - `test_budget_exhaustion_marks_tiles_skipped_budget`
  - `test_fake_discoverer_marks_implementation_fake`
  - `test_eval_discovery_refuses_fake_and_unverified_export`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_ai_discover.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS. Add `test_ai_live.py::test_live_discover_one_a1_tile` (opt-in): it asserts the schema, the usage fields, and that the points lie within the tile. No accuracy.
- [ ] **Step 5: Run `eval_ai_discovery.py`** if credentials exist; otherwise record the exit-3 reason. Never estimate.
- [ ] **Step 6: Commit** with the message `ai: ai-discover/1 tiled candidate discovery`. Include the report only if it was produced.

### Task 10: Analyzer — runs, AI stages, and the real-PDF re-analysis integration

**Files:** `backend/takeoff/app/analyze.py`; test `backend/tests/test_tk_analyze.py`

**Interfaces:**
- Consumes: `candidates.generate(pdf, pages=[n])`, `det0.detect`, `Verifier` (8), `Discoverer` (9), `merge_sheet` (4), `flags.sync_validation` (5), `TakeoffService` (6).
- Produces:
  - **`Analyzer(service, pdf_path_of, verifier: Verifier | None, discoverer: Discoverer | None, background=True, max_requests=DEFAULT_MAX_REQUESTS)`:**
    - **`start(document_id, actor, sheet_ids=None, ai: bool = True) -> Run`:**
      - refuses with `run_in_progress` when a sheet is `queued` or `running` *(Review Focus 1)*;
      - records `detector`, `detector_version`, `params`, `merge_version`, `ai_model` and `ai_versions`;
      - uses one `RequestBudget` shared by both stages for the run.
    - `run(run_id)`, `runs(document_id)`
  - **Per sheet:**
    1. a no-legend sheet → `skipped_no_legend`, with the message `"No recognizable symbol legend on this sheet; automated detection did not run. Add devices manually."`, and both AI stages `off`;
    2. `running`;
    3. candidates, det/0, then save the det/0 `RunDetection`s;
    4. **verify** (status `off`, `unavailable`, `done` or `partial`): store the verdicts;
    5. **discover** (same statuses): store the verdicts and the `ai_discover` `RunDetection`s;
    6. **one transaction:** `merge_sheet(run, det0 + ai detections, devices, propose, verdicts)`, inserts, and syncing the validation flags;
    7. `done`. Each stage's `summarize` goes into `SheetRunStatus`.

    A det/0 exception → `failed`. An AI-stage exception → that stage is `unavailable` with the message, and the merge still runs.

- [ ] **Step 1: Write the failing tests** (fakes unless noted):
  - `test_no_legend_sheet_is_skipped_with_message`
  - `test_failed_sheet_does_not_stop_others`
  - `test_second_start_while_running_is_refused` *(Review Focus 1)*
  - `test_ai_off_unavailable_partial_statuses_recorded_and_run_completes`
  - `test_shared_budget_across_stages_records_skipped_budget`
  - `test_usage_summed_per_stage_in_run_status`
  - `test_ai_only_candidates_created_flagged_and_not_bulk_eligible`
  - `test_verify_disagreement_flags_and_changes_nothing`
  - `test_two_runs_on_a1_preserve_corrections`: the real PDF, A1 only, with a `FakeVerifier` that agrees and a `FakeDiscoverer` that returns nothing.
    1. Run 1 creates N proposals, with `RunDetection`s equal to det0's output, the run id and versions, and a verdict per batch.
    2. Correct a subtype, move a device, reject one, add a manual device.
    3. Run 2: the device count is unchanged for existing devices, the corrected fields are unchanged, the rejected device has `rejected_redetected` evidence, the manual device has an info `not_redetected`, and no event in run 2 updates a `human_set` field.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_analyze.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS (about 60 s).
- [ ] **Step 5: Write the live-access preflight** `tools/ai_preflight.py [--model M] [--sheet A1-111] [--no-sheet-run]` (spec §5.9.5). It writes `reports/takeoff/ai_preflight.json` with `checks: [{name, status: pass|fail|not_run, reason, details}]`:
  1. **`credential_source`:**
     - which of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or an `ant` profile is present, with presence only and values never read into the report;
     - the API host from `ANTHROPIC_BASE_URL` (the host only) or `api.anthropic.com`;
     - `needs_confirmation: true` when the host isn't the default.
  2. **`model_available`:** `models.retrieve`.
  3. **`vision_request`:** one verify request on one A1 crop, with usage and latency.
  4. **`stages_on_sheet`:** an `Analyzer` run on a temp store, A1 only, `ai=True`; both stage statuses and the summed usage. Skipped by `--no-sheet-run`.

  Write the unit tests in `tests/test_ai_preflight.py` with a stub client:
  - `test_report_never_contains_secret_values`: set a fake key env var and assert its value isn't in the JSON;
  - `test_non_default_host_needs_confirmation`;
  - `test_each_check_failure_is_recorded_and_later_checks_not_run`.

  Run `uv run pytest -q tests/test_ai_preflight.py`. Expected: PASS.
- [ ] **Step 6: Run the preflight live:** `uv run python tools/ai_preflight.py`. The user confirmed API budget on 2026-10-25, so this is expected to pass; it makes about 25 real requests. Inspect the JSON.
  - **If `credential_source.needs_confirmation`,** stop and ask the user to confirm the host and account before continuing.
  - **If any check fails,** record the reason, and continue the plan with AI treated as unavailable.
  - **Never** substitute fakes as live evidence.
- [ ] **Step 7: Commit** with the message `takeoff: analyzer with immutable runs, AI stages, safe re-analysis and live preflight`, including `reports/takeoff/ai_preflight.json`.

### Task 11: Export

**Files:** `backend/takeoff/app/export.py`; test `backend/tests/test_tk_export.py`

**Interfaces:**
- Produces:
  - **`EXPORT_FORMAT = "takeoff-export/1"`**
  - **`export_takeoff(service, document_id, out_root, actor) -> Path`.** It writes `tk_NNN_<stamp>/` with `exist_ok=False`:
    - **`devices_official.csv`,** with these columns in order: `id, sheet_id, category, subtype, subtype_label, status, room_kind, room_name, u, v, x_pt, y_pt, review_state, origin_reported, accept_kind, created_by_run, detector_version, match_score, subtype_source, ai_summary, ai_implementation, ai_model`. `origin_reported` is `detector_accepted`, `ai_accepted` or `manual`.
    - **`devices_review.csv`:** the same columns, plus `deleted, delete_reason, open_flags`.
    - **`totals.json`:** `counts.derive(...)["official"]`, from the same device list in the same call.
    - **`manifest.json`:**
      - `export_format`, `schema_version`, `merge_version`, the document SHA;
      - runs (id, detector and version, per-sheet status), and per run and stage the AI status, implementation, model, prompt version, requests, tokens, errors and latency;
      - `review` (the §4.17 figures), `open_flags_by_type`, `complete`, `incomplete_reasons`;
      - `ai_note`: `"AI verdicts and AI-only candidates are suggestions; totals come from accepted and manual device records"`, plus `"(test double, not a model)"` when any stage was fake;
      - file hashes, the actor, and the time.
  - **`complete`** is true only with 0 awaiting devices and 0 open `review` flags. `incomplete_reasons` lists, for example, `"32 detected devices awaiting review"`, `"2 open model_disagrees flags"`, `"4 AI-only candidates awaiting review"`, and the informational `"AI discovery unavailable on A2-111: <reason>"`, which does not affect `complete`.

- [ ] **Step 1: Write the failing tests:**
  - `test_official_csv_holds_only_accepted_and_manual`
  - `test_totals_reconcile_with_csv_and_service`
  - `test_incomplete_takeoff_is_marked_with_reasons_including_ai_flags`
  - `test_complete_when_nothing_unresolved`
  - `test_export_never_overwrites`
  - `test_manifest_review_figures_satisfy_identities`
  - `test_manifest_states_ai_status_usage_and_fake_label`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_export.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: reconciling export with completeness and AI accounting manifest`.

### Task 12: API and CLI

**Files:** `backend/takeoff/app/api.py`, `backend/tools/takeoff_app.py`; test `backend/tests/test_tk_api.py`

**Interfaces:**
- Produces:
  - **`create_app(data_dir, supplied_pdf, default_reviewer="reviewer", background=True, ai: Literal["claude", "off"] | tuple[Verifier, Discoverer] = "claude") -> FastAPI`.** It registers the supplied PDF as a `Document` (`source="supplied"`) and runs `intake`. It uses the reviewer's no-cache middleware and error-to-JSON pattern, with `TakeoffError` mapped to `{code, message, …}` and its status. The actor comes from `X-Reviewer`.
  - **Endpoints:**
    - `GET /api/config`: the taxonomy, tolerances, versions, `score_label`, and `ai: {model, verify, discover: {version, prompt_version, implementation, available, reason}}`.
    - `GET /api/documents`
    - `POST /api/documents?file_name=…`: a raw `application/pdf` body. It must open with PyMuPDF, otherwise `invalid_pdf` and nothing written. Deduplicated by SHA-256.
    - `GET /api/documents/{doc}/sheets`
    - `GET /api/documents/{doc}/sheets/{sid}/meta`, `GET …/image`
    - `POST /api/documents/{doc}/analyze` `{sheet_ids?, ai?: bool}` → `Run`
    - `GET /api/runs/{id}`, `GET /api/documents/{doc}/runs`
    - `GET /api/documents/{doc}/devices?sheet_id=`
    - `POST /api/documents/{doc}/devices`
    - `GET /api/devices/{id}`
    - `PATCH /api/devices/{id}` `{changes, reason?}` → `{device, blocking_flags}`
    - `POST /api/devices/{id}/accept` `{reason?}`
    - `POST /api/devices/{id}/reject` `{reason?}`
    - `POST /api/devices/{id}/delete` `{reason}`
    - `POST /api/flags/{id}/acknowledge` `{reason}`
    - `POST /api/flags/{id}/resolve` `{choice}`
    - `GET /api/documents/{doc}/bulk-eligible?sheet_id=` → `{count, ids}`
    - `POST /api/documents/{doc}/bulk-accept` `{expected_count, sheet_id?}`
    - `POST /api/bulk/{id}/undo`
    - `GET /api/documents/{doc}/counts`
    - `POST /api/documents/{doc}/export`
    - `GET /`, `/static/*` (`backend/app/static`), `/shared/*` (`backend/shared/static`)
  - **CLI:** `tools/takeoff_app.py serve [--host 127.0.0.1] [--port 8766] [--reviewer NAME] [--data DIR] [--pdf PATH] [--ai on|off] [--ai-max-requests N]`

- [ ] **Step 1: Write the failing tests** (TestClient, `background=False`, fakes):
  - `test_supplied_document_and_four_sheets_listed`
  - `test_upload_same_pdf_twice_is_one_document` *(Review Focus 2)*
  - `test_upload_non_pdf_is_400_and_writes_nothing` *(Review Focus 3)*
  - `test_no_legend_upload_analyze_reports_skip_and_allows_manual_device`
  - `test_device_crud_round_trip_and_counts_update_in_same_response_cycle`
  - `test_disagreement_reason_flow_over_http`: PATCH without a reason → `blocking_flags`; accept without a reason → 409 `ai_disagreement_reason_required`; with a reason → 200 and the flag closed.
  - `test_error_codes_and_statuses`
  - `test_actor_header_recorded_in_history`
  - `test_config_reports_ai_availability_and_implementation`
  - `test_analyze_with_ai_off`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_api.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message `takeoff: API and serve command`.

### Task 13: Shared plan-view code (reviewer regression-guarded)

**Files:** create `backend/shared/static/planview.js`; modify `backend/reviewer/static/app.js`, `backend/reviewer/static/index.html`, `backend/takeoff/groundtruth/api.py` (mount `/shared`); test the existing `backend/tests/test_gt_e2e.py`, unchanged

**Interfaces:**
- Produces: **`window.makePlanView(state, { viewport, image, onChange })`,** where `state` holds `{meta, view, config}`. It returns:
  - the coordinate functions: `normToImg`, `imgToNorm`, `imgToScreen`, `screenToImg`, `normToScreen`, `screenToNorm`, `distPt`, `ptToScreenLen`;
  - the view functions: `applyView`, `setView`, `fitView`, `minScale`, `zoomAt`, `centerOnNorm`, `attachPanZoom()`;
  - helpers: `svg`, `subLabel`, `subtypeOptions`, `suggestSubtype`, `nearbyTokens`, `roomOptions`.

  The behaviour is moved, not rewritten. The reviewer destructures `makePlanView(st, …)`, so its call sites and the `window.gt` hooks are unchanged.

- [ ] **Step 1: Run** `uv run pytest -q tests/test_gt_e2e.py`. Record the baseline (16 at the time of writing).
- [ ] **Step 2: Move** the functions into `planview.js`, load it before `app.js`, and mount `/shared`.
- [ ] **Step 3: Run** the same command. Expected: the same passed count, including alignment (≤ 1.5 px) and click-to-point (≤ 0.5 pt).
- [ ] **Step 4: Commit** with the message `reviewer: share plan-view code via shared/static/planview.js`.

### Task 14: Takeoff page (layout A)

**Files:** `backend/app/static/{index.html,app.js,style.css}`; test `backend/tests/test_tk_e2e.py` (Playwright; a live uvicorn server on a temp data dir, as in `test_gt_e2e.py`)

**Interfaces:**
- Consumes: the API (12) and `planview.js` (13).
- Produces: `window.tk = { state, refresh, select, setView, get view }`.
- **Page structure (spec §5.6):**
  - **Top bar:** document selector, Upload, sheet selector, **Analyze** (with per-sheet progress polled from `GET /api/runs/{id}` every 1 s), the run summary, the two AI stage lines, and **Export**, which shows `complete` / `incomplete_reasons`.
  - **Drawing:**
    - markers coloured by category: proposed hollow; accepted filled; rejected crossed and faded; manual with a double outline; `ai` origin with a dashed outline and an "AI" tag; an orange ring for any open review flag;
    - layers: proposed, accepted, rejected, AI-only candidates, rooms (leader lines from the device to its room label centre), region;
    - selecting a totals row highlights its devices.
  - **Right panel:**
    1. **Selected device:**
       - category, subtype (legend wording), room and its source, state, origin, `match score (not a probability)`, run and versions, evidence, subtype source and tokens;
       - the AI second-opinion line with its rationale; for `ai` origin, the AI-proposed-location line;
       - flags with their actions, and history.
       - **Edit:** category, subtype and room selectors; **Save**, which sends one PATCH (a saved correction) and, when `blocking_flags` contains `model_disagrees`, shows the disagreement prompt and resends with `reason`; Move (click the new position, then Save); Accept (prompting for a reason when `model_disagrees` is open); Reject; Delete with a reason; Acknowledge with a reason.
    2. **Review lines,** verbatim from the Global Constraints.
    3. **Actions:**
       - **Accept all unflagged ({U}),** with N from `bulk-eligible` and the exact confirmation copy;
       - **Review flagged,** which steps through awaiting devices with review flags, then accepted devices with open flags;
       - **Undo last bulk accept.**
    4. **Official totals**, by room × type and the home total, under the verbatim heading.
  - **Create:** press `C` (or Add), click, choose the category and subtype (suggested from nearby tokens) and the room (proposed), then Save. Saved as `manual` and accepted.

- [ ] **Step 1: Write the failing e2e tests** (fakes; A1 only unless stated; `background=False` seeding):
  - `test_markers_stay_aligned_across_zoom_and_pan`: on all 4 sheets, with one seeded device each; ≤ 1.5 px at 4 zoom levels and one pan, against the independent `expected_screen` re-implementation of `test_gt_e2e.py`. *(AC-T2)*
  - `test_detail_panel_shows_read_fields_and_score_label` *(AC-T3)*
  - `test_saved_correction_accepts_and_totals_update_without_reload`: +1 within 1 s; persists after reload. *(AC-T4, AC-T5b)*
  - `test_move_create_reject_delete_from_the_plan`
  - `test_bulk_accept_confirm_and_undo` *(AC-T6)*
  - `test_every_counted_device_has_a_marker`: each official device id has exactly one `.device[data-id]` on its sheet, and the totals equal the number of counted markers. *(AC-T9, R1-03)*
  - `test_room_leader_lines_drawn_for_every_device` *(AC-T8)*
  - `test_no_percentage_or_accuracy_text_on_page`
  - `test_review_lines_show_identities_and_never_label_proposed` *(AC-T16)*
  - `test_ai_second_opinion_and_disagreement_reason_prompt_flow` *(AC-T19)*
  - `test_ai_only_candidate_shown_dashed_excluded_from_bulk_and_accepted_individually` *(AC-T18)*
  - `test_ai_unavailable_and_fake_labels_are_stated` *(AC-T15, AC-T20)*
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tk_e2e.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same command, then `uv run pytest -q tests/test_gt_e2e.py`. Expected: both PASS.
- [ ] **Step 5: Commit** with the message `takeoff: layout A page with on-plan CRUD, review flows, AI evidence and totals`.

### Task 15: Demo path (ceiling-fan fallback), evidence and tracker

**Files:** `backend/tests/test_tk_e2e.py`, `PROJECT_TRACKING.md`, `README.md`; output `reports/takeoff/demo/`

- [ ] **Step 1: Write the failing test** `test_demo_path_end_to_end`.
  - **Setup:** the real PDF, all 4 sheets, background analysis.
  - **AI implementation:** Claude, because live access is expected, provided `reports/takeoff/ai_preflight.json` shows every check passed. Otherwise the fakes (an agreeing verifier, an empty discoverer). The choice and the preflight result are recorded in `run.json`, and fakes are shown as "test double, not a model".
  - **Steps:**
    1. Analyze, and wait for all sheets to be `done`. The AI lines show the real statuses.
    2. Inspect a switch and a receptacle on A1.
    3. **Ceiling fan.** Take A1's `…/meta` tokens with text `J` and assert there's exactly one (the bedroom fan hub). If a ceiling-fan device lies within 6 pt of its centre, select it and check the panel. Otherwise:
       - press `C` and click there, as `ceiling_fan` / `optional_pre_wire`;
       - reload, and it persists;
       - that room's official `ceiling_fan` total is +1.

       Write `{"fan_branch": "detected" | "manual", "ai_implementation": …}` to `reports/takeoff/demo/run.json`.
    4. If any `model_disagrees` or `ai_only_candidate` exists (live AI only), resolve one through the UI flow. Otherwise record `"ai_flags_demo": "none produced"`.
    5. Correct one proposal's category; the totals change.
    6. Reload; it persists.
    7. Zoom and pan; the markers stay aligned (≤ 1.5 px).
    8. Bulk accept on A1, after the confirmation.
    9. Export: `totals.json` equals `GET /counts` and the recomputation from the CSV. `complete` is false, with reasons, while other sheets await review. The manifest's AI usage equals the sum over the run's verdicts.
    10. Screenshot each step.

  *(AC-T13)*
- [ ] **Step 2: Write the test** `test_missed_ceiling_fan_is_added_manually_and_counted`, which is deterministic. A stub `det0.detect` drops A1's ceiling-fan detections, and the discoverer is empty. Assert:
  - no ceiling-fan device after Analyze;
  - manual creation at the `J` hub;
  - it persists after reload;
  - official ceiling-fan total = 1;
  - `origin_reported == "manual"` in the export.

  *(§4.16)*
- [ ] **Step 3: Run** `uv run pytest -q tests/test_tk_e2e.py -k "demo_path or missed_ceiling_fan"`. Expected: PASS. Inspect every screenshot and `run.json`.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`. Expected: all pass, with the live tests skipped unless enabled. Record the counts.
- [ ] **Step 5: Update the docs.**
  - **Tracker:**
    - R1-01, R1-02, R1-03, B-09, B-10 and SUP-02: evidence, set to `Done` only where every acceptance item has evidence.
    - B-06: the nearest-label proposal and its report; `In progress`.
    - T-01: the AI stages, plus the `eval_ai_verifier` and `eval_ai_discovery` outcomes **exactly as they happened** (report paths with their token totals, or "not run: <reason>"). Fake results are never cited.
    - **The preflight result** (`reports/takeoff/ai_preflight.json`), and whether the demo ran with live AI.
    - **The assessment compliance status, stated plainly:** "AI participates in detection and classification", backed by live evaluation evidence; or "the AI requirement is implemented but not yet demonstrated with a live model".
    - The decision log and the current project pulse.
  - **README:**
    - the takeoff app, `--ai`, `--ai-max-requests`;
    - credentials (`ANTHROPIC_API_KEY` or `ant auth login`), `TAKEOFF_AI_MODEL`, `TAKEOFF_GT_EXPORT`;
    - the demo steps, both fan branches, and what "test double" means.
- [ ] **Step 6: Commit** with the message `takeoff: demo path evidence, tracker and README`. Then `git status --short` is empty.

---

## Spec coverage (self-review)

| Spec item | Task |
|---|---|
| §4.1 layout A | 14 |
| §4.2, §5.3 immutable runs, per-sheet processing | 10 |
| §4.3 origins; §5.5 only accepted and manual count | 1, 6, 11 |
| §4.4, §4.17 review figures and identities (with AI origin) | 6, 11, 14 |
| §4.5, §4.6, §5.3 merge outcomes | 4, 10 |
| §4.7 evidence and score label | 1, 6, 14 |
| §4.8, §5.4, §4.18 rooms from an explicit verified export | 3, 5, 14 |
| §4.9, §4.10 bulk accept | 5, 7, 14 |
| §4.11, §5.7 export | 11 |
| §4.12 uploads and no legend | 2, 10, 12 |
| §4.13 saved correction = acceptance | 6, 14 |
| §4.14, §4.16 demo with the fan fallback | 15 |
| §4.15, §5.9.1 AI verify | 8, 10 |
| §4.20, §5.9.2 AI discovery, dedupe, human approval | 4, 5, 6, 9, 10, 14 |
| §4.21, §5.9.3 `model_disagrees` lifecycle | 5, 6, 12, 14 |
| §4.22, §5.9.4 relevant icons, batching, usage accounting, fakes labelled, separate measurements | 8, 9, 10, 11, 14, 15 |
| §4.23 tile overlap keeps valid points; central representative; supporting evidence | 1, 9 |
| §4.24, §5.9.5 live-access preflight | 10, 15 |
| §4.19 git readiness | 0 |
| §5.2 delete vs reject | 6 |
| §5.8 versions recorded | 1, 8, 9, 10, 11 |
| §6 AC-T1…T20 | T1 10 · T2 14 · T3 14 · T4 6/14 · T5 6 · T5b 6/14 · T6 7 · T7 4/10 · T8 3/14 · T9 14 · T10 11 · T11 12 · T12 1 · T13 15 · T14 4/8/10 · T15 8/9/10/12/14 · T16 6/7/14 · T17 3 · T18 4/9/10/14 · T19 5/6/12/14 · T20 8/9/11/14 · T21 10/15 |
| §8 risks: compliance statement, API volume, AI discovery quality | 15 (tracker and README), 8, 9 (measurements) |
