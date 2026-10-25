"""The takeoff HTTP API (spec §5.1, §5.6, §5.7, §5.9): FastAPI over the store, service, analyzer, export and AI stages.

Every test uses a temporary data directory, ``background=False`` (a run finishes inside its request), a fake detector
(no candidate generation) and the AI test doubles or ``ai="off"``. No test makes a network request: the ``claude``
case replaces the availability check.
"""

from __future__ import annotations

import hashlib
import math
import shutil
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from takeoff.ai import common as AC
from takeoff.ai import discover as AD
from takeoff.ai import verifier as AV
from takeoff.app import models as TM
from takeoff.app.api import create_app
from takeoff.app.rooms import propose_room

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
A1 = "A1-111"
SHEETS = ["A1-111", "A2-111", "A3-111", "A4-111"]
LOCAL = "http://127.0.0.1:8766"  # the Host guard (final review I5) refuses TestClient's default "testserver"
NO_LEGEND_MESSAGE = ("No recognizable symbol legend on this sheet; automated detection did not run. "
                     "Add devices manually.")

pytestmark = pytest.mark.skipif(not PDF.exists(), reason="supplied PDF missing")


class FakeDetector:
    """A det0-shaped detector over scripted points (no candidate generation)."""

    name = "fake-detector"
    version = "fake/1"
    params = {"fake": True}

    def __init__(self, points_by_sheet: dict | None = None):
        self.points_by_sheet = points_by_sheet or {}
        self.calls: list[str] = []

    def __call__(self, pdf_path, sheet: TM.SheetInfo) -> dict:
        self.calls.append(sheet.sheet_id)
        dets = [{"id": f"{sheet.sheet_id}:fake-{n:04d}", "sheet_id": sheet.sheet_id, "category": c, "subtype": s,
                 "status": "as_drawn", "x_pt": x, "y_pt": y, "score": 0.9, "members": [],
                 "subtype_source": "default", "tokens": []}
                for n, (c, s, x, y) in enumerate(self.points_by_sheet.get(sheet.sheet_id, []))]
        return {"detector_version": self.version, "params": self.params,
                "input_runs": [{"generator": "fake", "run_hash": f"fakehash-{sheet.sheet_id}"}], "detections": dets}


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return tmp_path_factory.mktemp("render-cache")  # renders are keyed by PDF hash: safe to share


@pytest.fixture(scope="module")
def sha():
    return hashlib.sha256(PDF.read_bytes()).hexdigest()


@pytest.fixture
def make(tmp_path, cache):
    """``make(**create_app kwargs) -> TestClient`` (entered; closed at teardown)."""
    clients = []

    def _make(data=None, **kw):
        kw.setdefault("ai", "off")
        kw.setdefault("detector", FakeDetector())
        app = create_app(data or tmp_path / "data", PDF, background=False, cache_dir=cache, **kw)
        c = TestClient(app, base_url=LOCAL)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def blank_pdf(text: str | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    if text:
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def upload(client, data: bytes, name="upload.pdf", **kw):
    return client.post("/api/documents", params={"file_name": name}, content=data,
                       headers={"Content-Type": "application/pdf", "X-Takeoff-Client": "1", **kw.pop("headers", {})},
                       **kw)


def clear_points(meta: dict, n: int, sep: float = 40.0) -> list[tuple[float, float]]:
    """``n`` points inside the region with a clear (non-ambiguous) room proposal, ``sep`` pt apart."""
    x0, y0, x1, y1 = meta["region"]["bbox_pt"]
    out = []
    y = y0 + 30
    while y < y1 - 30 and len(out) < n:
        x = x0 + 30
        while x < x1 - 30 and len(out) < n:
            p = (round(x, 3), round(y, 3))
            if not propose_room(p, meta["rooms"]).ambiguous and all(math.dist(p, q) >= sep for q in out):
                out.append(p)
            x += 7.0
        y += 7.0
    assert len(out) == n
    return out


def to_norm(meta: dict, pt) -> dict:
    g = meta["geometry"]
    x0, y0, x1, y1 = g["crop_native"]
    return {"u": (pt[0] - x0) / (x1 - x0), "v": (pt[1] - y0) / (y1 - y0)}  # unrotated sheets


def meta_of(client, doc, sid=A1) -> dict:
    r = client.get(f"/api/documents/{doc}/sheets/{sid}/meta")
    assert r.status_code == 200, r.text
    return r.json()


def counts(client, doc) -> dict:
    r = client.get(f"/api/documents/{doc}/counts")
    assert r.status_code == 200, r.text
    return r.json()


def files_under(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*") if p.is_file()) if path.exists() else []


# --- documents and sheets -------------------------------------------------------------------------------------

def test_supplied_document_and_four_sheets_listed(make, tmp_path, sha):
    before = PDF.read_bytes()
    c = make()
    (doc,) = c.get("/api/documents").json()
    assert (doc["id"], doc["source"], doc["page_count"], doc["file_name"]) == (sha, "supplied", 4, PDF.name)
    sheets = c.get(f"/api/documents/{sha}/sheets").json()
    assert [s["sheet_id"] for s in sheets] == SHEETS
    assert [s["page_number"] for s in sheets] == [1, 2, 3, 4]
    assert all(s["legend_found"] and s["note"] is None for s in sheets)

    meta = meta_of(c, sha)
    assert meta["sheet_id"] == A1 and meta["legend_found"] is True
    assert {"geometry", "region", "image", "tokens", "rooms"} <= set(meta)
    assert any(r["kind"] == "labeled" for r in meta["rooms"])
    r = c.get(f"/api/documents/{sha}/sheets/{A1}/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img.shape == (meta["image"]["height_px"], meta["image"]["width_px"])

    page = c.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-cache"

    # a restart on the same data directory registers nothing twice and never re-runs intake
    c2 = make()
    assert [d["id"] for d in c2.get("/api/documents").json()] == [sha]
    store = c2.app.state.store
    assert len([s for s in store.list("sheets") if s.document_id == sha]) == 4
    assert len(store.events("document")) == 1
    assert PDF.read_bytes() == before  # the supplied PDF is only ever read


def test_upload_same_pdf_twice_is_one_document(make, tmp_path, sha):
    c = make(ai=(AV.FakeVerifier(), AD.FakeDiscoverer()))
    data = blank_pdf("an uploaded sheet")
    up_sha = hashlib.sha256(data).hexdigest()

    first = upload(c, data, "mine.pdf")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["created"] is True
    assert (body["document"]["id"], body["document"]["source"], body["document"]["page_count"]) == (up_sha, "upload", 1)
    stored = tmp_path / "data" / "documents" / f"{up_sha}.pdf"
    assert stored.read_bytes() == data

    sheet = c.get(f"/api/documents/{up_sha}/sheets").json()[0]["sheet_id"]
    meta = meta_of(c, up_sha, sheet)
    x0, y0, x1, y1 = meta["region"]["bbox_pt"]
    dev = c.post(f"/api/documents/{up_sha}/devices", json={
        "sheet_id": sheet, "category": "switch", "subtype": "standard",
        "point": to_norm(meta, ((x0 + x1) / 2, (y0 + y1) / 2))}).json()
    store = c.app.state.store
    events_before = len(store.events())

    second = upload(c, data, "renamed copy.pdf")
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False and second.json()["document"]["id"] == up_sha
    assert second.json()["document"]["file_name"] == "mine.pdf"  # the first registration stands
    assert len(store.events()) == events_before  # no second intake, nothing written
    assert [d["id"] for d in c.get("/api/documents").json()] == [sha, up_sha]
    assert len([s for s in store.list("sheets") if s.document_id == up_sha]) == 1
    assert store.get("devices", dev["id"]).revision == 1  # the takeoff is untouched
    assert files_under(tmp_path / "data" / "documents") == [stored]

    # the supplied PDF uploaded again is the supplied document
    again = upload(c, PDF.read_bytes(), "escondido.pdf")
    assert again.status_code == 200 and again.json()["document"]["id"] == sha
    assert again.json()["document"]["source"] == "supplied" and again.json()["created"] is False
    assert files_under(tmp_path / "data" / "documents") == [stored]


def _device_on_a1(c, doc) -> dict:
    meta = meta_of(c, doc)
    (pt,) = clear_points(meta, 1)
    r = c.post(f"/api/documents/{doc}/devices", json={"sheet_id": A1, "category": "switch", "subtype": "standard",
                                                       "point": to_norm(meta, pt)})
    assert r.status_code == 201, r.text
    return r.json()


def test_moved_supplied_pdf_is_repointed_by_sha_and_the_takeoff_keeps_working(tmp_path, sha):
    """I3 (the reviewer's moved.py): the project and its PDF move; the next start re-points the document by SHA."""
    old, new = tmp_path / "old" / "plans.pdf", tmp_path / "new" / "plans.pdf"
    old.parent.mkdir()
    new.parent.mkdir()
    shutil.copy(PDF, old)
    data = tmp_path / "data"
    with TestClient(create_app(data, old, ai="off", background=False, detector=FakeDetector()), base_url=LOCAL) as c:
        dev = _device_on_a1(c, sha)
        before = counts(c, sha)
        assert c.get("/api/documents").json()[0]["stored_path"] == str(old.resolve())
    shutil.move(str(old), str(new))
    shutil.rmtree(data / "cache", ignore_errors=True)
    with TestClient(create_app(data, new, ai="off", background=False, detector=FakeDetector()), base_url=LOCAL) as c:
        r = c.get(f"/api/documents/{sha}/counts")
        assert r.status_code == 200, r.text
        assert r.json()["official"] == before["official"] and r.json()["review"] == before["review"]
        assert r.json()["rooms_unavailable"] == {}
        (doc,) = c.get("/api/documents").json()
        assert doc["stored_path"] == str(new.resolve())
        store = c.app.state.store
        (ev,) = [e for e in store.events("document", sha) if e.action == "repoint"]
        assert (ev.before["stored_path"], ev.after["stored_path"]) == (str(old.resolve()), str(new.resolve()))
        assert store.get("devices", dev["id"]).revision == 1  # re-pointing never touches a device
        r = c.patch(f"/api/devices/{dev['id']}", json={"changes": {"notes": "after the move"}})
        assert r.status_code == 200, r.text
        meta = meta_of(c, sha)
        moved = dict(dev["point"], u=dev["point"]["u"] + 1.0 / (meta["geometry"]["crop_native"][2]
                                                               - meta["geometry"]["crop_native"][0]))
        r = c.patch(f"/api/devices/{dev['id']}", json={"changes": {"point": moved}})
        assert r.status_code == 200, r.text


def test_counts_degrade_when_the_pdf_is_missing_and_recover_when_it_is_back(tmp_path, sha):
    """I3c/d: the stored PDF is gone and nothing re-points it: /counts still answers from the saved devices, and the
    next request after the file is restored at its stored path works (the failure is not remembered)."""
    where = tmp_path / "plans.pdf"
    shutil.copy(PDF, where)
    data = tmp_path / "data"
    with TestClient(create_app(data, where, ai="off", background=False, detector=FakeDetector()), base_url=LOCAL) as c:
        dev = _device_on_a1(c, sha)
        before = counts(c, sha)
    hidden = tmp_path / "hidden.pdf"
    shutil.move(str(where), str(hidden))
    with TestClient(create_app(data, None, ai="off", background=False, detector=FakeDetector()), base_url=LOCAL) as c:
        k = counts(c, sha)
        assert k["review"] == before["review"] and k["official"] == before["official"]
        assert set(k["rooms_unavailable"]) == {A1} and "FileNotFoundError" in k["rooms_unavailable"][A1]
        assert c.get(f"/api/documents/{sha}/sheets/{A1}/meta").json()["view_error"]
        shutil.move(str(hidden), str(where))
        assert counts(c, sha)["rooms_unavailable"] == {}
        assert meta_of(c, sha)["view_error"] is None
        r = c.patch(f"/api/devices/{dev['id']}", json={"changes": {"notes": "file is back"}})
        assert r.status_code == 200, r.text


def test_uploads_are_stored_relative_to_the_data_dir(tmp_path, cache):
    """I3b: an upload's stored path is relative to the data directory, so the directory can move."""
    data = blank_pdf("relative")
    up_sha = hashlib.sha256(data).hexdigest()
    with TestClient(create_app(tmp_path / "data", None, ai="off", background=False, cache_dir=cache),
                    base_url=LOCAL) as c:
        body = upload(c, data, "rel.pdf").json()
        assert body["document"]["stored_path"] == f"documents/{up_sha}.pdf"
    shutil.move(str(tmp_path / "data"), str(tmp_path / "moved-data"))
    with TestClient(create_app(tmp_path / "moved-data", None, ai="off", background=False, cache_dir=cache),
                    base_url=LOCAL) as c:
        sheet = c.get(f"/api/documents/{up_sha}/sheets").json()[0]["sheet_id"]
        assert meta_of(c, up_sha, sheet)["view_error"] is None


def test_upload_restores_a_missing_stored_file(make, tmp_path):
    """I3a for uploads: the same bytes uploaded again re-store the file when the stored copy is gone."""
    c = make()
    data = blank_pdf("restore me")
    up_sha = hashlib.sha256(data).hexdigest()
    upload(c, data, "r.pdf")
    stored = tmp_path / "data" / "documents" / f"{up_sha}.pdf"
    stored.unlink()
    again = upload(c, data, "r.pdf")
    assert again.status_code == 200 and again.json()["created"] is False
    assert stored.read_bytes() == data


def test_upload_non_pdf_is_400_and_writes_nothing(make, tmp_path):
    c = make()
    store = c.app.state.store
    docs_dir = tmp_path / "data" / "documents"
    events, files = len(store.events()), files_under(docs_dir)
    for data in (b"this is not a PDF at all", b"", b"%PDF-1.7\n garbage with no objects"):
        r = upload(c, data, "fake.pdf")
        assert r.status_code == 400, (data, r.text)
        assert r.json()["code"] == "invalid_pdf" and r.json()["message"]
    assert len(store.events()) == events and files_under(docs_dir) == files
    assert len(c.get("/api/documents").json()) == 1
    r = c.post("/api/documents", content=blank_pdf(),
               headers={"Content-Type": "application/pdf", "X-Takeoff-Client": "1"})  # no file_name
    assert r.status_code in (400, 422) and r.json()["code"] == "invalid"
    assert len(store.events()) == events and files_under(docs_dir) == files


def test_no_legend_upload_analyze_reports_skip_and_allows_manual_device(make):
    detector = FakeDetector()
    c = make(ai=(AV.FakeVerifier(), AD.FakeDiscoverer()), detector=detector)
    doc = upload(c, blank_pdf(), "blank.pdf").json()["document"]["id"]
    (sheet,) = c.get(f"/api/documents/{doc}/sheets").json()
    assert sheet["sheet_id"] == "page-1" and sheet["legend_found"] is False

    r = c.post(f"/api/documents/{doc}/analyze", json={})
    assert r.status_code == 200, r.text
    run = r.json()
    s = run["sheets"]["page-1"]
    assert s["status"] == "skipped_no_legend" and s["message"] == NO_LEGEND_MESSAGE
    assert (s["ai_verify"]["status"], s["ai_verify"]["reason"]) == ("unavailable", "no legend")
    assert (s["ai_discover"]["status"], s["ai_discover"]["reason"]) == ("unavailable", "no legend")
    assert detector.calls == []
    assert c.get(f"/api/runs/{run['id']}").json()["sheets"]["page-1"]["status"] == "skipped_no_legend"

    meta = meta_of(c, doc, "page-1")
    x0, y0, x1, y1 = meta["region"]["bbox_pt"]
    r = c.post(f"/api/documents/{doc}/devices", json={
        "sheet_id": "page-1", "category": "switch", "subtype": "standard",
        "point": to_norm(meta, ((x0 + x1) / 2, (y0 + y1) / 2))})
    assert r.status_code == 201, r.text
    assert (r.json()["review_state"], r.json()["origin"]) == ("accepted", "manual")
    assert counts(c, doc)["review"]["official_total"] == 1


def test_rotated_page_meta_served_without_drawing(make):
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).set_rotation(90)
    data = doc.tobytes()
    doc.close()
    detector = FakeDetector()
    c = make(detector=detector)
    doc_id = upload(c, data, "rotated.pdf").json()["document"]["id"]
    (sheet,) = c.get(f"/api/documents/{doc_id}/sheets").json()
    assert sheet["note"] and "rotated" in sheet["note"]
    meta = meta_of(c, doc_id, sheet["sheet_id"])
    assert meta["note"] == sheet["note"] and meta["view_error"] and meta["image"] is None and meta["rooms"] == []
    r = c.get(f"/api/documents/{doc_id}/sheets/{sheet['sheet_id']}/image")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    run = c.post(f"/api/documents/{doc_id}/analyze", json={}).json()
    assert run["sheets"][sheet["sheet_id"]]["status"] == "skipped_no_legend" and detector.calls == []


# --- devices ------------------------------------------------------------------------------------------------------

def test_device_crud_round_trip_and_counts_update_in_same_response_cycle(make, sha):
    c = make()
    meta = meta_of(c, sha)
    p, q = clear_points(meta, 2)
    assert counts(c, sha)["review"]["official_total"] == 0

    r = c.post(f"/api/documents/{sha}/devices", json={"sheet_id": A1, "category": "switch", "subtype": "standard",
                                                     "point": to_norm(meta, p)})
    assert r.status_code == 201, r.text
    dev = r.json()
    assert (dev["review_state"], dev["origin"], dev["sheet_id"]) == ("accepted", "manual", A1)
    k = counts(c, sha)
    assert k["review"]["official_total"] == 1 and k["review"]["manual"] == 1
    assert k["official"][A1]["by_category"] == {"switch": {"standard": 1}}

    detail = c.get(f"/api/devices/{dev['id']}").json()
    assert detail["device"]["id"] == dev["id"] and detail["history"][0]["action"] == "create_manual"
    assert detail["ai_summary"]["status"] == "not_verified"
    listed = c.get(f"/api/documents/{sha}/devices", params={"sheet_id": A1}).json()
    assert [d["id"] for d in listed["devices"]] == [dev["id"]]

    r = c.patch(f"/api/devices/{dev['id']}", json={"changes": {"subtype": "dimmer", "point": to_norm(meta, q)}})
    assert r.status_code == 200, r.text
    assert r.json()["device"]["subtype"] == "dimmer" and r.json()["blocking_flags"] == []
    assert r.json()["device"]["point_pt"] == pytest.approx(q, abs=1e-3)
    assert counts(c, sha)["official"][A1]["by_category"] == {"switch": {"dimmer": 1}}

    r = c.post(f"/api/devices/{dev['id']}/delete", json={"reason": "duplicate of another entry"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    k = counts(c, sha)
    assert k["review"]["official_total"] == 0 and k["official"] == {}
    assert c.get(f"/api/documents/{sha}/devices", params={"sheet_id": A1}).json()["devices"] == []
    with_deleted = c.get(f"/api/documents/{sha}/devices", params={"sheet_id": A1, "include_deleted": True}).json()
    assert [d["id"] for d in with_deleted["devices"]] == [dev["id"]]
    assert c.get(f"/api/devices/{dev['id']}").json()["device"]["delete_reason"] == "duplicate of another entry"


def test_disagreement_reason_flow_over_http(make, sha, tmp_path):
    probe = make(data=tmp_path / "probe")
    meta = meta_of(probe, sha)
    (p,) = clear_points(meta, 1)
    det = f"run-001:{A1}:fake-0000"
    c = make(data=None, ai=(AV.FakeVerifier({det: {"category": "light", "subtype": "pendant"}}), AD.FakeDiscoverer()),
             detector=FakeDetector({A1: [("switch", "standard", *p)]}))
    run = c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1]}).json()
    assert run["sheets"][A1]["status"] == "done" and run["sheets"][A1]["ai_verify"]["status"] == "done"
    listed = c.get(f"/api/documents/{sha}/devices", params={"sheet_id": A1}).json()
    (dev,) = listed["devices"]
    assert dev["review_state"] == "proposed"
    (flag,) = [f for f in listed["open_flags"] if f["device_id"] == dev["id"]]
    assert flag["type"] == "model_disagrees"

    # a correction that still conflicts with the model: saved, still proposed, the flag named
    r = c.patch(f"/api/devices/{dev['id']}", json={"changes": {"subtype": "dimmer"}})
    assert r.status_code == 200, r.text
    assert r.json()["device"]["subtype"] == "dimmer" and r.json()["device"]["review_state"] == "proposed"
    assert [f["type"] for f in r.json()["blocking_flags"]] == ["model_disagrees"]

    r = c.post(f"/api/devices/{dev['id']}/accept", json={})
    assert r.status_code == 409 and r.json()["code"] == "ai_disagreement_reason_required"
    assert [f["type"] for f in r.json()["blocking_flags"]] == ["model_disagrees"]
    r = c.post(f"/api/devices/{dev['id']}/accept", content=b"",
               headers={"Content-Type": "application/json"})  # an empty JSON body: the same refusal
    assert r.status_code == 409 and r.json()["code"] == "ai_disagreement_reason_required"
    assert counts(c, sha)["review"]["official_total"] == 0

    r = c.post(f"/api/devices/{dev['id']}/accept", json={"reason": "the legend tag says dimmer"})
    assert r.status_code == 200, r.text
    assert r.json()["review_state"] == "accepted"
    flags = c.get(f"/api/devices/{dev['id']}").json()["flags"]
    (closed,) = [f for f in flags if f["type"] == "model_disagrees"]
    assert closed["open"] is False and closed["resolution"] == "kept_over_ai: the legend tag says dimmer"
    assert counts(c, sha)["review"]["official_total"] == 1


def test_error_codes_and_statuses(make, sha, tmp_path):
    probe = make(data=tmp_path / "probe")
    meta = meta_of(probe, sha)
    p, q = clear_points(meta, 2)
    c = make(data=None, detector=FakeDetector({A1: [("switch", "standard", *p), ("light", "pendant", *q)]}))

    def err(r, status, code):
        assert r.status_code == status, r.text
        body = r.json()
        assert body["code"] == code and isinstance(body["message"], str) and body["message"], body
        return body

    err(c.get("/api/documents/" + "0" * 64 + "/sheets"), 404, "not_found")
    err(c.get(f"/api/documents/{sha}/sheets/Z9-999/meta"), 404, "not_found")
    err(c.get(f"/api/documents/{sha}/sheets/Z9-999/image"), 404, "not_found")
    err(c.get("/api/devices/nope"), 404, "not_found")
    err(c.get("/api/runs/run-999"), 404, "not_found")
    err(c.post("/api/flags/nope/acknowledge", json={"reason": "x"}), 404, "not_found")
    err(c.post("/api/bulk/nope/undo", json={}), 404, "not_found")
    err(c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": ["Z9-999"]}), 404, "not_found")
    err(c.post(f"/api/documents/{sha}/devices", json={"sheet_id": A1, "category": "switch", "subtype": "standard",
                                                     "point": {"u": 0.01, "v": 0.99}}), 400, "outside_region")
    err(c.post(f"/api/documents/{sha}/devices", json={"sheet_id": A1, "category": "toaster", "subtype": "x",
                                                     "point": to_norm(meta, p)}), 400, "invalid")
    err(c.post(f"/api/documents/{sha}/devices", json={"sheet_id": A1, "category": "switch"}), 422, "invalid")
    err(c.post(f"/api/documents/{sha}/bulk-accept", json={"expected_count": "two"}), 422, "invalid")

    manual = c.post(f"/api/documents/{sha}/devices", json={"sheet_id": A1, "category": "switch",
                                                          "subtype": "standard", "point": to_norm(meta, p)}).json()
    err(c.post(f"/api/devices/{manual['id']}/reject", json={}), 400, "manual_cannot_be_rejected")
    err(c.post(f"/api/devices/{manual['id']}/delete", json={}), 400, "reason_required")
    err(c.post(f"/api/devices/{manual['id']}/delete", json={"reason": "  "}), 400, "reason_required")
    err(c.patch(f"/api/devices/{manual['id']}", json={"changes": {"review_state": "accepted"}}), 400, "invalid")
    c.post(f"/api/devices/{manual['id']}/delete", json={"reason": "test entry"})

    run = c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1]}).json()
    assert run["sheets"][A1]["status"] == "done"
    eligible = c.get(f"/api/documents/{sha}/bulk-eligible", params={"sheet_id": A1}).json()
    assert eligible["count"] == 2 and len(eligible["ids"]) == 2
    body = err(c.post(f"/api/documents/{sha}/bulk-accept", json={"expected_count": 3, "sheet_id": A1}), 409,
               "bulk_count_changed")
    assert (body["expected"], body["current"]) == (3, 2)
    bulk = c.post(f"/api/documents/{sha}/bulk-accept", json={"expected_count": 2})
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["sheet_id"] == "*" and sorted(bulk.json()["device_ids"]) == sorted(eligible["ids"])
    assert counts(c, sha)["review"]["official_total"] == 2
    last = c.get(f"/api/documents/{sha}/bulk-actions").json()
    assert last["last_undoable"]["id"] == bulk.json()["id"]
    undo = c.post(f"/api/bulk/{bulk.json()['id']}/undo", json={})
    assert undo.status_code == 200 and undo.json()["kind"] == "undo"
    assert c.get(f"/api/documents/{sha}/bulk-actions").json()["last_undoable"] is None
    assert counts(c, sha)["review"]["official_total"] == 0
    err(c.post(f"/api/bulk/{bulk.json()['id']}/undo", json={}), 400, "invalid")
    err(c.post(f"/api/documents/{sha}/bulk-accept", json={"expected_count": 2, "sheet_id": "Z9-999"}), 404,
        "not_found")

    dev = eligible["ids"][0]
    body = err(c.post(f"/api/devices/{dev}/delete", json={"reason": "not a device"}), 400,
               "detection_cannot_be_deleted")  # final review I4: reject it instead
    assert body["message"] == ("Use Reject for a detection that is not a real device. Reject keeps it from being "
                               "proposed again; Delete is for manual mistakes and duplicates.")
    assert c.get(f"/api/devices/{dev}").json()["device"]["deleted"] is False
    flag = c.app.state.store.insert("flags", TM.Flag(device_id=dev, document_id=sha, sheet_id=A1,
                                                     type="classification_conflict", severity="review",
                                                     details={}, run_id=run["id"]), "test")
    err(c.post(f"/api/flags/{flag.id}/acknowledge", json={}), 400, "reason_required")
    err(c.post(f"/api/flags/{flag.id}/resolve", json={"choice": "whatever"}), 400, "invalid")
    assert c.post(f"/api/flags/{flag.id}/resolve", json={"choice": "keep"}).status_code == 200

    # one takeoff per document: a run still in progress refuses a new one
    c.app.state.store.insert("runs", TM.Run(id="run-050", document_id=sha, detector="det0",
                                            detector_version="det/0", created_by="someone",
                                            sheets={A1: TM.SheetRunStatus(status="running")}), "test")
    body = err(c.post(f"/api/documents/{sha}/analyze", json={}), 409, "run_in_progress")
    assert body["run_id"] == "run-050"


def test_actor_header_recorded_in_history(make, sha):
    c = make(default_reviewer="dana")
    meta = meta_of(c, sha)
    (p,) = clear_points(meta, 1)
    dev = c.post(f"/api/documents/{sha}/devices", headers={"X-Reviewer": "alex"},
                 json={"sheet_id": A1, "category": "switch", "subtype": "standard", "point": to_norm(meta, p)}).json()
    c.patch(f"/api/devices/{dev['id']}", json={"changes": {"notes": "checked on site"}})
    c.patch(f"/api/devices/{dev['id']}", headers={"X-Reviewer": "  "}, json={"changes": {"notes": "again"}})
    history = [e for e in c.get(f"/api/devices/{dev['id']}").json()["history"] if e["entity"] == "device"]
    assert [(e["action"], e["actor"]) for e in history] == [("create_manual", "alex"), ("update", "dana"),
                                                           ("update", "dana")]
    run = c.post(f"/api/documents/{sha}/analyze", headers={"X-Reviewer": "sam"}, json={"sheet_ids": [A1]}).json()
    assert run["created_by"] == "sam"
    assert c.get("/api/config").json()["default_reviewer"] == "dana"


# --- config and analysis --------------------------------------------------------------------------------------------

def test_config_reports_ai_availability_and_implementation(make, tmp_path, monkeypatch):
    c = make(ai=(AV.FakeVerifier(), AD.FakeDiscoverer(available=(False, "scripted outage"))))
    cfg = c.get("/api/config").json()
    assert cfg["score_label"] == "match score (not a probability)"
    assert "switch" in cfg["taxonomy"]["categories"]
    assert cfg["tolerances_pt"]["match"] == 6.0 and cfg["tolerances_pt"]["duplicate"] == 3.0
    assert cfg["versions"]["schema"] == "takeoff-schema/1" and cfg["versions"]["merge"] == "takeoff-merge/2"
    assert cfg["versions"]["detector"] == "fake/1"
    ai = cfg["ai"]
    assert ai["model"] == "fake-verifier"
    assert ai["verify"] == {"version": "ai-verify/1", "prompt_version": "ai-verify-prompt/1", "implementation": "fake",
                            "available": True, "reason": None}
    assert ai["discover"] == {"version": "ai-discover/1", "prompt_version": "ai-discover-prompt/1",
                              "implementation": "fake", "available": False, "reason": "scripted outage"}

    off = make(data=tmp_path / "off").get("/api/config").json()["ai"]
    assert off["model"] is None and off["mode"] == "off"
    for stage in ("verify", "discover"):
        assert (off[stage]["implementation"], off[stage]["available"], off[stage]["reason"]) == (None, False, "off")

    # ai="claude": nothing touches the network at startup; availability is checked lazily and failures reported
    calls = []

    def check(self):
        calls.append(self.model)
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(AC.AIClient, "_check", check)
    live = make(data=tmp_path / "claude", ai="claude")
    assert calls == []
    ai = live.get("/api/config").json()["ai"]
    assert calls  # checked on demand, once per client
    assert ai["mode"] == "claude" and ai["model"] == AC.DEFAULT_MODEL
    for stage in ("verify", "discover"):
        assert ai[stage]["implementation"] == "claude" and ai[stage]["available"] is False
        assert "no network in tests" in ai[stage]["reason"]


def test_analyze_with_ai_off(make, sha, tmp_path):
    probe = make(data=tmp_path / "probe")
    meta = meta_of(probe, sha)
    pts = clear_points(meta, 2)
    detector = FakeDetector({A1: [("switch", "standard", *pt) for pt in pts]})
    c = make(data=tmp_path / "off", ai="off", detector=detector)
    r = c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1]})
    assert r.status_code == 200, r.text
    run = r.json()
    s = run["sheets"][A1]
    assert s["status"] == "done" and s["n_detections"] == 2
    assert (s["ai_verify"]["status"], s["ai_discover"]["status"]) == ("off", "off")
    assert run["ai_model"] is None and run["finished_at"] is not None
    assert c.get(f"/api/runs/{run['id']}").json()["sheets"][A1]["status"] == "done"
    assert [x["id"] for x in c.get(f"/api/documents/{sha}/runs").json()] == [run["id"]]
    assert c.app.state.store.list("verdicts") == []
    k = counts(c, sha)["review"]
    assert (k["detected"], k["awaiting"], k["official_total"]) == (2, 2, 0)

    # with the AI configured, a request can still turn it off
    fakes = make(data=tmp_path / "fakes", ai=(AV.FakeVerifier(), AD.FakeDiscoverer()), detector=detector)
    run = fakes.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1], "ai": False}).json()
    assert (run["sheets"][A1]["ai_verify"]["status"], run["sheets"][A1]["ai_discover"]["status"]) == ("off", "off")

    # the export of an unfinished takeoff says so
    r = c.post(f"/api/documents/{sha}/export", json={})
    assert r.status_code == 200, r.text
    out = Path(r.json()["path"])
    assert out.parent == tmp_path / "off" / "exports" and (out / "manifest.json").exists()
    assert r.json()["manifest"]["complete"] is False and r.json()["manifest"]["incomplete_reasons"]


def test_counts_by_sheet_match_the_per_sheet_bulk_scope(make, sha, tmp_path):
    probe = make(data=tmp_path / "probe")
    a1, a2 = clear_points(meta_of(probe, sha, A1), 3), clear_points(meta_of(probe, sha, "A2-111"), 2)
    dup = (a2[0][0] + 1.0, a2[0][1])  # within the duplicate tolerance: both A2 switches there are flagged
    detector = FakeDetector({A1: [("switch", "standard", *pt) for pt in a1],
                             "A2-111": [("switch", "standard", *pt) for pt in (*a2, dup)]})
    c = make(data=tmp_path / "bysheet", detector=detector)
    r = c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1, "A2-111"]})
    assert r.status_code == 200, r.text

    def state():
        k = counts(c, sha)
        n = {sid: c.get(f"/api/documents/{sha}/bulk-eligible", params={"sheet_id": sid}).json()["count"]
             for sid in (A1, "A2-111")}
        return k, n

    k, n = state()
    per = k["review_by_sheet"]
    assert set(per) == {A1, "A2-111"}
    assert {sid: (r["awaiting"], r["awaiting_unflagged"], r["awaiting_flagged"]) for sid, r in per.items()} == \
        {A1: (3, 3, 0), "A2-111": (3, 1, 2)}
    for sid, r in per.items():
        assert r["awaiting_unflagged"] == n[sid] and r["awaiting"] == r["awaiting_unflagged"] + r["awaiting_flagged"]
    assert sum(n.values()) == k["review"]["awaiting_unflagged"]
    assert sum(r["awaiting"] for r in per.values()) == k["review"]["awaiting"]

    r = c.post(f"/api/documents/{sha}/bulk-accept", json={"expected_count": 3, "sheet_id": A1})
    assert r.status_code == 200, r.text
    k2, n2 = state()
    assert k2["review_by_sheet"][A1]["awaiting_unflagged"] == 0 == n2[A1]
    assert k2["review_by_sheet"]["A2-111"] == per["A2-111"] and n2["A2-111"] == n["A2-111"]


# --- request guards: cross-site requests and DNS rebinding (final review I5) ------------------------------------

def _guard(r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["code"] == code and r.json()["message"], r.json()


def test_foreign_host_is_refused_for_reads_and_writes(make, sha):
    c = make()
    for host in ("evil.example", "evil.example:8766", "127.0.0.1.evil.example", "localhost.evil.example:80"):
        _guard(c.get("/api/config", headers={"Host": host}), 403, "forbidden")
        _guard(c.get(f"/api/documents/{sha}/counts", headers={"Host": host}), 403, "forbidden")
        _guard(c.post(f"/api/documents/{sha}/export", json={}, headers={"Host": host}), 403, "forbidden")
    for host in ("127.0.0.1", "127.0.0.1:8766", "localhost:9999", "LOCALHOST", "[::1]:8766", "[::1]"):
        assert c.get("/api/config", headers={"Host": host}).status_code == 200, host


def test_explicit_non_loopback_host_is_accepted_and_others_are_not(tmp_path, cache):
    app = create_app(tmp_path / "data", PDF, ai="off", background=False, cache_dir=cache, host="192.168.1.20")
    with TestClient(app, base_url="http://192.168.1.20:8766") as c:
        assert c.get("/api/config").status_code == 200
        assert c.get("/api/config", headers={"Host": "127.0.0.1:8766"}).status_code == 200
        _guard(c.get("/api/config", headers={"Host": "evil.example"}), 403, "forbidden")


def test_foreign_origin_is_refused_on_writes(make, sha):
    c = make()
    meta = meta_of(c, sha)
    (pt,) = clear_points(meta, 1)
    body = {"sheet_id": A1, "category": "switch", "subtype": "standard", "point": to_norm(meta, pt)}
    store = c.app.state.store
    events = len(store.events())
    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999", "https://127.0.0.1:8766"):
        _guard(c.post(f"/api/documents/{sha}/devices", json=body, headers={"Origin": origin}), 403, "forbidden")
    assert len(store.events()) == events  # nothing written
    assert c.post(f"/api/documents/{sha}/devices", json=body, headers={"Origin": LOCAL}).status_code == 201
    assert c.get("/api/config", headers={"Origin": "http://evil.example"}).status_code == 200  # reads: Host only


def test_text_plain_or_no_content_type_on_a_json_endpoint_is_refused(make, sha):
    c = make(detector=FakeDetector({A1: [("switch", "standard", 1080.4, 158.08)]}))
    run = c.post(f"/api/documents/{sha}/analyze", json={"sheet_ids": [A1]}).json()
    (dev,) = c.get(f"/api/documents/{sha}/devices").json()["devices"]
    for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
                    {"Content-Type": "multipart/form-data; boundary=x"}, {}):
        _guard(c.post(f"/api/devices/{dev['id']}/accept", content=b"{}", headers=headers), 415,
               "unsupported_media_type")
        _guard(c.post(f"/api/documents/{sha}/analyze", content=b"", headers=headers), 415, "unsupported_media_type")
    assert c.get(f"/api/devices/{dev['id']}").json()["device"]["review_state"] == "proposed"
    assert [r["id"] for r in c.get(f"/api/documents/{sha}/runs").json()] == [run["id"]]  # no run started
    r = c.post(f"/api/devices/{dev['id']}/accept", content=b"{}",
               headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200 and r.json()["review_state"] == "accepted"


def test_upload_needs_the_client_header_and_checks_the_size_first(make, monkeypatch):
    from takeoff.app import api as API

    c = make()
    data = blank_pdf("guarded")
    r = c.post("/api/documents", params={"file_name": "x.pdf"}, content=data,
               headers={"Content-Type": "application/pdf"})
    _guard(r, 403, "forbidden")
    assert len(c.get("/api/documents").json()) == 1  # nothing registered
    assert upload(c, data, "x.pdf").status_code == 201

    def never(_data):
        raise AssertionError("the body was read and checked although Content-Length is over the limit")

    monkeypatch.setattr(API, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(API, "check_pdf", never)
    r = upload(c, blank_pdf("too big"), "big.pdf")
    _guard(r, 400, "invalid_pdf")
    assert "larger than" in r.json()["message"]


def test_serve_warns_when_the_host_is_not_loopback():
    import importlib.util

    spec = importlib.util.spec_from_file_location("takeoff_app_cli", ROOT / "backend" / "tools" / "takeoff_app.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.host_warning("127.0.0.1") is None and cli.host_warning("localhost") is None
    assert cli.host_warning("::1") is None
    for host in ("0.0.0.0", "192.168.1.20", "my-laptop"):
        w = cli.host_warning(host)
        assert w and host in w and "not loopback" in w
