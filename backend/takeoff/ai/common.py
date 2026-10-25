"""The common layer for the AI stages (spec §5.9, §5.9.4): one Claude client, error codes, budget, concurrency, usage.

Everything the stages send goes through ``AIClient.call``, which never raises for a request failure: it returns a
``CallResult`` whose ``error`` is ``{"code": ...}`` with one of ``rate_limited``, ``timeout``, ``connection``,
``http_<status>``, ``refusal`` or ``parse_error`` (``skipped_budget`` is recorded by the stage when
``RequestBudget.take`` refuses, without calling). Credentials resolve as the SDK resolves them (environment or an
``ant`` profile); their values are never read, logged or stored here.
"""

from __future__ import annotations

import base64
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
    if isinstance(e, (pydantic.ValidationError, ValueError)):  # the SDK validates structured output and raises
        return {"code": "parse_error", "message": _message(e)}
    return {"code": "error", "type": type(e).__name__, "message": _message(e)}


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

        Checked once per client (``models.retrieve``) and cached.
        """
        with self._lock:
            if self._available is None:
                self._available = self._check()
            return self._available

    def _check(self) -> tuple[bool, str | None]:
        sdk = _import_sdk()
        if sdk is None:
            return False, "the anthropic SDK is not installed"
        try:
            client = self._sdk_client()
        except Exception as e:  # e.g. a broken profile
            return False, f"no usable credentials: the client could not be built ({type(e).__name__})"
        if _has_no_credentials(client):
            return False, "no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)"
        try:
            info = client.models.retrieve(self.model)
        except sdk.NotFoundError:
            return False, f"model {self.model!r} not found"
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as e:
            return False, f"credentials rejected (http_{e.status_code})"
        except TypeError:  # the SDK's "could not resolve authentication method"
            return False, "no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)"
        except Exception as e:
            return False, f"model check failed: {error_for(e, sdk)['code']}"
        caps = getattr(info, "capabilities", None)
        image = getattr(caps, "image_input", None) if caps is not None else None
        if image is not None and getattr(image, "supported", None) is False:
            return False, f"model {self.model!r} reports no image input"
        return True, None

    def call(self, system: str, content_blocks: list[dict], output_model, max_tokens: int) -> CallResult:
        sdk = _import_sdk()
        t0 = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - t0) * 1000.0, 3)  # noqa: E731
        try:
            response = self._sdk_client().with_options(timeout=TIMEOUT_S).messages.parse(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": content_blocks}], output_format=output_model)
        except Exception as e:
            return CallResult(latency_ms=elapsed(), error=error_for(e, sdk))
        latency = elapsed()
        u = getattr(response, "usage", None)
        result = CallResult(stop_reason=getattr(response, "stop_reason", None), latency_ms=latency,
                            **{f: getattr(u, f, None) for f in ("input_tokens", "output_tokens",
                                                                "cache_creation_input_tokens", "cache_read_input_tokens")})
        if result.stop_reason == "refusal":  # before reading any output
            result.error = {"code": "refusal"}
            return result
        parsed = getattr(response, "parsed_output", None)
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
    """Requests sent, failures by code (budget skips included), summed tokens, latency p50/p95 and implementation.

    The keys match ``AIStageStatus`` apart from ``status``, which the stage sets.
    """
    verdicts = list(verdicts)
    codes = Counter((v.error or {}).get("code") for v in verdicts if v.error)
    sent = [v for v in verdicts if (v.error or {}).get("code") != "skipped_budget"]
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
