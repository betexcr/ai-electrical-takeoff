# ai-verify/1 evaluation: gt_001_20261025T020009Z (batch size 1)

**measured separately; not used by the app's totals.** This report measures the AI second opinion against verified ground truth. The app's counts come only from accepted devices; an AI verdict never changes a device or a total.

- Model: `claude-opus-5-5` (claude), ai-verify/1, ai-verify-prompt/1, anthropic SDK 2.3.0
- det/0: det/0, 313 in-region detections on A1-111, A2-111, A3-111, A4-111
- Request budget: 320 (used 313, skipped 0)
- Reproduce (from backend/): `uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --batch-size 1 --max-requests 320`

| | dev | held_out | total |
|---|---|---|---|
| items (det/0 detections) | 122 | 191 | 313 |
| answered by the AI | 117 | 149 | 266 |
| det/0 errors (category, subtype or not a device) | 3 | 23 | 26 |
| AI category agrees with GT | 117/117 (1.0) | 142/149 (0.953) | 259/266 (0.9737) |
| AI subtype agrees with GT | 116/116 (1.0) | 139/140 (0.9929) | 255/256 (0.9961) |
| flags (would open model_disagrees) | 7 | 22 | 29 |
| flag precision vs det/0 errors | 0.4286 | 0.5 | 0.4828 |
| flag recall vs det/0 errors | 1.0 | 0.4783 | 0.5385 |
| flag recall, answered items only | 1.0 | 0.7857 | 0.8235 |
| refusals | 0 | 0 | 0 |
| budget skips | 0 | 0 | 0 |
| errors by code | {"parse_error": 5} | {"http_400": 34, "parse_error": 8} | {"http_400": 34, "parse_error": 13} |
| requests | 122 | 191 | 313 |
| input / output tokens | 380601 / 19529 | 487801 / 28137 | 868402 / 47666 |
| cache creation / read tokens | 0 / 0 | 0 / 0 | 0 / 0 |
| latency p50 / p95 (ms) | 2637.571 / 4925.992 | 2388.586 / 5538.373 | 2481.8 / 5296.513 |

## Notes

- Verify only sees det/0's detections: devices det/0 missed are not measured here (that is ai-discover/1's job).
- Truth at each detection: a same-category ground-truth device within 6 pt (eval/1 matching), else any remaining ground-truth device within 6 pt (det/0 got the category wrong), else none (det/0 false positive).
- An item counts as answered only as the app would use it (ruling R15): it was answered exactly once by a request that succeeded. An item the model left out or answered twice is unanswered (parse_error) and never flags; the other items of its batch still count. A whole-request failure leaves all its items unanswered.
- A flag is what the merge would open: category or subtype disagreement with det/0, not a device, or uncertain.
- The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.
