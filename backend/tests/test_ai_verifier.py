"""ai-verify/1 (spec §5.9.1): crops, batching, legend selection, request stability, per-item agreement, the fake,
and the evaluation script's refusals. Stub clients only; nothing here makes a network request."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import random
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from takeoff.ai import common as AC
from takeoff.ai import prompt as P
from takeoff.ai import verifier as AV
from takeoff.app import models as TM
from takeoff.groundtruth import taxonomy as T

from test_ai_common import StubClient, response, usage

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "data" / "ground_truth" / "exports" / "gt_001_20261025T020009Z"
SHEET = "A1-111"


def det(id, x, y, category="receptacle", subtype="gfi", members=(), sheet=SHEET):
    return TM.RunDetection(id=id, run_id="run-001", document_id="d" * 64, sheet_id=sheet, category=category,
                           subtype=subtype, point={"u": 0.5, "v": 0.5}, point_pt=(x, y),
                           members=[{"id": f"{id}-m{k}", "category": c} for k, c in enumerate(members)])


def png(value: int, size=(10, 10)) -> bytes:
    return cv2.imencode(".png", np.full(size, value, np.uint8))[1].tobytes()


def icon(category, subtype, shade=0):
    return {"category": category, "subtype": subtype, "label": T.label(category, subtype), "png": png(shade)}


def all_icons() -> dict:
    return {s.catalog_file: icon(c, s.name, k) for k, (c, s) in enumerate(
        (c, s) for c, subs in T.CATEGORIES.items() for s in subs if s.catalog_file)}


def verdict_row(item_id, category="receptacle", subtype="gfi", is_device=True, uncertain=False, rationale="r"):
    return AV.AIVerdict(item_id=item_id, is_device=is_device, category=category, subtype=subtype,
                        rationale=rationale, uncertain=uncertain)


def item_ids(kw) -> list[str]:
    return [b["text"].split(": detector says")[0].removeprefix("Item ")
            for b in kw["messages"][0]["content"] if b["type"] == "text" and b["text"].startswith("Item ")]


def answer_receptacle_gfi(kw):
    return response(AV.AIVerdictBatch(verdicts=[verdict_row(i) for i in item_ids(kw)]))


def claude(stub, **kw):
    return AV.ClaudeVerifier(AC.AIClient(model="claude-test", client=stub), **kw)


# --- crops ---------------------------------------------------------------------------------------------------

def test_crop_is_48pt_and_deterministic(tmp_path):
    scale = 300 / 72
    region = np.full((600, 600), 255, np.uint8)
    mapping = {"width_px": 600, "height_px": 600, "dpi": 300, "covers_pt": [100.0, 100.0, 100 + 600 / scale, 100 + 600 / scale]}
    cx, cy = (150 - 100) * scale, (160 - 100) * scale  # a dark mark at (150, 160) pt
    region[int(cy) - 3:int(cy) + 4, int(cx) - 3:int(cx) + 4] = 0

    crop = AV.build_crop(region, mapping, (150.0, 160.0))
    img = cv2.imdecode(np.frombuffer(crop, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img.shape == (200, 200)  # 48 pt at 300 DPI
    assert img[97:104, 97:104].max() == 0 and img[:80].min() == 255  # the mark sits at the centre, nothing else
    assert AV.build_crop(region, mapping, (150.0, 160.0)) == crop  # deterministic

    # the same crop from PNG bytes or a PNG path
    region_png = cv2.imencode(".png", region)[1].tobytes()
    path = tmp_path / "region.png"
    path.write_bytes(region_png)
    assert AV.build_crop(region_png, mapping, (150.0, 160.0)) == crop
    assert AV.build_crop(path, mapping, (150.0, 160.0)) == crop

    # near the image edge the crop is padded with white, keeping its size
    edge = cv2.imdecode(np.frombuffer(AV.build_crop(np.zeros((600, 600), np.uint8), mapping, (101.0, 101.0)), np.uint8),
                        cv2.IMREAD_GRAYSCALE)
    assert edge.shape == (200, 200)
    assert edge[:90, :90].min() == 255 and edge[110:, 110:].max() == 0
    assert AV.build_crop(region, mapping, (150.0, 160.0), size_pt=24.0) != crop


# --- batching ------------------------------------------------------------------------------------------------

def test_batches_of_at_most_8_in_reading_order():
    rng = random.Random(7)
    dets = [det(f"d{i:02d}", rng.uniform(0, 500), rng.choice([100.0, 200.0, 300.0, rng.uniform(0, 500)])) for i in range(19)]
    expected = sorted(dets, key=lambda d: (d.point_pt[1], d.point_pt[0], d.id))
    bs = AV.batches(list(reversed(dets)))
    assert [len(b) for b in bs] == [8, 8, 3] and AV.VERIFY_BATCH_SIZE == 8
    assert [d.id for b in bs for d in b] == [d.id for d in expected]
    assert [len(b) for b in AV.batches(dets, 1)] == [1] * 19

    stub = StubClient(default=answer_receptacle_gfi)
    out = claude(stub).verify("run-001", SHEET, dets, {d.id: png(200) for d in dets}, {}, {}, AC.RequestBudget(100))
    assert len(stub.calls) == 3 and len(out) == 3
    assert [x.detection_ids for x in out] == [[d.id for d in b] for b in bs]
    assert all(x.kind == "verify" and x.sheet_id == SHEET and x.run_id == "run-001" for x in out)


# --- legend references ---------------------------------------------------------------------------------------

def test_relevant_icons_only_and_at_most_8_without_repetition():
    icons = {"r1.png": icon("receptacle", "gfi"), "r2.png": icon("receptacle", "afi_duplex"),
             "l1.png": icon("light", "recessed_downlight"), "l2.png": icon("light", "pendant"),
             "f1.png": icon("fan", "exhaust_fan"), "s1.png": icon("switch", "standard"),
             "dt.png": icon("detector", "smoke_detector")}
    batch = [det("a", 10, 10, "receptacle", "gfi", members=["receptacle", "light"]),
             det("b", 20, 10, "receptacle", "afi_duplex", members=["receptacle"]),
             det("c", 30, 10, "receptacle", "gfi", members=["receptacle", "receptacle"]),
             det("d", 40, 10, "light", "pendant", members=["light", "fan"])]
    got = AV.relevant_icons(batch, icons)
    assert [(i["category"], i["subtype"]) for i in got] == [
        ("fan", "exhaust_fan"), ("light", "pendant"), ("light", "recessed_downlight"),
        ("receptacle", "afi_duplex"), ("receptacle", "gfi")]
    assert len({i["catalog_file"] for i in got}) == len(got)  # each icon once

    # the cap: at most MAX_ICONS, never repeated, and det/0's own claims are never the ones left out
    many = all_icons()
    wide = [det("w1", 10, 10, "receptacle", "range_hood", members=["receptacle", "light", "junction_box"]),
            det("w2", 20, 10, "switch", "vacancy_sensor", members=["switch", "fan"]),
            det("w3", 30, 10, "detector", "combined_smoke_co", members=["detector", "light"])]
    got = AV.relevant_icons(wide, many)
    assert len(got) == AV.MAX_ICONS == 8
    assert len({i["catalog_file"] for i in got}) == 8
    pairs = [(i["category"], i["subtype"]) for i in got]
    assert pairs == sorted(pairs)
    assert {("receptacle", "range_hood"), ("switch", "vacancy_sensor"), ("detector", "combined_smoke_co")} <= set(pairs)
    assert {c for c, _ in pairs} <= {"receptacle", "light", "junction_box", "switch", "fan", "detector"}
    assert AV.relevant_icons(wide, {}) == []


# --- the request ---------------------------------------------------------------------------------------------

def _request():
    icons = {"r1.png": icon("receptacle", "gfi", 10), "l1.png": icon("light", "recessed_downlight", 20),
             "s1.png": icon("switch", "standard", 30)}
    batch = [det("A1-111:det0-0002", 50, 20, "light", "recessed_downlight", members=["light"]),
             det("A1-111:det0-0001", 10, 20, "receptacle", "gfi", members=["receptacle"])]
    crops = {"A1-111:det0-0001": png(1), "A1-111:det0-0002": png(2)}
    tokens = {"A1-111:det0-0001": ["GFI", "+42\""]}
    return AV.build_verify_request(batch, crops, icons, tokens), crops, icons


def test_request_has_no_timestamps_and_stable_bytes():
    (system, content), crops, icons = _request()
    (system2, content2), _, _ = _request()
    blob = json.dumps({"system": system, "content": content}, sort_keys=True)
    assert blob == json.dumps({"system": system2, "content": content2}, sort_keys=True)
    assert system == P.VERIFY_SYSTEM and P.VERIFY_PROMPT_VERSION == AV.PROMPT_VERSION == "ai-verify-prompt/1"
    text = blob.replace("A1-111", "")
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", text)  # no dates or times
    assert not re.search(r"[0-9a-f]{32}", system + "".join(b.get("text", "") for b in content))  # no random ids

    texts = [b["text"] for b in content if b["type"] == "text"]
    assert texts[0].startswith("Reference legend")
    # only the relevant icons (receptacle, light), each a label then its image, before the items
    assert content[1]["type"] == "text" and T.label("light", "recessed_downlight") in content[1]["text"]
    assert content[2]["type"] == "image" and content[3]["type"] == "text" and T.label("receptacle", "gfi") in content[3]["text"]
    assert not any(T.label("switch", "standard") in t for t in texts)
    items = [t for t in texts if t.startswith("Item ")]
    assert items == ['Item A1-111:det0-0001: detector says receptacle/gfi; nearby text: GFI, +42"',
                     "Item A1-111:det0-0002: detector says light/recessed_downlight; nearby text: none"]
    # each item text is followed by its crop
    for i, b in enumerate(content):
        if b["type"] == "text" and b["text"].startswith("Item "):
            img = content[i + 1]
            assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
            did = b["text"].split(": detector says")[0].removeprefix("Item ")
            assert base64.standard_b64decode(img["source"]["data"]) == crops[did]
    assert AC.image_block(b"\x89PNG") == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                      "data": base64.standard_b64encode(b"\x89PNG").decode()}}


# --- verdicts ------------------------------------------------------------------------------------------------

def test_missing_item_id_is_parse_error_for_that_item_only():
    batch = [det("d0", 10, 10), det("d1", 20, 10), det("d2", 30, 10)]
    rows = [verdict_row("d0"), verdict_row("d1"), verdict_row("d1", category="light", subtype=None)]  # d1 twice, d2 missing
    stub = StubClient([response(AV.AIVerdictBatch(verdicts=rows), u=usage(500, 80))])
    (v,) = claude(stub).verify("run-001", SHEET, batch, {d.id: png(9) for d in batch}, {}, {}, AC.RequestBudget(10))
    assert set(v.agreement) == {"d0"}  # no agreement entry at all for d1 and d2: merge never flags them
    assert v.agreement["d0"] == TM.Agreement(category=True, subtype=True)
    assert v.error["code"] == "parse_error" and v.error["detection_ids"] == ["d1", "d2"]
    assert v.error["missing"] == ["d2"] and v.error["duplicated"] == ["d1"]
    assert [r["item_id"] for r in v.output["verdicts"]] == ["d0", "d1", "d1"]  # the model's answer is kept as given
    assert (v.input_tokens, v.output_tokens) == (500, 80)

    # a whole-request failure: no output, no agreement, the code recorded
    stub = StubClient([response(None, stop_reason="refusal")])
    (v,) = claude(stub).verify("run-001", SHEET, batch, {d.id: png(9) for d in batch}, {}, {}, AC.RequestBudget(10))
    assert v.error == {"code": "refusal"} and v.agreement is None and v.output is None and v.stop_reason == "refusal"


def test_agreement_fields_computed_per_item():
    batch = [det("d0", 10, 10, "receptacle", "gfi"), det("d1", 20, 10, "receptacle", "gfi"),
             det("d2", 30, 10, "receptacle", "gfi"), det("d3", 40, 10, "receptacle", "gfi"),
             det("d4", 50, 10, "receptacle", "gfi"), det("d5", 60, 10, "receptacle", "gfi")]
    rows = [verdict_row("d0"), verdict_row("d1", subtype="afi_duplex"), verdict_row("d2", subtype=None),
            verdict_row("d3", category="light", subtype="pendant"),
            verdict_row("d4", category="not_a_device", subtype=None, is_device=False),
            verdict_row("d5", uncertain=True)]
    icons = {"r1.png": icon("receptacle", "gfi", 40)}
    crops = {d.id: png(i * 20) for i, d in enumerate(batch)}
    stub = StubClient([response(AV.AIVerdictBatch(verdicts=list(reversed(rows))), u=usage(2100, 300, 0, 0))])
    (v,) = claude(stub).verify("run-001", SHEET, batch, crops, icons, {}, AC.RequestBudget(10))
    ag = {k: (a.category, a.subtype) for k, a in v.agreement.items()}
    assert ag == {"d0": (True, True), "d1": (True, False), "d2": (True, None), "d3": (False, False),
                  "d4": (False, None), "d5": (True, True)}  # uncertainty is in the row; merge flags it from there
    assert v.error is None and v.implementation == "claude" and v.model == "claude-test"
    assert (v.prompt_version, v.ai_version) == ("ai-verify-prompt/1", "ai-verify/1")
    assert v.output == {"verdicts": [r.model_dump() for r in reversed(rows)]}
    assert v.image_sha256s == [hashlib.sha256(icons["r1.png"]["png"]).hexdigest()] + \
        [hashlib.sha256(crops[d.id]).hexdigest() for d in batch]
    assert (v.input_tokens, v.output_tokens, v.cache_creation_input_tokens, v.cache_read_input_tokens) == (2100, 300, 0, 0)
    assert v.latency_ms is not None and v.stop_reason == "end_turn"
    # rationales are bounded by the schema
    long = AV.AIVerdict(item_id="x", is_device=True, category="switch", subtype=None, rationale="y" * 500, uncertain=False)
    assert len(long.rationale) == 300


def test_fake_verifier_marks_implementation_fake():
    batch = [det(f"d{i}", 10 * i, 10) for i in range(10)]
    script = {"d1": {"category": "light", "subtype": None}, "d2": {"uncertain": True}}
    fake = AV.FakeVerifier(script)
    assert fake.implementation == "fake" and fake.available() == (True, None)
    assert AV.FakeVerifier({}, available=(False, "off for this test")).available() == (False, "off for this test")
    out = fake.verify("run-001", SHEET, batch, {d.id: png(5) for d in batch}, {}, {}, AC.RequestBudget(10))
    again = fake.verify("run-001", SHEET, batch, {d.id: png(5) for d in batch}, {}, {}, AC.RequestBudget(10))
    assert [x.model_dump(exclude={"id", "created_at"}) for x in out] == [x.model_dump(exclude={"id", "created_at"}) for x in again]
    assert [len(x.detection_ids) for x in out] == [8, 2]
    assert all(x.implementation == "fake" and x.model == AV.FAKE_MODEL for x in out)
    ag = {k: a for x in out for k, a in x.agreement.items()}
    assert ag["d0"] == TM.Agreement(category=True, subtype=True)
    assert ag["d1"] == TM.Agreement(category=False, subtype=None)
    assert next(r for r in out[0].output["verdicts"] if r["item_id"] == "d2")["uncertain"] is True
    assert AC.summarize(out)["implementation"] == "fake"
    # a fake honours the budget like the real thing
    out = fake.verify("run-001", SHEET, batch, {d.id: png(5) for d in batch}, {}, {}, AC.RequestBudget(1))
    assert [x.error for x in out] == [None, {"code": "skipped_budget"}]
    # Verifier protocol
    assert isinstance(fake, AV.Verifier) and isinstance(claude(StubClient()), AV.Verifier)


# --- the evaluation script -----------------------------------------------------------------------------------

def _eval_module():
    path = ROOT / "backend" / "tools" / "eval_ai_verifier.py"
    spec = importlib.util.spec_from_file_location("eval_ai_verifier", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_ai_verifier"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not EXPORT.exists(), reason="gt_001 export missing")
def test_eval_script_refuses_fake_and_unverified_export(tmp_path, capsys):
    ev = _eval_module()
    out = tmp_path / "out"
    # the fake is refused, even when available
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], verifier=AV.FakeVerifier({})) == 3
    assert "fake" in capsys.readouterr().err and not out.exists()
    # an unavailable Claude verifier is refused, with the reason
    import anthropic, httpx2
    gone = anthropic.NotFoundError("no", response=httpx2.Response(404, request=httpx2.Request("GET", "https://x")), body=None)
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], verifier=claude(StubClient(model_error=gone))) == 3
    assert "not found" in capsys.readouterr().err and not out.exists()
    # an unverified export is refused before anything else
    bad = tmp_path / "gt_bad"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"level": "incomplete", "files": {}}), encoding="utf-8")
    assert ev.main(["--export", str(bad), "--out", str(out)], verifier=claude(StubClient())) == 2
    assert "verified" in capsys.readouterr().err and not out.exists()
    # default output directory names
    assert ev.out_dir(EXPORT, 8, None).name == f"{EXPORT.name}__ai-verify1"
    assert ev.out_dir(EXPORT, 1, None).name == f"{EXPORT.name}__ai-verify1_b1"


def test_eval_scoring_against_ground_truth_on_synthetic_items():
    ev = _eval_module()
    gt = [{"id": "g1", "sheet_id": "A1-111", "category": "receptacle", "subtype": "gfi", "point_pt": [100.0, 100.0]},
          {"id": "g2", "sheet_id": "A1-111", "category": "switch", "subtype": "standard", "point_pt": [200.0, 100.0]},
          {"id": "g3", "sheet_id": "A3-111", "category": "light", "subtype": "pendant", "point_pt": [100.0, 100.0]}]
    dets = [det("k1", 101, 100, "receptacle", "gfi"),                    # det/0 right
            det("k2", 202, 100, "receptacle", "afi_duplex"),             # det/0 wrong category (switch)
            det("k3", 400, 400, "light", "pendant"),                     # det/0 false positive: nothing there
            det("k4", 100, 103, "light", "recessed_downlight", sheet="A3-111")]  # det/0 wrong subtype
    rows = [verdict_row("k1"), verdict_row("k2", category="switch", subtype="standard"),
            verdict_row("k3", category="not_a_device", subtype=None, is_device=False)]
    agreement = {"k1": {"category": True, "subtype": True}, "k2": {"category": False, "subtype": False},
                 "k3": {"category": False, "subtype": None}}
    v1 = TM.ModelVerdict(run_id="run-eval", sheet_id="A1-111", kind="verify", implementation="claude", model="m",
                         prompt_version="p", ai_version="a", detection_ids=["k1", "k2", "k3"],
                         output={"verdicts": [r.model_dump() for r in rows]}, agreement=agreement,
                         input_tokens=10, output_tokens=5, latency_ms=100.0)
    v2 = TM.ModelVerdict(run_id="run-eval", sheet_id="A3-111", kind="verify", implementation="claude", model="m",
                         prompt_version="p", ai_version="a", detection_ids=["k4"], error={"code": "timeout"}, latency_ms=60000.0)
    m = ev.score(dets, gt, [v1, v2], {"dev": ["A1-111"], "held_out": ["A3-111"]})
    dev, held = m["dev"], m["held_out"]
    assert dev["items"] == 3 and dev["answered"] == 3 and dev["det0_errors"] == 2
    assert dev["ai_vs_gt"]["category"] == {"n": 3, "agree": 3, "rate": 1.0}
    assert dev["ai_vs_gt"]["subtype"] == {"n": 2, "agree": 2, "rate": 1.0}
    assert dev["flags"] == {"flagged": 2, "true_flags": 2, "precision": 1.0, "recall": 1.0, "recall_answered": 1.0}
    assert held["items"] == 1 and held["answered"] == 0 and held["det0_errors"] == 1
    assert held["flags"]["flagged"] == 0 and held["flags"]["recall"] == 0.0 and held["flags"]["precision"] is None
    assert held["unanswered_by_code"] == {"timeout": 1}
    assert held["usage"]["errors_by_code"] == {"timeout": 1} and dev["usage"]["tokens"]["input"] == 10
    assert m["total"]["items"] == 4


# --- fix round 1 ---------------------------------------------------------------------------------------------

def test_one_failing_batch_becomes_an_error_verdict_without_aborting():
    batch = [det(f"d{i}", 10 * i, 10) for i in range(10)]
    crops = {d.id: png(5) for d in batch if d.id != "d9"}  # broken input for the second batch only
    stub = StubClient(default=answer_receptacle_gfi)
    out = claude(stub).verify("run-001", SHEET, batch, crops, {}, {}, AC.RequestBudget(10))
    assert [x.detection_ids for x in out] == [[f"d{i}" for i in range(8)], ["d8", "d9"]]
    assert out[0].error is None and set(out[0].agreement) == {f"d{i}" for i in range(8)}
    assert out[1].error["code"] == "error" and out[1].error["type"] == "KeyError"
    assert out[1].agreement is None and out[1].output is None and len(stub.calls) == 1
    assert AV.MAX_OUTPUT_TOKENS == 2048 and stub.calls[0]["max_tokens"] == 2048  # ruling R16


def test_eval_scoring_counts_per_item_parse_errors():
    """R15 in the eval: the item the model left out is unanswered and never flags; the rest of its batch counts."""
    ev = _eval_module()
    gt = [{"id": f"g{i}", "sheet_id": "A1-111", "category": "receptacle", "subtype": "gfi",
           "point_pt": [100.0 * i, 100.0]} for i in (1, 2, 3)]
    dets = [det("k1", 100, 100, "receptacle", "afi_duplex"),  # det/0 subtype error, the AI disagrees: a true flag
            det("k2", 200, 100, "receptacle", "afi_duplex"),  # det/0 subtype error, but missing from the answer
            det("k3", 300, 100, "receptacle", "gfi")]          # det/0 right, the AI agrees
    rows = [verdict_row("k1", subtype="gfi"), verdict_row("k3")]
    v = TM.ModelVerdict(run_id="run-eval", sheet_id="A1-111", kind="verify", implementation="claude", model="m",
                        prompt_version="p", ai_version="a", detection_ids=["k1", "k2", "k3"],
                        output={"verdicts": [r.model_dump() for r in rows]},
                        agreement={"k1": {"category": True, "subtype": False}, "k3": {"category": True, "subtype": True}},
                        error={"code": "parse_error", "detection_ids": ["k2"], "missing": ["k2"], "duplicated": []})
    m = ev.score(dets, gt, [v], {"dev": ["A1-111"]})["dev"]
    assert m["items"] == 3 and m["answered"] == 2 and m["unanswered_by_code"] == {"parse_error": 1}
    assert m["flags"] == {"flagged": 1, "true_flags": 1, "precision": 1.0, "recall": 0.5, "recall_answered": 1.0}
    assert m["ai_vs_gt"]["subtype"] == {"n": 2, "agree": 2, "rate": 1.0}


@pytest.mark.skipif(not EXPORT.exists(), reason="gt_001 export missing")
def test_eval_checks_out_before_availability(tmp_path, capsys):
    ev = _eval_module()
    out = tmp_path / "exists"
    out.mkdir()
    stub = StubClient()
    assert ev.main(["--export", str(EXPORT), "--out", str(out)], verifier=claude(stub)) == 2
    assert "already exists" in capsys.readouterr().err and stub.retrieved == []  # no model check was made


def answer_echoing_det0(kw):
    """Agrees with whatever det/0 claimed for each item."""
    rows = []
    for b in kw["messages"][0]["content"]:
        if b["type"] == "text" and b["text"].startswith("Item "):
            item_id, rest = b["text"].removeprefix("Item ").split(": detector says ")
            cat, sub = rest.split(";")[0].split("/")
            rows.append(verdict_row(item_id, category=cat, subtype=None if sub == "None" else sub))
    return response(AV.AIVerdictBatch(verdicts=rows), u=usage(1500, 120, 0, 0))


PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"


@pytest.mark.skipif(not (EXPORT.exists() and PDF.exists()), reason="gt_001 export or supplied PDF missing")
def test_eval_success_path_offline(tmp_path, monkeypatch):
    """main -> run -> verify_sheets -> score -> write/report on the real A1 sheet, with a stub Claude client and
    det/0 replaced by three detections placed on ground-truth devices (one with a wrong category)."""
    ev = _eval_module()
    rows = [json.loads(line) for line in (EXPORT / "annotations.jsonl").read_text(encoding="utf-8").splitlines() if line]
    a1 = sorted((r for r in rows if r["sheet_id"] == SHEET and not r.get("deleted")), key=lambda r: r["id"])[:3]
    raw = []
    for k, r in enumerate(a1):
        cat, sub = r["category"], r["subtype"]
        if k == 2:
            cat, sub = ("switch", "standard") if cat != "switch" else ("receptacle", "gfi")
        raw.append({"id": f"{SHEET}:det0-90{k:02d}", "sheet_id": SHEET, "category": cat, "subtype": sub,
                    "status": "as_drawn", "x_pt": r["point_pt"][0], "y_pt": r["point_pt"][1], "members": [], "tokens": []})
    monkeypatch.setattr(ev, "det0_detections", lambda export, pdf: {"detector_version": "det/0", "detections": raw})
    stub = StubClient(default=answer_echoing_det0)
    out = tmp_path / "out"
    code = ev.main(["--export", str(EXPORT), "--sheets", SHEET, "--out", str(out), "--cache", str(tmp_path / "cache")],
                   verifier=claude(stub))
    assert code == 0 and len(stub.calls) == 1
    assert sorted(p.name for p in out.iterdir()) == ["metrics.json", "report.md", "verdicts.jsonl"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# ai-verify/1 evaluation") and "measured separately; not used by the app's totals" in report
    assert "| | dev | total |" in report and "## Notes" in report and "flag recall vs det/0 errors" in report
    m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    dev = m["results"]["dev"]
    assert m["implementation"] == "claude" and m["sheets"] == [SHEET] and m["max_output_tokens"] == 2048
    assert dev["items"] == 3 and dev["answered"] == 3 and dev["det0_errors"] == 1
    assert dev["ai_vs_gt"]["category"] == {"n": 3, "agree": 2, "rate": 0.6667}
    assert dev["flags"]["flagged"] == 0 and dev["flags"]["recall"] == 0.0
    assert dev["usage"]["tokens"]["input"] == 1500 and dev["usage"]["requests"] == 1
    assert len(m["items"]) == 3
    verdicts = [json.loads(line) for line in (out / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(verdicts) == 1 and verdicts[0]["implementation"] == "claude" and len(verdicts[0]["image_sha256s"]) >= 3
