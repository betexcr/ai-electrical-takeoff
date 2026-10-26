"""Live access preflight for the AI stages (spec §5.9.5). Makes real, billed Claude requests: run it only by explicit
command, never in the automated suite.

From backend/:
    uv run python tools/ai_preflight.py [--model M] [--sheet A1-111] [--no-sheet-run] [--confirm-host HOST] [--out PATH]

Writes ``reports/takeoff/ai_preflight.json`` (or ``--out``): ``checks`` is a list of ``{name, status, reason, details}``
with ``status`` one of ``pass``, ``fail`` or ``not_run``, in this order; a failed check stops the later ones, which are
recorded as ``not_run`` with the reason:

1. ``credential_source``: which of ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an ``ant`` profile is present
   (presence only: values are never read into the report or printed) and the API host: the host the SDK client
   resolves (``client.base_url``: keyword, then ``ANTHROPIC_BASE_URL``, then an ``ant`` profile's own ``base_url``),
   compared with the host of ``ANTHROPIC_BASE_URL`` (else ``api.anthropic.com``). A non-default resolved host, or a
   mismatch between the two, sets ``needs_confirmation``: the check then fails, and no request is sent, until it is
   re-run with ``--confirm-host <the resolved host>`` after the user confirmed it is their own API account (another
   tool's session variables may be present in the shell). Building the client sends nothing.
2. ``model_available``: ``client.models.retrieve(model)``, with image input where the field is reported.
3. ``vision_request``: one ``ai-verify/1`` request on one A1 crop (det/0's first detection in reading order), with
   its stop reason, usage and latency.
4. ``stages_on_sheet``: an ``Analyzer`` run of the sheet on a temporary store with ``ai=True`` (verify and discover);
   both stage statuses and the summed usage. Skipped by ``--no-sheet-run``.

Exit codes: 0 when no check failed; 1 otherwise. Only the check names, statuses and reasons are printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from takeoff.ai import common as AC  # noqa: E402
from takeoff.ai import discover as AD  # noqa: E402
from takeoff.ai import verifier as AV  # noqa: E402
from takeoff.groundtruth.models import utcnow  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
DEFAULT_CACHE = ROOT / "data" / "takeoff" / "cache"
DEFAULT_OUT = ROOT / "reports" / "takeoff" / "ai_preflight.json"
DEFAULT_SHEET = "A1-111"
DEFAULT_HOST = "api.anthropic.com"
VERSION = "ai-preflight/1"
CHECKS = ("credential_source", "model_available", "vision_request", "stages_on_sheet")
ACTOR = "ai_preflight"
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
REDACTED = "[redacted]"
_MESSAGE_CHARS = 300
NOTES = [
    "Presence of credentials only: no credential value is read into this report.",
    "A Claude subscription alone does not include API usage; the requests here are billed to the API account "
    "behind the credential and host above.",
    "This checks access and the integration, not accuracy: tools/eval_ai_verifier.py and "
    "tools/eval_ai_discovery.py measure that against a verified export.",
]


def _check(name: str, status: str, reason: str | None = None, **details) -> dict:
    return {"name": name, "status": status, "reason": reason, "details": details}


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:_MESSAGE_CHARS]


# --- 1. credential source -----------------------------------------------------------------------------------------

def sdk_profile_present() -> bool:
    """Whether the SDK would find an active ``ant`` profile config (file existence only; nothing is read into the
    report). Uses the SDK's own check; False if this SDK has none."""
    try:
        from anthropic.lib.credentials._constants import _has_active_profile_config
    except Exception:
        return False
    try:
        return bool(_has_active_profile_config())
    except Exception:
        return False


def api_host(env: Mapping[str, str]) -> tuple[str, str]:
    """``(host, source)``: the host of ``ANTHROPIC_BASE_URL`` only (no scheme, path, port or user info), else the
    default."""
    base = env.get("ANTHROPIC_BASE_URL")
    if not base:
        return DEFAULT_HOST, "default"
    try:
        host = urlsplit(base.strip()).hostname
    except ValueError:
        host = None
    return (host or "unparseable"), "ANTHROPIC_BASE_URL"


def resolved_host(client) -> str | None:
    """The host the SDK client will actually send to: ``client.base_url`` as the SDK resolved it (keyword, then
    ``ANTHROPIC_BASE_URL``, then an ``ant`` profile's own ``base_url``). Building the client sends nothing."""
    base = getattr(client, "base_url", None)
    if base is None:
        return None
    try:
        return urlsplit(str(base)).hostname or None
    except ValueError:
        return None


def check_credentials(env: Mapping[str, str], profile_present: Callable[[], bool], confirm_host: str | None,
                      make_client: Callable[[], Any]) -> tuple[dict, Any]:
    """``(check, client)``. The client is built only when a credential is present, and is returned only when the
    check passes, so no request can be made before it does."""
    present = {"ANTHROPIC_API_KEY": bool(env.get("ANTHROPIC_API_KEY")),
               "ANTHROPIC_AUTH_TOKEN": bool(env.get("ANTHROPIC_AUTH_TOKEN")),
               "ant_profile": bool(profile_present())}
    order = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ant_profile") if present[k]]
    env_host, source = api_host(env)
    details = dict(present=present, mechanism=order[0] if order else None, also_present=order[1:],
                   api_host=None, env_host=env_host, resolved_host=None, host_source=source,
                   needs_confirmation=None, confirmed=False)
    if not order:
        return _check("credential_source", "fail",
                      "no credentials (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an ant profile)", **details), None
    try:
        client = make_client()
    except Exception as e:
        return _check("credential_source", "fail", f"the SDK client could not be built: {_short(e)}", **details), None
    host = resolved_host(client)
    details.update(api_host=host, resolved_host=host)
    if host is None:
        details["needs_confirmation"] = True
        return _check("credential_source", "fail",
                      "could not determine the API host the SDK client will use", **details), None
    mismatch = host != env_host
    needs = host != DEFAULT_HOST or mismatch
    confirmed = needs and confirm_host is not None and confirm_host.strip().lower() == host.lower()
    details.update(host_mismatch=mismatch, needs_confirmation=needs, confirmed=confirmed)
    if needs and not confirmed:
        why = (f"the SDK resolves the API host {host}, but ANTHROPIC_BASE_URL gives {env_host} (an ant profile's "
               f"base_url?)" if mismatch else f"the API host is {host}, not {DEFAULT_HOST}")
        return _check("credential_source", "fail",
                      f"needs confirmation: {why}; confirm it is your own API account, then re-run with "
                      f"--confirm-host {host}", **details), None
    return _check("credential_source", "pass", None, **details), client


# --- 2. model ---------------------------------------------------------------------------------------------------

def check_model(client, model: str) -> dict:
    try:
        info = client.models.retrieve(model)
    except Exception as e:
        err = AC.error_for(e)
        code = "model_not_found" if err["code"] == "http_404" else err["code"]
        return _check("model_available", "fail", f"models.retrieve failed: {code}", model=model,
                      error={"code": err["code"], "message": _short(e)})
    caps = getattr(info, "capabilities", None)
    image = getattr(getattr(caps, "image_input", None), "supported", None) if caps is not None else None
    details = dict(model=model, id=getattr(info, "id", None),
                   image_input="not reported" if image is None else bool(image))
    if image is False:
        return _check("model_available", "fail", f"model {model!r} reports no image input", **details)
    return _check("model_available", "pass", None, **details)


# --- 3 and 4. the real sheet -------------------------------------------------------------------------------------------

class RealSheet:
    """The supplied PDF's sheet: its view, det/0's output (computed once, shared by checks 3 and 4) and the
    region image."""

    def __init__(self, pdf: Path, sheet_id: str, cache: Path):
        from takeoff.app import sheets as TS

        self.pdf, self.sheet_id, self.cache = Path(pdf), sheet_id, Path(cache)
        self.sha = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.infos = TS.intake(self.pdf, self.sha)
        found = [s for s in self.infos if s.sheet_id == sheet_id]
        if not found:
            raise ValueError(f"sheet {sheet_id} is not in {self.pdf.name}")
        self.info = found[0]
        self.view = TS.sheet_view(self.pdf, self.info, self.sha, self.cache)
        self._det: dict | None = None

    def det_output(self) -> dict:
        if self._det is None:
            from takeoff.app.analyze import Det0Detector

            self._det = Det0Detector()(self.pdf, self.info)
        return self._det

    def vision_inputs(self):
        """``(sheet_id, [detection], crops, icons, tokens)`` for one verify request on det/0's first detection."""
        import pymupdf

        from takeoff.app import models as TM
        from takeoff.app import sheets as TS
        from takeoff.groundtruth import sheets as S
        from takeoff.pdf import coords as C

        raw = sorted(self.det_output()["detections"], key=lambda d: (d["y_pt"], d["x_pt"], d["id"]))
        if not raw:
            raise ValueError(f"det/0 found nothing on {self.sheet_id}")
        d = raw[0]
        geo = self.view["geometry"]
        g = C.PageGeometry(*geo["crop_native"], rotation=geo["rotation"])
        u, v = C.unrotated_to_normalized(g, (d["x_pt"], d["y_pt"]))
        det = TM.RunDetection(id=f"preflight:{d['id']}", run_id="preflight", document_id=self.sha,
                              sheet_id=self.sheet_id, category=d["category"], subtype=d["subtype"], status=d["status"],
                              point={"u": u, "v": v}, point_pt=(d["x_pt"], d["y_pt"]), score=d.get("score"),
                              members=d.get("members", []), subtype_source=d.get("subtype_source"),
                              tokens=d.get("tokens", []))
        doc = pymupdf.open(stream=self.pdf.read_bytes(), filetype="pdf")
        try:
            path, mapping = S.render_region_image(doc[self.info.page_number - 1], self.view["region"]["bbox_pt"],
                                                  self.sha, self.cache)
        finally:
            doc.close()
        crops = {det.id: AV.build_crop(AV.load_gray(path), mapping, det.point_pt)}
        return self.sheet_id, [det], crops, TS.legend_icons(self.pdf, self.info), AV.tokens_near([det], self.view["tokens"])

    def run_stages(self, ai: AC.AIClient, max_requests: int):
        """An ``ai=True`` Analyzer run of the sheet on a temporary store: ``(SheetRunStatus, ai_usage, run_id)``."""
        from takeoff.app import analyze as AN
        from takeoff.app import models as TM
        from takeoff.app.service import TakeoffService
        from takeoff.app.store import TakeoffStore

        sheet = self

        class Precomputed:  # det/0's output for the sheet, computed once for checks 3 and 4
            name, version, params = AN.Det0Detector.name, AN.Det0Detector.version, AN.Det0Detector.params

            def __call__(self, pdf_path, info):
                return sheet.det_output()

        with tempfile.TemporaryDirectory(prefix="ai_preflight_") as tmp:
            store = TakeoffStore(Path(tmp) / "takeoff.sqlite")
            try:
                store.insert("documents", TM.Document(id=self.sha, file_name=self.pdf.name,
                                                      page_count=len(self.infos), source="supplied",
                                                      stored_path=str(self.pdf)), ACTOR)
                for info in self.infos:
                    store.insert("sheets", info, ACTOR)
                pdf_of = lambda _doc: self.pdf  # noqa: E731
                svc = TakeoffService(store, AN.sheet_views(store, pdf_of, self.cache))
                an = AN.Analyzer(svc, pdf_of, AV.ClaudeVerifier(ai), AD.ClaudeDiscoverer(ai), background=False,
                                 max_requests=max_requests, cache_dir=self.cache, detector=Precomputed())
                run = an.start(self.sha, ACTOR, sheet_ids=[self.sheet_id], ai=True)
                return run.sheets[self.sheet_id], an.ai_usage(run.id), run.id
            finally:
                store.close()


def _usage(v) -> dict:
    return dict(stop_reason=v.stop_reason, input_tokens=v.input_tokens, output_tokens=v.output_tokens,
                cache_creation_input_tokens=v.cache_creation_input_tokens,
                cache_read_input_tokens=v.cache_read_input_tokens, latency_ms=v.latency_ms)


def check_vision(ai: AC.AIClient, vision_inputs: Callable) -> dict:
    try:
        sheet_id, dets, crops, icons, tokens = vision_inputs()
    except Exception as e:
        return _check("vision_request", "fail", f"could not build the request: {_short(e)}")
    try:
        (v,) = AV.ClaudeVerifier(ai).verify("preflight", sheet_id, dets, crops, icons, tokens, AC.RequestBudget(1))
    except Exception as e:
        return _check("vision_request", "fail", f"verify raised: {_short(e)}")
    details = dict(sheet=sheet_id, implementation=v.implementation, model=v.model, n_items=len(v.detection_ids),
                   n_images=len(v.image_sha256s), answered=sorted((v.agreement or {})), **_usage(v))
    if v.error is not None:
        return _check("vision_request", "fail", f"request failed: {v.error.get('code')}",
                      error={k: v.error[k] for k in ("code", "message") if k in v.error}, **details)
    return _check("vision_request", "pass", None, **details)


def check_stages(ai: AC.AIClient, sheet_runner: Callable, max_requests: int) -> dict:
    try:
        status, usage, run_id = sheet_runner(ai, max_requests)
    except Exception as e:
        return _check("stages_on_sheet", "fail", f"the analyzer raised: {_short(e)}")
    stages = {"ai_verify": status.ai_verify.model_dump(mode="json"),
              "ai_discover": status.ai_discover.model_dump(mode="json")}
    details = dict(run_id=run_id, sheet_status=status.status, sheet_message=status.message,
                   n_detections=status.n_detections, max_requests=max_requests, **stages, usage=usage)
    bad = [k for k, s in stages.items() if s["status"] not in ("done", "partial")]
    if status.status != "done":
        return _check("stages_on_sheet", "fail", f"sheet {status.status}: {status.message}", **details)
    if bad:
        return _check("stages_on_sheet", "fail",
                      "; ".join(f"{k} {stages[k]['status']}: {stages[k]['reason']}" for k in bad), **details)
    return _check("stages_on_sheet", "pass", None, **details)


# --- the report --------------------------------------------------------------------------------------------------

def preflight(model: str | None = None, sheet: str = DEFAULT_SHEET, sheet_run: bool = True, *,
              env: Mapping[str, str] | None = None, client=None, profile_present: Callable[[], bool] | None = None,
              confirm_host: str | None = None, vision_inputs: Callable | None = None,
              sheet_runner: Callable | None = None, pdf: Path = DEFAULT_PDF, cache: Path = DEFAULT_CACHE) -> dict:
    """Run the checks in order; a failure stops the later ones. ``client``, ``profile_present``, ``vision_inputs``
    and ``sheet_runner`` are injectable for tests (stubs, no request); by default the real SDK and PDF are used."""
    env = os.environ if env is None else env
    model = model or env.get(AC.MODEL_ENV) or AC.DEFAULT_MODEL
    max_requests = AC.RequestBudget.from_env(env).max_requests
    checks: list[dict] = []
    real: dict[str, RealSheet] = {}

    def the_sheet() -> RealSheet:
        if "s" not in real:
            real["s"] = RealSheet(pdf, sheet, cache)
        return real["s"]

    def stop(failed: str) -> None:
        for name in CHECKS[len(checks):]:
            checks.append(_check(name, "not_run", f"not run: {failed} failed"))

    def make_client():
        if client is not None:
            return client
        import anthropic

        return anthropic.Anthropic()  # resolves credentials and the host as the app does; sends nothing

    check, resolved = check_credentials(env, profile_present or sdk_profile_present, confirm_host, make_client)
    checks.append(check)
    if check["status"] == "fail":
        stop("credential_source")
    else:
        checks.append(check_model(resolved, model))
        if checks[-1]["status"] == "fail":
            stop("model_available")
        else:
            ai = AC.AIClient(model=model, client=resolved)
            checks.append(check_vision(ai, vision_inputs or (lambda: the_sheet().vision_inputs())))
            if checks[-1]["status"] == "fail":
                stop("vision_request")
            elif not sheet_run:
                checks.append(_check("stages_on_sheet", "not_run", "not run: --no-sheet-run"))
            else:
                checks.append(check_stages(ai, sheet_runner or (lambda a, n: the_sheet().run_stages(a, n)),
                                           max_requests))
    return {
        "tool": "tools/ai_preflight.py", "version": VERSION, "created_at": utcnow(), "model": model, "sheet": sheet,
        "sheet_run": sheet_run,
        "ai_versions": {"verify": AV.AI_VERSION, "verify_prompt": AV.PROMPT_VERSION,
                        "discover": AD.AI_VERSION, "discover_prompt": AD.PROMPT_VERSION},
        "ok": not any(c["status"] == "fail" for c in checks),
        "all_passed": all(c["status"] == "pass" for c in checks),
        "checks": checks,
        "notes": NOTES,
    }


def to_json(report: dict, env: Mapping[str, str]) -> str:
    """The report as JSON, with any credential or base-URL value scrubbed (defence in depth: none is put in)."""
    text = json.dumps(report, indent=2, sort_keys=False, default=str) + "\n"
    for name in SECRET_ENV:
        value = env.get(name)
        if value and len(value) >= 4 and value != DEFAULT_HOST:
            text = text.replace(json.dumps(value)[1:-1], REDACTED).replace(value, REDACTED)
    return text


def main(argv: list[str] | None = None, **inject: Any) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", help=f"model id (default ${AC.MODEL_ENV} or {AC.DEFAULT_MODEL})")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="sheet for the vision request and the stage run")
    ap.add_argument("--no-sheet-run", action="store_true", help="skip the stage run on the sheet (check 4)")
    ap.add_argument("--confirm-host", help="the non-default API host the user confirmed as their own API account")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="report path")
    ap.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args(argv)
    env = inject.pop("env", None)
    env = os.environ if env is None else env
    report = preflight(args.model, args.sheet, not args.no_sheet_run, env=env, confirm_host=args.confirm_host,
                       pdf=args.pdf, cache=args.cache, **inject)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = to_json(report, env)
    args.out.write_text(text, encoding="utf-8")
    for c in json.loads(text)["checks"]:  # printed from the scrubbed report
        print(f"{c['name']}: {c['status']}" + (f" ({c['reason']})" if c["reason"] else ""))
    print(f"report: {args.out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    from takeoff.envfile import load_for_cli

    load_for_cli()
    sys.exit(main())
