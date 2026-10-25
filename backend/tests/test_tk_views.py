"""``api.SheetViews``: the lock order with the store (final review C1) and which failures are remembered (I3c).

The lock rule under test: a thread may take a SheetViews key lock while it holds the store lock (an export snapshot,
a merge transaction), never the reverse. Each test reproduces the interleaving of the reviewer's ``deadlock.py`` with
the real classes and must finish within a timeout.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from takeoff.app import api as API
from takeoff.app import models as TM
from takeoff.app.service import TakeoffError
from takeoff.app.store import TakeoffStore

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "plans" / "Escondido_Electrical_Plans_Ceiling_Fans.pdf"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="supplied plan PDF not present")
TIMEOUT = 10


def _store(tmp_path, doc="d"):
    store = TakeoffStore(tmp_path / "t.sqlite")
    store.insert("sheets", TM.SheetInfo(id=f"{doc}:1", document_id=doc, page_number=1, sheet_id="A1"), "x")
    return store


def _race(store, holder_call, other_call):
    """Thread A holds the store lock (like ``export_takeoff``'s snapshot) and then calls ``holder_call``; thread B
    calls ``other_call`` meanwhile (like a ``GET .../meta`` or ``.../image`` on an uncached sheet)."""
    started = threading.Event()
    res: dict[str, str] = {}

    def run(name, fn):
        try:
            fn()
            res[name] = "ok"
        except Exception as e:  # the outcome is recorded; only finishing matters here
            res[name] = type(e).__name__

    def a():
        with store.snapshot():
            started.set()
            time.sleep(0.5)  # B gets in first and takes its key lock, then waits for the store
            run("a", holder_call)

    def b():
        started.wait()
        run("b", other_call)

    ta, tb = threading.Thread(target=a, daemon=True), threading.Thread(target=b, daemon=True)
    ta.start()
    tb.start()
    ta.join(TIMEOUT)
    tb.join(TIMEOUT)
    assert not ta.is_alive() and not tb.is_alive(), f"deadlock: A alive {ta.is_alive()}, B alive {tb.is_alive()}"
    return res


def test_views_under_the_store_lock_do_not_deadlock_with_an_uncached_view(tmp_path):
    store = _store(tmp_path)
    views = API.SheetViews(store, lambda d: tmp_path / "nope.pdf", tmp_path / "cache")
    res = _race(store, lambda: views("d", "A1"), lambda: views("d", "A1"))
    assert res == {"a": "TakeoffError", "b": "TakeoffError"}  # the PDF is missing: refused, never hung


@needs_pdf
def test_image_does_not_take_the_store_lock_while_holding_a_key_lock(tmp_path):
    store = _store(tmp_path)
    views = API.SheetViews(store, lambda d: PDF, tmp_path / "cache")
    views("d", "A1")  # the view is cached; the image path is not
    res = _race(store, lambda: views("d", "A1"), lambda: views.image("d", "A1"))
    assert res == {"a": "ok", "b": "ok"}


def test_a_missing_pdf_is_not_remembered(tmp_path):
    """I3c: file-not-found (any OS error) is retried on the next call; once the file is back, the view works."""
    if not PDF.exists():
        pytest.skip("supplied plan PDF not present")
    store = _store(tmp_path)
    where = tmp_path / "plans.pdf"
    views = API.SheetViews(store, lambda d: where, tmp_path / "cache")
    with pytest.raises(TakeoffError) as e:
        views("d", "A1")
    assert "FileNotFoundError" in e.value.message
    assert views.unavailable("d", "A1") is not None
    shutil.copy(PDF, where)
    assert views.unavailable("d", "A1") is None
    assert views("d", "A1")["sheet_id"] == "A1"


def test_a_page_that_cannot_be_read_is_remembered(tmp_path):
    """I3c: a deterministic failure (the bytes are not a readable page) is cached and not re-parsed."""
    store = _store(tmp_path)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    calls = []

    def path_of(d):
        calls.append(d)
        return bad

    views = API.SheetViews(store, path_of, tmp_path / "cache")
    first = views.unavailable("d", "A1")
    assert first is not None and "FileNotFoundError" not in first
    assert views.unavailable("d", "A1") == first
    assert len(calls) == 1  # remembered: no second read
