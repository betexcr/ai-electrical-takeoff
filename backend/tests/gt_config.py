"""Which verified ground-truth export the tests calibrate and evaluate against.

``TAKEOFF_GT_EXPORT`` names an export directory explicitly; it must pass ``require_verified``
and an invalid path fails loudly. Without it the repo's committed export is used, and the test
is skipped when that is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from takeoff.groundtruth.export import require_verified

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPORT = ROOT / "data" / "ground_truth" / "exports" / "gt_001_20261025T020009Z"
ENV = "TAKEOFF_GT_EXPORT"


def gt_export() -> Path:
    explicit = os.environ.get(ENV)
    if explicit:
        path = Path(explicit)
        require_verified(path)  # ExportError (or OSError) for an invalid explicit path: never skipped
        return path
    if not (DEFAULT_EXPORT / "manifest.json").exists():
        pytest.skip(f"no verified ground-truth export: set {ENV}")
    require_verified(DEFAULT_EXPORT)
    return DEFAULT_EXPORT
