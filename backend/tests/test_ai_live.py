"""Opt-in live check of ai-verify/1 against the real API (billed). Skipped unless ``TAKEOFF_LIVE_AI=1``.

It asserts only that the integration works: schema-valid verdicts, real usage and latency, and the Claude
implementation. It never asserts accuracy; that is measured by ``tools/eval_ai_verifier.py``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("TAKEOFF_LIVE_AI") != "1", reason="live AI test: set TAKEOFF_LIVE_AI=1 to run (billed)"),
    pytest.mark.skipif(not PDF.exists(), reason="supplied PDF missing"),
]


def test_live_verify_three_a1_items(tmp_path):
    from takeoff.ai import common as AC
    from takeoff.ai import verifier as AV
    from takeoff.app import models as TM
    from takeoff.app import sheets as TS
    from takeoff.detection import det0
    from takeoff.groundtruth import candidates as CA
    from takeoff.groundtruth import sheets as S
    from takeoff.pdf import coords as C

    ai = AC.AIClient()
    ok, reason = ai.available()
    assert ok, f"AI unavailable: {reason}"

    sha = hashlib.sha256(PDF.read_bytes()).hexdigest()
    sheet = next(s for s in TS.intake(PDF, sha) if s.sheet_id == "A1-111")
    view = TS.sheet_view(PDF, sheet, sha, tmp_path)
    doc = pymupdf.open(PDF)
    try:
        region_path, mapping = S.render_region_image(doc[sheet.page_number - 1], view["region"]["bbox_pt"], sha, tmp_path)
    finally:
        doc.close()
    g = C.PageGeometry(*view["geometry"]["crop_native"], rotation=view["geometry"]["rotation"])
    found = det0.detect(PDF, CA.generate(PDF, pages=[sheet.page_number]))["detections"]
    dets = []
    for d in [d for d in found if d["sheet_id"] == "A1-111"][:3]:
        u, v = C.unrotated_to_normalized(g, (d["x_pt"], d["y_pt"]))
        dets.append(TM.RunDetection(id=d["id"], run_id="run-live", document_id=sha, sheet_id="A1-111",
                                    category=d["category"], subtype=d["subtype"], status=d["status"],
                                    point={"u": u, "v": v}, point_pt=(d["x_pt"], d["y_pt"]), members=d["members"],
                                    tokens=d["tokens"]))
    assert len(dets) == 3
    region = AV.load_gray(region_path)
    crops = {d.id: AV.build_crop(region, mapping, d.point_pt) for d in dets}
    tokens = AV.tokens_near(dets, view["tokens"])

    out = AV.ClaudeVerifier(ai).verify("run-live", "A1-111", dets, crops, TS.legend_icons(PDF, sheet), tokens,
                                       AC.RequestBudget(1))
    assert len(out) == 1
    v = out[0]
    assert v.implementation == "claude" and v.error is None, v.error
    assert v.input_tokens and v.input_tokens > 0 and v.latency_ms and v.latency_ms > 0
    for row in v.output["verdicts"]:
        AV.AIVerdict.model_validate(row)
    assert set(v.agreement) == {d.id for d in dets}
