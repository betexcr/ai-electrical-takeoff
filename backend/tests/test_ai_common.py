"""The AI common layer (spec §5.9, §5.9.4) against stub SDK clients. No test here makes a network request.

Stub responses mimic ``ParsedMessage`` (``parsed_output``, ``stop_reason``, ``usage``); errors are the SDK's own
exception classes, built with the SDK's own constructors.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from takeoff.ai import common as AC
from takeoff.ai import verifier as AV
from takeoff.app import models as TM

REQ = httpx2.Request("POST", "https://stub.invalid/v1/messages")


class Out(BaseModel):
    answer: str


def usage(i=100, o=20, cc=0, cr=None):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cc, cache_read_input_tokens=cr)


def response(parsed=None, stop_reason="end_turn", u=None):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=u or usage())


class StubMessages:
    def __init__(self, outer):
        self.outer = outer

    def parse(self, **kw):
        return self.outer._parse(**kw)


class StubClient:
    """Plays a script of responses or exceptions; records every call and the timeout it was given."""

    def __init__(self, script=(), model_info=None, model_error=None, delay_s=0.0, default=None):
        self.script = list(script)
        self.calls = []
        self.timeouts = []
        self.model_info = model_info or SimpleNamespace(id="m", capabilities=None)
        self.model_error = model_error
        self.retrieved = []
        self.delay_s = delay_s
        self.default = default
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.messages = StubMessages(self)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def with_options(self, timeout=None, **_):
        self.timeouts.append(timeout)
        return self

    def _retrieve(self, model):
        self.retrieved.append(model)
        if self.model_error is not None:
            raise self.model_error
        return self.model_info

    def _parse(self, **kw):
        with self.lock:
            self.calls.append(kw)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            item = self.script.pop(0) if self.script else self.default
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            if callable(item) and not isinstance(item, SimpleNamespace):
                item = item(kw)
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            with self.lock:
                self.active -= 1


def status_error(cls, code, msg="err"):
    return cls(msg, response=httpx2.Response(code, request=REQ), body=None)


def call(client):
    return AC.AIClient(model="claude-test", client=client).call("sys", [{"type": "text", "text": "hi"}], Out, 256)


# --- tests ---------------------------------------------------------------------------------------------------

def test_call_success_records_all_usage_fields_and_latency():
    stub = StubClient([response(Out(answer="ok"), u=usage(1234, 56, 7, 8))])
    r = call(stub)
    assert r.error is None and r.parsed == Out(answer="ok") and r.stop_reason == "end_turn"
    assert (r.input_tokens, r.output_tokens, r.cache_creation_input_tokens, r.cache_read_input_tokens) == (1234, 56, 7, 8)
    assert r.latency_ms is not None and r.latency_ms >= 0
    kw = stub.calls[0]
    assert kw["model"] == "claude-test" and kw["max_tokens"] == 256 and kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert kw["output_format"] is Out
    assert stub.timeouts == [AC.TIMEOUT_S] and AC.TIMEOUT_S == 60
    # a usage field the API leaves out stays None rather than becoming 0
    r2 = call(StubClient([response(Out(answer="ok"), u=usage(10, 2, None, None))]))
    assert (r2.cache_creation_input_tokens, r2.cache_read_input_tokens) == (None, None)


def test_each_exception_and_refusal_and_parse_failure_map_to_codes():
    cases = [
        (status_error(anthropic.RateLimitError, 429), "rate_limited"),
        (anthropic.APITimeoutError(request=REQ), "timeout"),  # subclasses APIConnectionError: order matters
        (anthropic.APIConnectionError(request=REQ), "connection"),
        (status_error(anthropic.InternalServerError, 503), "http_503"),
        (status_error(anthropic.BadRequestError, 400), "http_400"),
        (response(None, stop_reason="refusal"), "refusal"),
        (response(Out(answer="x"), stop_reason="refusal"), "refusal"),  # refusal is checked before the output
        (response(None, stop_reason="max_tokens"), "parse_error"),
    ]
    for item, code in cases:
        r = call(StubClient([item]))
        assert r.error is not None and r.error["code"] == code, (item, r.error)
        assert r.parsed is None
        assert r.latency_ms is not None
    # the SDK validates structured output itself and raises on invalid JSON: still a parse error
    def invalid(_kw):
        return Out.model_validate_json('{"nope": 1}')
    r = call(StubClient([invalid]))
    assert r.error["code"] == "parse_error" and r.parsed is None
    # usage of a refused request is still recorded
    r = call(StubClient([response(None, stop_reason="refusal", u=usage(900, 3))]))
    assert (r.input_tokens, r.output_tokens, r.stop_reason) == (900, 3, "refusal")


def test_unavailable_without_sdk_credentials_or_model(monkeypatch, tmp_path):
    # SDK missing
    monkeypatch.setattr(AC, "_import_sdk", lambda: None)
    ok, reason = AC.AIClient(model="m").available()
    assert ok is False and "anthropic" in reason
    monkeypatch.undo()

    # no credentials: a real SDK client with nothing to authenticate with; refused before any request.
    # The base URL points at a closed local port, so even a mistaken request could not leave the machine.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR",
                 "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ok, reason = AC.AIClient(model="m").available()
    assert ok is False and "credential" in reason
    monkeypatch.undo()

    # model not found
    stub = StubClient(model_error=status_error(anthropic.NotFoundError, 404))
    ok, reason = AC.AIClient(model="claude-nope", client=stub).available()
    assert ok is False and "claude-nope" in reason and "not found" in reason
    assert stub.retrieved == ["claude-nope"]

    # credentials rejected
    ok, reason = AC.AIClient(model="m", client=StubClient(model_error=status_error(anthropic.AuthenticationError, 401))).available()
    assert ok is False and "credential" in reason

    # no image input, where the field is reported
    no_img = SimpleNamespace(id="m", capabilities=SimpleNamespace(image_input=SimpleNamespace(supported=False)))
    ok, reason = AC.AIClient(model="m", client=StubClient(model_info=no_img)).available()
    assert ok is False and "image" in reason

    # available: with image support reported, and when the capability field is absent
    img = SimpleNamespace(id="m", capabilities=SimpleNamespace(image_input=SimpleNamespace(supported=True)))
    assert AC.AIClient(model="m", client=StubClient(model_info=img)).available() == (True, None)
    assert AC.AIClient(model="m", client=StubClient()).available() == (True, None)

    # the model defaults to claude-opus-5-5 and TAKEOFF_AI_MODEL overrides it
    monkeypatch.delenv("TAKEOFF_AI_MODEL", raising=False)
    assert AC.AIClient(client=StubClient()).model == AC.DEFAULT_MODEL == "claude-opus-5-5"
    monkeypatch.setenv("TAKEOFF_AI_MODEL", "claude-other")
    assert AC.AIClient(client=StubClient()).model == "claude-other"


def _dets(n, sheet="A1-111"):
    return [TM.RunDetection(id=f"d{i:02d}", run_id="run-001", document_id="d" * 64, sheet_id=sheet, category="switch",
                            subtype="standard", point={"u": 0.1, "v": 0.1}, point_pt=(100.0 + 10 * i, 100.0))
            for i in range(n)]


def _agreeing(kw):
    ids = [b["text"].split(": detector says")[0].removeprefix("Item ")
           for b in kw["messages"][0]["content"] if b["type"] == "text" and b["text"].startswith("Item ")]
    return response(AV.AIVerdictBatch(verdicts=[AV.AIVerdict(item_id=i, is_device=True, category="switch", subtype="standard",
                                                            rationale="looks like a switch", uncertain=False) for i in ids]))


def test_budget_skips_are_recorded_not_dropped():
    b = AC.RequestBudget(2)
    assert [b.take(), b.take(), b.take()] == [True, True, False]
    assert (b.used, b.skipped) == (2, 1)

    stub = StubClient(default=_agreeing)
    dets = _dets(5)
    v = AV.ClaudeVerifier(AC.AIClient(model="m", client=stub), batch_size=1)
    out = v.verify("run-001", "A1-111", dets, {d.id: b"" for d in dets}, {}, {}, AC.RequestBudget(3))
    assert len(out) == 5  # one verdict per batch, sent or not
    skipped = [x for x in out if x.error == {"code": "skipped_budget"}]
    assert len(skipped) == 2 and len(stub.calls) == 3
    # skips are the last batches in reading order, deterministically; nothing was sent for them
    assert [x.detection_ids for x in skipped] == [["d03"], ["d04"]]
    assert all(x.input_tokens is None and x.agreement is None for x in skipped)
    assert AC.summarize(out)["errors_by_code"] == {"skipped_budget": 2}

    # the default cap comes from TAKEOFF_AI_MAX_REQUESTS (default 200)
    assert AC.RequestBudget.from_env({}).max_requests == AC.DEFAULT_MAX_REQUESTS == 200
    assert AC.RequestBudget.from_env({"TAKEOFF_AI_MAX_REQUESTS": "7"}).max_requests == 7

    # concurrent takes never hand out more than the cap
    b = AC.RequestBudget(50)
    got = []
    threads = [threading.Thread(target=lambda: got.append(sum(b.take() for _ in range(20)))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(got) == 50 and b.skipped == 110


def test_concurrency_never_exceeds_4():
    assert AC.MAX_CONCURRENCY == 4
    lock, state = threading.Lock(), {"active": 0, "max": 0}

    def job(x):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1
        return x * 2

    assert AC.map_concurrent(job, list(range(16))) == [x * 2 for x in range(16)]  # order kept
    assert 1 < state["max"] <= 4

    stub = StubClient(default=_agreeing, delay_s=0.03)
    dets = _dets(16)
    out = AV.ClaudeVerifier(AC.AIClient(model="m", client=stub), batch_size=1).verify(
        "run-001", "A1-111", dets, {d.id: b"" for d in dets}, {}, {}, AC.RequestBudget(100))
    assert len(stub.calls) == 16 and 1 < stub.max_active <= 4
    assert [x.detection_ids[0] for x in out] == [d.id for d in dets]  # verdicts come back in reading order


def _verdict(code=None, i=100, o=10, cc=0, cr=0, lat=10.0, impl="claude", n=2):
    return TM.ModelVerdict(run_id="run-001", sheet_id="A1-111", kind="verify", implementation=impl, model="m",
                           prompt_version="p", ai_version="a", detection_ids=[f"d{k}" for k in range(n)],
                           input_tokens=None if code == "skipped_budget" else i,
                           output_tokens=None if code == "skipped_budget" else o,
                           cache_creation_input_tokens=None if code == "skipped_budget" else cc,
                           cache_read_input_tokens=None if code == "skipped_budget" else cr,
                           latency_ms=None if code == "skipped_budget" else lat,
                           error={"code": code} if code else None)


def test_summarize_sums_tokens_and_counts_errors():
    vs = [_verdict(i=100, o=10, cc=5, cr=1, lat=10.0), _verdict(i=200, o=20, cc=None, cr=None, lat=30.0),
          _verdict("timeout", i=0, o=0, lat=60000.0), _verdict("refusal", i=50, o=1, lat=20.0),
          _verdict("skipped_budget"), _verdict("skipped_budget")]
    s = AC.summarize(vs)
    assert s["requests"] == 4  # budget skips were never sent
    assert s["errors_by_code"] == {"refusal": 1, "skipped_budget": 2, "timeout": 1}
    assert s["tokens"] == {"input": 350, "output": 31, "cache_creation": 5, "cache_read": 1}
    assert s["latency_ms"] == {"p50": 20.0, "p95": 60000.0}
    assert s["implementation"] == "claude" and s["n_items"] == 12
    # the shape fits the per-sheet stage summary record
    TM.AIStageStatus(status="partial", **s)
    assert AC.summarize([])["requests"] == 0 and AC.summarize([])["latency_ms"] == {"p50": None, "p95": None}
    assert AC.summarize([_verdict(impl="fake")])["implementation"] == "fake"
