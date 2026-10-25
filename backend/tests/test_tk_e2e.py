"""End-to-end tests of the takeoff page, layout A (spec §5.6; AC-T2 to AC-T9, AC-T15, AC-T16, AC-T18 to AC-T20).

Playwright + Chromium against a live uvicorn server on a temporary data directory. The app runs with
``background=False`` (a run finishes inside its request), a scripted fake detector (no candidate generation) and the
AI test doubles (``FakeVerifier`` / ``FakeDiscoverer``); only A1 is analyzed. No test makes a network request.

The demo path (AC-T13, §4.14, §4.16) is the exception to the scripted detector: it runs the real det/0 on all 4
sheets in the background, as the page does, and writes its screenshots, ``run.json`` and a copy of the export to
``reports/takeoff/demo/``. It uses Claude only when ``reports/takeoff/ai_preflight.json`` shows every check passed
*and* ``TAKEOFF_LIVE_AI=1``; otherwise it uses the test doubles, labelled "test double, not a model".

Expected screen positions are recomputed here in Python from the served image mapping and the page's view transform
(the same independent re-implementation as ``test_gt_e2e.py``), never taken from the page's own helpers. The figures
the page shows are compared with ``GET /counts`` and the saved device records, never with numbers the page computed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import socket
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest
import uvicorn

from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import analyze as AN
from takeoff.app import models as TM
from takeoff.app.api import create_app
from takeoff.app.rooms import propose_room
from takeoff.detection import det0

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
pytestmark = [pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present"), pytest.mark.e2e]
A1 = "A1-111"
SHEETS = ["A1-111", "A2-111", "A3-111", "A4-111"]
FAKE = " · test double, not a model"
SCORE_LABEL = "match score (not a probability)"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1400, "height": 900}}


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("tk_render_cache")  # renders are keyed by PDF hash: safe to share


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeDetector:
    """A det0-shaped detector over scripted points (no candidate generation); points are set per test."""

    name = "fake-detector"
    version = "fake/1"
    params = {"fake": True}

    def __init__(self):
        self.points_by_sheet: dict[str, list] = {}

    def __call__(self, pdf_path, sheet: TM.SheetInfo) -> dict:
        dets = [{"id": f"{sheet.sheet_id}:fake-{n:04d}", "sheet_id": sheet.sheet_id, "category": c, "subtype": s,
                 "status": "as_drawn", "x_pt": x, "y_pt": y, "score": 0.9, "members": [],
                 "subtype_source": "default", "tokens": []}
                for n, (c, s, x, y) in enumerate(self.points_by_sheet.get(sheet.sheet_id, []))]
        return {"detector_version": self.version, "params": self.params,
                "input_runs": [{"generator": "fake", "run_hash": f"fakehash-{sheet.sheet_id}"}], "detections": dets}


class Server:
    """``detector="fake"`` (default) scripts det0-shaped points; ``None`` is the real det/0; any other object is used
    as the detector. ``ai`` overrides the (verifier, discoverer) test doubles (``"claude"`` for a live demo)."""

    def __init__(self, tmp_path: Path, cache: Path, verifier=None, discoverer=None, *, detector="fake",
                 background: bool = False, ai=None):
        self.detector = FakeDetector() if detector == "fake" else detector
        self.verifier = verifier if verifier is not None else AV.FakeVerifier()
        self.discoverer = discoverer if discoverer is not None else AD.FakeDiscoverer()
        self.app = create_app(tmp_path / "data", PDF, default_reviewer="e2e", background=background,
                              ai=ai if ai is not None else (self.verifier, self.discoverer), detector=self.detector,
                              cache_dir=cache)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.srv = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.srv.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 60
        while not self.srv.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        self.http = httpx.Client(base_url=self.url, headers={"X-Reviewer": "e2e"}, timeout=120)
        self.doc = self.call("GET", "/api/config")["default_document"]
        self._meta: dict[str, dict] = {}

    def call(self, method, path, json=None):
        r = self.http.request(method, path, json=json)
        assert r.status_code < 400, (path, r.status_code, r.text)
        return r.json()

    def meta(self, sid=A1) -> dict:
        if sid not in self._meta:
            self._meta[sid] = self.call("GET", f"/api/documents/{self.doc}/sheets/{sid}/meta")
        return self._meta[sid]

    def counts(self) -> dict:
        return self.call("GET", f"/api/documents/{self.doc}/counts")

    def devices(self, sid=None) -> list[dict]:
        q = f"?sheet_id={sid}" if sid else ""
        return self.call("GET", f"/api/documents/{self.doc}/devices{q}")["devices"]

    def device(self, did) -> dict:
        return self.call("GET", f"/api/devices/{did}")

    def create(self, sid, category, subtype, pt, **kw) -> dict:
        return self.call("POST", f"/api/documents/{self.doc}/devices",
                         {"sheet_id": sid, "category": category, "subtype": subtype,
                          "point": to_norm(self.meta(sid), pt), **kw})

    def analyze_a1(self, specs, verify=None, discover=()) -> list[dict]:
        """Analyze A1 with the fake detector over ``specs`` [(category, subtype, (x, y))]; ``verify`` maps a spec
        index to the FakeVerifier's answer for it; ``discover`` lists the FakeDiscoverer's (pt, category, subtype)
        points. Returns the devices of ``specs``, in order (by position)."""
        run_id = f"run-{len(self.call('GET', f'/api/documents/{self.doc}/runs')) + 1:03d}"
        self.detector.points_by_sheet[A1] = [(c, s, *pt) for c, s, pt in specs]
        for i, answer in (verify or {}).items():
            self.verifier.script[f"{run_id}:{A1}:fake-{i:04d}"] = answer
        m = self.meta(A1)
        for pt, cat, sub in discover:
            tid, point = tile_point(m, pt, cat, sub)
            self.discoverer.points_by_tile.setdefault(tid, []).append(point)
        run = self.call("POST", f"/api/documents/{self.doc}/analyze", {"sheet_ids": [A1]})
        assert run["id"] == run_id and run["sheets"][A1]["status"] == "done", run
        devs = self.devices(A1)
        return [next(d for d in devs if math.dist(d["point_pt"], pt) < 1e-3) for _, _, pt in specs]

    def stop(self):
        self.http.close()
        self.srv.should_exit = True
        self.thread.join(15)


@pytest.fixture
def server(tmp_path, cache_dir):
    s = Server(tmp_path, cache_dir)
    yield s
    s.stop()


# --- geometry helpers (independent of the page) -----------------------------------------------------------------

def clear_points(meta: dict, n: int, sep: float = 40.0) -> list[tuple[float, float]]:
    """``n`` points inside the region with a clear (non-ambiguous, labeled) room proposal, ``sep`` pt apart."""
    x0, y0, x1, y1 = meta["region"]["bbox_pt"]
    out = []
    y = y0 + 30
    while y < y1 - 30 and len(out) < n:
        x = x0 + 30
        while x < x1 - 30 and len(out) < n:
            p = (round(x, 3), round(y, 3))
            prop = propose_room(p, meta["rooms"])
            if (not prop.ambiguous and prop.room is not None and prop.room["kind"] == "labeled"
                    and all(math.dist(p, q) >= sep for q in out)):
                out.append(p)
            x += 7.0
        y += 7.0
    assert len(out) == n
    return out


def to_norm(meta: dict, pt) -> dict:
    g = meta["geometry"]
    x0, y0, x1, y1 = g["crop_native"]
    return {"u": (pt[0] - x0) / (x1 - x0), "v": (pt[1] - y0) / (y1 - y0)}  # the supplied sheets are unrotated


def tile_point(meta: dict, pt, category: str, subtype=None) -> tuple[str, dict]:
    """The discover tile in which ``pt`` is most central, and the AI point (tile pixels) for it."""
    tile = max(AD.tiles(meta["region"]["bbox_pt"], A1), key=lambda t: AD.centrality_pt(t, pt))
    x, y = AD.pt_to_tile_px(meta["image"], tile, pt)
    return tile.id, {"x_px": x, "y_px": y, "category": category, "subtype": subtype, "uncertain": False,
                     "rationale": "fake: scripted point"}


def expected_screen(m, view, u, v):
    """Independent re-implementation: normalized -> image px -> viewport-local screen px."""
    img, c = m["image"], m["image"]["covers"]
    x = (u - c["u0"]) / (c["u1"] - c["u0"]) * img["width_px"]
    y = (v - c["v0"]) / (c["v1"] - c["v0"]) * img["height_px"]
    return view["tx"] + x * view["s"], view["ty"] + y * view["s"]


def dist_pt(m, a, b):
    g = m["geometry"]
    return math.hypot((a["u"] - b["u"]) * g["displayed_width_pt"], (a["v"] - b["v"]) * g["displayed_height_pt"])


# --- page helpers --------------------------------------------------------------------------------------------------

def open_page(page, server, sheet_id=A1):
    page.goto(f"{server.url}/?sheet={sheet_id}")
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("window.tk && tk.state.meta && tk.state.meta.sheet_id === %r && !tk.state.loading" % sheet_id)


def choose_sheet(page, sheet_id):
    page.select_option("#sheet", sheet_id)
    page.wait_for_function("tk.state.meta && tk.state.meta.sheet_id === %r && !tk.state.loading" % sheet_id)


def viewport_rect(page):
    return page.evaluate("(() => { const r = document.getElementById('viewport').getBoundingClientRect(); "
                         "return {x: r.left, y: r.top, w: r.width, h: r.height}; })()")


def fit_scale(page, m):
    vp = viewport_rect(page)
    return min(vp["w"] / m["image"]["width_px"], vp["h"] / m["image"]["height_px"]) * 0.96


def set_view_centered(page, m, u, v, s):
    vp = viewport_rect(page)
    img, c = m["image"], m["image"]["covers"]
    x = (u - c["u0"]) / (c["u1"] - c["u0"]) * img["width_px"]
    y = (v - c["v0"]) / (c["v1"] - c["v0"]) * img["height_px"]
    page.evaluate("([s, tx, ty]) => tk.setView(s, tx, ty)", [s, vp["w"] / 2 - x * s, vp["h"] / 2 - y * s])
    return page.evaluate("tk.view")


def center_of(page, selector):
    return page.evaluate("""(sel) => { const el = document.querySelector(sel); if (!el) return null;
        const r = el.getBoundingClientRect(), v = document.getElementById('viewport').getBoundingClientRect();
        return [r.left + r.width / 2 - v.left, r.top + r.height / 2 - v.top]; }""", selector)


def marker(did):
    return f'.device[data-id="{did}"]'


def select_device(page, did):
    page.dispatch_event(marker(did), "mousedown")
    page.wait_for_function("tk.state.detail && tk.state.detail.device.id === %r" % did)


def click_plan(page, m, u, v, s=1.5):
    """Centre the view on (u, v) and click there."""
    view = set_view_centered(page, m, u, v, s)
    vp = viewport_rect(page)
    sx, sy = expected_screen(m, view, u, v)
    page.mouse.click(vp["x"] + sx, vp["y"] + sy)


def official_total(page) -> int:
    return int(page.get_attribute("#official-total", "data-total"))


def wait_total(page, n, timeout=5000):
    page.wait_for_function("(document.querySelector('#official-total') || {dataset: {}}).dataset.total === %r"
                           % str(n), timeout=timeout)


class Dialogs:
    """Answers the page's prompt/confirm dialogs from a queue (None dismisses) and records every message."""

    def __init__(self, page):
        self.answers: list = []
        self.messages: list[str] = []
        page.on("dialog", self._handle)

    def _handle(self, d):
        self.messages.append(d.message)
        answer = self.answers.pop(0) if self.answers else None
        if answer is None:
            d.dismiss()
        elif answer is True:
            d.accept()
        else:
            d.accept(answer)


def review_lines_expected(r: dict) -> dict:
    return {
        "detected": f"Detected by analysis {r['detected']} = {r['accepted_detected']} accepted + {r['rejected']} "
                    f"rejected + {r['awaiting']} awaiting review · of which AI-only candidates {r['detected_ai_only']}",
        "awaiting": f"Awaiting review {r['awaiting']} = {r['awaiting_unflagged']} unflagged + "
                    f"{r['awaiting_flagged']} flagged",
        "manual": f"Added manually {r['manual']}",
        "official": f"Official takeoff total {r['official_total']} = {r['accepted_detected']} accepted + "
                    f"{r['manual']} manual",
        "open_flags": f"Accepted with open flags {r['accepted_with_open_flags']} (still counted)",
    }


def assert_review_lines(page, server):
    r = server.counts()["review"]
    for key, want in review_lines_expected(r).items():
        assert page.inner_text(f'#review-lines [data-line="{key}"]').strip() == want, key
    assert page.inner_text("#bulk-accept").strip() == f"Accept all unflagged ({r['awaiting_unflagged']})"
    return r


# --- AC-T2: alignment ------------------------------------------------------------------------------------------------

def test_markers_stay_aligned_across_zoom_and_pan(page, server):
    seeded = {}
    for sid in SHEETS:
        (pt,) = clear_points(server.meta(sid), 1)
        seeded[sid] = server.create(sid, "other_electrical", "other", pt, notes="e2e alignment probe")
    open_page(page, server)
    for sid in SHEETS:
        choose_sheet(page, sid)
        m = page.evaluate("tk.state.meta")
        dev = seeded[sid]
        assert page.locator(".device[data-id]").count() == 1
        fit = fit_scale(page, m)
        u, v = dev["point"]["u"], dev["point"]["v"]
        for factor in (0.5, 1, 2, 4):
            for pan in ((0, 0), (137.5, -61.25)):
                set_view_centered(page, m, u, v, fit * factor)
                view = page.evaluate("([dx, dy]) => { const v = tk.view; tk.setView(v.s, v.tx + dx, v.ty + dy); "
                                     "return tk.view; }", list(pan))
                got = center_of(page, f'{marker(dev["id"])} circle.core')
                want = expected_screen(m, view, u, v)
                assert got is not None
                assert abs(got[0] - want[0]) <= 1.5 and abs(got[1] - want[1]) <= 1.5, (sid, factor, pan, got, want)


# --- AC-T3: read -------------------------------------------------------------------------------------------------------

def test_detail_panel_shows_read_fields_and_score_label(page, server):
    p, q = clear_points(server.meta(), 2)
    d0, _ = server.analyze_a1([("switch", "standard", p), ("receptacle", "afi_duplex", q)])
    open_page(page, server)
    select_device(page, d0["id"])
    text = page.inner_text("#device")
    detail = server.device(d0["id"])
    dev = detail["device"]
    assert dev["room"]["kind"] == "labeled"
    for want in ("switch", "ELECTRICAL SWITCH", dev["room"]["name"], "proposed nearest label", "proposed",
                 "detector", SCORE_LABEL, "0.90", "run-001", "fake-detector", "fake/1", "takeoff-merge/1",
                 "ai-verify/1", "created", "Subtype source", "default", "tokens"):
        assert want in text, (want, text)
    assert page.inner_text("#ai-line").strip() == "AI second opinion: agrees" + FAKE
    assert "fake verifier: scripted answer" in page.inner_text("#ai-rationale")
    hist = page.inner_text("#history")
    assert len(detail["history"]) >= 1 and all(e["action"] in hist for e in detail["history"])
    assert "analyzer:run-001" in hist
    # the edit selectors start at the saved values
    assert page.input_value("#ed-category") == "switch" and page.input_value("#ed-subtype") == "standard"


# --- AC-T4 / AC-T5b: a saved correction accepts, totals update at once and persist ----------------------------------------

def test_saved_correction_accepts_and_totals_update_without_reload(page, server):
    p, q = clear_points(server.meta(), 2)
    d0, d1 = server.analyze_a1([("switch", "standard", p), ("switch", "standard", q)])
    open_page(page, server)
    assert official_total(page) == 0
    select_device(page, d0["id"])
    page.select_option("#ed-subtype", "vacancy_sensor")
    # an unsaved edit changes nothing
    page.dispatch_event(marker(d1["id"]), "mousedown")
    page.wait_for_function("tk.state.detail.device.id === %r" % d1["id"])
    assert server.device(d0["id"])["device"]["review_state"] == "proposed" and official_total(page) == 0

    select_device(page, d0["id"])
    page.select_option("#ed-subtype", "vacancy_sensor")
    page.click("#save")
    wait_total(page, 1, timeout=1000)  # +1 within 1 s, without a reload
    dev = server.device(d0["id"])["device"]
    assert (dev["review_state"], dev["accept_kind"], dev["subtype"]) == ("accepted", "corrected_and_accepted",
                                                                         "vacancy_sensor")
    assert page.locator(f"{marker(d0['id'])}.counted").count() == 1
    rows = page.locator("#totals tr.total-row")
    assert rows.count() == 1 and rows.first.get_attribute("data-ids") == d0["id"]
    assert "ELECTRICAL SWITCH-VACANCY SENSOR" in rows.first.inner_text()
    assert page.inner_text("#sheet-total").startswith("Home total (this sheet): 1")

    page.reload()
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("!tk.state.loading")
    assert official_total(page) == 1 == server.counts()["review"]["official_total"]
    assert page.locator(f"{marker(d0['id'])}.counted").count() == 1
    select_device(page, d0["id"])
    assert "corrected_and_accepted" in page.inner_text("#device")
    assert "correct_and_accept" in page.inner_text("#history")


# --- AC-T4: move, create, reject, delete on the plan --------------------------------------------------------------------

def test_move_create_reject_delete_from_the_plan(page, server):
    m = server.meta()
    p, q, target, new = clear_points(m, 4, sep=60.0)
    d0, d1 = server.analyze_a1([("switch", "standard", p), ("light", "pendant", q)])
    open_page(page, server)
    dialogs = Dialogs(page)

    # move: click the new position, then Save (a saved position correction accepts the device)
    select_device(page, d0["id"])
    page.click("#move")
    tgt = to_norm(m, target)
    click_plan(page, m, tgt["u"], tgt["v"])
    page.wait_for_selector(".pending-move")
    assert server.device(d0["id"])["device"]["point_pt"] == pytest.approx(p)  # nothing saved yet
    page.click("#save")
    wait_total(page, 1)
    moved = server.device(d0["id"])["device"]
    assert math.dist(moved["point_pt"], target) <= 0.5, moved["point_pt"]
    assert (moved["review_state"], moved["accept_kind"]) == ("accepted", "corrected_and_accepted")

    # create: C, click, choose the category; the subtype is suggested; Save -> manual and accepted
    page.locator("#viewport").focus()
    page.keyboard.press("c")
    nw = to_norm(m, new)
    click_plan(page, m, nw["u"], nw["v"])
    page.wait_for_selector("#new-form")
    page.select_option("#new-category", "receptacle")
    assert page.input_value("#new-subtype") in [s["name"] for s in
                                                server.call("GET", "/api/config")["taxonomy"]["categories"]["receptacle"]]
    page.select_option("#new-subtype", "gfi")
    page.click("#new-save")
    wait_total(page, 2)
    (manual,) = [d for d in server.devices(A1) if d["origin"] == "manual"]
    assert (manual["category"], manual["subtype"], manual["review_state"]) == ("receptacle", "gfi", "accepted")
    assert dist_pt(m, manual["point"], nw) <= 0.5
    assert page.locator(marker(manual["id"])).count() == 1

    # reject: never counted, shown crossed and faded
    select_device(page, d1["id"])
    page.click("#reject-selected")
    page.wait_for_selector(f"{marker(d1['id'])}.state-rejected")
    assert server.device(d1["id"])["device"]["review_state"] == "rejected"
    assert page.locator(f"{marker(d1['id'])} line.cross").count() == 2
    assert official_total(page) == 2

    # delete with a reason: the marker goes, the total drops
    select_device(page, manual["id"])
    dialogs.answers.append("e2e: placed by mistake")
    page.click("#delete")
    wait_total(page, 1)
    assert page.locator(marker(manual["id"])).count() == 0
    gone = server.call("GET", f"/api/devices/{manual['id']}")["device"]
    assert gone["deleted"] is True and gone["delete_reason"] == "e2e: placed by mistake"
    assert any("reason" in msg.lower() for msg in dialogs.messages)

    # after a reload: the move stays accepted, the reject stays rejected, the deleted device stays gone
    page.reload()
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("!tk.state.loading")
    assert official_total(page) == 1 == server.counts()["review"]["official_total"]
    assert page.locator(f"{marker(d0['id'])}.counted").count() == 1
    assert page.locator(f"{marker(d1['id'])}.state-rejected").count() == 1
    assert page.locator(marker(manual["id"])).count() == 0


# --- AC-T6: bulk accept with confirmation, and undo --------------------------------------------------------------------

def test_bulk_accept_confirm_and_undo(page, server):
    pts = clear_points(server.meta(), 4)
    devs = server.analyze_a1([("switch", "standard", pt) for pt in pts],
                             verify={3: {"category": "light", "subtype": "pendant"}})
    open_page(page, server)
    dialogs = Dialogs(page)
    r = assert_review_lines(page, server)
    assert (r["awaiting_unflagged"], r["awaiting_flagged"]) == (3, 1)
    eligible = server.call("GET", f"/api/documents/{server.doc}/bulk-eligible")
    assert eligible["count"] == 3 and devs[3]["id"] not in eligible["ids"]
    assert page.locator("#undo-bulk").is_disabled()

    dialogs.answers.append(None)  # cancel: nothing happens
    page.click("#bulk-accept")
    page.wait_for_timeout(300)
    assert dialogs.messages == ["Accept 3 unflagged proposals? Official totals will update."]
    assert server.counts()["review"]["official_total"] == 0 and official_total(page) == 0

    dialogs.answers.append(True)
    page.click("#bulk-accept")
    wait_total(page, 3)
    assert dialogs.messages[-1] == "Accept 3 unflagged proposals? Official totals will update."
    assert sorted(d["id"] for d in server.devices(A1) if d["review_state"] == "accepted") == sorted(eligible["ids"])
    r = assert_review_lines(page, server)
    assert r["awaiting_unflagged"] == 0 and page.locator("#bulk-accept").is_disabled()
    assert server.device(devs[3]["id"])["device"]["review_state"] == "proposed"  # flagged: never bulk-accepted

    dialogs.answers.append(True)
    page.click("#undo-bulk")
    wait_total(page, 0)
    assert all(d["review_state"] == "proposed" for d in server.devices(A1))
    assert server.call("GET", f"/api/documents/{server.doc}/bulk-actions")["last_undoable"] is None
    page.wait_for_function("document.getElementById('undo-bulk').disabled")
    assert_review_lines(page, server)
    hist = [e["action"] for e in server.device(devs[0]["id"])["history"] if e["entity"] == "device"]
    assert "bulk_accept" in hist and "bulk_undo" in hist  # history kept


# --- AC-T9 / R1-03: every counted device has a marker -------------------------------------------------------------------

def test_every_counted_device_has_a_marker(page, server):
    pts = clear_points(server.meta(), 5)
    devs = server.analyze_a1([("switch", "standard", pt) for pt in pts[:4]])
    server.call("POST", f"/api/devices/{devs[0]['id']}/accept", {})
    server.call("POST", f"/api/devices/{devs[1]['id']}/accept", {})
    server.call("POST", f"/api/devices/{devs[2]['id']}/reject", {})
    server.create(A1, "ceiling_fan", "optional_pre_wire", pts[4])
    a2 = clear_points(server.meta("A2-111"), 2)
    server.create("A2-111", "light", "pendant", a2[0])
    gone = server.create("A2-111", "light", "pendant", a2[1])
    server.call("POST", f"/api/devices/{gone['id']}/delete", {"reason": "e2e"})

    open_page(page, server)
    k = server.counts()
    seen_total = 0
    for sid in SHEETS:
        choose_sheet(page, sid)
        official = [d for d in server.devices(sid) if d["review_state"] == "accepted"]
        for d in official:
            assert page.locator(marker(d["id"])).count() == 1, (sid, d["id"])
            assert page.locator(f"{marker(d['id'])}.counted").count() == 1
        counted = page.locator(".device.counted")
        want = k["official"].get(sid, {}).get("total", 0)
        assert counted.count() == len(official) == want, sid
        assert int(page.get_attribute("#sheet-total", "data-total")) == want
        assert page.inner_text("#sheet-total").startswith(f"Home total (this sheet): {want}")
        # every totals row is traceable: its count equals the markers it names
        ids_in_rows = []
        for row in page.locator("#totals tr.total-row").all():
            ids = [i for i in (row.get_attribute("data-ids") or "").split(",") if i]
            assert len(ids) == int(row.get_attribute("data-count")), row.inner_text()
            for i in ids:
                assert page.locator(f"{marker(i)}.counted").count() == 1
            ids_in_rows += ids
        assert sorted(ids_in_rows) == sorted(d["id"] for d in official)
        # selecting a totals row highlights its devices
        rows = page.locator("#totals tr.total-row")
        if rows.count():
            rows.first.click()
            ids = [i for i in rows.first.get_attribute("data-ids").split(",") if i]
            assert sorted(page.eval_on_selector_all(".device.highlight", "els => els.map(e => e.dataset.id)")) == \
                sorted(ids)
        seen_total += counted.count()
        assert page.locator(".device[data-id]").count() == len(server.devices(sid))
    assert seen_total == k["review"]["official_total"] == official_total(page) == 4
    assert page.inner_text("#official-total").startswith("All sheets total (4 homes): 4")
    assert page.locator(marker(gone["id"])).count() == 0


# --- AC-T8: rooms and leader lines ------------------------------------------------------------------------------------

def test_room_leader_lines_drawn_for_every_device(page, server):
    m = server.meta()
    pts = clear_points(m, 4, sep=60.0)
    server.analyze_a1([("switch", "standard", pt) for pt in pts[:3]])
    server.create(A1, "light", "pendant", pts[3])
    open_page(page, server)
    devices = server.devices(A1)
    assert len(devices) == 4 and all(d["room"]["kind"] == "labeled" for d in devices)
    fit = fit_scale(page, m)
    for s, pan in ((fit, (0, 0)), (fit * 2.5, (40.0, -25.0))):
        d0 = devices[0]
        set_view_centered(page, m, d0["point"]["u"], d0["point"]["v"], s)
        view = page.evaluate("([dx, dy]) => { const v = tk.view; tk.setView(v.s, v.tx + dx, v.ty + dy); "
                             "return tk.view; }", list(pan))
        assert page.locator("line.leader").count() == len(devices)
        for d in devices:
            line = page.locator(f'line.leader[data-id="{d["id"]}"]')
            assert line.count() == 1
            x1, y1, x2, y2 = (float(line.get_attribute(a)) for a in ("x1", "y1", "x2", "y2"))
            want_dev = expected_screen(m, view, d["point"]["u"], d["point"]["v"])
            room = next(r for r in m["rooms"] if r["kind"] == "labeled" and r["name"] == d["room"]["name"]
                        and r["label_bbox_pt"] == d["room"]["label_bbox_pt"])
            b = room["bbox"]
            want_lbl = expected_screen(m, view, (b["u0"] + b["u1"]) / 2, (b["v0"] + b["v1"]) / 2)
            assert abs(x1 - want_dev[0]) <= 1.5 and abs(y1 - want_dev[1]) <= 1.5
            assert abs(x2 - want_lbl[0]) <= 1.5 and abs(y2 - want_lbl[1]) <= 1.5
    # the proposed room and its source are shown, and the reviewer can correct it
    det = next(d for d in devices if d["origin"] == "detector")
    select_device(page, det["id"])
    assert "proposed nearest label" in page.inner_text("#device")
    other = next(i for i, r in enumerate(m["rooms"]) if r["kind"] == "labeled" and r["name"] != det["room"]["name"])
    page.select_option("#ed-room", str(other))
    page.click("#save")
    page.wait_for_function("tk.state.detail && tk.state.detail.device.room_source === 'reviewer'")
    assert server.device(det["id"])["device"]["room"]["name"] == m["rooms"][other]["name"]
    assert "reviewer" in page.inner_text("#device")
    # the rooms layer can be hidden
    page.uncheck("#layer-rooms")
    assert page.locator("line.leader").count() == 0


# --- no accuracy claims ---------------------------------------------------------------------------------------------

def test_no_percentage_or_accuracy_text_on_page(page, server):
    pts = clear_points(server.meta(), 3)
    devs = server.analyze_a1([("switch", "standard", pt) for pt in pts],
                             verify={1: {"category": "light", "subtype": "pendant"}})
    open_page(page, server)

    def check():
        text = page.inner_text("body")
        path = page.evaluate("tk.state.exportResult && tk.state.exportResult.path")
        if path:  # the server's export directory (pytest's temp dir is named after this test)
            text = text.replace(path, "")
        assert "%" not in text, text
        low = text.lower()
        assert "accuracy" not in low and "accurate" not in low and "confidence" not in low, text

    check()
    for d in devs:
        select_device(page, d["id"])
        check()
    page.click("#export")
    page.wait_for_function("document.getElementById('export-status').textContent.includes('Export')")
    check()


# --- AC-T16: review figures ----------------------------------------------------------------------------------------------

def test_review_lines_show_identities_and_never_label_proposed(page, server):
    pts = clear_points(server.meta(), 6, sep=60.0)
    devs = server.analyze_a1([("switch", "standard", pt) for pt in pts[:5]],
                             verify={1: {"category": "light", "subtype": "pendant"}},
                             discover=[(pts[5], "light", "recessed_downlight")])
    server.call("POST", f"/api/devices/{devs[0]['id']}/accept", {})
    server.call("POST", f"/api/devices/{devs[2]['id']}/reject", {})
    # a manual device within the duplicate tolerance of an accepted one: both counted, both flagged
    server.create(A1, "switch", "standard", (pts[0][0] + 1.0, pts[0][1]))
    open_page(page, server)
    r = assert_review_lines(page, server)
    assert (r["detected"], r["accepted_detected"], r["rejected"], r["awaiting"], r["detected_ai_only"]) == (6, 1, 1, 4, 1)
    assert (r["awaiting_unflagged"], r["awaiting_flagged"], r["manual"], r["official_total"],
            r["accepted_with_open_flags"]) == (2, 2, 1, 2, 2)
    # identities, as shown
    assert r["detected"] == r["accepted_detected"] + r["rejected"] + r["awaiting"]
    assert r["awaiting"] == r["awaiting_unflagged"] + r["awaiting_flagged"]
    assert r["official_total"] == r["accepted_detected"] + r["manual"]
    assert r["awaiting_unflagged"] == server.call("GET", f"/api/documents/{server.doc}/bulk-eligible")["count"]
    assert official_total(page) == r["official_total"]
    assert page.inner_text("#totals-heading").strip() == "Official takeoff totals (accepted and manual devices only)"
    text = page.inner_text("body")
    assert not re.search(r"proposed\W{0,3}\d", text, re.IGNORECASE), text
    assert not re.search(r"\d\s+proposed\b", text, re.IGNORECASE), text
    assert "Proposed" not in page.inner_text("#review-lines") + page.inner_text("#totals")
    # the figures change with a decision, still from the saved records
    select_device(page, devs[3]["id"])
    page.click("#accept-selected")
    wait_total(page, 3)
    assert_review_lines(page, server)


# --- AC-T19: the AI second opinion and the disagreement-reason flow ----------------------------------------------------

def test_ai_second_opinion_and_disagreement_reason_prompt_flow(page, server):
    pts = clear_points(server.meta(), 4)
    pendant = {"category": "light", "subtype": "pendant", "rationale": "fake: looks like a pendant"}
    devs = server.analyze_a1([("switch", "standard", pt) for pt in pts], verify={i: pendant for i in range(4)})
    open_page(page, server)
    dialogs = Dialogs(page)
    cfg = server.call("GET", "/api/config")["taxonomy"]["categories"]
    label = {(c, s["name"]): s["label"] for c, subs in cfg.items() for s in subs}
    ai_desc = f"light / {label[('light', 'pendant')]}"
    prompt_for = lambda sub: (f"The AI classified this as {ai_desc}. Give a reason for keeping "  # noqa: E731
                              f"switch / {label[('switch', sub)]}.")

    def md_flag(did):
        return next(f for f in server.device(did)["flags"] if f["type"] == "model_disagrees")

    # the line, its rationale, and the open flag (orange ring)
    d0 = devs[0]
    select_device(page, d0["id"])
    assert page.inner_text("#ai-line").strip() == f"AI second opinion: disagrees ({ai_desc})" + FAKE
    assert "fake: looks like a pendant" in page.inner_text("#ai-rationale")
    assert page.locator(f"{marker(d0['id'])} .flag-ring").count() == 1
    assert "model_disagrees" in page.inner_text("#flags")

    # a correction that still conflicts: saved, the prompt asks for a reason; cancelled -> stays proposed
    page.select_option("#ed-subtype", "vacancy_sensor")
    dialogs.answers.append(None)
    n_dialogs = len(dialogs.messages)
    page.click("#save")
    page.wait_for_function("tk.state.detail && tk.state.detail.device.subtype === 'vacancy_sensor'")
    page.wait_for_timeout(200)
    assert dialogs.messages[n_dialogs:] == [prompt_for("vacancy_sensor")]  # asked once, before the save
    dev = server.device(d0["id"])["device"]
    assert (dev["subtype"], dev["review_state"]) == ("vacancy_sensor", "proposed") and md_flag(d0["id"])["open"]
    assert "model_disagrees" in page.inner_text("#blocking")
    assert official_total(page) == 0

    # accept without a reason is not possible: the prompt asks; a reason accepts and closes the flag (audited)
    dialogs.answers.append("e2e: the legend tag says VS")
    page.click("#accept-selected")
    wait_total(page, 1)
    assert dialogs.messages[-1] == prompt_for("vacancy_sensor")
    f = md_flag(d0["id"])
    assert (f["open"], f["resolution"]) == (False, "kept_over_ai: e2e: the legend tag says VS")
    assert server.device(d0["id"])["device"]["review_state"] == "accepted"

    # Save with a conflicting correction, reason given in the prompt -> ONE PATCH {changes, reason}: kept over the AI
    # and accepted as a correction (ruling R23)
    d1 = devs[1]
    select_device(page, d1["id"])
    page.select_option("#ed-subtype", "three_way_dimmer")
    dialogs.answers.append("e2e: dimmer per legend")
    writes = []
    page.on("request", lambda r: writes.append((r.method, r.url, r.post_data)) if r.method in ("POST", "PATCH") else None)
    page.click("#save")
    wait_total(page, 2)
    assert dialogs.messages[-1] == prompt_for("three_way_dimmer")
    f = md_flag(d1["id"])
    assert (f["open"], f["resolution"]) == (False, "kept_over_ai: e2e: dimmer per legend")
    dev = server.device(d1["id"])["device"]
    assert (dev["subtype"], dev["review_state"], dev["accept_kind"]) == ("three_way_dimmer", "accepted",
                                                                         "corrected_and_accepted")
    actions = [e["action"] for e in server.device(d1["id"])["history"] if e["entity"] == "device"]
    assert actions.count("correct_and_accept") == 1 and "accept" not in actions, actions
    assert [(m, u.rsplit("/api/", 1)[1]) for m, u, _ in writes] == [("PATCH", f"devices/{d1['id']}")]
    assert '"reason": "e2e: dimmer per legend"' in writes[0][2].replace('":"', '": "')

    # acknowledge with a reason: the flag closes, the device stays proposed
    d2 = devs[2]
    select_device(page, d2["id"])
    flag_id = md_flag(d2["id"])["id"]
    dialogs.answers.append("e2e: checked, symbol is a switch")
    page.click(f'button.ack[data-flag="{flag_id}"]')
    page.wait_for_function("tk.state.detail && tk.state.detail.flags.every(f => !f.open || f.type !== 'model_disagrees')")
    f = md_flag(d2["id"])
    assert (f["open"], f["resolution"]) == (False, "acknowledged: e2e: checked, symbol is a switch")
    assert server.device(d2["id"])["device"]["review_state"] == "proposed" and official_total(page) == 2

    # a correction to the AI's answer resolves it: no prompt, accepted, and the line now agrees
    d3 = devs[3]
    select_device(page, d3["id"])
    n_dialogs = len(dialogs.messages)
    page.select_option("#ed-category", "light")
    page.select_option("#ed-subtype", "pendant")
    page.click("#save")
    wait_total(page, 3)
    assert len(dialogs.messages) == n_dialogs
    f = md_flag(d3["id"])
    assert (f["open"], f["resolution"]) == (False, "resolved_by_correction")
    dev = server.device(d3["id"])["device"]
    assert (dev["category"], dev["review_state"], dev["accept_kind"]) == ("light", "accepted", "corrected_and_accepted")
    page.wait_for_function("tk.state.detail && tk.state.detail.device.category === 'light'")
    assert page.inner_text("#ai-line").strip() == "AI second opinion: agrees" + FAKE
    assert "resolved by correction" in page.inner_text("#device")


# --- AC-T18: AI-only candidates -----------------------------------------------------------------------------------------

def test_ai_only_candidate_shown_dashed_excluded_from_bulk_and_accepted_individually(page, server):
    pts = clear_points(server.meta(), 3, sep=60.0)
    server.analyze_a1([("switch", "standard", pt) for pt in pts[:2]],
                      discover=[(pts[2], "light", "recessed_downlight")])
    (cand,) = [d for d in server.devices(A1) if d["origin"] == "ai"]
    open_page(page, server)
    dialogs = Dialogs(page)
    mk = page.locator(marker(cand["id"]))
    assert mk.count() == 1 and "origin-ai" in mk.get_attribute("class")
    assert page.get_attribute(f"{marker(cand['id'])} circle.core", "stroke-dasharray")
    assert page.text_content(f"{marker(cand['id'])} text.ai-tag") == "AI"
    assert page.locator(f"{marker(cand['id'])} .flag-ring").count() == 1
    select_device(page, cand["id"])
    assert page.inner_text("#ai-location").strip() == "AI-proposed location: check position before accepting"
    assert "ai_only_candidate" in page.inner_text("#flags")
    assert "fake: scripted point" in page.inner_text("#device")
    # the AI-only layer can be hidden without hiding the detector's proposals
    page.uncheck("#layer-ai")
    assert mk.count() == 0 and page.locator(".device.state-proposed").count() == 2
    page.check("#layer-ai")

    r = assert_review_lines(page, server)
    assert (r["detected_ai_only"], r["awaiting_unflagged"]) == (1, 2)
    dialogs.answers.append(True)
    page.click("#bulk-accept")
    wait_total(page, 2)
    assert server.device(cand["id"])["device"]["review_state"] == "proposed"  # never bulk-accepted

    select_device(page, cand["id"])
    page.click("#accept-selected")
    wait_total(page, 3)
    dev = server.device(cand["id"])["device"]
    assert (dev["review_state"], dev["origin"]) == ("accepted", "ai")
    k = server.counts()
    assert k["official"][A1]["by_origin"]["ai_accepted"] == 1
    assert page.locator(f"{marker(cand['id'])}.counted").count() == 1
    assert "ai_accepted 1" in page.inner_text("#totals")


# --- AC-T15 / AC-T20: AI unavailable and test doubles are stated --------------------------------------------------------

def test_ai_unavailable_and_fake_labels_are_stated(page, tmp_path, cache_dir):
    srv = Server(tmp_path, cache_dir, verifier=AV.FakeVerifier(available=(False, "scripted outage")))
    try:
        (p,) = clear_points(srv.meta(), 1)
        srv.detector.points_by_sheet[A1] = [("switch", "standard", *p)]
        open_page(page, srv)
        assert page.inner_text("#stage-verify").strip() == "AI verification: unavailable: scripted outage" + FAKE
        # Analyze this sheet from the page; the per-sheet progress and run summary follow the run
        page.check("#analyze-sheet-only")
        page.click("#analyze")
        page.wait_for_function("document.getElementById('run-summary').textContent.includes('run-001')")
        page.wait_for_function("!tk.state.polling")
        run = srv.call("GET", f"/api/documents/{srv.doc}/runs")[-1]
        assert list(run["sheets"]) == [A1]
        assert "A1-111: done" in page.inner_text("#analyze-progress")
        summary = page.inner_text("#run-summary")
        assert "fake/1" in summary and "1 detection" in summary and "test double, not a model" in summary
        s = run["sheets"][A1]
        assert (s["ai_verify"]["status"], s["ai_discover"]["status"]) == ("unavailable", "done")
        assert page.inner_text("#stage-verify").strip() == "AI verification: unavailable: scripted outage" + FAKE
        assert page.inner_text("#stage-discover").strip() == \
            f"AI discovery: done ({s['ai_discover']['n_items']})" + FAKE
        (dev,) = srv.devices(A1)
        page.wait_for_selector(marker(dev["id"]))
        select_device(page, dev["id"])
        assert page.inner_text("#ai-line").strip().startswith("AI second opinion: not verified (")
        # the export states whether the takeoff is complete
        page.click("#export")
        page.wait_for_function("document.getElementById('export-status').textContent.includes('Export')")
        assert "incomplete" in page.inner_text("#export-status")
    finally:
        srv.stop()

    # a partial stage reports its errors
    off = Server(tmp_path / "partial", cache_dir, verifier=AV.FakeVerifier({f"run-001:{A1}:fake-0000": {"_error": "timeout"}}))
    try:
        (p,) = clear_points(off.meta(), 1)
        off.analyze_a1([("switch", "standard", p)])
        open_page(page, off)
        assert page.inner_text("#stage-verify").strip() == "AI verification: partial (1 errors)" + FAKE
    finally:
        off.stop()


# --- review fixes: running stage, null subtype, stale detail ---------------------------------------------------------------

def test_stage_line_says_running_while_a_run_is_in_progress(page, server):
    store = server.app.state.store
    pending = TM.AIStageStatus(status="pending", implementation="fake")
    store.insert("runs", TM.Run(id="run-001", document_id=server.doc, detector="fake-detector", detector_version="fake/1",
                                created_by="e2e", sheets={A1: TM.SheetRunStatus(status="running", ai_verify=pending,
                                                                                ai_discover=pending)}), "test")
    open_page(page, server)
    assert page.inner_text("#stage-verify").strip() == "AI verification: running" + FAKE
    assert page.inner_text("#stage-discover").strip() == "AI discovery: running" + FAKE
    assert "A1-111: running" in page.inner_text("#analyze-progress")
    assert page.evaluate("tk.state.polling") is True  # the page follows the run (GET /api/runs/{id} every 1 s)
    assert page.locator("#analyze").is_disabled()
    # before any run on a sheet, the stage is "not run on this sheet yet"
    choose_sheet(page, "A2-111")
    assert page.inner_text("#stage-verify").strip() == "AI verification: not run on this sheet yet" + FAKE


def test_room_only_save_on_a_null_subtype_device_writes_no_subtype(page, server):
    m = server.meta()
    (p,) = clear_points(m, 1)
    (dev,) = server.analyze_a1([("switch", None, p)])
    assert dev["subtype"] is None
    open_page(page, server)
    select_device(page, dev["id"])
    other = next(i for i, r in enumerate(m["rooms"]) if r["kind"] == "labeled" and r["name"] != dev["room"]["name"])
    patches = []
    page.on("request", lambda r: patches.append(r.post_data) if r.method == "PATCH" else None)
    page.select_option("#ed-room", str(other))
    page.click("#save")
    page.wait_for_function("tk.state.detail && tk.state.detail.device.room_source === 'reviewer'")
    saved = server.device(dev["id"])["device"]
    assert saved["subtype"] is None and saved["category"] == "switch"
    assert saved["room"]["name"] == m["rooms"][other]["name"]
    assert len(patches) == 1 and "subtype" not in patches[0] and "category" not in patches[0], patches


def test_refresh_never_applies_a_stale_detail_over_a_newer_selection(page, server):
    p, q = clear_points(server.meta(), 2)
    a, b = server.analyze_a1([("switch", "standard", p), ("light", "pendant", q)])
    open_page(page, server)
    select_device(page, a["id"])
    got = page.evaluate("""async ([a, b]) => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const real = window.fetch;
        window.__slowStarted = false;
        window.fetch = async (url, opts) => {  // the refresh's detail request for A is slow
            if (String(url).endsWith('/api/devices/' + a)) { window.__slowStarted = true; await sleep(700); }
            return real(url, opts);
        };
        const pending = tk.refresh();
        while (!window.__slowStarted) await sleep(5);
        await tk.select(b);  // the reviewer selects B while A's detail is still loading
        await pending;
        window.fetch = real;
        return [tk.state.selectedId, tk.state.detail && tk.state.detail.device.id];
    }""", [a["id"], b["id"]])
    assert got == [b["id"], b["id"]]
    assert page.locator(f"#ed-form[data-id='{b['id']}']").count() == 1


# --- AC-T13 / §4.14 / §4.16: the demo path, end to end on the real PDF ----------------------------------------------------

DEMO_DIR = ROOT / "reports" / "takeoff" / "demo"
PREFLIGHT = ROOT / "reports" / "takeoff" / "ai_preflight.json"
HUB_TOL_PT = 6.0  # the protocol's match tolerance
ALIGN_PX = 1.5
FAN = ("ceiling_fan", "optional_pre_wire")
AI_FLAG_TYPES = ("model_disagrees", "ai_only_candidate")


class Evidence:
    """Screenshots and ``run.json`` for one demo test, in a directory cleared of its previous outputs first (the
    hand-written ``README.md`` is kept)."""

    def __init__(self, page, out: Path, record: dict):
        self.page, self.out, self.record = page, out, record
        out.mkdir(parents=True, exist_ok=True)
        for p in out.iterdir():
            if p.is_file() and (p.suffix == ".png" or p.name == "run.json"):
                p.unlink()
            elif p.is_dir() and p.name == "export":
                shutil.rmtree(p)
        self.record.setdefault("screenshots", [])

    def shot(self, name: str, what: str, panel: str = "#device") -> None:
        """A viewport screenshot, with the right panel scrolled to ``panel`` (``#device`` or ``#totals-heading``)."""
        n = len(self.record["screenshots"]) + 1
        file = f"{n:02d}_{name}.png"
        self.page.evaluate("(sel) => { const p = document.getElementById('panel'), el = document.querySelector(sel); "
                           "p.scrollTop = el ? el.getBoundingClientRect().top - p.getBoundingClientRect().top "
                           "+ p.scrollTop - 8 : 0; }", panel)
        self.page.mouse.move(1, 1)  # no hover highlight left over from the last click
        self.page.screenshot(path=str(self.out / file))
        self.record["screenshots"].append({"file": file, "shows": what})

    def write(self) -> None:
        (self.out / "run.json").write_text(json.dumps(self.record, indent=2) + "\n", encoding="utf-8", newline="\n")


def preflight_summary() -> dict:
    if not PREFLIGHT.exists():
        return {"path": "reports/takeoff/ai_preflight.json", "present": False}
    pre = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    return {"path": "reports/takeoff/ai_preflight.json", "present": True, "created_at": pre.get("created_at"),
            "model": pre.get("model"), "sheet_run": pre.get("sheet_run"), "ok": pre.get("ok"),
            "all_passed": pre.get("all_passed"),
            "checks": [{"name": c["name"], "status": c["status"], "reason": c["reason"]} for c in pre["checks"]]}


def choose_ai(pre: dict) -> tuple[str, str]:
    """Claude only with a fully passed live preflight *and* the explicit live opt-in; else the test doubles."""
    if pre.get("all_passed") and os.environ.get("TAKEOFF_LIVE_AI") == "1":
        return "claude", "ai_preflight.json: every check passed, and TAKEOFF_LIVE_AI=1"
    if not pre.get("present"):
        why = "no reports/takeoff/ai_preflight.json"
    elif not pre.get("all_passed"):
        failed = [f"{c['name']}: {c['status']} ({c['reason']})" for c in pre["checks"] if c["status"] != "pass"]
        why = "ai_preflight.json did not pass every check: " + "; ".join(failed)
    else:
        why = "TAKEOFF_LIVE_AI is not 1 (live, billed requests are opt-in)"
    return "fake", why + ". The AI stages ran with FakeVerifier (agrees with det/0) and FakeDiscoverer (no points): " \
                         "test doubles, not a model; nothing here is evidence of model performance."


def hub_token(meta: dict) -> dict:
    """A1's single ``J`` token (the bedroom ceiling-fan hub), with its centre in points and normalized."""
    hubs = [t for t in meta["tokens"] if t["text"] == "J"]
    assert len(hubs) == 1, [t["bbox_pt"] for t in hubs]
    (t,) = hubs
    b = t["bbox_pt"]
    return {"id": t["id"], "bbox_pt": b, "pt": ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), "norm": t["center"]}


def ceiling_fans_near(devices, pt, tol=HUB_TOL_PT) -> list[dict]:
    return sorted((d for d in devices if d["category"] == "ceiling_fan" and not d["deleted"]
                   and math.dist(d["point_pt"], pt) <= tol), key=lambda d: math.dist(d["point_pt"], pt))


def show_device(page, m, d, factor=3.0):
    set_view_centered(page, m, d["point"]["u"], d["point"]["v"], fit_scale(page, m) * factor)
    select_device(page, d["id"])


def room_ceiling_fans(counts: dict, sheet: str, room: str) -> int:
    return sum(counts["official"].get(sheet, {}).get("by_room", {}).get(room, {}).get("ceiling_fan", {}).values())


def row_room_of(page, did) -> str:
    """The official totals row (room key) that names this device among its markers."""
    for row in page.locator("#totals tr.total-row").all():
        if did in (row.get_attribute("data-ids") or "").split(","):
            return row.get_attribute("data-room")
    raise AssertionError(f"no totals row lists {did}")


def add_fan_at_hub(page, server, m, hub, ev: Evidence | None = None) -> dict:
    """The §4.16 fallback: press C, click the J hub, save as ceiling_fan / optional_pre_wire; then reload. Returns
    the manual device, the room row it is counted in, and that row's ceiling-fan totals before and after."""
    before = server.counts()
    total0 = before["review"]["official_total"]
    set_view_centered(page, m, hub["norm"]["u"], hub["norm"]["v"], fit_scale(page, m) * 3)
    page.locator("#viewport").focus()
    page.keyboard.press("c")
    page.wait_for_function("tk.state.placing === true")
    click_plan(page, m, hub["norm"]["u"], hub["norm"]["v"], s=fit_scale(page, m) * 3)
    page.wait_for_selector("#new-form")
    page.select_option("#new-category", FAN[0])
    page.select_option("#new-subtype", FAN[1])
    if ev:
        ev.shot("fan_manual_form", "fan fallback: C pressed, the J hub clicked, the new-device form set to "
                                   "ceiling_fan / optional pre-wire before Save")
    page.click("#new-save")
    wait_total(page, total0 + 1)
    (dev,) = [d for d in server.devices(A1) if d["origin"] == "manual" and d["category"] == FAN[0]]
    assert (dev["subtype"], dev["review_state"]) == (FAN[1], "accepted")
    assert math.dist(dev["point_pt"], hub["pt"]) <= 0.5, (dev["point_pt"], hub["pt"])
    room = row_room_of(page, dev["id"])
    if ev:
        ev.shot("fan_manual_saved", "fan fallback: the manual ceiling fan saved (manual, accepted, counted); "
                                    "the official totals now include it", panel="#totals-heading")
    page.reload()
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("!tk.state.loading")
    assert official_total(page) == total0 + 1 == server.counts()["review"]["official_total"]
    assert page.locator(f"{marker(dev['id'])}.counted").count() == 1
    show_device(page, m, dev)
    text = page.inner_text("#device")
    assert "ceiling_fan" in text and "manual" in text and "accepted" in text
    after = server.counts()
    assert row_room_of(page, dev["id"]) == room
    fan_before, fan_after = room_ceiling_fans(before, A1, room), room_ceiling_fans(after, A1, room)
    assert fan_after == fan_before + 1, (room, fan_before, fan_after)
    if ev:
        ev.shot("fan_manual_reloaded", f"fan fallback after reload: the manual fan persists and room {room}'s "
                                       f"official ceiling_fan total is {fan_after} (was {fan_before})",
                panel="#totals-heading")
    return {"device_id": dev["id"], "point_pt": dev["point_pt"], "room_row": room,
            "room_ceiling_fan_before": fan_before, "room_ceiling_fan_after": fan_after,
            "official_total_before": total0, "official_total_after": total0 + 1}


def csv_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def totals_from_csv(path: Path) -> dict:
    """The official totals recomputed from ``devices_official.csv`` alone (room by its ``room_key`` column; an
    empty subtype is JSON's ``null`` key, as in ``totals.json``)."""
    out: dict = {}
    for r in csv_rows(path):
        s = out.setdefault(r["sheet_id"], {"by_room": {}, "by_category": {}, "total": 0,
                                           "by_origin": {"detector_accepted": 0, "ai_accepted": 0, "manual": 0}})
        sub = r["subtype"] or "null"
        s["by_room"].setdefault(r["room_key"], {}).setdefault(r["category"], Counter())[sub] += 1
        s["by_category"].setdefault(r["category"], Counter())[sub] += 1
        s["total"] += 1
        s["by_origin"][r["origin_reported"]] += 1
    return json.loads(json.dumps(out, sort_keys=True))


def usage_from_verdicts(store, run_id: str) -> dict:
    """Each stage's requests, failures, tokens and items, summed here from the saved verdicts."""
    out = {}
    for kind in ("verify", "discover"):
        mine = [v for v in store.list("verdicts") if v.run_id == run_id and v.kind == kind]
        codes = Counter((v.error or {}).get("code") for v in mine if v.error)
        sent = [v for v in mine if not str((v.error or {}).get("code") or "").startswith("skipped_")]
        out[kind] = {"requests": len(sent), "errors_by_code": dict(sorted(codes.items())),
                     "tokens": {"input": sum(v.input_tokens or 0 for v in mine),
                                "output": sum(v.output_tokens or 0 for v in mine),
                                "cache_creation": sum(v.cache_creation_input_tokens or 0 for v in mine),
                                "cache_read": sum(v.cache_read_input_tokens or 0 for v in mine)},
                     "n_items": sum(len(v.detection_ids) for v in mine),
                     "implementations": sorted({v.implementation for v in mine})}
    return out


def assert_aligned(page, m, devices, factors=(1.0, 3.0), pans=((0, 0), (137.5, -61.25))) -> float:
    worst = 0.0
    fit = fit_scale(page, m)
    for d in devices:
        u, v = d["point"]["u"], d["point"]["v"]
        for factor in factors:
            for pan in pans:
                set_view_centered(page, m, u, v, fit * factor)
                view = page.evaluate("([dx, dy]) => { const v = tk.view; tk.setView(v.s, v.tx + dx, v.ty + dy); "
                                     "return tk.view; }", list(pan))
                got = center_of(page, f'{marker(d["id"])} circle.core')
                want = expected_screen(m, view, u, v)
                assert got is not None, d["id"]
                err = max(abs(got[0] - want[0]), abs(got[1] - want[1]))
                assert err <= ALIGN_PX, (d["id"], factor, pan, got, want)
                worst = max(worst, err)
    return worst


def test_demo_path_end_to_end(page, tmp_path, cache_dir):
    pre = preflight_summary()
    impl, why = choose_ai(pre)
    record = {"test": "backend/tests/test_tk_e2e.py::test_demo_path_end_to_end", "pdf_sha256": None,
              "ai_implementation": impl, "ai_choice": why, "ai_preflight": pre,
              "result": "did not finish: an assertion or error stopped the test (see the pytest output)"}
    ev = Evidence(page, DEMO_DIR, record)
    srv = Server(tmp_path, cache_dir, detector=None, background=True, ai="claude" if impl == "claude" else None)
    try:
        record["pdf_sha256"] = srv.doc
        open_page(page, srv)
        dialogs = Dialogs(page)
        m = srv.meta(A1)

        # 1. Analyze all 4 sheets from the page (background run, the page polls) and wait for every sheet
        assert not page.is_checked("#analyze-sheet-only")
        t0 = time.time()
        page.click("#analyze")
        page.wait_for_function("document.getElementById('run-summary').textContent.includes('run-001')")
        page.wait_for_function("!tk.state.polling", timeout=900_000)
        record["analysis_seconds"] = round(time.time() - t0, 1)
        (run,) = srv.call("GET", f"/api/documents/{srv.doc}/runs")
        assert list(run["sheets"]) == SHEETS and all(s["status"] == "done" for s in run["sheets"].values()), run
        progress = page.inner_text("#analyze-progress")
        assert all(f"{sid}: done" in progress for sid in SHEETS), progress
        summary = page.inner_text("#run-summary")
        assert "run-001" in summary and "4 sheets done" in summary and det0.DETECTOR_VERSION in summary
        fake = FAKE if impl == "fake" else ""
        if impl == "fake":
            assert "AI: test double, not a model" in summary
        s1 = run["sheets"][A1]
        for kind, el, name in (("verify", "#stage-verify", "AI verification"),
                               ("discover", "#stage-discover", "AI discovery")):
            st = s1[f"ai_{kind}"]
            if st["status"] == "done":
                want = f"{name}: done ({st['n_items']})"
            elif st["status"] == "partial":
                want = f"{name}: partial ({sum(st['errors_by_code'].values())} errors)"
            else:
                want = f"{name}: {st['status']}: {st['reason']}"
            assert page.inner_text(el).strip() == want + fake, (kind, st)
        record.update(run_id=run["id"], detector=run["detector"], detector_version=run["detector_version"],
                      merge_version=run["merge_version"],
                      sheets={sid: {"status": s["status"], "n_detections": s["n_detections"],
                                    "ai_verify": s["ai_verify"]["status"], "ai_discover": s["ai_discover"]["status"]}
                              for sid, s in run["sheets"].items()},
                      ai_usage_run=run["ai_usage"], review_after_analysis=srv.counts()["review"])
        ev.shot("analyzed", "all 4 sheets analyzed by det/0 in one background run; per-sheet progress, run summary "
                            "and the AI stage lines (test double) in the status bar; A1 proposals on the plan")

        # 2. inspect a switch and a receptacle on A1
        devs = srv.devices(A1)
        open_by_device: dict[str, list[str]] = {}
        for f in srv.call("GET", f"/api/documents/{srv.doc}/devices?sheet_id={A1}")["open_flags"]:
            open_by_device.setdefault(f["device_id"], []).append(f["type"])
        labels = {(c, s["name"]): s["label"] for c, subs in srv.call("GET", "/api/config")["taxonomy"][
            "categories"].items() for s in subs}
        inspected = {}
        for cat in ("switch", "receptacle"):
            d = next(x for x in devs if x["category"] == cat and x["origin"] == "detector")
            show_device(page, m, d)
            text = page.inner_text("#device")
            for want in (cat, labels[(cat, d["subtype"])], d["review_state"], "detector", SCORE_LABEL, run["id"],
                         run["detector_version"], "takeoff-merge/1", "Subtype source", "History"):
                assert want in text, (cat, want, text)
            if d["room"]:
                assert d["room"]["name"] in text
            ai_line = page.inner_text("#ai-line").strip()
            if impl == "fake":
                assert ai_line == "AI second opinion: agrees" + FAKE
            inspected[cat] = {"device_id": d["id"], "subtype": d["subtype"], "room": (d["room"] or {}).get("name"),
                              "point_pt": d["point_pt"], "ai_line": ai_line,
                              "open_flags": open_by_device.get(d["id"], [])}
            ev.shot(f"inspect_{cat}", f"A1 {cat} selected: category, legend subtype, room and source, review "
                                      f"state, origin, '{SCORE_LABEL}', run and versions, AI line, evidence, history")
        record["inspected"] = inspected

        # 3. the ceiling fan: inspected if det/0 found it at the J hub, otherwise added manually (§4.16)
        hub = hub_token(m)
        near = ceiling_fans_near(devs, hub["pt"])
        record["fan_hub"] = {"token_id": hub["id"], "text": "J", "bbox_pt": hub["bbox_pt"], "centre_pt": hub["pt"],
                             "tolerance_pt": HUB_TOL_PT}
        if near:
            fan = near[0]
            record["fan_branch"] = "detected"
            show_device(page, m, fan)
            text = page.inner_text("#device")
            for want in ("ceiling_fan", labels[(fan["category"], fan["subtype"])], "detector", SCORE_LABEL):
                assert want in text, (want, text)
            record["fan"] = {"device_id": fan["id"], "subtype": fan["subtype"], "point_pt": fan["point_pt"],
                             "distance_to_hub_pt": round(math.dist(fan["point_pt"], hub["pt"]), 3),
                             "review_state": fan["review_state"], "room": (fan["room"] or {}).get("name"),
                             "ai_line": page.inner_text("#ai-line").strip()}
            ev.shot("fan_detected", f"the A1 bedroom ceiling fan, detected by det/0 "
                                    f"{record['fan']['distance_to_hub_pt']} pt from the J hub, selected and inspected")
        else:
            record["fan_branch"] = "manual"
            record["fan"] = add_fan_at_hub(page, srv, m, hub, ev)
        ev.write()

        # 4. AI flags: resolve one through the UI if the run produced any (live AI only)
        flags = srv.call("GET", f"/api/documents/{srv.doc}/devices")["open_flags"]
        ai_flags = [f for f in flags if f["type"] in AI_FLAG_TYPES]
        if not ai_flags:  # always so with the test doubles: they agree with det/0 and discover nothing
            record["ai_flags_demo"] = "none produced"
        else:
            f = ai_flags[0]
            d = srv.device(f["device_id"])["device"]
            if d["sheet_id"] != A1:
                choose_sheet(page, d["sheet_id"])
            sm = page.evaluate("tk.state.meta")
            show_device(page, sm, d)
            if f["type"] == "model_disagrees":
                dialogs.answers.append("demo: reviewed on the plan, keeping det/0's classification")
                page.click("#accept-selected")
            else:
                page.click("#reject-selected")
            page.wait_for_function("tk.state.detail && tk.state.detail.flags.every(x => x.id !== %r || !x.open)"
                                   % f["id"])
            closed = next(x for x in srv.device(d["id"])["flags"] if x["id"] == f["id"])
            assert not closed["open"]
            record["ai_flags_demo"] = {"flag": f["type"], "device_id": d["id"], "resolution": closed["resolution"]}
            ev.shot("ai_flag_resolved", f"an AI {f['type']} flag resolved through the panel: {closed['resolution']}")
            if d["sheet_id"] != A1:
                choose_sheet(page, A1)

        # 5. correct one proposal's category: a saved correction accepts it; the totals change at once
        devs = srv.devices(A1)
        target = next(d for d in devs if d["category"] == "light" and d["review_state"] == "proposed"
                      and not open_by_device.get(d["id"]))
        k0 = srv.counts()
        show_device(page, m, target)
        page.select_option("#ed-category", "fan")
        new_sub = page.input_value("#ed-subtype")
        page.click("#save")
        wait_total(page, k0["review"]["official_total"] + 1)
        dev = srv.device(target["id"])["device"]
        assert (dev["category"], dev["review_state"], dev["accept_kind"]) == ("fan", "accepted",
                                                                              "corrected_and_accepted")
        k1 = srv.counts()
        assert sum(k1["official"][A1]["by_category"]["fan"].values()) == \
            sum(k0["official"].get(A1, {}).get("by_category", {}).get("fan", {}).values()) + 1
        assert page.locator(f"{marker(target['id'])}.counted").count() == 1
        record["correction"] = {"device_id": target["id"], "from": [target["category"], target["subtype"]],
                                "to": [dev["category"], dev["subtype"]], "accept_kind": dev["accept_kind"],
                                "official_total_before": k0["review"]["official_total"],
                                "official_total_after": k1["review"]["official_total"],
                                "note": "a demonstration of the correction flow on an unflagged proposal; it is not "
                                        "a claim that det/0 classified this device wrongly"}
        assert new_sub == dev["subtype"]
        ev.shot("corrected", "one proposal's category corrected (light -> fan) and saved: accepted as "
                             "corrected_and_accepted; the official totals updated without a reload",
                panel="#totals-heading")

        # 6. reload: the correction persists
        page.reload()
        page.wait_for_selector("body[data-ready='1']")
        page.wait_for_function("!tk.state.loading")
        assert official_total(page) == k1["review"]["official_total"]
        show_device(page, m, dev)
        assert "corrected_and_accepted" in page.inner_text("#device")
        assert "correct_and_accept" in page.inner_text("#history")
        assert_review_lines(page, srv)
        ev.shot("reloaded", "after a page reload: the corrected device is still accepted (corrected_and_accepted), "
                            "with its history; the totals and review figures equal GET /counts")

        # 7. zoom and pan: the markers stay aligned
        probes = [srv.device(inspected["switch"]["device_id"])["device"],
                  srv.device(inspected["receptacle"]["device_id"])["device"],
                  srv.device(record["fan"]["device_id"])["device"], srv.device(target["id"])["device"]]
        worst = assert_aligned(page, m, probes)
        record["alignment"] = {"devices": [d["id"] for d in probes], "zoom_factors_of_fit": [1.0, 3.0],
                               "pans_px": [[0, 0], [137.5, -61.25]], "max_error_px": round(worst, 3),
                               "tolerance_px": ALIGN_PX}
        set_view_centered(page, m, hub["norm"]["u"], hub["norm"]["v"], fit_scale(page, m) * 4)
        ev.shot("zoomed", "zoomed in (4x fit) on the bedroom fan hub: markers sit on their symbols")
        page.evaluate("(() => { const v = tk.view; tk.setView(v.s, v.tx + 137.5, v.ty - 61.25); })()")
        ev.shot("panned", f"the same view panned; every probed marker stayed within {ALIGN_PX} px "
                          f"(worst {worst:.3f} px)")
        page.locator("#viewport").focus()
        page.keyboard.press("f")  # back to the fitted view

        # 8. bulk accept, after the confirmation (the button covers every sheet's unflagged proposals)
        eligible = srv.call("GET", f"/api/documents/{srv.doc}/bulk-eligible")
        eligible_a1 = srv.call("GET", f"/api/documents/{srv.doc}/bulk-eligible?sheet_id={A1}")
        k2 = srv.counts()
        n = eligible["count"]
        assert n == k2["review"]["awaiting_unflagged"] > 0
        dialogs.answers.append(True)
        page.click("#bulk-accept")
        wait_total(page, k2["review"]["official_total"] + n, timeout=30_000)
        assert dialogs.messages[-1] == f"Accept {n} unflagged proposals? Official totals will update."
        k3 = srv.counts()
        assert k3["review"]["official_total"] == k2["review"]["official_total"] + n
        assert srv.call("GET", f"/api/documents/{srv.doc}/bulk-eligible?sheet_id={A1}")["count"] == 0
        assert_review_lines(page, srv)
        record["bulk_accept"] = {"confirmation": dialogs.messages[-1], "accepted": n,
                                 "of_which_on_A1": eligible_a1["count"], "scope": "every sheet of the document "
                                 "(the page's button); flagged proposals excluded",
                                 "official_total_after": k3["review"]["official_total"],
                                 "review_after": k3["review"]}
        ev.shot("bulk_accepted", f"after confirming 'Accept {n} unflagged proposals?': flagged proposals stay "
                                 f"awaiting review; official totals {k3['review']['official_total']}",
                panel="#review-lines")
        # R1-03 on the real takeoff: every officially counted device has its counted marker on its sheet
        invariant = {}
        for sid in SHEETS:
            choose_sheet(page, sid)
            official = sorted(d["id"] for d in srv.devices(sid) if d["review_state"] == "accepted")
            drawn = sorted(page.eval_on_selector_all(".device.counted", "els => els.map(e => e.dataset.id)"))
            assert drawn == official and len(official) == k3["official"].get(sid, {}).get("total", 0), sid
            assert int(page.get_attribute("#sheet-total", "data-total")) == len(official)
            invariant[sid] = {"official": len(official), "counted_markers": len(drawn)}
        choose_sheet(page, A1)
        record["r1_03_counted_have_markers"] = invariant

        # 9. export: totals.json = GET /counts = recomputed from the CSV; incomplete with reasons; AI usage summed
        page.click("#export")
        page.wait_for_function("document.getElementById('export-status').textContent.includes('Export')")
        res = page.evaluate("tk.state.exportResult")
        out = Path(res["path"])
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest == res["manifest"]
        totals = json.loads((out / "totals.json").read_text(encoding="utf-8"))
        api_counts = srv.counts()
        assert totals == api_counts["official"]
        assert totals_from_csv(out / "devices_official.csv") == json.loads(json.dumps(totals, sort_keys=True))
        assert manifest["review"] == api_counts["review"]
        awaiting_elsewhere = sorted({d["sheet_id"] for d in srv.devices() if d["review_state"] == "proposed"
                                     and d["sheet_id"] != A1})
        assert awaiting_elsewhere, "other sheets should still await review"
        assert manifest["complete"] is False and manifest["incomplete_reasons"]
        awaiting = api_counts["review"]["awaiting"]
        assert f"{awaiting} detected device{'s' if awaiting != 1 else ''} awaiting review" in \
            manifest["incomplete_reasons"]
        assert "Export incomplete" in page.inner_text("#export-status")
        (mrun,) = manifest["runs"]
        summed = usage_from_verdicts(srv.app.state.store, run["id"])
        for kind in ("verify", "discover"):
            got = mrun["ai"][kind]
            for key in ("requests", "errors_by_code", "tokens", "n_items"):
                assert got[key] == summed[kind][key], (kind, key, got[key], summed[kind][key])
            assert got["implementation"] == impl and summed[kind]["implementations"] == [impl]
        assert manifest["ai_test_double"] is (impl == "fake")
        if impl == "fake":
            assert manifest["ai_note"].endswith("(test double, not a model)")
        fan_row = next((r for r in csv_rows(out / "devices_official.csv") if r["id"] == record["fan"]["device_id"]),
                       None)
        shutil.copytree(out, DEMO_DIR / "export")
        record["export"] = {"source_dir_name": out.name, "copied_to": "reports/takeoff/demo/export/",
                            "complete": manifest["complete"], "incomplete_reasons": manifest["incomplete_reasons"],
                            "sheets_still_awaiting_review_other_than_A1": awaiting_elsewhere,
                            "totals_json_equals_api_counts": True, "totals_json_equals_csv_recomputation": True,
                            "manifest_ai_usage_equals_verdict_sums": True, "ai_usage_summed": summed,
                            "official_records": manifest["counts"]["official_records"],
                            "review_records": manifest["counts"]["review_records"],
                            "official_total": api_counts["review"]["official_total"],
                            "fan_row": ({"origin_reported": fan_row["origin_reported"],
                                         "room_key": fan_row["room_key"]} if fan_row else
                                        "not in the official records (the detected fan is still awaiting review)")}
        ev.shot("exported", "Export: the status bar states 'Export incomplete' with the reasons (flagged proposals "
                            "still awaiting review on every sheet), as manifest.json does")
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["result"] = "passed: every step's assertions held"
    finally:
        ev.write()
        srv.stop()


class Det0WithoutA1CeilingFans:
    """The real det/0 with A1's ceiling-fan detections removed: forces the demo's "fan missed" branch (§4.16)."""

    name = "det0-test-stub"
    version = f"{det0.DETECTOR_VERSION}+stub-drops-A1-ceiling-fans"

    def __init__(self):
        self.inner = AN.Det0Detector()
        self.params = {"det0": det0.PARAMS, "test_stub": "drops ceiling_fan detections on A1-111"}
        self.dropped: list[dict] = []

    def __call__(self, pdf_path, sheet):
        out = self.inner(pdf_path, sheet)
        if sheet.sheet_id == A1:
            self.dropped += [d for d in out["detections"] if d["category"] == "ceiling_fan"]
            out = {**out, "detections": [d for d in out["detections"] if d["category"] != "ceiling_fan"]}
        return out


def test_missed_ceiling_fan_is_added_manually_and_counted(page, tmp_path, cache_dir):
    stub = Det0WithoutA1CeilingFans()
    srv = Server(tmp_path, cache_dir, detector=stub)  # FakeVerifier agrees, FakeDiscoverer finds nothing
    record = {"test": "backend/tests/test_tk_e2e.py::test_missed_ceiling_fan_is_added_manually_and_counted",
              "fan_branch": "manual (forced: the detector stub drops A1's ceiling-fan detections)",
              "ai_implementation": "fake", "ai_choice": "deterministic test: FakeVerifier and an empty "
                                                       "FakeDiscoverer (test doubles, not a model)",
              "result": "did not finish: an assertion or error stopped the test (see the pytest output)"}
    ev = Evidence(page, DEMO_DIR / "fan_missed", record)
    try:
        run = srv.call("POST", f"/api/documents/{srv.doc}/analyze", {"sheet_ids": [A1]})
        assert run["sheets"][A1]["status"] == "done", run
        assert len(stub.dropped) >= 1  # det/0 did find the fan; the stub removed it
        record.update(run_id=run["id"], detector_version=run["detector_version"],
                      dropped_detections=[{"category": d["category"], "subtype": d["subtype"],
                                           "x_pt": d["x_pt"], "y_pt": d["y_pt"]} for d in stub.dropped])
        assert not [d for d in srv.devices(A1) if d["category"] == "ceiling_fan"]
        assert sum(srv.counts()["official"].get(A1, {}).get("by_category", {}).get("ceiling_fan", {}).values()) == 0
        m = srv.meta(A1)
        hub = hub_token(m)
        record["fan_hub"] = {"token_id": hub["id"], "bbox_pt": hub["bbox_pt"], "centre_pt": hub["pt"]}
        assert not ceiling_fans_near(srv.devices(A1), hub["pt"])
        open_page(page, srv)
        set_view_centered(page, m, hub["norm"]["u"], hub["norm"]["v"], fit_scale(page, m) * 3)
        ev.shot("fan_missing", "after Analyze (det/0 stub): no ceiling-fan marker at the A1 bedroom J hub")

        fan = add_fan_at_hub(page, srv, m, hub, ev)
        record["fan"] = fan
        k = srv.counts()
        fans = sum(sum(s["by_category"].get("ceiling_fan", {}).values()) for s in k["official"].values())
        assert fans == 1 == sum(k["official"][A1]["by_category"]["ceiling_fan"].values())
        assert k["official"][A1]["by_category"]["ceiling_fan"] == {FAN[1]: 1}
        assert k["review"]["manual"] == 1

        res = srv.call("POST", f"/api/documents/{srv.doc}/export")
        out = Path(res["path"])
        (row,) = [r for r in csv_rows(out / "devices_official.csv") if r["id"] == fan["device_id"]]
        assert (row["origin_reported"], row["category"], row["subtype"], row["review_state"]) == \
            ("manual", *FAN, "accepted")
        assert json.loads((out / "totals.json").read_text(encoding="utf-8")) == k["official"]
        record["export"] = {"origin_reported": row["origin_reported"], "room_key": row["room_key"],
                            "official_ceiling_fans": fans, "complete": res["manifest"]["complete"]}
        record["result"] = "passed: every assertion held"
    finally:
        ev.write()
        srv.stop()


def test_originals_untouched_by_e2e_session():
    expected = "714af7da3154a1152c21656a7ef515daa1eb985998cdaaadcd7ac3f642fe2587"
    assert hashlib.sha256(PDF.read_bytes()).hexdigest() == expected
