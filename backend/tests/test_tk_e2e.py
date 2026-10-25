"""End-to-end tests of the takeoff page, layout A (spec §5.6; AC-T2 to AC-T9, AC-T15, AC-T16, AC-T18 to AC-T20).

Playwright + Chromium against a live uvicorn server on a temporary data directory. The app runs with
``background=False`` (a run finishes inside its request), a scripted fake detector (no candidate generation) and the
AI test doubles (``FakeVerifier`` / ``FakeDiscoverer``); only A1 is analyzed. No test makes a network request.

Expected screen positions are recomputed here in Python from the served image mapping and the page's view transform
(the same independent re-implementation as ``test_gt_e2e.py``), never taken from the page's own helpers. The figures
the page shows are compared with ``GET /counts`` and the saved device records, never with numbers the page computed.
"""

from __future__ import annotations

import hashlib
import math
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import models as TM
from takeoff.app.api import create_app
from takeoff.app.rooms import propose_room

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
    def __init__(self, tmp_path: Path, cache: Path, verifier=None, discoverer=None):
        self.detector = FakeDetector()
        self.verifier = verifier if verifier is not None else AV.FakeVerifier()
        self.discoverer = discoverer if discoverer is not None else AD.FakeDiscoverer()
        self.app = create_app(tmp_path / "data", PDF, default_reviewer="e2e", background=False,
                              ai=(self.verifier, self.discoverer), detector=self.detector, cache_dir=cache)
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


def test_originals_untouched_by_e2e_session():
    expected = "714af7da3154a1152c21656a7ef515daa1eb985998cdaaadcd7ac3f642fe2587"
    assert hashlib.sha256(PDF.read_bytes()).hexdigest() == expected
