"""The gate (GET /v3/site/update_curr_year) probes the trailing year too
(trailing-year refresh spec §5, §7), the trailing data endpoints, and the
/info trailing fields.

The current-year half of the gate is pinned to "no new data" so each test
looks only at the trailing action. The trailing probe runs the REAL
check_year_partial against stubbed TBA readers, so the partial triggers are
the same ones the current year uses.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.data.router as dr
import src.data.tba as data_tba
from src.constants import CURR_YEAR
from src.db.models import ETag
from src.google import storage
from src.google.publish import Manifest
from src.types.enums import EventStatus

TY = CURR_YEAR - 1
TODAY = datetime.now().strftime("%Y-%m-%d")
TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def mk_event(key, status=EventStatus.ONGOING):
    return SimpleNamespace(
        key=key,
        status=status,
        start_date=TODAY,
        end_date=TOMORROW,
        time=0,
        qual_matches=0,
        current_match=0,
    )


def snapshot_for(events, etags):
    objs = (
        "year",
        {},
        {e.key: e for e in events},
        {},
        {},
        {t.path: t for t in etags},
    )
    return (objs, [])


TY_EVENT = mk_event(f"{TY}iri")
TY_ETAGS = [
    ETag(year=TY, path=f"{TY}/events", etag="ev1"),
    ETag(year=TY, path=f"{TY}iri/matches", etag="m1"),
    ETag(year=TY, path=f"{TY}iri/rankings", etag="r1"),
]


class Rig:
    def __init__(self, monkeypatch):
        self.calls = []
        self.manifest = Manifest(trailing_year=TY)
        self.snapshots = {CURR_YEAR: snapshot_for([], []), TY: snapshot_for([TY_EVENT], TY_ETAGS)}
        self.events_etag = {TY: "ev1"}
        self.match_etag = "m1"
        self.candidates = []
        self.mp = monkeypatch

        monkeypatch.setattr(dr.requests, "get", lambda url, **kw: self.calls.append(url))
        monkeypatch.setattr(dr, "read_snapshot", lambda year: self.snapshots.get(year))
        monkeypatch.setattr(storage, "read_manifest", lambda bucket=None: self.read_manifest())
        # Current year: no new data (the trailing probe is under test).
        real_check = dr.check_year_partial_tba
        monkeypatch.setattr(
            dr,
            "check_year_partial_tba",
            lambda y, e, t: False if y == CURR_YEAR else real_check(y, e, t),
        )
        monkeypatch.setattr(
            data_tba,
            "get_events_tba",
            lambda year, etag=None, cache=True, tier_probes=False: (
                [],
                self.events_etag[year],
            ),
        )
        monkeypatch.setattr(
            data_tba,
            "get_event_matches_tba",
            lambda year, key, time, etag, cache=False: ([], self.match_etag),
        )
        monkeypatch.setattr(
            data_tba,
            "get_event_rankings_tba",
            lambda key, etag, cache=False: ([], etag),
        )
        monkeypatch.setattr(
            data_tba, "get_raw_offseason_events_tba", lambda year: self.candidates
        )
        monkeypatch.setattr(
            data_tba,
            "probe_offseason_event_tba",
            lambda key: (list(range(1, 31)), 40),
        )
        app = FastAPI()
        app.include_router(dr.site_router, prefix="/v3/site")
        self.client = TestClient(app)

    def read_manifest(self):
        if isinstance(self.manifest, Exception):
            raise self.manifest
        return self.manifest

    def gate(self):
        resp = self.client.get("/v3/site/update_curr_year")
        assert resp.status_code == 200
        return resp.json()


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def test_gate_bootstraps_when_marker_absent(rig, trailing_open):
    rig.manifest = Manifest(trailing_year=None)
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": "full", "chain": False}
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/update_trailing_year?full=1")


def test_gate_bootstraps_when_manifest_absent(rig, trailing_open):
    rig.manifest = None
    assert rig.gate()["trailing"] == "full"


def test_gate_full_when_trailing_snapshot_unreadable(rig, trailing_open):
    del rig.snapshots[TY]
    assert rig.gate()["trailing"] == "full"


def test_gate_partial_on_events_list_etag_change(rig, trailing_open):
    rig.events_etag[TY] = "ev2"
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": "partial", "chain": False}
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/update_trailing_year?full=0")


def test_gate_partial_on_match_change(rig, trailing_open):
    rig.match_etag = "m2"
    assert rig.gate()["trailing"] == "partial"


def test_gate_partial_on_dropped_candidate(rig, trailing_open):
    rig.candidates = [
        {"key": f"{TY}newoff", "start_date": TODAY, "end_date": TOMORROW}
    ]
    assert rig.gate()["trailing"] == "partial"


def test_gate_idle_when_unchanged(rig, trailing_open):
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": None, "chain": False}
    assert rig.calls == []


def test_gate_closes_after_window(rig):
    # Window closed (conftest default), marker still set: close would act.
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": "close", "chain": False}
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/close_trailing_year")


def test_gate_closes_for_leftover_year_keys(rig):
    rig.manifest = Manifest(
        trailing_year=None, blobs={f"event/{TY}iri": f"v2/event/{TY}iri.abc"}
    )
    assert rig.gate()["trailing"] == "close"


def test_gate_idle_after_close_done(rig):
    rig.manifest = Manifest(
        trailing_year=None,
        blobs={f"parquet/{TY}/matches.parquet": "v2/x.parquet"},
    )
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": None, "chain": False}
    assert rig.calls == []


def test_gate_trailing_runs_when_current_snapshot_unreadable(rig, trailing_open):
    del rig.snapshots[CURR_YEAR]
    rig.manifest = Manifest(trailing_year=None)
    body = rig.gate()
    assert body == {
        "status": "skipped",
        "reason": "snapshot-unreadable",
        "trailing": "full",
        "chain": False,
    }
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/update_trailing_year?full=1")


def test_gate_backgrounds_both(rig, monkeypatch, trailing_open):
    monkeypatch.setattr(dr, "check_year_partial_tba", lambda y, e, t: True)
    body = rig.gate()
    assert body == {"status": "backgrounded", "trailing": "partial", "chain": False}
    assert rig.calls[0].endswith("/v3/data/update_curr_year")
    assert rig.calls[1].endswith("/v3/data/update_trailing_year?full=0")


def test_gate_survives_manifest_read_error(rig, capsys):
    rig.manifest = storage.ManifestReadError("boom")
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": None, "chain": False}
    assert rig.calls == []
    assert "boom" in capsys.readouterr().out


def test_gate_survives_manifest_read_error_while_open(rig, trailing_open):
    rig.manifest = storage.ManifestReadError("boom")
    assert rig.gate()["trailing"] is None


# ------------------------------------------------ chain_pending, busy, errors


def test_gate_schedules_chain_when_pending(rig, trailing_open):
    rig.manifest = Manifest(trailing_year=TY, chain_pending=True)
    body = rig.gate()
    assert body == {"status": "skipped", "trailing": None, "chain": True}
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/reset_curr_year")


def test_gate_schedules_chain_after_window_closed(rig):
    rig.manifest = Manifest(trailing_year=None, chain_pending=True)
    assert rig.gate()["chain"] is True
    assert rig.calls[-1].endswith("/v3/data/reset_curr_year")


def test_gate_leaves_chain_to_scheduled_trailing_request(rig, trailing_open):
    """A scheduled trailing cycle reads chain_pending after its work and
    chains itself: the gate does not send a second reset_curr_year."""
    rig.manifest = Manifest(trailing_year=TY, chain_pending=True)
    rig.events_etag[TY] = "ev2"
    body = rig.gate()
    assert body["trailing"] == "partial" and body["chain"] is False
    assert not any(c.endswith("/v3/data/reset_curr_year") for c in rig.calls)


def test_gate_skips_probes_while_pipeline_busy(rig, monkeypatch, trailing_open):
    from src.data.pipeline_lock import PIPELINE_LOCK

    def no_probe(*a, **k):
        raise AssertionError("the gate must not probe while a pipeline runs")

    monkeypatch.setattr(dr, "read_snapshot", no_probe)
    monkeypatch.setattr(dr, "trailing_gate_action", no_probe)
    monkeypatch.setattr(dr, "chain_gate_pending", no_probe)
    assert PIPELINE_LOCK.acquire(blocking=False)
    try:
        body = rig.gate()
    finally:
        PIPELINE_LOCK.release()
    assert body == {
        "status": "skipped",
        "reason": "pipeline-busy",
        "trailing": None,
        "chain": False,
    }
    assert rig.calls == []


def test_gate_runs_trailing_when_current_probe_raises(rig, monkeypatch, trailing_open, capsys):
    rig.manifest = Manifest(trailing_year=None)

    def boom(year):
        if year == CURR_YEAR:
            raise RuntimeError("current-year probe failed")
        return rig.snapshots.get(year)

    monkeypatch.setattr(dr, "read_snapshot", boom)
    resp = rig.client.get("/v3/site/update_curr_year")
    assert resp.status_code == 500
    body = resp.json()
    assert body["status"] == "error" and "current-year probe failed" in body["error"]
    assert body["trailing"] == "full"
    # The trailing self-call still ran (background tasks ride the 500).
    assert len(rig.calls) == 1
    assert rig.calls[0].endswith("/v3/data/update_trailing_year?full=1")
    assert "current-year probe failed" in capsys.readouterr().err


def test_curr_year_endpoints_report_busy(monkeypatch):
    """A busy skip is never reported as success (lock held elsewhere)."""
    from src.data.pipeline_lock import PIPELINE_LOCK

    ran = []
    monkeypatch.setattr(dr.data_main, "_update_curr_year_held", lambda p, t: ran.append("curr"))
    monkeypatch.setattr(dr.data_main, "_refresh_teams_held", lambda: ran.append("teams") or {})
    client = data_client()
    assert PIPELINE_LOCK.acquire(blocking=False)
    try:
        for path in (
            "/v3/data/reset_curr_year",
            "/v3/data/update_curr_year",
            "/v3/data/update_curr_year_debug",
            "/v3/data/refresh_teams",
        ):
            assert client.get(path).json() == {"status": "busy"}, path
    finally:
        PIPELINE_LOCK.release()
    assert ran == []

    assert client.get("/v3/data/update_curr_year").json() == {"status": "success"}
    assert client.get("/v3/data/reset_curr_year").json() == {"status": "success"}
    assert ran == ["curr", "curr", "teams"]


def test_reset_curr_year_busy_when_refresh_teams_busy(monkeypatch):
    monkeypatch.setattr(dr, "update_curr_year", lambda **kw: None)
    monkeypatch.setattr(dr, "refresh_teams", lambda: {"status": "busy"})
    assert data_client().get("/v3/data/reset_curr_year").json() == {"status": "busy"}


def test_update_curr_year_returns_busy_sentinel(monkeypatch):
    from src.data.pipeline_lock import PIPELINE_LOCK

    monkeypatch.setattr(dr.data_main, "_update_curr_year_held", lambda p, t: None)
    assert dr.data_main.update_curr_year(partial=True, tba_partial=True) is None
    assert PIPELINE_LOCK.acquire(blocking=False)
    try:
        assert dr.data_main.update_curr_year(partial=True, tba_partial=True) == "busy"
    finally:
        PIPELINE_LOCK.release()


# ---------------------------------------------------------------- endpoints


def data_client():
    app = FastAPI()
    app.include_router(dr.data_router, prefix="/v3/data")
    return TestClient(app)


def test_update_trailing_year_endpoint(monkeypatch):
    seen = []
    monkeypatch.setattr(
        dr.data_main,
        "update_trailing_year",
        lambda full, chain="http": seen.append(full) or {"status": "success"},
    )
    client = data_client()
    assert client.get("/v3/data/update_trailing_year?full=1").json() == {
        "status": "success"
    }
    client.get("/v3/data/update_trailing_year?full=0")
    client.get("/v3/data/update_trailing_year")
    assert seen == [True, False, False]


def test_close_trailing_year_endpoint(monkeypatch):
    monkeypatch.setattr(
        dr.data_main, "close_trailing_year", lambda chain="http": {"status": "busy"}
    )
    assert data_client().get("/v3/data/close_trailing_year").json() == {
        "status": "busy"
    }


def test_trailing_endpoints_are_plain_def():
    import inspect

    assert not inspect.iscoroutinefunction(dr.update_trailing_year_endpoint)
    assert not inspect.iscoroutinefunction(dr.close_trailing_year_endpoint)


# --------------------------------------------------------------------- /info


def test_info_reports_trailing_fields(monkeypatch):
    import main
    from src.google import trailing_publish

    status = {"year": TY, "state": "ok"}
    monkeypatch.setattr(trailing_publish, "read_trailing_status", lambda: status)
    monkeypatch.setattr(
        storage,
        "read_manifest",
        lambda bucket=None: Manifest(hist_epochs={str(TY - 1): 3}),
    )
    monkeypatch.setattr(storage, "manifest_conflicts", 2)
    info = main.get_info()
    assert info["TRAILING_YEAR"] == TY
    assert info["TRAILING_WINDOW_OPEN"] is False
    assert info["TRAILING_STATUS"] == status
    assert info["HIST_EPOCHS"] == {str(TY - 1): 3}
    assert info["HIST_EPOCH_NEXT_MIN"] == max(main.HIST_EPOCH, 3) + 1
    assert "HIST_EPOCH_SHADOWED" not in info
    assert info["MANIFEST_CONFLICTS"] == 2


def test_info_best_effort(monkeypatch):
    import main
    from src.google import trailing_publish

    def boom(*a, **kw):
        raise storage.ManifestReadError("boom")

    monkeypatch.setattr(trailing_publish, "read_trailing_status", boom)
    monkeypatch.setattr(storage, "read_manifest", boom)
    info = main.get_info()
    assert info["TRAILING_STATUS"] is None
    assert info["HIST_EPOCHS"] is None
    assert info["HIST_EPOCH_NEXT_MIN"] is None


def test_hist_epoch_next_min_rule(monkeypatch):
    """The smallest global HIST_EPOCH a future bump must use: above every
    year epoch and the deployed HIST_EPOCH; None when the manifest is
    unreadable."""
    import main

    monkeypatch.setattr(main, "HIST_EPOCH", 2)
    assert main.hist_epoch_next_min(None) is None
    assert main.hist_epoch_next_min({}) == 3
    assert main.hist_epoch_next_min({"2025": 3, "2024": 5}) == 6
    # A stale entry at or below the global epoch never lowers the answer.
    assert main.hist_epoch_next_min({"2025": 1}) == 3


def test_read_trailing_status(monkeypatch):
    from src.google import trailing_publish
    from tests.fake_gcs import FakeBucket

    b = FakeBucket()
    monkeypatch.setattr(storage, "_bucket", lambda: b)
    assert trailing_publish.read_trailing_status() is None
    trailing_publish.write_trailing_status({"year": TY, "state": "ok"})
    assert trailing_publish.read_trailing_status() == {"year": TY, "state": "ok"}
    b.put(trailing_publish.TRAILING_STATUS_OBJECT, b"not json")
    assert trailing_publish.read_trailing_status() is None
