"""Versioned review-protocol parameters (see docs/ground_truth_protocol.md).

Changing any value here changes how reviews are checked, so bump
PROTOCOL_VERSION and record the change in PROJECT_TRACKING.md.
"""

PROTOCOL_VERSION = "protocol/1"

# Two non-deleted annotations closer than this are flagged (same category:
# possible duplicate; different categories: possible combined fixture).
DUPLICATE_TOLERANCE_PT = 3.0
# Reconciliation suggests the nearest annotation within this distance of a
# blind observation; also the radius for "orphan candidate" warnings.
MATCH_TOLERANCE_PT = 6.0
# Text tokens within this distance of a new annotation are suggested for linking.
TOKEN_SUGGEST_RADIUS_PT = 15.0
# Coverage-grid cell size; cells tile the electrical-region hypothesis.
CELL_SIZE_PT = 100.0
