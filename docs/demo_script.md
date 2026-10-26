# Demo script: AI Electrical Takeoff

This is a run-of-show for the interview's live demo, about 15 minutes, followed by questions. The design walkthrough before it uses [design_document.md](design_document.md). Every example below comes from the live run committed in [`reports/takeoff/demo/`](../reports/takeoff/demo/README.md) and from the evaluations in `reports/evaluation/`.

**The one sentence to say first:** *detectors propose, reviewers decide, and the totals are only ever counts of saved, accepted devices, each one drawn on the plan.*

---

## 1. The day before

1. **Decide which credential to use.** If the repo-root `.env` holds `ANTHROPIC_API_KEY`, the tools use it ahead of your `ant` login. The key there on 2026-10-26 was rejected by the API because it isn't scoped to a workspace; the launcher's check stops and says so. Either replace it with a workspace-scoped key, or add `-IgnoreEnvFile` to every launcher command below.
2. **Sign in:** run `ant auth login`. The token lasts about 8 hours, so sign in again on the morning of the interview.
3. **Build the demo store with live AI.** This takes about 6 minutes and about $5 of API credit:
   ```powershell
   .\demo.ps1 -Fresh
   ```
   In the browser:
   1. Click **Analyze** with *this sheet only* unticked.
   2. Wait until all four sheets show `done`.
   3. Leave the store as it is. Don't review anything yet, so the interview starts from proposals.
4. **Rehearse once** in a throwaway store, starting a fresh one before the real interview:
   1. Rehearse with `.\demo.ps1 -Fresh`.
   2. Analyze again.
   3. Then run `.\demo.ps1 -Fresh` one last time for the store you'll present.

   `-Fresh` never deletes. It moves the old store aside as `data/takeoff/demo-<time>`.
5. **Keep a fallback open:** the screenshots in `reports/takeoff/demo/` (live AI, all steps) and `.\demo.ps1 -NoAI`, which needs no API.

**Budget:** each full 4-sheet Analyze with AI takes about 6 minutes and costs about $5. A single sheet takes 1–2 minutes and costs about $1.

## 2. Start (morning of the interview)

```powershell
ant auth login
.\demo.ps1
```

The launcher does the following:
- checks your credentials and the API host (the preflight's cheap checks, a few cents);
- starts the app on the demo store;
- opens http://127.0.0.1:8766.

Enter your name in **Reviewer**, top left, so every correction is attributed.

**Page layout:** the sheet selector and **Analyze** are at the top. The status bar shows each sheet's state, the AI request and token totals, and the API host being billed. The drawing is in the middle, with layer toggles at the bottom. The right panel holds the selected device, the review figures, the actions and the official totals.

**Keys:** `C` add a device · `A` accept · `R` reject · `G` next flagged · `Del` delete · `Esc` cancel · `1`–`8` category for a new device · wheel zoom · drag pan · `F` fit.

---

## 3. Run of show

### 3.1 What the analysis produced (1 min), sheet A1-111

- **Do:**
  1. Point at the status bar: `4 sheets done · 313 detections · AI requests: verify 41, discover 84`, the token totals, and *AI requests go to api.anthropic.com*.
  2. Point at **Review (document-wide)**: *Detected by analysis 320 = 0 accepted + 0 rejected + 320 awaiting · of which AI-only candidates 7*.
- **Say:**
  - Each sheet is analyzed separately. det/0 proposes devices from templates cut from each sheet's own legend.
  - Claude gives a second opinion on each proposal and scans the plan in tiles for anything det/0 missed.
  - Nothing counts yet: the **official total is 0** until a reviewer accepts.

### 3.2 Inspect a detection (1 min), A1

- **Do:** click a switch marker, say the `F` fan switch in KITCHEN / DINING.
- **Point at:**
  - category, the legend's exact subtype wording, and the room with its source (*proposed nearest label*);
  - **match score (not a probability)**, run and versions, text tokens (`F`);
  - the **AI second opinion**, the evidence and the history.
- **Say:**
  - The subtype comes from the letter next to the symbol, as the counting rules require.
  - The score is a template-match score. I don't present it as confidence, because nothing is calibrated yet.

### 3.3 The ceiling fan (1 min), A1 BEDROOM / STUDIO

- **Do:** click the fan at the `J` hub.
- **Say:**
  - det/0 found it 0.4 pt from the hub.
  - If it hadn't, you'd add it here: press **C**, click the hub, choose *ceiling fan*, Save. It counts at once and persists.
  - That missed-fan branch is tested separately; the evidence is in `reports/takeoff/demo/fan_missed/`.

### 3.4 The AI catches a detector mistake (2 min), A3-111

- **Do:**
  1. Switch to A3. Find the exterior wall light at the door, beside the `3D` and `A` switches, labelled `EXT +84`.
  2. det/0 proposed it as an **exhaust fan**. The AI's line reads *disagrees (light / exterior_wall_photosensor)*, and a `model_disagrees` flag is open.
- **Say:**
  - The model doesn't change the record; it raises a flag.
  - I correct it to the light it is: change Category and Subtype, then Save.
  - A saved correction is an acceptance, and the flag closes as resolved by correction.
  - Then set the room to **Exterior**, because the counting rules put anything outside the building under Exterior.
- **Point at:** the totals change immediately, with no reload.

### 3.5 False positives the AI calls "not a device" (1 min), A4-111

- **Do:**
  1. Switch to A4.
  2. Below OPTIONAL BED 3 are three square porch-post markers (small squares with an X) that det/0 proposed as **recessed downlights**. Click one: the AI line reads *disagrees (not a device)*.
  3. Press **R** to reject each one.
- **Say:**
  - Rejected devices stay visible as rejected, never count, and are never proposed again on re-analysis.
  - Delete is only for my own mistakes and duplicates; the app refuses to delete a detection otherwise.

### 3.6 Something det/0 missed, found by the AI (1 min), A4 LNDRY

- **Do:**
  1. Turn on the **AI-only candidates** layer if it's off.
  2. In LNDRY, beside the `GFI 240V`, a dashed marker shows the **indoor air quality fan** proposed by Claude's discover pass.
  3. Select it, check it against the drawing, and press **A**.
- **Say:**
  - det/0 missed this one. The AI-only candidate needs an individual decision: it can never be bulk-accepted.
  - Across all sheets, discover found 294 of the 300 hand-counted devices.

### 3.7 Something everyone missed (1 min), A3 BATH 1

- **Do:**
  1. The wall light above the round sink is drawn on top of the basin. Neither det/0 nor the AI proposed it.
  2. Press **C**, click it, choose *light*, then the wall-mounted subtype, and Save.
- **Say:** this is why the reviewer is in the loop. Its origin is *manual*, and the export reports it that way.

### 3.8 "Show me what this number is made of" (2 min, the drill they'll ask for)

- **Do:**
  1. In **Official takeoff totals**, click any row, for example *BEDROOM / STUDIO · receptacle · DUPLEX OUTLET ARC-FAULT…*.
  2. Its markers highlight on the plan. Click the row again to clear it.
- **Say:**
  - Every row carries the ids of the devices it counts, and the page checks that the count matches the markers.
  - *Home total (this sheet)* and *All sheets total* are the same sums over the same saved records.

### 3.9 Bulk accept, honestly scoped (1 min), A1

- **Do:**
  1. Read the line under **Accept all unflagged (N)**: *This sheet (A1-111): awaiting X = U unflagged + F flagged*.
  2. Click the button and confirm: *Accept N unflagged proposals on sheet A1-111?*
- **Say:**
  - It covers only the sheet shown, and only proposals with no open flag.
  - It is one transaction, and **Undo last bulk accept** reverses it.

### 3.10 Persistence and alignment (1 min)

- **Do:**
  1. Reload the page: the corrections, rejections and the manual light are all still there, with their history.
  2. Zoom with the wheel and pan by dragging: the markers stay on their symbols. In the tests they stay within 1.5 px of independently computed positions.

### 3.11 Re-analysis never overwrites your work (2 min), A1

- **Do:**
  1. Tick **this sheet only** and click **Analyze** on A1. With AI this takes 1–2 minutes and about $1.
  2. Afterwards, the corrected and accepted devices are unchanged, and no duplicate devices appear.
- **Say:**
  - Every Analyze is a new, immutable run with its own versions.
  - A re-run only adds evidence and flags. It never changes a field a reviewer set, and it matches one-to-one, so an unchanged plan adds nothing.

### 3.12 Export (1 min)

- **Do:** click **Export**. The status bar says *Export incomplete* and gives the reasons: devices still awaiting review, AI-only candidates, open flags.
- **Say:**
  - `totals.json` equals the app's totals, and equals the totals recomputed from `devices_official.csv`.
  - An unfinished takeoff is never presented as complete.

---

## 4. Numbers to have ready (with their caveats)

| Claim | Figure | Caveat |
|---|---|---|
| det/0, held-out sheets A3/A4 | precision 0.92, recall 0.98, subtype 0.95 | The ground truth was seeded from the same candidate generators, so recall is an upper bound; 2 sheets of 1 plan set |
| det/0, all sheets | ceiling fans 8/8, lights 61/63 | same |
| AI verify (batches of 8) | category agrees with ground truth 0.96 (0.94 held out); its 32 flags catch 20 of det/0's 26 errors | one plan set |
| AI discover | 294/300 devices found, precision 0.96; 2 of det/0's 3 misses | not seeded from the AI, so less biased; 8 "background" candidates not yet checked |
| Room proposal | 48.8% correct held out | the weakest piece: a default the reviewer corrects |
| Tests | 577 automated, including Playwright | |

Sources:
- `reports/evaluation/gt_001_20261025T020009Z__eval1.1__det0/`, `__ai-verify1/`, `__ai-discover1/`
- `reports/takeoff/room_proposal_eval.md`

## 5. Likely questions, short answers

- **Why not let the model decide?** Totals would then depend on a model that has only been measured on one plan set. The model is evidence; a reviewer decides. The adopted ship gates (design document §2) decide whether the model's role grows.
- **How would this scale to other plans?** Templates come from each sheet's own legend. A sheet with no legend is skipped with a message and can be counted by hand. New styles get a small hand-counted set before they're enabled.
- **What happens when you change the model or prompt?** A new run with the new version recorded. Finished takeoffs are never recomputed in place, and every change is re-evaluated against the fixed ground truth first.
- **Where is it wrong?**
  - room proposals;
  - the porch posts on A4;
  - the A3 exterior light;
  - the A3 bathroom wall light that everyone missed;
  - the verifier's 8 unreadable answers. In the one-detection-per-request run, the model echoed the detection id with a trailing colon, so those answers were dropped.
- **What would you do with more time?** See design document §5: room polygons and automatic Exterior, a blind multi-reviewer ground truth on new plans, then the bill of materials.

## 6. If something goes wrong

| Symptom | Do |
|---|---|
| *AI check failed … The .env key was used* | `.\demo.ps1 -IgnoreEnvFile`, or put a workspace-scoped key in `.env` |
| The preflight fails on credentials or host | `ant auth login` again, then rerun. Or present with `.\demo.ps1 -NoAI` and the live-AI screenshots in `reports/takeoff/demo/` |
| The status bar says `AI verification: unavailable: …` | The run still completes with det/0; say so, and show the committed live-AI evidence |
| "Credit balance is too low" | Same as above; top up after the interview |
| Page looks wrong after a code change | Restart the launcher; the store is unaffected |
