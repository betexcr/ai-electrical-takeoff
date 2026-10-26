# ai-discover/1 evaluation: gt_001_20261025T020009Z

**measured separately; candidates need human approval; not used by totals.** This report measures AI-proposed device locations against verified ground truth. The app's counts come only from accepted devices; an AI candidate is a proposal a reviewer must check and accept individually.

- Model: `claude-opus-5-5` (claude), ai-discover/1, ai-discover-prompt/1, anthropic SDK 2.3.0
- Tiles: 84 (200.0 pt, 24.0 pt overlap) on A1-111, A2-111, A3-111, A4-111
- Request budget: 200 (used 84, skipped 0)
- det/0 misses from `reports\evaluation\gt_001_20261025T020009Z__eval1.1__det0\matches.jsonl`
- Reproduce (from backend/): `uv run python tools/eval_ai_discovery.py --export ../data/ground_truth/exports/gt_001_20261025T020009Z --max-requests 200`

| | dev | held_out | total |
|---|---|---|---|
| ground-truth devices | 121 | 179 | 300 |
| AI candidates | 125 | 182 | 307 |
| matched (TP) | 121 | 173 | 294 |
| recall | 1.0 | 0.9665 | 0.98 |
| precision | 0.968 | 0.9505 | 0.9577 |
| localization median / p90 (pt) | 0.937 / 2.108 | 0.99 / 2.412 | 0.99 / 2.283 |
| det/0 misses found | 0/0 (n/a) | 2/3 (0.6667) | 2/3 (0.6667) |
| false candidates per sheet | 2.0 | 4.5 | 3.25 |
| false candidate kinds | {"background": 2, "wrong_category": 2} | {"background": 6, "wrong_category": 3} | {"background": 8, "wrong_category": 5} |
| requests | 44 | 40 | 84 |
| errors by code | {} | {} | {} |
| input / output tokens | 361460 / 11696 | 329688 / 15720 | 691148 / 27416 |
| cache creation / read tokens | 0 / 0 | 0 / 0 | 0 / 0 |
| latency p50 / p95 (ms) | 3157.754 / 9360.448 | 4403.227 / 9187.562 | 4060.36 / 9360.448 |

## Notes

- Scored are the merged candidates (one per same-category cluster within 6 pt across tiles), which is what the app stores and proposes; a reviewer must accept each one individually before it can count.
- Matching is eval/1's: one-to-one within 6 pt, same sheet and category. A candidate of the wrong category at a device is a false candidate, and the device stays missed.
- det/0's misses are its false negatives at 6 pt in the det/0 evaluation of the same export; the share found is how many of those ground-truth devices a candidate matched.
- Localization error is the distance of matched pairs in PDF points.
- A sheet without legend icons sends no request (skipped_no_legend); failed tiles give no candidates.
- The ground truth was built with the candidate generators det/0 uses as hints; it was not built with the AI.
