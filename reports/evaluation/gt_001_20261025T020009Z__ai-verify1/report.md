# ai-verify/1 evaluation: gt_001_20261025T020009Z (batch size 8)

**measured separately; not used by the app's totals.** This report measures the AI second opinion against verified ground truth. The app's counts come only from accepted devices; an AI verdict never changes a device or a total.

- Model: `claude-opus-5-5` (claude), ai-verify/1, ai-verify-prompt/1, anthropic SDK 2.3.0
- det/0: det/0, 313 in-region detections on A1-111, A2-111, A3-111, A4-111
- Request budget: 200 (used 41, skipped 0)
- Reproduce (from backend/): `uv run python tools/eval_ai_verifier.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --batch-size 8 --max-requests 200`

| | dev | held_out | total |
|---|---|---|---|
| items (det/0 detections) | 122 | 191 | 313 |
| answered by the AI | 122 | 191 | 313 |
| det/0 errors (category, subtype or not a device) | 3 | 23 | 26 |
| AI category agrees with GT | 121/122 (0.9918) | 180/191 (0.9424) | 301/313 (0.9617) |
| AI subtype agrees with GT | 119/120 (0.9917) | 172/173 (0.9942) | 291/293 (0.9932) |
| flags (would open model_disagrees) | 7 | 25 | 32 |
| flag precision vs det/0 errors | 0.2857 | 0.72 | 0.625 |
| flag recall vs det/0 errors | 0.6667 | 0.7826 | 0.7692 |
| flag recall, answered items only | 0.6667 | 0.7826 | 0.7692 |
| refusals | 0 | 0 | 0 |
| budget skips | 0 | 0 | 0 |
| errors by code | {} | {} | {} |
| requests | 16 | 25 | 41 |
| input / output tokens | 62107 / 15161 | 96919 / 26039 | 159026 / 41200 |
| cache creation / read tokens | 0 / 0 | 0 / 0 | 0 / 0 |
| latency p50 / p95 (ms) | 10005.946 / 12301.303 | 10193.964 / 14222.206 | 10193.964 / 14113.292 |

## Notes

- Verify only sees det/0's detections: devices det/0 missed are not measured here (that is ai-discover/1's job).
- Truth at each detection: a same-category ground-truth device within 6 pt (eval/1 matching), else any remaining ground-truth device within 6 pt (det/0 got the category wrong), else none (det/0 false positive).
- An item counts as answered only as the app would use it (ruling R15): it was answered exactly once by a request that succeeded. An item the model left out or answered twice is unanswered (parse_error) and never flags; the other items of its batch still count. A whole-request failure leaves all its items unanswered.
- A flag is what the merge would open: category or subtype disagreement with det/0, not a device, or uncertain.
- The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.
