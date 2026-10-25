# AI Electrical Takeoff

Assessment project: read the supplied electrical plans, find every electrical component, count it per room and let a user correct the result on the plan. The working record of status, evidence and decisions is [PROJECT_TRACKING.md](PROJECT_TRACKING.md).

> **Status (2026-10-25):** PDF feasibility (Milestone 1) is complete. The four sheets have hand-verified ground truth (`gt_001`, 300 devices), and the evaluator (`eval/1`) has measured the Milestone 1 candidate generators against it ([report](reports/evaluation/gt_001_20261025T020009Z__eval1/report.md)). That baseline is biased upward, because the ground truth was built from those candidates. **No detector has been chosen yet.**

## Layout
| Path | Contents |
|---|---|
| `plans/`, `docs/Stage 3 …docx`, `electrical_takeoff_symbols/` | Original assessment material. Never modified. |
| `backend/takeoff/pdf/` | Coordinate spaces (`coords.py`), sheet layout (`layout.py`), symbol-matching primitives (`symbols.py`) |
| `backend/takeoff/groundtruth/` | Ground-truth schema, store, review workflow, checks, export, API |
| `backend/takeoff/evaluation/` | Matching and metrics against a verified export (`eval/1`) |
| `backend/reviewer/static/` | Reviewer page (vanilla JS) |
| `backend/tools/` | `pdf_diagnostics.py`, `symbol_probe.py`, `gt_review.py`, `evaluate.py` |
| `docs/` | `pdf_feasibility_findings.md`, `ground_truth_protocol.md` |
| `reports/` | Generated evidence (overlays, probe results, reviewer screenshots, `evaluation/` reports) |
| `data/ground_truth/` | Working DB and cache (git-ignored); `exports/` holds immutable ground-truth exports |

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
```
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
