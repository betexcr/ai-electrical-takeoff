"""End-to-end reviewer tests (Playwright + Chromium against a live uvicorn server).

AC1 alignment and click-to-coordinate, AC2 manual creation + reload, candidate
accept/reject, cell navigation, AC11 blind scan hides pass-1 data (DOM and
network), AC13 blind progress persists, AC12 reconciliation without duplicates.

Expected screen positions are recomputed here in Python from the served image
mapping and the page's view transform, not taken from the page's own helpers.
Each test uses a fresh database; the render cache is shared.
"""

from __future__ import annotations

import hashlib
import math
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from takeoff.groundtruth import models as M
from takeoff.groundtruth.api import create_app
from takeoff.groundtruth.store import Store

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
pytestmark = [pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present"), pytest.mark.e2e]
SHEETS = ["A1-111", "A2-111", "A3-111", "A4-111"]
FORBIDDEN = ("/annotations", "/candidates", "/warnings", "/history")


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1280, "height": 800}}


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("render_cache")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_candidate(db: Path, sheet_id: str, page: int, point: dict, point_pt):
    store = Store(db)
    run = M.CandidateRun(generator="raster_plain", generator_version="e2e", params={}, input_sha256="0" * 64,
                         run_hash=f"e2e-{sheet_id}-{point['u']:.6f}")
    cand = M.Candidate(run_id=run.id, sheet_id=sheet_id, page_number=page, class_hypothesis="detector_smoke",
                       category_hint="detector", subtype_hint="smoke_detector", score=0.93, point=point, point_pt=point_pt)
    store.import_candidate_run(run, [cand], actor="e2e-seed")
    store.close()


class Server:
    def __init__(self, tmp_path: Path, cache: Path, seed=None):
        self.db = tmp_path / "gt.sqlite"
        if seed:
            seed(self.db)
        self.app = create_app(PDF, self.db, cache, tmp_path / "exports", default_reviewer="e2e")
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.srv = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.srv.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while not self.srv.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        self.http = httpx.Client(base_url=self.url, headers={"X-Reviewer": "e2e"}, timeout=30)

    def call(self, method, path, json=None):
        r = self.http.request(method, path, json=json)
        assert r.status_code < 400, (path, r.status_code, r.text)
        return r.json()

    def stop(self):
        self.http.close()
        self.srv.should_exit = True
        self.thread.join(10)


@pytest.fixture
def server(tmp_path, cache_dir):
    s = Server(tmp_path, cache_dir)
    yield s
    s.stop()


# --- page helpers ------------------------------------------------------------------------------

def open_sheet(page, server, sheet_id="A1-111"):
    page.goto(f"{server.url}/?sheet={sheet_id}")
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("window.gt && gt.state.meta && gt.state.meta.sheet_id === %r" % sheet_id)


def meta(page):
    return page.evaluate("gt.state.meta")


def viewport_rect(page):
    return page.evaluate("(() => { const r = document.getElementById('viewport').getBoundingClientRect(); return {x: r.left, y: r.top, w: r.width, h: r.height}; })()")


def expected_screen(m, view, u, v):
    """Independent re-implementation: normalized -> image px -> viewport-local screen px."""
    img, c = m["image"], m["image"]["covers"]
    x = (u - c["u0"]) / (c["u1"] - c["u0"]) * img["width_px"]
    y = (v - c["v0"]) / (c["v1"] - c["v0"]) * img["height_px"]
    return view["tx"] + x * view["s"], view["ty"] + y * view["s"]


def set_view_centered(page, m, u, v, s):
    vp = viewport_rect(page)
    img, c = m["image"], m["image"]["covers"]
    x = (u - c["u0"]) / (c["u1"] - c["u0"]) * img["width_px"]
    y = (v - c["v0"]) / (c["v1"] - c["v0"]) * img["height_px"]
    page.evaluate("([s, tx, ty]) => gt.setView(s, tx, ty)", [s, vp["w"] / 2 - x * s, vp["h"] / 2 - y * s])
    return page.evaluate("gt.view")


def fit_scale(page, m):
    vp = viewport_rect(page)
    return min(vp["w"] / m["image"]["width_px"], vp["h"] / m["image"]["height_px"]) * 0.96


def marker_center(page, selector):
    return page.evaluate("""(sel) => { const el = document.querySelector(sel); if (!el) return null;
        const r = el.getBoundingClientRect(), v = document.getElementById('viewport').getBoundingClientRect();
        return [r.left + r.width / 2 - v.left, r.top + r.height / 2 - v.top]; }""", selector)


def token(m, text):
    return next(t for t in m["tokens"] if t["text"] == text)


def dist_pt(m, a, b):
    g = m["geometry"]
    return math.hypot((a["u"] - b["u"]) * g["displayed_width_pt"], (a["v"] - b["v"]) * g["displayed_height_pt"])


def drive_to_blind(server, sheet_id="A1-111"):
    server.call("POST", f"/api/sheets/{sheet_id}/region", {"confirmed": True})
    for cl in server.call("GET", f"/api/sheets/{sheet_id}/candidates")["clusters"]:
        if cl["unreviewed"]:
            server.call("POST", f"/api/sheets/{sheet_id}/clusters/{cl['id']}/reject", {"reason": "e2e"})
    for c in server.call("GET", f"/api/sheets/{sheet_id}/meta")["cells"]:
        server.call("POST", f"/api/sheets/{sheet_id}/cells/{c['id']}", {"done": True})
    server.call("POST", f"/api/sheets/{sheet_id}/phase/start-blind-scan")


# --- AC1: alignment -------------------------------------------------------------------------------

@pytest.mark.parametrize("sheet_id", SHEETS)
def test_markers_stay_aligned_across_zoom_and_pan(page, server, sheet_id):
    open_sheet(page, server, sheet_id)
    m = meta(page)
    targets = [t for t in m["tokens"] if t["text"] in ("LIVING", "J")][:2]
    ids = []
    for t in targets:
        a = server.call("POST", f"/api/sheets/{sheet_id}/annotations",
                        {"category": "other_electrical", "subtype": "other", "point": t["center"], "notes": "e2e alignment probe"})
        ids.append((a["id"], t))
    page.evaluate("gt.refresh()")
    page.wait_for_function("gt.state.annotations.length === %d" % len(ids))
    fit = fit_scale(page, m)
    for factor in (0.5, 1, 2, 4):
        for pan in ((0, 0), (137.5, -61.25)):
            for aid, t in ids:
                view = set_view_centered(page, m, t["center"]["u"], t["center"]["v"], fit * factor)
                view = page.evaluate("([dx, dy]) => { const v = gt.view; gt.setView(v.s, v.tx + dx, v.ty + dy); return gt.view; }", list(pan))
                got = marker_center(page, f'.annotation[data-id="{aid}"] circle')
                want = expected_screen(m, view, t["center"]["u"], t["center"]["v"])
                assert got is not None
                assert abs(got[0] - want[0]) <= 1.5 and abs(got[1] - want[1]) <= 1.5, (sheet_id, factor, pan, got, want)


@pytest.mark.parametrize("s", [0.5, 1.0, 2.0, 4.0])
def test_click_stores_the_clicked_drawing_point(page, server, s):
    open_sheet(page, server)
    m = meta(page)
    target = token(m, "LIVING")["center"]
    view = set_view_centered(page, m, target["u"], target["v"], s)
    vp = viewport_rect(page)
    sx, sy = expected_screen(m, view, target["u"], target["v"])
    page.mouse.move(vp["x"] + sx, vp["y"] + sy)
    page.keyboard.down("Shift")
    page.mouse.down()
    page.mouse.up()
    page.keyboard.up("Shift")
    page.wait_for_selector("#new-form")
    page.click("#save")
    page.wait_for_function("gt.state.annotations.length === 1")
    (a,) = server.call("GET", "/api/sheets/A1-111/annotations")
    assert dist_pt(m, a["point"], target) <= 0.5, dist_pt(m, a["point"], target)


# --- AC2: manual creation anywhere, persists across reload ---------------------------------------------

def test_manual_fan_annotation_where_no_candidate_exists_persists(page, server):
    open_sheet(page, server)
    m = meta(page)
    hub = token(m, "J")["center"]  # the A1 bedroom fan: missed by every Milestone 1 generator
    view = set_view_centered(page, m, hub["u"], hub["v"], 1.5)
    vp = viewport_rect(page)
    page.keyboard.press("4")  # ceiling_fan
    page.keyboard.press("c")
    sx, sy = expected_screen(m, view, hub["u"], hub["v"])
    page.mouse.click(vp["x"] + sx, vp["y"] + sy)
    page.wait_for_selector("#new-form")
    assert page.input_value("#new-category") == "ceiling_fan"
    assert page.input_value("#new-subtype") == "optional_pre_wire"
    page.check(".new-tok")  # link the nearby J token
    page.click("#save")
    page.wait_for_function("gt.state.annotations.length === 1")
    page.reload()
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("gt.state.annotations.length === 1")
    a = page.evaluate("gt.state.annotations[0]")
    assert (a["category"], a["status"], a["provenance"]["origin"]) == ("ceiling_fan", "optional_pre_wire", "manual")
    assert [t["text"] for t in a["text_tokens"]] == ["J"]
    assert page.locator(f'.annotation[data-id="{a["id"]}"]').count() == 1


# --- candidate review and cell navigation -----------------------------------------------------------------

def test_accept_and_reject_candidates_with_keys(page, tmp_path, cache_dir):
    def seed(db):
        import pymupdf
        from takeoff.groundtruth import sheets as S
        from takeoff.pdf import coords as C
        p = pymupdf.open(PDF)[0]
        g = C.PageGeometry.from_page(p)
        toks = S.text_tokens(p, g, S.electrical_region(p)["bbox_pt"])
        for t in [x for x in toks if x["text"] in ("SD", "SD/MA")][:2]:
            x0, y0, x1, y1 = t["bbox_pt"]
            u, v = C.unrotated_to_normalized(g, ((x0 + x1) / 2, y0 - 8))
            seed_candidate(db, "A1-111", 1, {"u": u, "v": v}, ((x0 + x1) / 2, y0 - 8))

    srv = Server(tmp_path, cache_dir, seed=seed)
    try:
        open_sheet(page, srv)
        assert page.locator(".candidate").count() == 2
        page.dispatch_event(".candidate >> nth=0", "mousedown")
        page.wait_for_selector("#accept")
        page.locator("#viewport").focus()
        page.keyboard.press("a")
        page.wait_for_function("gt.state.annotations.length === 1")
        page.once("dialog", lambda d: d.accept("e2e: not a device"))
        page.dispatch_event(".candidate >> nth=0", "mousedown")
        page.wait_for_selector("#reject")
        page.locator("#viewport").focus()
        page.keyboard.press("r")
        page.wait_for_function("gt.state.progress.candidates_unreviewed === 0")
        assert page.locator(".candidate").count() == 0
        (a,) = srv.call("GET", "/api/sheets/A1-111/annotations")
        assert a["provenance"]["origin"] == "candidate" and a["category"] == "detector"
        outcomes = sorted(cl["members"][0]["outcome"] for cl in srv.call("GET", "/api/sheets/A1-111/candidates")["clusters"])
        assert outcomes == ["accepted", "rejected"]
    finally:
        srv.stop()


def test_cell_navigation_and_marking(page, server):
    open_sheet(page, server)
    page.locator("#viewport").focus()
    page.keyboard.press("n")
    assert "cell 2/54" in page.inner_text("[data-testid=cell-nav]")
    page.keyboard.press(" ")
    page.wait_for_function("gt.state.progress.cells_pass1_done === 1")
    assert "cell 3/54" in page.inner_text("[data-testid=cell-nav]")  # advances after marking
    cells = server.call("GET", "/api/sheets/A1-111/meta")["cells"]
    assert [c["id"] for c in cells if c["pass1_done"]] == ["A1-111:r00c01"]


# --- AC11 / AC13 / AC12: blind scan and reconciliation ------------------------------------------------------

def test_blind_scan_hides_candidates_and_annotations_everywhere(page, tmp_path, cache_dir):
    def seed(db):
        seed_candidate(db, "A1-111", 1, {"u": 0.45, "v": 0.2}, (1166.4, 345.6))

    srv = Server(tmp_path, cache_dir, seed=seed)
    try:
        open_sheet(page, srv)
        hub = token(meta(page), "J")["center"]
        a = srv.call("POST", "/api/sheets/A1-111/annotations",
                     {"category": "ceiling_fan", "subtype": "optional_pre_wire", "point": hub})
        drive_to_blind(srv)
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        open_sheet(page, srv)
        assert page.inner_text("[data-testid=phase]").startswith("Pass 2a")
        m = meta(page)
        fit = fit_scale(page, m)
        for factor in (1, 3):
            set_view_centered(page, m, hub["u"], hub["v"], fit * factor)
            assert page.locator(".candidate, .annotation").count() == 0
        n_cells = len(m["cells"])
        for i in range(n_cells):
            page.evaluate("(i) => gt.goToCell(i)", i)
            assert page.locator(".candidate, .annotation").count() == 0, i
        assert not [u for u in requests if any(f in u for f in FORBIDDEN)], requests
        assert page.locator("#layers [data-layer=candidates]").is_hidden()
        assert page.locator("#layers [data-layer=annotations]").is_hidden()
        panel = page.inner_text("#panel")
        assert "tallies" not in panel and "candidates unreviewed" not in panel
        assert a["id"] not in page.content()
    finally:
        srv.stop()


def test_blind_observations_persist_then_reconcile_without_duplicates(page, server):
    open_sheet(page, server)
    m = meta(page)
    hub = token(m, "J")["center"]
    fan = server.call("POST", "/api/sheets/A1-111/annotations",
                      {"category": "ceiling_fan", "subtype": "optional_pre_wire", "point": hub})
    living = token(m, "LIVING")["center"]
    extra = server.call("POST", "/api/sheets/A1-111/annotations",
                        {"category": "light", "subtype": "pendant", "point": {"u": living["u"], "v": living["v"] - 0.01}})
    drive_to_blind(server)
    open_sheet(page, server)
    vp = viewport_rect(page)
    # observation 1: the fan again (matches); observation 2: a receptacle pass 1 missed (promote)
    marks = [("4", hub), ("2", {"u": hub["u"] + 0.02, "v": hub["v"]})]
    for key, pt in marks:
        view = set_view_centered(page, m, pt["u"], pt["v"], 1.0)
        page.locator("#viewport").focus()
        page.keyboard.press(key)
        page.keyboard.press("c")
        sx, sy = expected_screen(m, view, pt["u"], pt["v"])
        page.mouse.click(vp["x"] + sx, vp["y"] + sy)
    page.wait_for_function("gt.state.observations.length === 2")
    page.evaluate("gt.goToCell(0)")
    page.locator("#viewport").focus()
    page.keyboard.press(" ")
    page.wait_for_function("gt.state.progress.cells_blind_scan_done === 1")
    page.reload()  # AC13: progress and observations survive a reload
    page.wait_for_selector("body[data-ready='1']")
    page.wait_for_function("gt.state.observations.length === 2 && gt.state.progress.cells_blind_scan_done === 1")
    assert page.locator(".observation").count() == 2 and page.locator(".annotation").count() == 0
    for c in server.call("GET", "/api/sheets/A1-111/blind")["cells"]:
        if not c["blind_scan_done"]:
            server.call("POST", f"/api/sheets/A1-111/cells/{c['id']}", {"done": True})
    page.reload()
    page.wait_for_selector("body[data-ready='1']")
    page.once("dialog", lambda d: d.accept())
    page.click("text=Open reconciliation")
    page.wait_for_function("gt.state.meta.review.phase === 'reconcile'")
    n_before = len(server.call("GET", "/api/sheets/A1-111/annotations"))
    obs = page.evaluate("gt.state.observations")
    fan_obs = next(o for o in obs if o["category"] == "ceiling_fan")
    new_obs = next(o for o in obs if o["category"] == "receptacle")
    page.dispatch_event(f'.observation[data-id="{fan_obs["id"]}"]', "mousedown")
    page.click("#match")
    page.wait_for_function("(gt.state.annotations.find(a => a.category === 'ceiling_fan') || {}).blind_confirmation === 'confirmed'")
    assert len(server.call("GET", "/api/sheets/A1-111/annotations")) == n_before  # match adds no record
    page.dispatch_event(f'.observation[data-id="{new_obs["id"]}"]', "mousedown")
    page.select_option("#promote-subtype", "gfi")
    page.click("#promote")
    page.wait_for_function("gt.state.annotations.length === %d" % (n_before + 1))
    anns = server.call("GET", "/api/sheets/A1-111/annotations")
    promoted = [a for a in anns if a["provenance"]["origin"] == "blind_scan"]
    assert len(promoted) == 1 and promoted[0]["blind_confirmation"] == "added_from_blind_scan"
    unseen = page.inner_text("#lists")
    assert "PENDANT LIGHT HIGH-EFFICACY" in unseen and "Annotations without a blind-scan match (1)" in unseen
    assert extra["id"] in [a["id"] for a in server.call("GET", "/api/sheets/A1-111/reconcile")["unseen"]]
    assert fan["id"] not in [a["id"] for a in server.call("GET", "/api/sheets/A1-111/reconcile")["unseen"]]


# --- reconciliation keyboard shortcuts ------------------------------------------------------------------------

def drive_to_reconcile(server, observations, sheet_id="A1-111"):
    """Blind-scan via the API: place the given (category, point) observations, finish every cell, open reconcile."""
    drive_to_blind(server, sheet_id)
    for category, pt in observations:
        server.call("POST", f"/api/sheets/{sheet_id}/observations", {"category": category, "point": pt})
    for c in server.call("GET", f"/api/sheets/{sheet_id}/blind")["cells"]:
        server.call("POST", f"/api/sheets/{sheet_id}/cells/{c['id']}", {"done": True})
    server.call("POST", f"/api/sheets/{sheet_id}/phase/open-reconcile")


def press_on(page, selector, key):
    """Select a marker, return focus to the plan (as after a click), press a key."""
    page.dispatch_event(selector, "mousedown")
    page.locator("#viewport").focus()
    page.keyboard.press(key)


def selected(page):
    return page.evaluate("gt.state.selected")


def test_reconcile_shortcuts_match_promote_dismiss_keep_verify(page, server):
    open_sheet(page, server)
    m = meta(page)
    hub = token(m, "J")["center"]
    living = token(m, "LIVING")["center"]
    fan = server.call("POST", "/api/sheets/A1-111/annotations",
                      {"category": "ceiling_fan", "subtype": "optional_pre_wire", "point": hub})
    extra = server.call("POST", "/api/sheets/A1-111/annotations",
                        {"category": "light", "subtype": "pendant", "point": {"u": living["u"], "v": living["v"] - 0.01}})
    drive_to_reconcile(server, [("ceiling_fan", hub), ("receptacle", {"u": hub["u"] + 0.02, "v": hub["v"]}),
                                ("switch", {"u": living["u"] + 0.03, "v": living["v"]})])
    open_sheet(page, server)
    assert "M: match" in page.inner_text("#help") and "G: next pending item" in page.inner_text("#help")
    obs = {o["category"]: o for o in page.evaluate("gt.state.observations")}
    n_before = len(server.call("GET", "/api/sheets/A1-111/annotations"))

    # G with nothing selected: the first pending observation, centred
    page.locator("#viewport").focus()
    page.keyboard.press("g")
    page.wait_for_function("gt.state.selected && gt.state.selected.type === 'observation'")
    first = selected(page)["id"]
    assert first in [o["id"] for o in obs.values()]
    vp, view = viewport_rect(page), page.evaluate("gt.view")
    first_pt = next(o for o in obs.values() if o["id"] == first)["point"]
    cx, cy = expected_screen(m, view, first_pt["u"], first_pt["v"])
    assert abs(cx - vp["w"] / 2) <= 1.5 and abs(cy - vp["h"] / 2) <= 1.5

    # M: match to the suggestion (no new record), then the selection advances to another pending observation
    press_on(page, f'.observation[data-id="{obs["ceiling_fan"]["id"]}"]', "m")
    page.wait_for_function("(gt.state.annotations.find(a => a.category === 'ceiling_fan') || {}).blind_confirmation === 'confirmed'")
    assert len(server.call("GET", "/api/sheets/A1-111/annotations")) == n_before
    page.wait_for_function("""(id) => { const s = gt.state.selected; return s && s.type === 'observation' && s.id !== id &&
        gt.state.observations.find(o => o.id === s.id).reconciliation === 'pending'; }""", arg=obs["ceiling_fan"]["id"])

    # O: promote with the subtype chosen in the panel (focus back on the plan after using the dropdown)
    page.dispatch_event(f'.observation[data-id="{obs["receptacle"]["id"]}"]', "mousedown")
    page.select_option("#promote-subtype", "gfi")
    page.locator("#viewport").focus()
    page.keyboard.press("o")
    page.wait_for_function("gt.state.annotations.length === %d" % (n_before + 1))
    promoted = [a for a in server.call("GET", "/api/sheets/A1-111/annotations") if a["provenance"]["origin"] == "blind_scan"]
    assert len(promoted) == 1 and promoted[0]["subtype"] == "gfi" and promoted[0]["category"] == "receptacle"

    # D: dismiss with the prompted reason
    page.once("dialog", lambda d: d.accept("e2e: mis-click"))
    press_on(page, f'.observation[data-id="{obs["switch"]["id"]}"]', "d")
    page.wait_for_function("gt.state.observations.every(o => o.reconciliation !== 'pending')")
    sw = next(o for o in server.call("GET", "/api/sheets/A1-111/reconcile")["observations"] if o["id"] == obs["switch"]["id"])
    assert (sw["reconciliation"], sw["reason"]) == ("dismissed", "e2e: mis-click")

    # with no observation pending, G reaches the annotation the blind scan did not see; K keeps it
    page.evaluate("gt.select(null)")
    page.locator("#viewport").focus()
    page.keyboard.press("g")
    page.wait_for_function("gt.state.selected && gt.state.selected.id === %r" % extra["id"])
    page.once("dialog", lambda d: d.accept("e2e: re-inspected, faint symbol"))
    page.keyboard.press("k")
    page.wait_for_function("(gt.state.annotations.find(a => a.id === %r) || {}).blind_confirmation === 'not_seen_in_blind_scan'" % extra["id"])
    assert server.call("GET", "/api/sheets/A1-111/reconcile")["unseen"] == []

    # V: saves the unsaved room choice, then verifies; repeat over the remaining work list via auto-advance
    room_idx = next(i for i, r in enumerate(m["rooms"]) if r["kind"] == "labeled")
    page.dispatch_event(f'.annotation[data-id="{fan["id"]}"]', "mousedown")
    for _ in range(3):
        page.wait_for_selector("#ed-room")
        page.select_option("#ed-room", str(room_idx))
        page.locator("#viewport").focus()
        before = selected(page)["id"]
        page.keyboard.press("v")
        page.wait_for_function("(gt.state.annotations.find(a => a.id === %r) || {}).review_state === 'verified'" % before)
    anns = server.call("GET", "/api/sheets/A1-111/annotations")
    assert len(anns) == n_before + 1
    assert all(a["review_state"] == "verified" and a["room"]["name"] == m["rooms"][room_idx]["name"] for a in anns)
    page.keyboard.press("g")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Nothing left to reconcile')")


def test_reconcile_shortcuts_are_inert_outside_reconcile_and_while_typing(page, server):
    open_sheet(page, server)
    m = meta(page)
    hub = token(m, "J")["center"]
    fan = server.call("POST", "/api/sheets/A1-111/annotations",
                      {"category": "ceiling_fan", "subtype": "optional_pre_wire", "point": hub})
    page.evaluate("gt.refresh()")
    page.wait_for_function("gt.state.annotations.length === 1")
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method in ("POST", "PATCH") else None)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    for key in "modkvg":  # pass 1: none of these may act, prompt or complain
        press_on(page, f'.annotation[data-id="{fan["id"]}"]', key)
    page.wait_for_timeout(300)
    assert posts == [] and dialogs == [] and page.inner_text("#toast") == ""
    assert server.call("GET", "/api/sheets/A1-111/annotations")[0]["review_state"] == "draft"

    drive_to_reconcile(server, [("ceiling_fan", hub)])
    open_sheet(page, server)
    (o,) = page.evaluate("gt.state.observations")
    page.dispatch_event(f'.observation[data-id="{o["id"]}"]', "mousedown")
    page.focus("#promote-subtype")
    for key in "mod":  # focus is in a field: keys belong to the field
        page.keyboard.press(key)
    page.wait_for_timeout(300)
    assert posts == [] and dialogs == []
    assert server.call("GET", "/api/sheets/A1-111/reconcile")["observations"][0]["reconciliation"] == "pending"


def test_originals_untouched_by_e2e_session():
    expected = "714af7da3154a1152c21656a7ef515daa1eb985998cdaaadcd7ac3f642fe2587"
    assert hashlib.sha256(PDF.read_bytes()).hexdigest() == expected
