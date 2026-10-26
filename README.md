# AI Electrical Takeoff

Assessment project: read the supplied electrical plans, find every electrical component, count it per room and let a user correct the result on the plan. The working record of status, evidence and decisions is [PROJECT_TRACKING.md](PROJECT_TRACKING.md). The 2-page design document (breakdown, first release, release, support, trade-offs) is [docs/design_document.md](docs/design_document.md).

> **Status (2026-10-25):** The four sheets have hand-verified ground truth (`gt_001`, 300 devices). The evaluator (`eval/1.1`) has scored the Milestone 1 candidate generators and a first rule-based detector, `det/0`, which reaches F1 0.95 on the held-out sheets A3/A4 ([report](reports/evaluation/gt_001_20261025T020009Z__eval1.1__det0/report.md)). Those numbers are biased upward: the ground truth was built from the same candidates, and all 4 sheets come from one plan set. **The takeoff app works end to end on the supplied PDF** (analyze, review on the plan, correct/add/delete, totals, export; [demo evidence](reports/takeoff/demo/README.md)), with two qualifications: the AI stages ran as test doubles rather than a live model, and uploading a PDF is tested in the browser (Chromium) but is not part of the recorded demo path. **The AI stages are implemented but not yet demonstrated with a live model.** No Anthropic API credentials were configured for the app, so the demo ran with test doubles, and no AI accuracy figures exist. No detector is final.

## Layout
| Path | Contents |
|---|---|
| `plans/`, `docs/Stage 3 …docx`, `electrical_takeoff_symbols/` | Original assessment material. Never modified. |
| `backend/takeoff/pdf/` | Coordinate spaces (`coords.py`), sheet layout (`layout.py`), symbol-matching primitives (`symbols.py`) |
| `backend/takeoff/groundtruth/` | Ground-truth schema, store, review workflow, checks, export, API |
| `backend/takeoff/evaluation/` | Matching and metrics against a verified export (`eval/1.1`) |
| `backend/takeoff/detection/` | Detectors (`det0.py`: rule-based fusion of the candidate generators) |
| `backend/takeoff/app/` | Takeoff app: store, sheet intake, rooms, merge, flags, service, counts, analyzer, export, API |
| `backend/takeoff/ai/` | Non-authoritative AI stages: `ai-verify/1` (verifier), `ai-discover/1` (discovery), prompts, test doubles |
| `backend/reviewer/static/`, `backend/app/static/`, `backend/shared/static/` | Ground-truth reviewer page, takeoff page, and shared plan-view code (vanilla JS) |
| `backend/tools/` | `pdf_diagnostics.py`, `symbol_probe.py`, `gt_review.py`, `evaluate.py`, `takeoff_app.py`, `ai_preflight.py`, `eval_ai_verifier.py`, `eval_ai_discovery.py`, `eval_rooms.py` |
| `docs/` | `design_document.md` (the 2-page design document), `pdf_feasibility_findings.md`, `ground_truth_protocol.md` |
| `reports/` | Generated evidence (overlays, probe results, reviewer screenshots, `evaluation/` reports) |
| `data/ground_truth/` | Working DB and cache (git-ignored); `exports/` holds immutable ground-truth exports |
| `data/takeoff/` | Takeoff store, uploads, exports and render cache (git-ignored) |

## Setup
Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).
```bash
cd backend
uv sync
```
The browser tests use Playwright's Chromium (`uv run playwright install chromium` if it isn't already in the local Playwright cache).

## Tests
```bash
cd backend
uv run pytest -q
```
Add `-m "not e2e"` to skip the browser end-to-end tests.
- Tests marked `live` make real, billed Anthropic requests. They are skipped unless `TAKEOFF_LIVE_AI=1`.
- The ground-truth tests read the verified export named by `TAKEOFF_GT_EXPORT` (default `data/ground_truth/exports/gt_001_20261025T020009Z`). It must pass `require_verified`.
- By default, the demo tests (`test_demo_path_end_to_end`, `test_missed_ceiling_fan_is_added_manually_and_counted`) write their screenshots and `run.json` under pytest's temporary directory, and never touch the committed evidence.
- Set `TAKEOFF_DEMO_EVIDENCE=1` to publish a passing run's evidence to `reports/takeoff/demo/` (or `fan_missed/`). A failing run publishes nothing, and a test-double run never replaces evidence recorded with the live model.

## Takeoff app (R1-01, B-09, B-10)
```bash
cd backend
uv run python tools/takeoff_app.py serve                          # http://127.0.0.1:8766, AI on (Claude)
uv run python tools/takeoff_app.py serve --ai off                 # det/0 only; no Anthropic request is ever made
uv run python tools/takeoff_app.py serve --ai-max-requests 150    # per-run AI request cap
```
- The supplied Escondido PDF is registered at startup and only ever read. Other PDFs can be uploaded from the page.
- The store is `data/takeoff/takeoff.sqlite`. Each export goes to a new, immutable `data/takeoff/exports/tk_NNN_<UTC>/` directory.
- **Moving the project is safe.** Documents are identified by SHA-256. When the supplied PDF is found at a new path, its document is re-pointed (one `repoint` event; no device changes). Uploads are stored relative to the data directory. If a PDF cannot be read, `/counts` still answers: devices are counted under their saved rooms, and the page shows a "Rooms unavailable" notice.
- **Reject or delete.** Reject a detection that is not a real device: it stays visible as rejected and is never proposed again. Delete is only for a mistaken manual entry or a duplicate. A detector or AI device can be deleted only while it has an open `duplicate_warning`, and a deleted duplicate is never re-proposed either. Re-running Analyze adds nothing for devices it already has (`takeoff-merge/2`: one-to-one, nearest-first matching).
- **Local only by default.** The server answers only to a loopback `Host` (`127.0.0.1`, `localhost`, `[::1]`), or to the explicit `--host` you start it with, in which case `serve` prints a warning. API writes must be same-origin JSON requests from the page, so another web site cannot change the takeoff. The status bar names the API host the AI requests go to (and are billed to); it never shows a key.
- `--ai on` (the default) runs two non-authoritative Claude vision stages after det/0 on every sheet:
  - **verify** gives a second opinion on det/0's classifications. A disagreement opens a `model_disagrees` flag.
  - **discover** proposes devices det/0 may have missed. Each becomes an AI-only candidate that needs an individual decision.

  Neither stage changes a saved device or a total; totals come only from accepted and manual device records. If AI is unavailable, the run still completes, and the page and the export state `unavailable: <reason>`.
- `--ai-max-requests` sets the per-run request cap (default `TAKEOFF_AI_MAX_REQUESTS`, else 200). A full 4-sheet analysis needs about 41 verify and 84 discover requests. Requests over the cap are recorded as `skipped_budget`.
- `TAKEOFF_AI_MODEL` sets the model (default `claude-opus-5-5`).

### Credentials for the AI stages
- The app uses the official Anthropic SDK and resolves credentials the same way the SDK does: `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`), or an `ant auth login` profile. Credential values are never printed or stored.
- **A Claude subscription (Pro/Max) does not include API access.** The requests are billed to the Anthropic API account behind the credential.
- Run the live preflight before relying on AI in a demo. It makes real, billed requests, so it runs only by explicit command:
  ```bash
  uv run python tools/ai_preflight.py                                   # writes ../reports/takeoff/ai_preflight.json
  uv run python tools/ai_preflight.py --no-sheet-run                    # checks 1-3 only
  uv run python tools/ai_preflight.py --confirm-host api.example.com    # after confirming a non-default API host is yours
  ```
  - **What it checks:** the credential source and API host, the model, one real vision request, and both stages on one real sheet.
  - **Non-default host:** if `ANTHROPIC_BASE_URL` (or a profile) points to a non-default host, the check fails and nothing is sent. Confirm the host with `--confirm-host` first, because another tool's session variables may be in your shell.
  - **No credentials:** it stops at check 1 and sends nothing.
- Model accuracy is measured separately, against the verified export. These also make real, billed requests:
  ```bash
  uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z
  uv run python tools/eval_ai_discovery.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z
  ```
  Both refuse the test doubles. Without credentials they exit 3 and print the reason.

### Demo path (AC-T13)
1. **Analyze** all sheets and wait until every sheet is `done`. The status bar shows the run summary and each AI stage's status.
2. Click a switch marker, then a receptacle marker, on A1. The panel shows:
   - category, legend subtype, and room with its source;
   - review state, origin, and "match score (not a probability)";
   - run and versions, the AI second opinion, evidence and history.
3. **The ceiling fan** (A1 bedroom, at the single `J` hub):
   - **If det/0 detected it**, select it and inspect it. It did in the committed demo run; the measured distance is in `reports/takeoff/demo/run.json`.
   - **If it was missed**, press **C**, click the hub, choose `ceiling_fan` / *CEILING FAN OPTIONAL (PRE WIRE…)* and Save. The device is manual and counted at once. Reload the page: the fan persists, and that room's official ceiling-fan total has gone up by 1.
4. With live AI, resolve one `model_disagrees` flag or AI-only candidate from the panel. Keeping a classification the model disputes requires a reason.
5. Change one proposal's category and Save. A saved correction is an acceptance, and the totals change at once.
6. Reload: the correction and its history persist.
7. Zoom (wheel) and pan (drag): the markers stay on their symbols.
8. Click **Accept all unflagged (N)**, then confirm. It covers the sheet shown only, and the confirmation names the sheet. The line under the button, "This sheet (…): awaiting X = U unflagged + F flagged", shows that sheet's figures, and N = U. The review lines above it are document-wide. Flagged proposals are never included.
9. **Export**. The status bar says whether the takeoff is complete, and gives the reasons if not. `totals.json` equals both the API's totals and the totals recomputed from `devices_official.csv`.

The automated version is `tests/test_tk_e2e.py::test_demo_path_end_to_end`. Its published screenshots are in [`reports/takeoff/demo/`](reports/takeoff/demo/README.md); run it with `TAKEOFF_DEMO_EVIDENCE=1` to refresh them. It drives zoom with a real mouse wheel and pan with a real drag. `::test_missed_ceiling_fan_is_added_manually_and_counted` forces the "fan missed" branch.

**"Test double"** means `FakeVerifier` and `FakeDiscoverer`. They are deterministic stand-ins used by the tests, and by the demo test when no live preflight has passed. The verifier agrees with det/0 and the discoverer proposes nothing. Everything they produce is labelled "test double, not a model" in the page (stage lines, run summary and each device's AI line, including discover corroboration and AI-only candidates), the export (`ai_implementation`, `ai_summary` and the manifest) and the demo evidence. It is never evidence of model performance.

## Ground-truth review (B-03)
Read [docs/ground_truth_protocol.md](docs/ground_truth_protocol.md) first.
```bash
cd backend
uv run python tools/gt_review.py import-candidates
uv run python tools/gt_review.py --reviewer YOUR_NAME serve
uv run python tools/gt_review.py export
```
- `import-candidates` pre-fills hints. They are never ground truth and are hidden during the blind scan.
- `serve` opens the reviewer at http://127.0.0.1:8765.
- `export` writes a new immutable directory under `data/ground_truth/exports/`.

Each sheet goes through four phases: **pass 1** (annotate every device, cell by cell) → **blind scan** (candidates and annotations hidden; mark devices independently) → **reconcile** (match, promote or dismiss observations, and decide on unmatched annotations) → **verified**.

An export is `verified` only when all four sheets are verified. Any evaluation must call `takeoff.groundtruth.export.require_verified` first.

## Evaluation (B-11)
```bash
cd backend
uv run python tools/evaluate.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z
uv run python tools/evaluate.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --detector det0 --groups "dev=A1-111,A2-111;held_out=A3-111,A4-111"
```
- The second command also scores `det/0`, built from the same checked candidates, and reports the development and held-out sheets separately.
- The evaluator refuses an export that isn't verified or has been altered.
- It regenerates the candidates and refuses to continue if they differ from the ones the reviewer saw (run hashes in the manifest).
- It writes `metrics.json`, `matches.jsonl`, `report.md` and example images to a new folder under `reports/evaluation/`, and never overwrites an existing one.
- Matching rules and the tolerance are in `backend/takeoff/evaluation/matching.py`. Changing them means bumping `EVALUATOR_VERSION`.

## Feasibility tools (Milestone 1)
```bash
cd backend
uv run python tools/pdf_diagnostics.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --out ../reports/feasibility/diagnostics
uv run python tools/symbol_probe.py ../plans/Escondido_Electrical_Plans_Ceiling_Fans.pdf --symbols ../electrical_takeoff_symbols --diag ../reports/feasibility/diagnostics --out ../reports/feasibility/symbols
```
On Windows consoles, set `PYTHONIOENCODING=utf-8`.
