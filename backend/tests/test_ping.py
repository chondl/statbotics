import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.data.router as dr
from src.constants import CURR_YEAR


def make_client(monkeypatch, last_probe=float("-inf"), inflight=False):
    calls = []
    monkeypatch.setattr(dr.requests, "get", lambda url, **kw: calls.append(url))
    monkeypatch.setattr(dr, "_ping_last_probe", last_probe)
    monkeypatch.setattr(dr, "_ping_inflight", inflight)
    app = FastAPI()
    app.include_router(dr.site_router, prefix="/v3/site")
    return TestClient(app), calls


def test_cold_ping_schedules_probe(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 202
    # TestClient runs background tasks before returning
    assert len(calls) == 1
    assert calls[0].endswith("/v3/site/update_curr_year")


def test_ping_within_cooldown_is_noop(monkeypatch):
    client, calls = make_client(monkeypatch, last_probe=time.monotonic())
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 204
    assert calls == []


def test_ping_while_inflight_is_noop(monkeypatch):
    client, calls = make_client(monkeypatch, inflight=True)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 204
    assert calls == []


def test_second_ping_hits_cooldown(monkeypatch):
    client, calls = make_client(monkeypatch)
    assert client.get(f"/v3/site/ping/event/{CURR_YEAR}iri").status_code == 202
    assert client.get(f"/v3/site/ping/event/{CURR_YEAR}iri").status_code == 204
    assert len(calls) == 1


def test_non_current_year_key_rejected(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get("/v3/site/ping/event/2019ncwak")
    assert resp.status_code == 204
    assert calls == []


def test_malformed_key_rejected(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}IRI!")
    assert resp.status_code == 204
    assert calls == []


# ---------------- /v3/site/update_curr_year db-less probe branch --------------


def test_site_update_dbless_snapshot_miss_skips_probe(monkeypatch):
    # Db-less, an unreadable snapshot must degrade to "assume no new data":
    # skip without probing TBA (an empty etag baseline would always report
    # new data) and without triggering a partial cycle (which the pipeline's
    # snapshot-miss guard would skip anyway).
    monkeypatch.setattr(dr, "DISABLE_DB", True)
    monkeypatch.setattr(dr, "read_snapshot", lambda year: None)
    monkeypatch.setattr(
        dr,
        "check_year_partial_tba",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("must not probe TBA with an empty etag baseline")
        ),
    )
    monkeypatch.setattr(dr.data_main, "db_less_partial_skipped", False)
    client, calls = make_client(monkeypatch)
    resp = client.get("/v3/site/update_curr_year")
    assert resp.status_code == 200
    assert resp.json() == {"status": "skipped", "reason": "snapshot-unreadable"}
    # A persistent snapshot failure must be visible at /info, not hidden
    # behind an ordinary-looking skip.
    assert dr.data_main.db_less_partial_skipped is True
    assert calls == []


def test_site_update_dbless_snapshot_hit_probes_and_backgrounds(monkeypatch):
    monkeypatch.setattr(dr, "DISABLE_DB", True)
    snap_objs = ("year", {}, {}, {}, {}, {})
    monkeypatch.setattr(dr, "read_snapshot", lambda year: (snap_objs, []))
    monkeypatch.setattr(dr, "check_year_partial_tba", lambda y, e, t: True)
    client, calls = make_client(monkeypatch)
    resp = client.get("/v3/site/update_curr_year")
    assert resp.json() == {"status": "backgrounded"}
    assert len(calls) == 1
    assert calls[0].endswith("/v3/data/update_curr_year")
