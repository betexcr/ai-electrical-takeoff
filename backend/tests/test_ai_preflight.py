"""tools/ai_preflight.py against stub clients (spec §5.9.5, AC-T21). No test here makes a network request, reads a
real credential or writes to reports/."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import cv2
import numpy as np
import pytest

from takeoff.app import models as TM
from test_ai_common import StubClient, response, status_error, usage

TOOL = Path(__file__).resolve().parents[1] / "tools" / "ai_preflight.py"
KEY = "sk-ant-api03-FAKE-preflight-secret-0123456789"
TOKEN = "fake-auth-token-preflight-987654321"
DET_ID = "preflight:A1-111:det0-0000"


@pytest.fixture(scope="module")
def pf():
    spec = importlib.util.spec_from_file_location("ai_preflight", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vision_inputs():
    det = TM.RunDetection(id=DET_ID, run_id="preflight", document_id="d" * 64, sheet_id="A1-111", category="switch",
                          subtype="standard", status="as_drawn", point={"u": 0.5, "v": 0.5}, point_pt=(100.0, 100.0))
    ok, png = cv2.imencode(".png", np.full((20, 20), 255, np.uint8))
    return "A1-111", [det], {det.id: png.tobytes()}, {}, {det.id: []}


def agreeing(kw):
    from takeoff.ai import verifier as AV

    row = AV.AIVerdict(item_id=DET_ID, is_device=True, category="switch", subtype="standard", rationale="ok",
                       uncertain=False)
    return response(parsed=AV.AIVerdictBatch(verdicts=[row]), u=usage(1200, 80, 0, 0))


class Runner:
    """A stand-in for the stage run: records that it was called and returns a sheet status."""

    def __init__(self, verify="done", discover="done", reason=None):
        self.calls = 0
        self.status = TM.SheetRunStatus(status="done", n_detections=55,
                                        ai_verify=TM.AIStageStatus(status=verify, reason=reason, implementation="claude",
                                                                   requests=7),
                                        ai_discover=TM.AIStageStatus(status=discover, implementation="claude",
                                                                     requests=20))

    def __call__(self, ai, max_requests):
        self.calls += 1
        return self.status, {"verify": {"requests": 7}, "discover": {"requests": 20}}, "run-001"


def run(pf, env, client=None, profile=False, runner=None, **kw):
    client = client or StubClient(default=agreeing)
    runner = runner or Runner()
    report = pf.preflight("claude-test", env=env, client=client, profile_present=lambda: profile,
                          vision_inputs=vision_inputs, sheet_runner=runner, **kw)
    return report, client, runner


def statuses(report):
    return [(c["name"], c["status"]) for c in report["checks"]]


def test_report_never_contains_secret_values(pf, tmp_path):
    env = {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_AUTH_TOKEN": TOKEN}
    out = tmp_path / "ai_preflight.json"
    assert pf.main(["--model", "claude-test", "--out", str(out)], env=env, client=StubClient(default=agreeing),
                   profile_present=lambda: True, vision_inputs=vision_inputs, sheet_runner=Runner()) == 0
    text = out.read_text(encoding="utf-8")
    report = json.loads(text)
    assert KEY not in text and TOKEN not in text and KEY[-12:] not in text
    assert [c["status"] for c in report["checks"]] == ["pass"] * 4 and report["ok"] and report["all_passed"]
    cred = report["checks"][0]["details"]
    assert cred["present"] == {"ANTHROPIC_API_KEY": True, "ANTHROPIC_AUTH_TOKEN": True, "ant_profile": True}
    assert (cred["mechanism"], cred["also_present"]) == ("ANTHROPIC_API_KEY", ["ANTHROPIC_AUTH_TOKEN", "ant_profile"])
    assert (cred["api_host"], cred["needs_confirmation"]) == ("api.anthropic.com", False)
    vision = report["checks"][2]["details"]
    assert (vision["input_tokens"], vision["output_tokens"], vision["implementation"]) == (1200, 80, "claude")
    assert vision["stop_reason"] == "end_turn" and vision["latency_ms"] is not None and vision["answered"] == [DET_ID]
    assert any("subscription" in n for n in report["notes"])

    # an error whose message carries the secret (and a base URL with user info) is scrubbed too
    env = {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": f"https://user:{TOKEN}@api.anthropic.com/"}
    leaky = StubClient(model_error=status_error(anthropic.InternalServerError, 500, f"bad key {KEY}"))
    assert pf.main(["--out", str(out)], env=env, client=leaky, profile_present=lambda: False,
                   vision_inputs=vision_inputs, sheet_runner=Runner()) == 1
    text = out.read_text(encoding="utf-8")
    assert KEY not in text and TOKEN not in text
    report = json.loads(text)
    assert report["checks"][0]["details"]["api_host"] == "api.anthropic.com"
    assert report["checks"][1]["status"] == "fail" and "http_500" in report["checks"][1]["reason"]


def test_non_default_host_needs_confirmation(pf):
    env = {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": "https://proxy.example.com:8443/v1"}
    report, client, runner = run(pf, env)
    cred = report["checks"][0]
    assert cred["status"] == "fail" and "needs confirmation" in cred["reason"]
    assert (cred["details"]["api_host"], cred["details"]["host_source"]) == ("proxy.example.com", "ANTHROPIC_BASE_URL")
    assert cred["details"]["needs_confirmation"] is True and cred["details"]["confirmed"] is False
    assert statuses(report)[1:] == [(n, "not_run") for n in pf.CHECKS[1:]]
    assert client.retrieved == [] and client.calls == [] and runner.calls == 0  # nothing sent to that host
    assert not report["ok"]

    report, client, _ = run(pf, env, confirm_host="another.example.com")  # the wrong host is no confirmation
    assert report["checks"][0]["status"] == "fail" and client.retrieved == []

    report, client, runner = run(pf, env, confirm_host="proxy.example.com")
    assert report["checks"][0]["status"] == "pass"
    assert report["checks"][0]["details"]["needs_confirmation"] is True
    assert report["checks"][0]["details"]["confirmed"] is True
    assert client.retrieved == ["claude-test"] and runner.calls == 1 and report["all_passed"]

    report, _, _ = run(pf, {"ANTHROPIC_API_KEY": KEY})
    assert report["checks"][0]["details"]["needs_confirmation"] is False and report["checks"][0]["status"] == "pass"


@pytest.mark.parametrize("failing", ["credential_source", "model_available", "vision_request", "stages_on_sheet"])
def test_each_check_failure_is_recorded_and_later_checks_not_run(pf, failing):
    env = {"ANTHROPIC_API_KEY": KEY}
    client, runner = StubClient(default=agreeing), Runner()
    if failing == "credential_source":
        env = {}
    elif failing == "model_available":
        client = StubClient(model_error=status_error(anthropic.NotFoundError, 404, "no such model"), default=agreeing)
    elif failing == "vision_request":
        client = StubClient(default=lambda kw: status_error(anthropic.RateLimitError, 429, "slow down"))
    else:
        runner = Runner(discover="unavailable", verify="done")
        runner.status.ai_discover.reason = "no legend"
    report, client, runner = run(pf, env, client=client, runner=runner)

    i = pf.CHECKS.index(failing)
    assert statuses(report) == ([(n, "pass") for n in pf.CHECKS[:i]] + [(failing, "fail")]
                                + [(n, "not_run") for n in pf.CHECKS[i + 1:]])
    failed = report["checks"][i]
    assert failed["reason"]
    for c in report["checks"][i + 1:]:
        assert c["reason"] == f"not run: {failing} failed"
    assert not report["ok"] and not report["all_passed"]
    expected_reason = {"credential_source": "no credentials", "model_available": "model_not_found",
                       "vision_request": "rate_limited", "stages_on_sheet": "ai_discover unavailable: no legend"}
    assert expected_reason[failing] in failed["reason"]
    # later checks really did not run
    assert (len(client.retrieved) > 0) == (i >= 1)
    assert (len(client.calls) > 0) == (i >= 2)
    assert runner.calls == (1 if i >= 3 else 0)


def test_no_sheet_run_and_report_schema(pf, tmp_path):
    out = tmp_path / "r.json"
    runner = Runner()
    assert pf.main(["--no-sheet-run", "--out", str(out)], env={"ANTHROPIC_API_KEY": KEY},
                   client=StubClient(default=agreeing), profile_present=lambda: False, vision_inputs=vision_inputs,
                   sheet_runner=runner) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert runner.calls == 0
    assert report["checks"][3] == {"name": "stages_on_sheet", "status": "not_run",
                                   "reason": "not run: --no-sheet-run", "details": {}}
    assert report["ok"] and not report["all_passed"]
    assert {"tool", "version", "created_at", "model", "sheet", "sheet_run", "ai_versions", "ok", "all_passed",
            "checks", "notes"} <= set(report)
    for c in report["checks"]:
        assert set(c) == {"name", "status", "reason", "details"} and c["status"] in ("pass", "fail", "not_run")
    model = report["checks"][1]["details"]
    assert model["image_input"] == "not reported"
    unsupported = StubClient(model_info=SimpleNamespace(id="m", capabilities=SimpleNamespace(
        image_input=SimpleNamespace(supported=False))), default=agreeing)
    report, _, _ = run(pf, {"ANTHROPIC_API_KEY": KEY}, client=unsupported)
    assert report["checks"][1]["status"] == "fail" and "no image input" in report["checks"][1]["reason"]
