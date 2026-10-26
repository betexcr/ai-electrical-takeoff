# ai-verify/1 evaluation: gt_001_20261025T020009Z (batch size 1)

**measured separately; not used by the app's totals.** This report measures the AI second opinion against verified ground truth. The app's counts come only from accepted devices; an AI verdict never changes a device or a total.

- Model: `claude-opus-5-5` (claude), ai-verify/1, ai-verify-prompt/1, anthropic SDK 2.3.0
- det/0: det/0, 313 in-region detections on A1-111, A2-111, A3-111, A4-111
- Request budget: 320 (used 313, skipped 0)
- Reproduce (from backend/): `uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --batch-size 1 --max-requests 320`

| | dev | held_out | total |
|---|---|---|---|
| items (det/0 detections) | 122 | 191 | 313 |
| answered by the AI | 119 | 186 | 305 |
| det/0 errors (category, subtype or not a device) | 3 | 23 | 26 |
| AI category agrees with GT | 119/119 (1.0) | 175/186 (0.9409) | 294/305 (0.9639) |
| AI subtype agrees with GT | 118/118 (1.0) | 168/169 (0.9941) | 286/287 (0.9965) |
| flags (would open model_disagrees) | 6 | 29 | 35 |
| flag precision vs det/0 errors | 0.5 | 0.6207 | 0.6 |
| flag recall vs det/0 errors | 1.0 | 0.7826 | 0.8077 |
| flag recall, answered items only | 1.0 | 0.7826 | 0.8077 |
| refusals | 0 | 0 | 0 |
| budget skips | 0 | 0 | 0 |
| errors by code | {"parse_error": 3} | {"parse_error": 5} | {"parse_error": 8} |
| requests | 122 | 191 | 313 |
| input / output tokens | 380601 / 19572 | 594020 / 33895 | 974621 / 53467 |
| cache creation / read tokens | 0 / 0 | 0 / 0 | 0 / 0 |
| latency p50 / p95 (ms) | 2703.726 / 4573.451 | 2596.072 / 5793.335 | 2632.897 / 5321.571 |

## Notes

- Verify only sees det/0's detections: devices det/0 missed are not measured here (that is ai-discover/1's job).
- Truth at each detection: a same-category ground-truth device within 6 pt (eval/1 matching), else any remaining ground-truth device within 6 pt (det/0 got the category wrong), else none (det/0 false positive).
- An item counts as answered only as the app would use it (ruling R15): it was answered exactly once by a request that succeeded. An item the model left out or answered twice is unanswered (parse_error) and never flags; the other items of its batch still count. A whole-request failure leaves all its items unanswered.
- A flag is what the merge would open: category or subtype disagreement with det/0, not a device, or uncertain.
- The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.
