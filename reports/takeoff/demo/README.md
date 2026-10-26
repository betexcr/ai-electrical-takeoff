# Takeoff demo evidence (AC-T13, spec §4.14, §4.16)

Produced by the Playwright/Chromium tests in `backend/tests/test_tk_e2e.py`. From `backend/`:

```bash
uv run pytest -q tests/test_tk_e2e.py -k "demo_path or missed_ceiling_fan"                            # checks only
TAKEOFF_DEMO_EVIDENCE=1 uv run pytest -q tests/test_tk_e2e.py -k "demo_path or missed_ceiling_fan"    # and publishes here
```

## When this evidence changes

**By default** (including the full suite), both tests write their screenshots, `run.json` and export copy to a
staging directory under pytest's `tmp_path`. This directory is never touched.

**With `TAKEOFF_DEMO_EVIDENCE=1`**, a test that passed swaps its staged files into place: the screenshots,
`run.json` and `export/` here, or `fan_missed/` for the deterministic test.
- A failing run publishes nothing.
- A run with the test doubles never replaces evidence whose `run.json` says `"ai_implementation": "claude"`. It
  skips publishing with a warning.
- This README is never rewritten.

The measured, run-specific facts are in `run.json`, not here:
- the analysis time (`analysis_seconds`);
- the fan's distance from the hub (`fan.distance_to_hub_pt`);
- the bulk-accept count (`bulk_accept`);
- the alignment errors (`alignment`);
- the export's reasons (`export.incomplete_reasons`).

## AI in this evidence: Claude, live

The demo test uses Claude only when `reports/takeoff/ai_preflight.json` shows **every** check passed **and**
`TAKEOFF_LIVE_AI=1` is set. Both held for the committed run (2026-10-26, `claude-opus-5-5`, through the user's
`ant` login; `run.json` → `ai_implementation`, `ai_choice`, `ai_usage_run`). Both AI stages ran on all 4 sheets with
no request errors. They produced `model_disagrees` flags and AI-only candidates, and the demo resolved one
`model_disagrees` flag through the panel (`ai_flags_demo`, `05_ai_flag_resolved.png`). Neither stage changed a device
or a total: every count is still from accepted and manual device records.

The demo shows the workflow, not the model's accuracy, which is measured separately against `gt_001`
(`reports/evaluation/…__ai-verify1/`, `__ai-verify1_b1_rerun/`, `__ai-discover1/`). Earlier evidence in this directory
used the labelled test doubles (`FakeVerifier`, `FakeDiscoverer`); a test-double run can no longer replace this one.

Detection here is the real `det/0` on the real Escondido PDF, all 4 sheets in one background run. Its accuracy is
measured separately in `reports/evaluation/` and is never inferred from this demo.

## Files

| File | What it is |
|---|---|
| `run.json` | The demo test's record. It covers the AI implementation and why; the preflight summary; the run id and versions; per-sheet status; the devices inspected and the score each panel showed; the fan branch; the correction; the alignment errors (programmatic views, a real mouse-wheel zoom, a real drag); bulk accept; the R1-03 marker check per sheet; the export checks; and whether it was published |
| `01_analyzed.png` | All 4 sheets `done`, with the run summary, the AI request and token totals, and the live AI stage lines |
| `02_inspect_switch.png`, `03_inspect_receptacle.png` | An A1 switch, then a receptacle, selected. The panel shows category, legend subtype, room and its source, state, origin, "match score (not a probability)", run and versions, the AI second opinion, evidence and history. A det/0 detection with no raster member shows "none (no raster score)" |
| `04_fan_detected.png` (or `04_fan_manual_*.png`) | The fan step, in whichever branch the run took (see below) |
| `05_ai_flag_resolved.png` | A `model_disagrees` flag resolved through the panel. The reviewer kept det/0's classification, with the required reason, so the flag closes as `kept_over_ai`. The panel shows the AI's rationale and evidence |
| `06_corrected.png` | One proposal's category corrected and saved. It becomes `corrected_and_accepted`, and the totals update without a reload. This shows the correction flow; it is not a claim that det/0 was wrong there |
| `07_reloaded.png` | After a reload, the correction persists with its history |
| `08_zoomed.png`, `09_panned.png` | After a real mouse-wheel zoom, then a real mouse drag. The probed markers stay within 1.5 px of independently computed positions |
| `10_bulk_accepted.png` | After the confirmation *Accept N unflagged proposals on sheet A1-111?*. The button is scoped to the sheet shown. The line under it ("This sheet (A1-111): awaiting X = U unflagged + F flagged", from `GET /counts`) now reads 0 unflagged, and A1's flagged proposals stay awaiting review. A2–A4 are untouched. The test first checks every sheet's N and line, and their sums (`bulk_accept.per_sheet_figures_before`) |
| `11_exported.png` | Export: the status bar states *Export incomplete* with the manifest's reasons: devices and AI-only candidates still awaiting review, and open flags. The local export path is shortened to its directory name for the screenshot |
| `export/` | A byte-exact copy of that export: `devices_official.csv`, `devices_review.csv`, `totals.json`, `manifest.json`. The test checked three things: `totals.json` equals `GET /counts`; it equals the totals recomputed from `devices_official.csv`; and the manifest's AI usage equals the sums over the run's saved verdicts |
| `fan_missed/` | The deterministic "fan missed" branch (below): its screenshots and its own `run.json` |

## The ceiling-fan step: both branches

A1 has exactly one `J` text token, the bedroom fan hub.

**Detected.** If a `ceiling_fan` device lies within 6 pt of the hub centre, the demo selects it and checks the
panel. `run.json` records `fan_branch` and the measured distance.

**Manual.** If no ceiling fan lies within 6 pt of the hub, the reviewer presses **C**, clicks the J hub and saves
`ceiling_fan` / `optional_pre_wire`. The test then checks:
- the device persists after a reload;
- the room's official `ceiling_fan` total goes up by 1;
- the export reports it as `origin_reported = manual`.

`test_missed_ceiling_fan_is_added_manually_and_counted` always takes this branch. It runs the real det/0 on A1 with
a stub that drops A1's ceiling-fan detections, and its evidence is in `fan_missed/`.
