# Takeoff workflow design (R1-01, R1-02, R1-03, B-09, B-10)

**Date:** 2026-10-25 · **Status:** revision 3, awaiting approval.
- Revision 2 added AI verification, the ceiling-fan fallback, exact counters, an explicit ground-truth path and a git preflight (§4.15 to §4.19).
- Revision 3 adds a non-authoritative AI **discovery** pass, the full `model_disagrees` lifecycle, and a leaner AI payload with honest usage accounting (§4.20 to §4.22).
- Revision 3.1 corrects the tile-overlap dedupe and adds a live-access preflight (§4.23, §4.24).

**Detectors:** `det/0` (`backend/takeoff/detection/det0.py`), plus the AI stages `ai-verify/1` and `ai-discover/1` (§5.9).

## 1. Intent

The assessor opens the Escondido PDF, clicks **Analyze**, and sees electrical devices marked on the drawing. They check a few devices (the ceiling fan, some switches and receptacles) for category, subtype and position. They read the takeoff grouped by room and device type, correct a misclassified device and watch the totals update immediately. They zoom in to check marker alignment, then export the takeoff and confirm the exported quantities match the app.

To be convinced, they need to see accurate detection, reliable classification, precise positioning, persistent corrections, and consistent totals from the drawing through to the export.

**Governing rule:** *detectors propose, reviewers decide, saved device records determine the official totals.* Automate discovery and make review efficient, but never let convenience silently compromise the integrity of the takeoff.

## 2. Goals, non-goals, anti-goals

**Goals**
- Analyze the supplied Escondido PDF, with each of the 4 sheets processed separately. Uploads of other PDFs are also supported.
- Show every proposal and device on the drawing, with its review status and a match score labelled as *not a probability*.
- Inspect, correct (category, subtype, room, position), add, reject and delete devices on the plan. Every change updates the counts immediately and persists.
- Counts by sheet, room, category and subtype. Official totals come only from accepted and manually added devices.
- Each Analyze creates a new identifiable run. Earlier results and human corrections stay traceable, and a new run never silently changes them.
- Export device records and totals that reconcile exactly with the saved records.
- Show how uncertain, duplicated, misclassified and missed devices are handled.
- A multimodal model takes part in every analysis in two non-authoritative roles (§5.9):
  - a **classification verifier**, giving a second opinion on each det/0 detection;
  - a **discovery pass** over tiles of the electrical plan, proposing locations det/0 may have missed.

  Both only produce reviewable evidence and candidates.

**Non-goals (this slice)**
- The model deciding anything: it never sets a category, subtype, room, position or review state on a saved device, and never contributes to a total. An AI-discovered location becomes a device only as a *proposal*, and only a reviewer's individual decision can make it count.
- Room boundary extraction (B-06). Rooms are a *proposed* nearest-label assignment, which the reviewer can correct.
- The bill of materials, wiring runs, boxes and gangs (B-12, deferred).
- Calibrated confidence. Thresholds and auto-accept are R1-05, after calibration exists.
- Accuracy claims inside the app. Accuracy comes only from the separate evaluation against `gt_001` (`reports/evaluation/`), with its caveats.
- Multi-user accounts, authentication and hosting (T-04).
- Detector improvements (`det/1`).

**Anti-goals: these would make it a failure even if it technically works**
- A proposal, or a rejected device, contributing to an official total.
- Re-analysis resetting a human-corrected category, subtype, room, position or review status, or deleting any record.
- A count with no marker on the drawing behind it.
- An export with unresolved proposals that looks like a complete, verified takeoff.
- Implying that room detection, or the match score, is reliable when it isn't.

## 3. Constraints (from the assessment and the project rules)

- **The assessment's minimum requirements:**
  - every detection stores its type, its position and its room;
  - counts per room and per plan, with a total for the home;
  - every number traces to marks on the plan;
  - CRUD on the plan (Read shows type, room and confidence), with counts updating immediately.
- **"Show your work on the plan":** room assignments must also be visible on the drawing, not only in a table.
- **Counting rules:**
  - Kitchen and Dining are separate only when labelled separately.
  - Devices outside the building count under **Exterior**.
  - A combined fixture counts once, under its combined type.
  - Switch subtype follows the adjacent letter.
- **Project rules:**
  - Every displayed count derives from saved records.
  - Schema, detector and rule changes are versioned.
  - Completed takeoffs and corrections are never silently overwritten.
  - History is kept (append-only events, as in the ground-truth store).
- **Existing building blocks to reuse:**
  - `takeoff/pdf/coords.py`: canonical normalized (u, v) space, tested alignment.
  - `takeoff/groundtruth/sheets.py`: region, tokens, room labels, region image with exact mapping.
  - `takeoff/groundtruth/candidates.py`: proposals.
  - `takeoff/detection/det0.py`.
  - The reviewer page's overlay approach: raster image plus an SVG drawn in screen space, measured ≤ 1.5 px in Chromium.
  - The store pattern: SQLite, validated JSON rows, an append-only event log with triggers.
- **Timing:** a full `det/0` analysis of 4 sheets takes about 90 s, dominated by candidate generation. Analyze runs in the background and reports progress.

## 4. Decided (by the user)

1. **Layout A:** the drawing on the left; a right panel with the selected device, then review figures, actions and official totals.
2. **Each Analyze creates a new, identifiable run.** Previous results and corrections stay traceable. Whether to show a run picker is a UI decision, not a requirement.
3. **Proposals are separate from devices that count.** Only *accepted* (origin `detector_accepted`) and *manual* (origin `manual`) devices count. *Rejected* devices never count. The two origins are always kept distinct.
4. **Review figures** sit side by side, and **official takeoff totals** come below them. The totals are computed only from accepted and manual devices and clearly labelled, so proposals are never mistaken for verified quantities. Revision 2 defines the figures exactly in §4.17, replacing the overlapping "Proposed / Awaiting review" pair.
5. **Re-analysis outcomes, applied exactly:**
   - Same device, same classification → attach the new detection as evidence.
   - Same device, conflicting classification → preserve the existing record and flag the disagreement.
   - Existing device not detected again → keep it and flag it *not re-detected*.
   - A previously rejected device found again → keep it rejected and attach the new evidence.
   - A new detection with no confident match → create a proposed record.
   - A new run never silently resets a manually corrected subtype, room, position or review status.
6. **Distance alone does not prove identity.** Distance and category establish a *likely* match. When the evidence conflicts or the match is uncertain, the detection becomes a proposal flagged for review.
7. **Evidence:** every proposal references its run and detector version. Every accepted device keeps its candidate evidence and its complete edit history. The subtype source is recorded, especially when adjacent text decides the subtype. The raw score is distinguished from calibrated confidence.
8. **Rooms:**
   - nearest-label assignment is the initial heuristic only;
   - the proposed room is shown;
   - the reviewer can correct it;
   - ambiguous assignments are flagged;
   - automatic room detection is never implied to be reliable.
9. **Bulk accept:**
   - **Scope:** only *unflagged* proposals.
   - **Confirmation:** for example, "Accept 32 unflagged proposals? Official totals will update."
   - **Transactional:** totals change only after the whole transaction succeeds.
   - **Auditable:** records the affected device ids, the reviewer, the timestamp, and the previous and new states.
   - **Reversible:** the whole bulk action can be undone, without losing history.
   - **"Unflagged"** means no ambiguous match, duplicate warning, classification conflict, room ambiguity or other unresolved validation issue. Flagged proposals are never bulk-accepted.
10. **Panel actions:** *Accept selected* · *Accept all unflagged (N)* · *Review flagged* · *Reject selected*.
11. **Export contents:**
    - The **official section** has accepted and manual device records and their totals.
    - **Proposed and rejected records** go in a separate review section.
    - **Each device record:** id, sheet, category, subtype, room, status, coordinates, review state and origin.
    - **The manifest:** the runs, detector versions, review-status counts and unresolved warnings.
    - **If anything is unresolved**, the export says the takeoff is incomplete.
12. **Uploads:** the supplied Escondido plans are ready to analyze, and other PDFs can be uploaded. If a sheet has no recognizable legend, the app explains the limitation, reports that automated detection did not run, and allows manual annotation.
13. **An explicit, saved correction is an acceptance decision.**
    - **Correct and save** (category, subtype, room, or a moved position) → accepted and counted, recorded as `corrected_and_accepted`.
    - **Edit without saving** → stays proposed.
    - **Accept without changes** → accepted and counted.
    - **Reject** → never counted.
    - **Flagged or ambiguous proposals** cannot be bulk-accepted; they need individual review.
    - **History** records the original proposal, the corrected values, the reviewer, the time and the resulting status.
    - **Unresolved warnings:** a saved position correction is accepted only if no unresolved review warning remains. *(My call:)* the same applies to every saved correction. When the save resolves a flag, that flag closes as part of the save: changing the category resolves `classification_conflict`, and choosing a room resolves `room_ambiguous`. If a review flag is still open after the save (for example, `duplicate_warning` still holds), the correction is saved, the device stays proposed, and the panel names the flag that blocks acceptance.
14. **The demo proves:**
    - persistence, by reloading after a correction;
    - alignment, by zooming and panning;
    - export reconciliation, by checking the export against the saved records, not just the UI;
    - the handling of uncertain and missed devices.

**Revision 2 (the user's corrections, 2026-10-25):**

15. **A multimodal model must take part, and must never be authoritative.**
    - **Determination:** the assessment's minimum requirement says "Detect components with AI", and the brief says the app "analyzes each plan separately with AI". `det/0` is a template, vector and text-rule pipeline with no learned model, so on its own it doesn't plainly meet that wording.
    - **Decision:** keep `det/0` as the detector and baseline. Every analysis also runs a Claude vision model as a classification verifier, and, from revision 3, as a discovery pass (§5.9).
    - **Integration, evidence format, failure handling and tests** are specified in §5.9.
    - **The model never produces authoritative totals:** totals come only from saved device records that a reviewer accepted or added.
16. **The demo is robust to a missed ceiling fan.** If the detector finds the A1 ceiling fan, the demo inspects it. If it misses it, the demo shows manual creation, persistence after reload, and the correct totals. Which branch ran is recorded in the demo evidence. Automated detection performance stays a separately measured result (`reports/evaluation/`), never inferred from the demo.
17. **Review figures have exact, non-overlapping meanings.** Every non-deleted device is in exactly one review state; the figures are built from that partition:

    | Figure | Meaning | Identity |
    |---|---|---|
    | **Detected by analysis** *D* | non-deleted devices with an automated origin (`detector` or `ai`, revision 3), in any state | *D = A_det + R + W* |
    | **…of which AI-only candidates** *D_ai* | origin `ai`: found by the AI discovery pass and by nothing else | subset of *D* |
    | **Accepted from detection** *A_det* | automated origin, `accepted` | |
    | **Rejected** *R* | automated origin, `rejected` (manual devices are deleted, never rejected) | |
    | **Awaiting review** *W* | automated origin, `proposed` | *W = W_unflagged + W_flagged* |
    | **…of which flagged** *W_flagged* | awaiting devices with an open review-severity flag. These are never bulk-accepted. Every AI-only candidate is flagged (§4.20) | |
    | **Added manually** *M* | origin `manual` (always accepted) | |
    | **Official takeoff total** | *A_det + M*, reported by origin as `detector_accepted`, `ai_accepted` and `manual` | |
    | **Accepted with open flags** *K* | accepted devices (either origin) with an open review flag, for example `not_redetected` after a re-run or `model_disagrees`. They are still counted, and listed under *Review flagged* | |

    - The "Proposed" label is retired. "Detected by analysis" replaces it, because "proposed" is also a review state.
    - The bulk button's N equals *W_unflagged*.
    - The UI shows each identity as a sum, for example `Detected by analysis 54 = 18 accepted + 4 rejected + 32 awaiting review`.
    - Tests assert every identity.
18. **The ground-truth export path is configurable and verified.** No component assumes a location:
    - room calibration (§5.4) and every test that reads ground truth take the export path explicitly (`--export`, or the `TAKEOFF_GT_EXPORT` environment variable for tests);
    - they call `export.require_verified` first, and stop with a clear message if the export is missing or not verified;
    - the calibrated room parameters record the export id and manifest SHA-256 they came from.

    Checked 2026-10-25: `data/ground_truth/exports/gt_001_20261025T020009Z` exists and passes `require_verified` (level `verified`, hashes intact).
19. **Git readiness is verified before the first task commit:**
    - a clean tree, apart from the design and plan files, which are committed first;
    - a dedicated branch;
    - identity configured;
    - ignore and line-ending rules in place for every new data and evidence path.

    See plan Task 0.

**Revision 3 (the user's final adjustments, 2026-10-25):**

20. **A separate, non-authoritative AI discovery pass** (`ai-discover/1`, §5.9.2).
    - **What it does:** the model looks at overlapping tiles of each sheet's electrical region and proposes device locations.
    - **Storage:** they're kept as immutable `RunDetection`s from source `ai_discover`, deduplicated across tile overlaps and against this run's det/0 detections and the existing devices.
    - **Overlap with something existing** → corroborating evidence, or a `model_disagrees` flag when the category differs.
    - **Nothing at that spot** → a new device with origin `ai`, `proposed`, and the flag `ai_only_candidate` (review). It needs an individual human decision and is never bulk-accepted.
    - **Totals** stay derived only from saved, accepted device records.
21. **The full `model_disagrees` lifecycle** (§5.9.3):
    - a correction that resolves the disagreement closes the flag;
    - keeping a classification that conflicts with the model needs an explicit accept, or an acknowledgement, with an audit reason;
    - unresolved flags block bulk accept and mark exports incomplete;
    - every transition is tested.
22. **A lean AI payload and honest accounting** (§5.9.4):
    - only the legend references relevant to each request are sent;
    - verification is batched;
    - actual token usage (including cache fields), latency and failures are recorded per request and summed per run;
    - `FakeVerifier` and `FakeDiscoverer` outputs are labelled as such everywhere and are **never** used as evidence of live model performance.

**Revision 3.1 (the user's final review, 2026-10-25):**

23. **Tile overlap keeps every valid point.**
    - A detection seen only in a neighbouring tile's overlap survives.
    - Same-category duplicates across tiles become one candidate (the most central observation), with all the observations kept as supporting evidence.
    - Different-category observations stay separate, so the review workflow flags them.

    See §5.9.2.
24. **Live AI access is confirmed before the demo relies on it** (§5.9.5).
    - **The credentials must be the user's own Anthropic API access, with billing.** A Claude Pro subscription does not include API usage. The user confirmed on 2026-10-25 that API budget is available.
    - **The preflight checks:**
      - which credential source and API host would be used, reported without secrets;
      - that the configured model is available to those credentials;
      - that one real vision request succeeds;
      - that both AI stages complete on one real sheet, with usage and errors recorded.
    - **If any check fails,** the demo runs with AI reported as unavailable, never with simulated verdicts presented as live.

## 5. Design (the builder's calls are marked *(my call)*)

### 5.1 Stack *(my call: build on what exists)*

- **Backend:** a new FastAPI app, `takeoff/app/`, launched by `backend/tools/takeoff_app.py serve`. It runs alongside the reviewer on a different default port (8766).
- **Frontend:** a vanilla JS page, `backend/app/static/`, with no build step, like the reviewer. The coordinate and overlay code is shared with the reviewer, not re-implemented, so the alignment tests keep covering it.
- **Storage:**
  - a new SQLite store, `data/takeoff/takeoff.sqlite` (git-ignored), following the ground-truth store pattern: validated Pydantic rows plus an append-only `events` table protected by triggers;
  - uploaded PDFs stored by SHA-256 under `data/takeoff/documents/`, and only ever read.
- **Schema version:** `takeoff-schema/1`.

### 5.2 Records

| Record | Purpose | Mutable? |
|---|---|---|
| `Document` | A PDF: SHA-256, file name, page count, source (`supplied` or `upload`) | No |
| `Sheet` | Per page: sheet id (e.g. A1-111), region, legend found yes/no, any processing error | Recomputed only by a run |
| `Run` | One Analyze: id (`run-NNN`), detector and version, parameters, input run hashes, sheets, status per sheet (`queued`, `running`, `done`, `failed: reason`, `skipped: no legend`), timings | No (status fields until finished) |
| `RunDetection` | One detector output: category, subtype, point, score, members, subtype source, tokens | **Never** |
| `Device` | One physical device in the takeoff (see below) | Yes; every change is an event |
| `Evidence` | A link from a `RunDetection` to a `Device`, with the match outcome and distance | No |
| `Flag` | An open or resolved review issue on a device: type, severity, details, run, resolution | Resolution only |
| `BulkAction` | A bulk accept and its undo: device ids, the previous and new state for each, actor, time | No (an undo is a new linked action) |
| `ModelVerdict` | One AI request's result (§5.9), with `kind` `verify` or `discover`: model id, prompt version, image SHA-256s, the detection ids (verify) or tile id (discover), the parsed output, agreement, stop reason, usage (input, output, cache-creation and cache-read tokens), latency, and the verifier implementation name (`claude` or `fake`), or the error | **Never** |
| `RunDetection` (revision 3) | Gains `source`: `det0` or `ai_discover`. AI-discovered rows carry the model's category, subtype, point and rationale, the `ModelVerdict` id that produced them, and `supporting`: every tile observation merged into it, each with tile id, verdict id, point and centrality | **Never** |
| `Event` | Append-only audit log: entity, action, before and after, actor, time, schema version | **Never** |

**`Device` fields:**
- `id`, `document_sha`, `sheet_id`, `category`, `subtype`, `status`;
- `point` (normalized) and `point_pt`;
- `room` (kind, name, label bbox) and `room_source` (`proposed_nearest_label` or `reviewer`);
- `review_state` (`proposed`, `accepted` or `rejected`) and `origin` (`detector`, `ai` or `manual`);
- `created_by_run`;
- `human_set`: the set of fields a reviewer has changed (`category`, `subtype`, `room`, `point`, `review_state`), used to protect them;
- `deleted`, plus timestamps and a revision number.

An accepted detector device is reported as origin `detector_accepted`. A manual device is `manual` and is accepted on creation.

**Delete vs reject** *(my call)*:
- **Reject** is a review decision on a **detector** device. The device stays visible as rejected and blocks re-proposal at that spot. Manual devices can't be rejected, only deleted (§4.17).
- **Delete** is a soft delete for a mistaken *manual* entry or a duplicate, with a required reason. It's kept in history and the export's review section.
- Both remove the device from the official totals.

### 5.3 Analyze and merge

1. **The run.** `POST /analyze` creates a `Run` and processes each sheet independently in a background worker:
   - candidates (`candidates.generate` for that page);
   - then `det/0`;
   - then `RunDetection` rows;
   - then the AI stages (§5.9): verify det/0's detections, and discover over tiles. Their `ModelVerdict`s and `ai_discover` detections are stored before the merge, so their flags and candidates are created in the same merge transaction.

   A sheet without a recognizable legend gets `skipped: no legend`, with a plain message. Other sheets still complete.
2. **Merge.** Merging into the takeoff happens per sheet, in one transaction, after that sheet's detections are saved. The rules, in deterministic order:
   - **Possible matches** for a detection are non-deleted devices on the same sheet within **6 pt** (the protocol's match tolerance).
   - **Likely match:** exactly one possible device, the same category, and no other detection of this run claims that device (one-to-one, nearest first). Then:
     - subtype equal → attach `Evidence(outcome=confirmed)`;
     - subtype different → attach `Evidence(outcome=conflict)` and open the flag `classification_conflict`. The record is unchanged.
   - **Different category:** exactly one possible device, with a different category. Attach `Evidence(outcome=conflict)` to the existing device and open `classification_conflict`, recording the run's category. The reviewer decides between "same device, keep", "same device, change category" and "separate device, create it". The last option creates a proposed record from that detection.
   - **Ambiguous:** two or more possible devices. Create a new **proposed** device from the detection, flagged `ambiguous_match`, and link the candidate devices in the flag.
   - **No possible match:** create a new **proposed** device.
   - **A rejected device as the likely match:** attach evidence; it stays rejected.
   - **Not re-detected:** any non-deleted device that no detection in this run matched gets the flag `not_redetected`. *(My call:)* this is severity *review* for detector-origin devices, and *info* for manual ones ("still missed by the detector"). Info flags don't block bulk accept.
   - **Fields are never overwritten.** The merge never writes category, subtype, room, point or review state on an existing device. It only adds evidence and flags.
3. **Validation flags**, computed after a merge and after every edit:
   - `duplicate_warning`: two non-deleted, non-rejected devices of the same category within 3 pt.
   - `room_ambiguous`: see 5.4.
   - `no_room`: the device has no room.

   A flag resolves when the reviewer acts on it (accept, edit or acknowledge, with a reason for acknowledgement), or automatically when its condition clears. Either way, the resolution is recorded.
4. **New proposed devices.** They take category, subtype, point and status from the detection, plus a proposed room (5.4), with `review_state=proposed` and `origin=detector`.

### 5.4 Rooms

- **Candidate rooms** *(my call)*:
  - every room label inside the electrical region (`sheets.room_labels`), with each label instance kept separate, so the two `CL` labels on A1 are two rooms;
  - plus **Exterior** and **Unlabeled**.

  Kitchen and Dining are separate only where they are labelled separately.
- **Proposed room:** the label whose box is nearest the device. The device is flagged `room_ambiguous` when:
  - the second-nearest label is within 1.25× the nearest distance, or
  - the nearest label is farther than a distance threshold. Then the proposal becomes *Unlabeled* and the reviewer picks, possibly Exterior.

  *(My call:)* both numbers are set from A1/A2 against the ground-truth rooms of a verified export, passed explicitly (§4.18; today `gt_001`). They are then reported on all 4 sheets in the evaluation, and are never shown in the app as accuracy.
- **Exterior** is never proposed automatically, because building outlines aren't extracted yet. The reviewer sets it.
- **On the plan:**
  - the selected device draws a leader line to its room label;
  - a **Rooms** layer draws every device's leader line;
  - selecting a room row in the totals highlights that room's devices.

### 5.5 Counts

- **One function** derives every count from saved devices. Nothing is cached or stored as a number.
  - **Official totals:** `review_state=accepted` and not deleted, both origins, grouped by sheet → room → category → subtype, with room and sheet totals and a home total per sheet. Each sheet is one home.
  - **Review figures:** exactly as defined in §4.17, with every identity asserted by a test.
- **The invariant (R1-03):** every device counted in an official total is drawn as a marker on its sheet. An automated test asserts it.

### 5.6 UI (layout A)

- **Top bar:**
  - the document (supplied Escondido, or upload…) and the sheet selector;
  - **Analyze**, with progress per sheet;
  - a run summary ("run-002 · det/0 · 4 sheets done · 54 proposals");
  - **Export**.
- **Drawing:** zoom and pan (the reviewer's interaction), with markers coloured by category.
  - **Review state:** proposed is hollow, accepted filled, rejected crossed and faded, manual has a distinct outline.
  - **Flags:** shown as an orange ring.
  - **Layers:** proposed, accepted, rejected, rooms (leader lines), and region.
  - **Selected device:** highlighted.
- **Right panel, top to bottom:**
  1. **Selected device (Read and Update):**
     - category, subtype (legend wording), room and its source, review state and origin;
     - the match score labelled "match score (not a probability)";
     - the run and detector version, the evidence list, the subtype source and tokens;
     - open flags, with their resolve actions;
     - history.
     - **Edit:** category, subtype and room selectors; move by drag or "click new position"; Accept, Reject and Delete (reason).
  2. **Review figures** (§4.17), each shown with its identity:
     - `Detected by analysis D = A accepted + R rejected + W awaiting review`;
     - `Awaiting review W = U unflagged + F flagged`;
     - `Added manually M`;
     - `Official takeoff total T = A accepted + M manual`;
     - `Accepted with open flags K (still counted)`.

     The AI stage's status line sits beside them: `AI verification: done (n) · unavailable: <reason> · off`.
  3. **Actions:** *Accept selected* · *Accept all unflagged (N)*, which needs confirmation · *Review flagged*, which steps through flagged devices · *Reject selected* · *Undo last bulk accept*.
  4. **Official takeoff totals:** room × type, and the home total, for the sheet. Rows link to markers.
- **Create:** a key or button, then click on the plan. Choose category and subtype (suggested from nearby tokens, as in the reviewer) and a room (proposed, editable). The device is saved as `manual` and accepted.
- **Shortcuts** are consistent with the reviewer where they apply.

### 5.7 Export

`POST /export` writes a new immutable directory `data/takeoff/exports/tk_NNN_<timestamp>/`. It contains:

- `devices_official.csv`: accepted and manual devices. Each row has:
  - id, sheet, category, subtype, subtype label, status;
  - room kind and name;
  - u, v, x_pt, y_pt;
  - review state, origin, created-by run, detector version, match score, subtype source.
- `devices_review.csv`: proposed, rejected and deleted devices, with their open flags.
- `totals.json`: official totals by sheet, room, category and subtype, derived from the same records in the same call.
- `manifest.json`:
  - document SHA, schema version, runs and detector versions;
  - review-status counts and open flags by type;
  - `complete: true` only if there are 0 proposed and 0 open review flags; otherwise `complete: false`, with the reasons spelled out;
  - file hashes.
- **The UI** shows the same completeness statement as the manifest.
- **A reconciliation test** recomputes the totals from `devices_official.csv` and asserts they equal both `totals.json` and the API totals.

### 5.8 Versioning and re-analysis safety

- **Bumps:** the detector version is in every run. The schema version is in every record and event. A changed matching or flag rule bumps `takeoff-merge/1`, which is also recorded in the run. The AI stage records its own `ai-verify/1` version, its prompt version and its model id in the run.
- **Re-running** with the same detector and version is allowed. It creates a new run and only adds evidence and flags.

### 5.9 AI stages (`ai-verify/1`, `ai-discover/1`)

**Role.** Two non-authoritative uses of a Claude vision model, run per sheet after det/0 and before the merge:
- **Verify:** a second opinion on det/0's classifications.
- **Discover:** propose locations det/0 may have missed.

Neither ever writes a saved device's fields, its review state, or a total.

**Common settings:**
- **SDK:** the official Anthropic Python SDK (`anthropic`), using `client.messages.parse(..., output_format=<Pydantic model>)`. Images are sent as base64 PNG `image` blocks.
- **Model:** default `claude-opus-5-5`, configurable with `TAKEOFF_AI_MODEL`, and checked with `client.models.retrieve(model)` before the first call.
- **Limits:** a 60 s timeout per request, the SDK's default `max_retries` (2), and up to 4 concurrent requests per sheet.
- **Budget:** a per-run request cap `TAKEOFF_AI_MAX_REQUESTS` (default 200). Requests over the cap are recorded as `skipped_budget`, never silently dropped.
- **Prompts:** versioned (`ai-verify-prompt/1`, `ai-discover-prompt/1`) and stable, in `takeoff/ai/prompt.py`.

#### 5.9.1 Verify (`ai-verify/1`)

**Batching** *(my call)*: up to **8 detections per request** (`VERIFY_BATCH_SIZE = 8`), grouped by sheet, in reading order.
- **Each item** is a labelled 48 × 48 pt crop at 300 DPI (about 200 × 200 px) centred on the detection, plus its text tokens within 15 pt and det/0's category and subtype.
- **The response** is one `AIVerdict` per item id.
- **A missing or duplicated id** gives a `parse_error` for that item only.
- **Why batch:** it cuts requests about 8×, to roughly 40 for 4 sheets instead of about 300. Whether it costs accuracy is measured by `eval_ai_verifier.py`, which also supports `--batch-size 1` for comparison.

**Relevant legend references only.** Each request includes the sheet-native legend icons for:
- the det/0 categories in the batch;
- the categories of every template that fired at those sites (det/0's member proposals);

deduplicated, at most 8 icons per request, each labelled with its legend wording. Icons are not repeated per item.

**Output schema** (`AIVerdict`):
- `item_id: str`;
- `is_device: bool`;
- `category`, a taxonomy category or `"not_a_device"`;
- `subtype: str | None`;
- `rationale: str`, at most 300 characters;
- `uncertain: bool`.

**Effects:**
- **Disagreement** (category differs, subtype differs, not a device, or `uncertain`) opens `model_disagrees` on the device the detection created or matched (§5.9.3).
- **Agreement** is evidence only. It never auto-accepts.

#### 5.9.2 Discover (`ai-discover/1`)

**Tiles:** the electrical region is cut into **200 × 200 pt tiles with 24 pt overlap**, rendered from the 300 DPI region image (about 833 × 833 px, within current models' 2576 px long-edge limit, where coordinates map 1:1 to pixels). That's about 20 tiles per sheet, so roughly 80 requests for 4 sheets.

**Each request:**
- one tile;
- one composite legend reference image (the sheet's legend icons, each labelled), sent once per request;
- the taxonomy list.

*(My call:)* because the static prefix is far below Opus-class prompt caching's 4096-token minimum, caching is not relied on. The cache fields are still recorded, so this is visible.

**Output schema** (`AIDiscovery`): a list of `{x_px, y_px, category, subtype | None, uncertain, rationale (≤150 chars)}` in tile pixel coordinates, with at most 60 items per tile.
- Coordinates are converted to PDF points through the region image mapping.
- **Only invalid points are dropped:** outside the electrical region, or outside the tile's own image (a malformed coordinate). A point anywhere inside its tile, including its overlap strip, is kept. Revision 3.1 corrects this: the earlier "ownership" rule discarded valid detections that only a neighbouring tile's overlap view found.

**From AI points to candidates:**
1. **Merge across tiles** (`ai-discover/1` dedupe):
   - points of the **same category** within 6 pt of each other, from any tiles, form one cluster, built by linking every same-category pair within 6 pt;
   - the cluster's **representative** is the point most central within its own source tile, that is, with the largest distance to that tile's nearest edge. Ties go deterministically to the smallest `(tile id, y_pt, x_pt)`;
   - the representative keeps **every member as supporting evidence**: tile id, verdict id, point and centrality;
   - points of **different categories are never merged** with each other, however close. Each stays its own candidate, so the takeoff merge (step 3) flags the disagreement through the normal review workflow.
2. **Store** each representative as an immutable `RunDetection` with `source="ai_discover"` and `supporting=[…]`, where `supporting` lists all the contributing tile observations.
3. **Merge into the takeoff** *(this is `takeoff-merge/1`; the merge rules themselves are unchanged)*. An AI detection is matched against existing devices **and** against the devices this run's det/0 detections created or matched:
   - **within 6 pt of a device of the same category** → `Evidence(outcome=ai_corroborates)`, and nothing new;
   - **within 6 pt of a device of a different category** → evidence, plus a `model_disagrees` flag on that device;
   - **nothing within 6 pt** → a new device with `origin="ai"`, `review_state="proposed"`, its proposed room, and the flag `ai_only_candidate` (severity review, details: rationale, verdict id, tile id).
   - **A rejected device** at that spot stays rejected; the new detection is linked to it as evidence, nothing more.
4. **`ai_only_candidate`** is closed only by a reviewer: accept (individually), a saved correction (which also accepts), or reject. It is never bulk-accepted. The model's position estimate may be off by several points, so the panel says "AI-proposed location: check position before accepting". *(My call:)* an `ai`-origin device that a later run doesn't find again gets `not_redetected` with severity *info*, as manual devices do.

#### 5.9.3 `model_disagrees` lifecycle

The flag is opened by a verify disagreement or by a discover overlap with a different category. It carries the model's category and subtype and its rationale. Transitions, each recorded with actor, time and resolution:

| Reviewer action | Result |
|---|---|
| **Saved correction** whose final category (and subtype, if the model gave one) **equals the model's** | The flag closes as `resolved_by_correction`. The device is accepted as `corrected_and_accepted`, unless other blocking flags remain |
| **Saved correction** to a classification that **still conflicts** with the model | The correction is saved, the flag stays open, and the device stays proposed. The response names `model_disagrees` and asks for a reason. The same PATCH may include `reason`; then the flag closes as `kept_over_ai: <reason>` and the device is accepted |
| **Accept with no change** | Refused with `ai_disagreement_reason_required`. Accept with `reason` closes the flag as `kept_over_ai: <reason>` and accepts |
| **Acknowledge** (reason required) | The flag closes as `acknowledged: <reason>`. The device stays in its current state and can then be accepted normally |
| **Reject** | The flag closes as `rejected_by_reviewer`. No reason is needed, because rejecting never adds to a total |
| **Bulk accept** | The device is never eligible while the flag is open |
| **Export** | Open flags are listed in `incomplete_reasons` |

#### 5.9.4 Payload, usage accounting and honesty

- **Recorded per request:** `ModelVerdict` stores `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `latency_ms`, plus `stop_reason` and any error.
- **Summed per run:** the run sums requests, tokens, failures by code, skipped-for-budget counts and the p50/p95 latency for each stage. They're shown in the run summary and the export manifest.
- **Failure handling, for both stages:**
  - **SDK missing, no credentials, model not found, or no image capability** → the stage is recorded as `unavailable: <reason>`. Credentials resolve as the SDK does, so an `ant auth login` profile works.
  - **Per-request errors** → a `ModelVerdict` with an error code: `rate_limited`, `timeout`, `connection`, `http_<status>`, `refusal`, `parse_error` or `skipped_budget`. No flags and no candidates come from failed requests.
  - **The stage status** becomes `partial (k errors)`.
  - **The run always completes.**
  - **The UI and manifest** always state each stage's status.
- **Fakes are labelled.** `FakeVerifier` and `FakeDiscoverer` exist only for tests. Every `ModelVerdict` records `implementation` (`claude` or `fake`).
  - Runs made with fakes are marked as such in the UI ("AI: test double, not a model"), the export manifest and the demo README.
  - `eval_ai_verifier.py` and `eval_ai_discovery.py` refuse to run with anything but the Claude implementation.
  - The tracker and reports never cite fake results as model performance.
- **Measured separately**, against a verified export (results to `reports/evaluation/<export>__ai-verify1/` and `__ai-discover1/`):
  - **`eval_ai_verifier.py`:** agreement with the ground truth, and flag precision and recall against det/0's errors, split dev and held-out, at batch sizes 8 and 1.
  - **`eval_ai_discovery.py`:** discovery recall and precision against the ground truth at 6 pt, the share of det/0's misses that discovery finds, localization error, and false candidates per sheet.
  - **Both** record the actual tokens, latency and failures.
  - **Without credentials** they exit with the reason, and the tracker records *not run*.

#### 5.9.5 Live access preflight (`tools/ai_preflight.py`)

The user has confirmed that Anthropic API budget is available (2026-10-25). The preflight confirms that the app actually uses that access. It makes real, billed requests, so it runs only by explicit command, never in the automated suite. It writes `reports/takeoff/ai_preflight.json`, with each check recorded as pass, fail or not run, and its reason:

1. **Credential source:**
   - which mechanism the SDK will use (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant` profile), with values never printed or stored;
   - the API host, from `ANTHROPIC_BASE_URL` or the default.

   A non-default host is flagged, so the user can confirm it's their own API account rather than another tool's environment (for example, a coding assistant's session variables). The report also notes that a Claude subscription alone does not include API usage.
2. **Model availability:** `client.models.retrieve(TAKEOFF_AI_MODEL or default)` succeeds, with image input supported where the field is reported.
3. **One real vision request:** a verify request for one A1 crop succeeds, recording the stop reason, all usage fields and the latency.
4. **Both stages on one real sheet:** an `ai=on` analysis of A1 through the `Analyzer` completes, with both stage statuses `done` or `partial`, and the summed requests, tokens, errors and latency recorded.

The demo README cites the result. If any check fails, the demo states that AI is unavailable, and simulated verdicts are never presented as live.

**Assessment compliance.** With both stages, AI participates in detection (discovery) and classification (verification). The remaining risk is stated plainly rather than claimed away:
- the model's accuracy is unknown until the evaluations run with credentials;
- if the AI is unavailable at demo time, the demo shows det/0-only analysis with "AI unavailable" stated, and the requirement is then not demonstrated live.

## 6. Acceptance criteria

| AC | Criterion | Evidence |
|---|---|---|
| AC-T1 | Analyze the supplied PDF: a new run, 4 sheets processed independently with per-sheet status; `det/0` detections saved unchanged as `RunDetection`s, with run id and detector version | API test plus a real-PDF integration test |
| AC-T2 | Proposals appear on the drawing, aligned within the reviewer's tolerance (≤ 1.5 px across zoom and pan) on all 4 sheets | Playwright |
| AC-T3 | Read: the selected device shows category, subtype, room and its source, review state, origin, the match score labelled "not a probability", run and version, evidence, subtype source and history | Playwright |
| AC-T4 | Update category, subtype, room and position; accept, reject; delete with a reason; create manually. Each persists across reload and updates the totals immediately, with no extra refresh | Playwright and API |
| AC-T5 | Official totals count only accepted and manual devices. Proposed, rejected and deleted devices never count | Unit and property tests |
| AC-T5b | A saved correction of a proposed device (category, subtype, room or position) accepts it as `corrected_and_accepted`, unless a review flag is still open after the save. An unsaved edit changes nothing. History holds the original proposal, the corrected values, the reviewer, the time and the resulting status | Unit, API and Playwright tests |
| AC-T6 | Bulk accept: only unflagged proposals, confirmation, one transaction (a simulated failure leaves totals unchanged), audit record, and full undo that keeps history (a device changed after the bulk accept is skipped and reported) | Unit and API tests |
| AC-T7 | Re-analysis follows the five outcomes in §4.5 exactly, plus the ambiguous and different-category rules in §5.3. Human-set fields are never changed; nothing is deleted | Unit tests per rule, plus a two-run integration test on the real PDF with corrections in between |
| AC-T8 | Rooms: proposed nearest label with source shown; reviewer correction; `room_ambiguous` flagged; leader lines on the plan; no reliability claim | Unit and Playwright tests; room-proposal accuracy against `gt_001` reported in the evaluation, not the app |
| AC-T9 | R1-03 invariant: every officially counted device has a rendered marker on its sheet | Automated test over the API state and the DOM |
| AC-T10 | Export: the files and contents in §5.7; totals reconcile exactly with the saved records; an incomplete takeoff is labelled `complete: false` with reasons | Unit and integration tests |
| AC-T11 | Upload: an uploaded PDF is stored by hash and never modified. A sheet without a legend is reported as not analyzed, with a reason, and manual devices can still be added on it | API test with a synthetic PDF |
| AC-T12 | History: every change is an append-only event (before and after, actor, time, schema version) | Store test (triggers) |
| AC-T13 | Demo path, end to end in Chromium: Analyze → inspect a switch and a receptacle → the ceiling fan, which is inspected if detected and otherwise created manually, with persistence and the correct totals (§4.16; the branch taken is recorded) → correct one → totals change → reload → zoom and pan → export → export totals equal the API totals | Playwright, plus screenshots in `reports/takeoff/demo/`. A separate deterministic test forces the "fan missed" branch |
| AC-T14 | Verify: batched requests (≤ 8 items) carry only the relevant legend icons (≤ 8). Every det/0 detection gets a `ModelVerdict` or a recorded error. A disagreement opens `model_disagrees`; agreement never auto-accepts; no verdict changes a device or a total | Unit and integration tests with `FakeVerifier`; opt-in live smoke test |
| AC-T15 | AI failure handling for both stages: SDK or credentials missing, model not found, each per-request error code, refusal, parse error, and the budget cap give the recorded statuses. The run always completes, and the UI and manifest state each stage's status | Unit and API tests |
| AC-T16 | Review figures follow §4.17. Every identity holds under random device states, and the bulk button's N equals *W_unflagged* | Property test, API and Playwright tests |
| AC-T17 | Ground truth is read only from an explicit export path that passes `require_verified`. The room parameters record their source export id and manifest SHA-256 | Unit test and calibration report |
| AC-T18 | Discover: tiles of 200 pt with 24 pt overlap cover the region. AI points are converted to PDF points. Every valid point is kept, including overlap-only finds. Same-category duplicates within 6 pt become one candidate (the most central observation, with deterministic ties), and every observation is kept as supporting evidence. Different categories are never merged. The candidates are stored as `RunDetection(source="ai_discover")`, and deduplicated against det/0's devices and existing ones (corroborate or `model_disagrees`). New ones become origin `ai` proposals flagged `ai_only_candidate`, never bulk-eligible and counted only after an individual acceptance | Unit tests with `FakeDiscoverer`; analyzer integration; Playwright |
| AC-T19 | The `model_disagrees` lifecycle of §5.9.3: a resolving correction closes the flag; a conflicting correction stays proposed; accept without a reason is refused; accept or acknowledge with a reason closes it with an audit entry; reject closes it; bulk excludes it; the export lists open ones | Unit, API and Playwright tests |
| AC-T21 | The live preflight (§5.9.5) reports the credential source and host without secrets, checks the model, makes one real vision request, and runs both stages on one real sheet. The result is in `reports/takeoff/ai_preflight.json`, and the demo cites it | Run explicitly before the demo; unit tests of its reporting with a stub client |
| AC-T20 | Every AI request records its tokens (including the cache fields), latency, stop reason, errors and implementation, and runs sum them. Fake implementations are labelled in the UI, manifest and README, and the evaluation scripts refuse them | Unit tests, and the eval scripts' guard tests |

## 7. Left to the builder

- **One takeoff per document** (keyed by the PDF's SHA-256) *(my call)*. Analyze defaults to all sheets, and can also run on a single sheet.
- **While a sheet is merging,** edits on that sheet wait for the short merge transaction. Edits on other sheets are unaffected *(my call)*.

- Exact endpoints and payloads, the file layout inside `takeoff/app/`, and the background-task mechanism, which must survive a page reload while the server runs.
- How the reviewer's overlay and coordinate JS is shared. It must be shared, not forked.
- Marker styling details, colours, and keyboard shortcuts.
- The room-ambiguity thresholds, set from A1/A2 as described in §5.4.
- Whether to expose earlier runs in the UI. A run list is not required; the run summary plus per-device evidence is.

## 8. Risks and open points

- **"Detect components with AI"** is resolved in revision 2 (§4.15, §5.9): a non-authoritative Claude vision verifier runs on every detection. **Remaining risks:**
  - its accuracy is unmeasured until `eval_ai_verifier.py` runs with credentials;
  - it costs about 300 requests per full analysis;
  - it doesn't find misses.
- **API access is separate from a Claude subscription.** The user has confirmed API budget. The preflight (§5.9.5) confirms the app uses that account before the demo relies on AI.
- **The AI stages depend on network and credentials at demo time.** If unavailable, the run completes and says so. The demo shows it honestly rather than faking a verdict, and then the AI requirement is not demonstrated live. That is an assessment compliance risk, stated in the demo README.
- **AI discovery quality is unknown:**
  - the localization precision of model-proposed points may be several points off;
  - false candidates add review work (each needs an individual decision);
  - recall on det/0's misses is unmeasured until `eval_ai_discovery.py` runs.
- **API volume:** about 40 verify and 80 discover requests per full analysis, capped by `TAKEOFF_AI_MAX_REQUESTS`. Actual usage is recorded, not estimated.
- **`det/0` was tuned on this plan set.** On a new PDF it may find little or nothing (SUP-04). The upload path makes that visible instead of hiding it.
- **Analyze takes about 90 s,** so the demo shows progress. Generating candidates for one sheet at a time keeps the first results quick.
- **Room proposals:** without room boundaries, nearest-label room proposals will be wrong near walls and in open-plan areas. That's handled by flags and review, not by claiming accuracy.
