# Takeoff demo evidence (AC-T13, spec §4.14, §4.16)

Written by the Playwright/Chromium tests in `backend/tests/test_tk_e2e.py`, from `backend/`:

```bash
uv run pytest -q tests/test_tk_e2e.py -k "demo_path or missed_ceiling_fan"
```

Every run of these tests (also inside the full suite) rewrites the screenshots, `run.json` and `export/` here, so the
files always describe the latest run. This file is not rewritten.

## AI was a test double in this evidence, not a model

**No Claude request was made.** `reports/takeoff/ai_preflight.json` (run with `--no-sheet-run` on 2026-10-25) failed its
first check, `credential_source`: *no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)*. No
request was sent, and the later checks are `not_run`.

The demo test uses Claude only when that file shows **every** check passed **and** `TAKEOFF_LIVE_AI=1` is set.
Otherwise it uses the test doubles:
- `FakeVerifier` agrees with every det/0 detection;
- `FakeDiscoverer` proposes no points.

They are labelled "test double, not a model" in the page, the run summary, the export manifest (`ai_test_double: true`)
and `run.json` (`"ai_implementation": "fake"`, with the reason in `ai_choice`). Their verdicts are **not** evidence of
model performance. Because they agree and discover nothing, the run produced no `model_disagrees` or
`ai_only_candidate` flags (`"ai_flags_demo": "none produced"`). So the assessment's AI requirement is implemented but
**not yet demonstrated with a live model**.

Detection here is the real `det/0` on the real Escondido PDF (all 4 sheets, one background run, about 100 s). How
accurate det/0 is gets measured separately, in `reports/evaluation/`, and is never inferred from this demo.

## Files

| File | What it is |
|---|---|
| `run.json` | The demo test's record: AI implementation and why, the preflight summary, run id and versions, per-sheet status, the devices inspected, the fan branch, the correction, alignment errors, bulk accept, the R1-03 marker check per sheet, and the export checks |
| `01_analyzed.png` | All 4 sheets `done`; run summary; AI stage lines (test double) |
| `02_inspect_switch.png`, `03_inspect_receptacle.png` | An A1 switch and receptacle selected. The panel shows category, legend subtype, room and its source, state, origin, "match score (not a probability)", run and versions, AI line, evidence and history |
| `04_fan_detected.png` | The fan step (detected branch, see below) |
| `05_corrected.png` | One proposal's category corrected (light → fan) and saved. It becomes `corrected_and_accepted`, and the totals update without a reload. This shows the correction flow; it is not a claim that det/0 was wrong there |
| `06_reloaded.png` | After a reload, the correction persists, with its history |
| `07_zoomed.png`, `08_panned.png` | Zoom and pan. The probed markers stay within 1.5 px of the independently computed positions (`run.json` → `alignment`) |
| `09_bulk_accepted.png` | After the confirmation *Accept N unflagged proposals?* The page's button covers every sheet. Flagged proposals (`room_ambiguous`) stay awaiting review |
| `10_exported.png` | Export. The status bar states *Export incomplete* with the manifest's reasons |
| `export/` | A byte-exact copy of that export (`devices_official.csv`, `devices_review.csv`, `totals.json`, `manifest.json`). The test checked that `totals.json` equals `GET /counts`, equals the totals recomputed from `devices_official.csv`, and that the manifest's AI usage equals the sums over the run's saved verdicts |
| `fan_missed/` | The deterministic "fan missed" branch (below): screenshots and its own `run.json` |

## The ceiling-fan step: both branches

A1 has exactly one `J` text token, the bedroom fan hub.

- **Detected (taken in this run).** det/0 detected a `ceiling_fan / optional_pre_wire` 0.395 pt from the hub centre,
  within the 6 pt match tolerance. The demo selects it and checks the panel. `run.json` → `"fan_branch": "detected"`.
- **Manual (forced in `fan_missed/`).** `test_missed_ceiling_fan_is_added_manually_and_counted` runs the real det/0 on A1
  with a stub that drops A1's ceiling-fan detections. The test then checks four things:
  1. There is no ceiling-fan device. The reviewer presses **C**, clicks the J hub and saves `ceiling_fan` /
     `optional_pre_wire`.
  2. The device persists after a reload.
  3. The room's official `ceiling_fan` total goes from 0 to 1 (BEDROOM / STUDIO).
  4. The export reports it as `origin_reported = manual`.

  The live demo uses the same helper whenever no ceiling fan lies within 6 pt of the hub.
