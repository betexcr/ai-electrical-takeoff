"""The common layer for the AI stages (spec §5.9, §5.9.4): one Claude client, error codes, budget, concurrency, usage.

Everything the stages send goes through ``AIClient.call``, which never raises for a request failure: it returns a
``CallResult`` whose ``error`` is ``{"code": ...}`` with one of ``rate_limited``, ``timeout``, ``connection``,
``http_<status>``, ``refusal`` or ``parse_error`` (``skipped_budget`` is recorded by the stage when
``RequestBudget.take`` refuses, without calling). ``{"code": "error", "type": <exception class>, "message": ...}``
is the deliberate catch-all for an unexpected exception (a bug or an SDK change rather than an API outcome): it
keeps the run completing and the failure visible, and it is never mistaken for one of the API codes above.
Credentials resolve as the SDK resolves them (environment or an ``ant`` profile); their values are never read,
logged or stored here.

Refusal-safe parsing: ``messages.parse`` in anthropic 2.3.0 validates the JSON while building its result, so a
refusal whose text is not JSON would raise before ``stop_reason`` could be read. ``call`` therefore asks for the
raw response of ``messages.parse`` (the SDK's own raw-response wrapper; 2.3.0's ``messages.with_raw_response``
has no ``parse``), reads ``stop_reason`` and the four usage fields from the raw message first, and only then runs
the SDK's validation, so usage and stop reason are kept whatever the outcome.
"""

from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV = "TAKEOFF_AI_MODEL"
MAX_CONCURRENCY = 4
TIMEOUT_S = 60
DEFAULT_MAX_REQUESTS = 200
MAX_REQUESTS_ENV = "TAKEOFF_AI_MAX_REQUESTS"
_MESSAGE_CHARS = 300  # error messages kept for diagnosis (SDK messages carry no credentials)


def _import_sdk():
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic


@dataclass
class CallResult:
    parsed: Any = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    latency_ms: float | None = None
    error: dict | None = None


def _message(e: BaseException) -> str:
    return str(e)[:_MESSAGE_CHARS]


def error_for(e: BaseException, sdk=None) -> dict:
    """The error record for an exception raised by a request (most specific SDK class first)."""
    import pydantic

    sdk = sdk or _import_sdk()
    if sdk is not None:
        if isinstance(e, sdk.RateLimitError):
            return {"code": "rate_limited", "message": _message(e)}
        if isinstance(e, sdk.APITimeoutError):  # subclasses APIConnectionError
            return {"code": "timeout"}
        if isinstance(e, sdk.APIConnectionError):
            return {"code": "connection", "message": _message(e)}
        if isinstance(e, sdk.APIStatusError):
            return {"code": f"http_{e.status_code}", "message": _message(e)}
        if isinstance(e, sdk.APIResponseValidationError):
            return {"code": "parse_error", "message": _message(e)}
    if isinstance(e, (pydantic.ValidationError, json.JSONDecodeError)):  # invalid structured output
        return {"code": "parse_error", "message": _message(e)}
    return unexpected_error(e)


def unexpected_error(e: BaseException) -> dict:
    """The catch-all record for an exception that is not an API outcome (see the module docstring)."""
    return {"code": "error", "type": type(e).__name__, "message": _message(e)}


def _raw_parse(messages, sdk):
    """``messages.parse`` returning the SDK's raw ``APIResponse`` (validation deferred to ``.parse()``)."""
    raw = getattr(getattr(messages, "with_raw_response", None), "parse", None)
    if raw is not None:  # a future SDK may expose it directly
        return raw
    from anthropic._response import to_raw_response_wrapper  # what the SDK's own ``with_raw_response`` uses

    return to_raw_response_wrapper(messages.parse)


def _has_no_credentials(client) -> bool:
    """True when an SDK client resolved no credential at all (no key, token or profile). Makes no request."""
    attrs = ("api_key", "auth_token", "credentials")
    return all(hasattr(client, a) for a in attrs) and all(getattr(client, a) is None for a in attrs)


class AIClient:
    """A thin wrapper over ``anthropic.Anthropic``; tests pass a stub ``client``."""

    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self._client = client
        self._available: tuple[bool, str | None] | None = None
        self._lock = threading.Lock()

    def _sdk_client(self):
        if self._client is None:
            sdk = _import_sdk()
            if sdk is None:
                raise RuntimeError("the anthropic SDK is not installed")
            self._client = sdk.Anthropic()
        return self._client

    def available(self) -> tuple[bool, str | None]:
        """Whether the stage can run: SDK installed, credentials resolved, model found and accepting images.

        Checked with ``models.retrieve``. A success and a credential or configuration failure are cached for the
        client's lifetime; any other failure of the check (network, timeout, 5xx, 429) is transient and checked
        again on the next call, so the next run can use the AI once the network is back (final review M-e).
        """
        with self._lock:
            if self._available is not None:
                return self._available
            ok, reason, lasting = self._check()
            if lasting:
                self._available = (ok, reason)
            return ok, reason

    def _check(self) -> tuple[bool, str | None, bool]:
        """``(ok, reason, lasting)``: ``lasting`` is False for a transient failure (not cached)."""
        sdk = _import_sdk()
        if sdk is None:
            return False, "the anthropic SDK is not installed", True
        try:
            client = self._sdk_client()
        except Exception as e:  # e.g. a broken profile
            return False, f"no usable credentials: the client could not be built ({type(e).__name__})", True
        if _has_no_credentials(client):
            return False, "no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)", True
        try:
            info = client.models.retrieve(self.model)
        except sdk.NotFoundError:
            return False, f"model {self.model!r} not found", True
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as e:
            return False, f"credentials rejected (http_{e.status_code})", True
        except TypeError:  # the SDK's "could not resolve authentication method"
            return False, "no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)", True
        except Exception as e:  # network, timeout, 5xx, 429 ...: try again next time
            return False, f"model check failed: {error_for(e, sdk)['code']}", False
        caps = getattr(info, "capabilities", None)
        image = getattr(caps, "image_input", None) if caps is not None else None
        if image is not None and getattr(image, "supported", None) is False:
            return False, f"model {self.model!r} reports no image input", True
        return True, None, True

    def call(self, system: str, content_blocks: list[dict], output_model, max_tokens: int) -> CallResult:
        sdk = _import_sdk()
        t0 = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - t0) * 1000.0, 3)  # noqa: E731
        try:
            messages = self._sdk_client().with_options(timeout=TIMEOUT_S).messages
            raw = _raw_parse(messages, sdk)(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": content_blocks}], output_format=output_model)
            body = json.loads(raw.read())  # the message as sent by the API, before any validation
        except Exception as e:
            return CallResult(latency_ms=elapsed(), error=error_for(e, sdk))
        u = body.get("usage") if isinstance(body, dict) else None
        u = u if isinstance(u, dict) else {}
        result = CallResult(stop_reason=body.get("stop_reason") if isinstance(body, dict) else None,
                            **{f: u.get(f) for f in ("input_tokens", "output_tokens",
                                                     "cache_creation_input_tokens", "cache_read_input_tokens")})
        if result.stop_reason == "refusal":  # before any output is read or validated
            result.latency_ms, result.error = elapsed(), {"code": "refusal"}
            return result
        try:
            parsed = getattr(raw.parse(), "parsed_output", None)  # the SDK's own validation runs here
        except Exception as e:
            result.latency_ms, result.error = elapsed(), error_for(e, sdk)  # usage and stop_reason are kept
            return result
        result.latency_ms = elapsed()
        if parsed is None:
            result.error = {"code": "parse_error", "message": f"no parsed output (stop_reason {result.stop_reason})"}
            return result
        result.parsed = parsed
        return result


class RequestBudget:
    """A per-run cap on requests, shared by every stage and thread of the run."""

    def __init__(self, max_requests: int):
        self.max_requests = int(max_requests)
        self.used = 0
        self.skipped = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RequestBudget":
        env = os.environ if env is None else env
        return cls(int(env.get(MAX_REQUESTS_ENV) or DEFAULT_MAX_REQUESTS))

    def take(self) -> bool:
        with self._lock:
            if self.used < self.max_requests:
                self.used += 1
                return True
            self.skipped += 1
            return False


def map_concurrent(fn: Callable, items: Iterable, limit: int = MAX_CONCURRENCY) -> list:
    """``[fn(x) for x in items]`` with at most ``limit`` running at once; results keep the input order."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(limit, MAX_CONCURRENCY, len(items)))) as pool:
        return list(pool.map(fn, items))


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.standard_b64encode(png).decode()}}


def _percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (as ``evaluation.matching``)."""
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def summarize(verdicts) -> dict:
    """Requests sent, failures by code (skips included), summed tokens, latency p50/p95 and implementation.

    The keys match ``AIStageStatus`` apart from ``status``, which the stage sets.
    """
    verdicts = list(verdicts)
    codes = Counter((v.error or {}).get("code") for v in verdicts if v.error)
    # skipped_budget and discovery's skipped_no_legend were never sent
    sent = [v for v in verdicts if not str((v.error or {}).get("code") or "").startswith("skipped_")]
    total = lambda f: sum(getattr(v, f) or 0 for v in verdicts)  # noqa: E731
    latencies = [v.latency_ms for v in sent if v.latency_ms is not None]
    impls = sorted({v.implementation for v in verdicts})
    return {
        "implementation": impls[0] if len(impls) == 1 else ("mixed" if impls else None),
        "requests": len(sent),
        "errors_by_code": dict(sorted(codes.items())),
        "tokens": {"input": total("input_tokens"), "output": total("output_tokens"),
                   "cache_creation": total("cache_creation_input_tokens"), "cache_read": total("cache_read_input_tokens")},
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "n_items": sum(len(v.detection_ids) for v in verdicts),
    }
