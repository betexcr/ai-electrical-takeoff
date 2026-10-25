# Room proposal evaluation

## not shown in the app; not a room-detection accuracy claim

Export `gt_001_20261025T020009Z` (manifest SHA-256 `f03b11a38264a348e700b618f3342fed64eb30589a922dee715ae07852b14a1f`).

Parameters in effect: `max_label_distance_pt` = 100.0, `ambiguity_ratio` = 1.25.

Calibration recomputed on A1-111, A2-111: n = 84 devices with a labeled GT room and a correct nearest label; 95th percentile of d1 = 96.483 pt (max 104.912 pt), rounded up to 100.0 pt.

| group | sheets | GT-labeled devices | correct | accuracy | ambiguous | ambiguous rate | correct but flagged | wrong and unflagged |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| dev | A1-111, A2-111 | 113 | 81 | 71.7% | 21 | 18.6% | 7 (8.6% of correct) | 18 |
| held_out | A3-111, A4-111 | 168 | 82 | 48.8% | 52 | 30.9% | 13 (15.8% of correct) | 47 |

| group | GT Exterior | of which given a labeled proposal | GT Unlabeled | of which given a labeled proposal | ambiguous reasons |
|---|---:|---:|---:|---:|---|
| dev | 7 | 7 | 1 | 1 | {'two_labels_close': 15, 'far_from_labels': 6} |
| held_out | 10 | 2 | 1 | 1 | {'far_from_labels': 23, 'two_labels_close': 29} |

| sheet | GT-labeled | correct | accuracy | ambiguous rate | GT Exterior | GT Unlabeled |
|---|---:|---:|---:|---:|---:|---:|
| A1-111 | 51 | 34 | 66.7% | 23.5% | 4 | 0 |
| A2-111 | 62 | 47 | 75.8% | 14.5% | 3 | 1 |
| A3-111 | 76 | 40 | 52.6% | 36.8% | 5 | 1 |
| A4-111 | 92 | 42 | 45.6% | 26.1% | 5 | 0 |

Notes

- dev (A1, A2) is the calibration set: its numbers are in-sample. held_out (A3, A4) did not influence the parameters.
- Accuracy is measured over devices whose ground-truth room is a labeled room; a proposal is correct when kind, name and label box all match.
- Exterior is never proposed and Unlabeled is only proposed far from every label; ground-truth Exterior/Unlabeled devices are counted, not scored.
- 'wrong and unflagged' proposals carry no ambiguity flag, so every room stays a reviewer decision; the proposal is a convenience default only.
- Room polygons are not extracted; this checks a nearest-label heuristic on one supplied plan set and is not evidence that room detection is reliable.
