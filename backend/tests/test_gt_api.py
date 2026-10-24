"""API tests (TestClient, real sheet A1): endpoints, AC11 server-side blind enforcement, AC13 persistence."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from takeoff.groundtruth.api import create_app

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
pytestmark = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")
SID = "A1-111"
HDR = {"X-Reviewer": "alex"}


def make_client(tmp_path) -> TestClient:
    app = create_app(PDF, tmp_path / "gt.sqlite", tmp_path / "cache", tmp_path / "exports", pages=[1])
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def region_point(client, fx=0.5, fy=0.5):
    meta = client.get(f"/api/sheets/{SID}/meta").json()
    b = meta["region"]["bbox"]
    return {"u": b["u0"] + fx * (b["u1"] - b["u0"]), "v": b["v0"] + fy * (b["v1"] - b["v0"])}


def to_blind(client):
    assert client.post(f"/api/sheets/{SID}/region", json={"confirmed": True}, headers=HDR).status_code == 200
    for c in client.get(f"/api/sheets/{SID}/meta").json()["cells"]:
        assert client.post(f"/api/sheets/{SID}/cells/{c['id']}", json={"done": True}, headers=HDR).status_code == 200
    r = client.post(f"/api/sheets/{SID}/phase/start-blind-scan", headers=HDR)
    assert r.status_code == 200, r.text


def test_sheet_list_and_meta(client):
    (s,) = client.get("/api/sheets").json()
    assert s["sheet_id"] == SID and s["phase"] == "pass1" and s["cells_total"] == 54
    meta = client.get(f"/api/sheets/{SID}/meta").json()
    assert meta["region"]["status"] == "hypothesis" and len(meta["cells"]) == 54
    assert {"annotations", "candidates", "tallies"}.isdisjoint(meta)
    assert any(t["text"] == "J" for t in meta["tokens"])
    cfg = client.get("/api/config").json()
    assert cfg["tolerances_pt"]["duplicate"] == 3.0 and "switch" in cfg["taxonomy"]["categories"]


def test_image_matches_its_mapping(client):
    meta = client.get(f"/api/sheets/{SID}/meta").json()
    r = client.get(f"/api/sheets/{SID}/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img.shape == (meta["image"]["height_px"], meta["image"]["width_px"])


def test_annotation_crud_history_and_validation(client):
    p = region_point(client, 0.7, 0.6)
    r = client.post(f"/api/sheets/{SID}/annotations", headers=HDR,
                    json={"category": "ceiling_fan", "subtype": "optional_pre_wire", "point": p})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "optional_pre_wire" and a["provenance"]["origin"] == "manual" and a["created_by"] == "alex"
    bad = client.post(f"/api/sheets/{SID}/annotations", headers=HDR,
                      json={"category": "light", "subtype": "chandelier", "point": p})
    assert bad.status_code == 422 and "not valid" in bad.json()["message"]
    assert client.post(f"/api/sheets/{SID}/annotations", headers=HDR,
                       json={"category": "wiring_run", "subtype": "other", "point": p, "notes": "x"}).status_code == 422
    r = client.patch(f"/api/annotations/{a['id']}", headers=HDR, json={"notes": "hub J at bedroom"})
    assert r.json()["revision"] == 2
    assert client.post(f"/api/annotations/{a['id']}/delete", headers=HDR, json={"reason": ""}).status_code == 400
    assert client.post(f"/api/annotations/{a['id']}/delete", headers=HDR, json={"reason": "test"}).status_code == 200
    hist = client.get(f"/api/annotations/{a['id']}/history").json()
    assert [e["action"] for e in hist] == ["create", "update", "delete"] and {e["actor"] for e in hist} == {"alex"}
    assert client.get(f"/api/sheets/{SID}/annotations").json() == []


def test_candidates_are_labelled_as_candidates(client):
    body = client.get(f"/api/sheets/{SID}/candidates").json()
    assert "never detections or counts" in body["note"] and body["clusters"] == []


def test_gate_failure_is_409_with_reasons(client):
    r = client.post(f"/api/sheets/{SID}/phase/start-blind-scan", headers=HDR)
    assert r.status_code == 409 and r.json()["code"] == "gate_not_met"
    assert any("not confirmed" in x for x in r.json()["reasons"])


def test_blind_scan_endpoints_hide_pass1_data(client):
    a = client.post(f"/api/sheets/{SID}/annotations", headers=HDR,
                    json={"category": "switch", "subtype": "dimmer", "point": region_point(client, 0.3, 0.3)}).json()
    to_blind(client)
    for method, url, body in [("get", f"/api/sheets/{SID}/annotations", None),
                              ("get", f"/api/sheets/{SID}/candidates", None),
                              ("get", f"/api/sheets/{SID}/warnings", None),
                              ("get", f"/api/annotations/{a['id']}/history", None),
                              ("patch", f"/api/annotations/{a['id']}", {"notes": "x"}),
                              ("post", f"/api/sheets/{SID}/annotations",
                               {"category": "switch", "subtype": "standard", "point": region_point(client)})]:
        r = getattr(client, method)(url, headers=HDR, **({"json": body} if body else {}))
        assert r.status_code == 409 and r.json()["code"] == "blind_scan_in_progress", (url, r.text)
    blind = client.get(f"/api/sheets/{SID}/blind").json()
    assert set(blind) == {"phase", "cells", "observations", "progress"}
    prog = client.get(f"/api/sheets/{SID}/progress").json()
    assert set(prog) == {"phase", "cells_total", "cells_blind_scan_done", "observations"}
    for url in (f"/api/sheets/{SID}/blind", f"/api/sheets/{SID}/meta", f"/api/sheets/{SID}/progress", "/api/sheets"):
        text = client.get(url).text
        assert a["id"] not in text and "dimmer" not in text and "tallies" not in text, url


def test_blind_progress_persists_and_reconcile_without_duplicates(tmp_path):
    with make_client(tmp_path) as c:
        a = c.post(f"/api/sheets/{SID}/annotations", headers=HDR,
                   json={"category": "switch", "subtype": "dimmer", "point": region_point(c, 0.3, 0.3)}).json()
        to_blind(c)
        cells = c.get(f"/api/sheets/{SID}/blind").json()["cells"]
        c.post(f"/api/sheets/{SID}/cells/{cells[0]['id']}", json={"done": True}, headers=HDR)
        o = c.post(f"/api/sheets/{SID}/observations", headers=HDR,
                   json={"point": a["point"], "category": "switch"}).json()
    with make_client(tmp_path) as c:  # server restart, same database
        blind = c.get(f"/api/sheets/{SID}/blind").json()
        assert [x["id"] for x in blind["observations"]] == [o["id"]]
        assert [x["id"] for x in blind["cells"] if x["blind_scan_done"]] == [cells[0]["id"]]
        r = c.post(f"/api/sheets/{SID}/phase/open-reconcile", headers=HDR)
        assert r.status_code == 409 and "53 cells not blind-scanned" in r.json()["reasons"][0]
        for x in blind["cells"][1:]:
            c.post(f"/api/sheets/{SID}/cells/{x['id']}", json={"done": True}, headers=HDR)
        assert c.post(f"/api/sheets/{SID}/phase/open-reconcile", headers=HDR).status_code == 200
        assert c.post(f"/api/sheets/{SID}/observations", headers=HDR,
                      json={"point": a["point"], "category": "switch"}).status_code == 409
        rec = c.get(f"/api/sheets/{SID}/reconcile").json()
        assert rec["suggestions"][0]["suggested_annotation_id"] == a["id"]
        n_before = len(c.get(f"/api/sheets/{SID}/annotations").json())
        m = c.post(f"/api/observations/{o['id']}/match", headers=HDR, json={"annotation_id": a["id"]})
        assert m.status_code == 200 and m.json()["blind_confirmation"] == "confirmed"
        assert len(c.get(f"/api/sheets/{SID}/annotations").json()) == n_before
        r = c.post("/api/export", headers=HDR)
        assert r.status_code == 200 and r.json()["level"] == "incomplete"
        manifest = json.loads((Path(r.json()["path"]) / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["sheets"][SID]["phase"] == "reconcile"
        assert manifest["sheets"][SID]["blind_scan_consistency"]["matched"] == 1
