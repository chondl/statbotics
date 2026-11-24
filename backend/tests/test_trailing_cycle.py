"""Trailing cycles, the trailing publish and window close (trailing-year
refresh spec §5.2, §6, §7; plan Task 6).

The pipeline runs against the shared fake GCS with process_year_tba
replaced by a fixture source (except where the TBA status semantics are the
subject), so every manifest, snapshot and hist write is observable."""

import gzip
import json
import threading
import zlib
from collections import defaultdict
from copy import deepcopy
from typing import Any, Dict, List

import pytest

import src.data.main as data_main
import src.data.sweep as sweep
import src.data.tba as data_tba
import src.google.parquet as parquet
import src.google.snapshot as snapshot
import src.google.storage as storage
import src.google.trailing_publish as tp
from src.constants import HIST_EPOCH
from src.data import trailing
from src.data.pipeline_lock import PIPELINE_LOCK
from src.db.models import Event, Match, Team, TeamEvent, TeamYear, Year
from src.google.publish import MANIFEST_OBJECT, Manifest, historical_key
from src.types.enums import (
    CompLevel,
    EventStatus,
    EventType,
    MatchStatus,
    MatchWinner,
)
from tests.fake_gcs import FakeBucket

Y = trailing.trailing_year()


# ------------------------------------------------------------------ fixtures


def make_match(key, event, red, blue, time, red_score=100):
    return Match(
        key=key,
        year=Y,
        event=event,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        status=MatchStatus.COMPLETED,
        winner=MatchWinner.RED,
        red_1=red[0],
        red_2=red[1],
        blue_1=blue[0],
        blue_2=blue[1],
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
        red_score=red_score,
        blue_score=90,
        time=time,
    )


def make_source(casj_score=100, extra_team=False):
    year_obj = Year(year=Y)
    team_years = {
        f"254_{Y}": TeamYear(team=254, year=Y, count=10),
        f"1678_{Y}": TeamYear(team=1678, year=Y, count=8),
    }
    if extra_team:
        team_years[f"9999_{Y}"] = TeamYear(team=9999, year=Y, count=1)
    events = {
        f"{Y}casj": Event(
            key=f"{Y}casj", year=Y, name="Silicon Valley",
            status=EventStatus.COMPLETED, week=2, time=100,
        ),
        f"{Y}cc": Event(
            key=f"{Y}cc", year=Y, name="Chezy Champs",
            status=EventStatus.COMPLETED, week=9, time=200,
        ),
    }
    team_events = {
        f"254_{Y}casj": TeamEvent(team=254, year=Y, event=f"{Y}casj"),
        f"1678_{Y}casj": TeamEvent(team=1678, year=Y, event=f"{Y}casj"),
        f"254_{Y}cc": TeamEvent(team=254, year=Y, event=f"{Y}cc"),
    }
    matches = {
        f"{Y}casj_qm1": make_match(
            f"{Y}casj_qm1", f"{Y}casj", (254, 1678), (118, 148), 110, casj_score
        ),
        f"{Y}cc_qm1": make_match(
            f"{Y}cc_qm1", f"{Y}cc", (254, 118), (1678, 148), 210
        ),
    }
    return (year_obj, team_years, events, team_events, matches, {})


TEAMS = [
    Team(team=254, name="The Cheesy Poofs", rookie_year=1999),
    Team(team=1678, name="Citrus Circuits", rookie_year=2004),
]


class NoTbaCache:
    def hydrate(self, year=None):
        pass

    def persist(self):
        pass

    def set_force_refresh(self, on):
        pass


def seed(bucket: FakeBucket, manifest: Manifest) -> int:
    return bucket.put(
        MANIFEST_OBJECT,
        gzip.compress(manifest.to_json().encode("utf-8")),
        cache_control=storage.MANIFEST_CACHE,
        content_encoding="gzip",
    )


def stored(bucket: FakeBucket) -> Manifest:
    return Manifest.from_json(gzip.decompress(bucket.objects[MANIFEST_OBJECT]))


def manifest_versions(bucket: FakeBucket) -> List[Manifest]:
    gens = sorted(g for (n, g) in bucket.history if n == MANIFEST_OBJECT)
    return [
        Manifest.from_json(gzip.decompress(bucket.history[(MANIFEST_OBJECT, g)]))
        for g in gens
    ]


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    for module in (storage, parquet, snapshot):
        monkeypatch.setattr(module, "_bucket", lambda: b)
    return b


def clear_chain_pending() -> None:
    """What the full current-year publish does to the flag."""
    storage.update_manifest(data_main._clear_chain_pending)


class Pipeline:
    def __init__(self):
        self.source = make_source()
        self.tba_calls: List[Any] = []
        self.load_teams_calls: List[bool] = []
        self.chains = 0


@pytest.fixture
def pipe(monkeypatch, bucket):
    p = Pipeline()

    def fake_tba(year_num, teams, objs, partial, cache):
        p.tba_calls.append((year_num, partial, cache))
        src = deepcopy(p.source)
        for i in range(1, 5):
            objs[i].clear()
            objs[i].update(src[i])
        return [], objs

    def fake_load_teams(cache=True):
        p.load_teams_calls.append(cache)
        return deepcopy(TEAMS)

    def chain():
        # Stands in for the chained GET /v3/data/reset_curr_year, whose full
        # current-year publish clears chain_pending.
        p.chains += 1
        clear_chain_pending()

    monkeypatch.setattr(data_main, "DISABLE_GCS", False)
    monkeypatch.setattr(data_main, "tba_cache", NoTbaCache())
    monkeypatch.setattr(data_main, "process_year_tba", fake_tba)
    monkeypatch.setattr(data_main, "process_year_avg", lambda year_obj, m: year_obj)
    monkeypatch.setattr(data_main, "process_year_wins", lambda objs: objs)
    monkeypatch.setattr(data_main, "process_year_epa", lambda objs, aty: objs)
    monkeypatch.setattr(data_main, "get_team_years_db", lambda **kw: [])
    monkeypatch.setattr(data_main, "load_teams_tba", fake_load_teams)
    monkeypatch.setattr(data_main, "_schedule_reset_curr_year", chain)
    return p


def set_window(monkeypatch, open_: bool) -> None:
    monkeypatch.setattr(trailing, "trailing_window_open", lambda now=None: open_)


def decode(data: bytes) -> Any:
    return json.loads(zlib.decompress(data))


# ------------------------------------------------------------ trailing cycle


def test_full_trailing_cycle_fetches_fresh(pipe, trailing_open, capsys):
    result = data_main.update_trailing_year(full=True)

    assert result["status"] == "success" and result["full"] is True
    # cache=False reaches process_year_tba (and the team list is fetched
    # fresh): a full trailing cycle trusts no pickle.
    assert pipe.tba_calls == [(Y, False, False)]
    assert pipe.load_teams_calls == [False]
    assert f"TRAILING {Y} CYCLE COMPLETE full=1 secs=" in capsys.readouterr().out


def test_partial_trailing_cycle_resumes_from_snapshot(pipe, bucket, trailing_open, capsys):
    data_main.update_trailing_year(full=True)
    pipe.tba_calls.clear()
    pipe.load_teams_calls.clear()

    result = data_main.update_trailing_year(full=False)

    assert result["full"] is False
    assert pipe.tba_calls == [(Y, True, True)]
    assert pipe.load_teams_calls == []
    assert f"TRAILING {Y} CYCLE COMPLETE full=0 secs=" in capsys.readouterr().out


def test_partial_without_snapshot_escalates_to_full(pipe, trailing_open):
    result = data_main.update_trailing_year(full=False)
    assert result["full"] is True
    assert pipe.tba_calls == [(Y, False, False)]


def test_bootstrap_publishes_then_snapshot_then_marker(pipe, bucket, monkeypatch, trailing_open):
    # No chained re-render here: only the cycle's own manifest writes count.
    monkeypatch.setattr(data_main, "_schedule_reset_curr_year", lambda: None)
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bucket.uploads.clear()

    data_main.update_trailing_year(full=True)

    names = bucket.uploads
    manifest_idx = [i for i, n in enumerate(names) if n == MANIFEST_OBJECT]
    snap_idx = [i for i, n in enumerate(names) if n.startswith(f"state/snapshot.{Y}")]
    v2_idx = [i for i, n in enumerate(names) if n.startswith("v2/")]
    assert len(manifest_idx) == 2 and len(snap_idx) == 1 and v2_idx
    # versioned objects -> trailing publish -> snapshot -> bootstrap marker
    assert max(v2_idx) < manifest_idx[0] < snap_idx[0] < manifest_idx[1]

    publish, marker = manifest_versions(bucket)[-2:]
    assert publish.trailing_year is None
    assert f"events/{Y}" in publish.blobs
    assert f"parquet/{Y}/team_years.parquet" in publish.blobs
    assert marker.trailing_year == Y
    assert snapshot.read_snapshot(Y) is not None

    status = json.loads(bucket.objects[tp.TRAILING_STATUS_OBJECT])
    assert status["year"] == Y and status["state"] == "ok" and status["full"] is True
    assert status["finished"] and status["error"] is None
    assert bucket.metadata[tp.TRAILING_STATUS_OBJECT]["cache_control"] == "no-store"


def test_bootstrap_removes_unrendered_year_keys(pipe, bucket, trailing_open):
    seed(
        bucket,
        Manifest(
            hist_epoch=HIST_EPOCH,
            blobs={
                f"team_years/{Y}.limit=100.metric=epa": "v2/a.1",
                f"noteworthy_matches/{Y}": "v2/b.1",
                f"event/{Y}gone": "v2/c.1",
                f"parquet/{Y}/old.parquet": "v2/d.1",
                f"event/{Y + 1}abc": "v2/e.1",
                "teams/all": "v2/f.1",
            },
        ),
    )

    data_main.update_trailing_year(full=True)

    m = stored(bucket)
    for gone in (
        f"team_years/{Y}.limit=100.metric=epa",
        f"noteworthy_matches/{Y}",
        f"event/{Y}gone",
    ):
        assert gone not in m.blobs
    assert m.blobs[f"parquet/{Y}/old.parquet"] == "v2/d.1"
    assert m.blobs[f"event/{Y + 1}abc"] == "v2/e.1"
    assert m.blobs["teams/all"] == "v2/f.1"
    for kept in (f"events/{Y}", f"team_years/{Y}", f"event/{Y}casj", f"team/254/{Y}"):
        assert kept in m.blobs


def test_trailing_publish_is_hash_gated(bucket, trailing_open):
    objs, teams = make_source(), deepcopy(TEAMS)
    assert tp.trailing_publish(Y, objs, teams) is True
    first = list(bucket.uploads)
    assert first[-1] == MANIFEST_OBJECT

    bucket.uploads.clear()
    assert tp.trailing_publish(Y, make_source(), deepcopy(TEAMS)) is False
    assert bucket.uploads == []  # unchanged: no object, no manifest write

    changed = make_source(casj_score=150)
    assert tp.trailing_publish(Y, changed, deepcopy(TEAMS)) is False
    uploaded = [n for n in bucket.uploads if n != MANIFEST_OBJECT]
    assert any(n.startswith(f"v2/event/{Y}casj.") for n in uploaded)
    assert any(n.startswith(f"v2/parquet/{Y}/matches.parquet.") for n in uploaded)
    assert not any(n.startswith(f"v2/event/{Y}cc.") for n in uploaded)
    assert not any(n.startswith(f"v2/parquet/{Y}/team_years.parquet.") for n in uploaded)
    assert bucket.uploads.count(MANIFEST_OBJECT) == 1


def test_trailing_cycle_writes_no_hist(pipe, bucket, trailing_open):
    data_main.update_trailing_year(full=True)
    pipe.source = make_source(casj_score=150)
    data_main.update_trailing_year(full=False)

    assert not any(n.startswith("hist/") for n in bucket.objects)
    # Only versioned objects, the manifest, the snapshot and the status: no
    # unversioned legacy copies of logical paths.
    for name in bucket.uploads:
        assert (
            name.startswith("v2/")
            or name == MANIFEST_OBJECT
            or name.startswith("state/")
            or name == tp.TRAILING_STATUS_OBJECT
        ), name


def test_chain_scheduled_only_when_team_years_change(pipe, trailing_open):
    data_main.update_trailing_year(full=True)  # first publish: new digest
    assert pipe.chains == 1

    data_main.update_trailing_year(full=False)  # nothing changed
    assert pipe.chains == 1

    pipe.source = make_source(casj_score=150)  # matches only
    data_main.update_trailing_year(full=False)
    assert pipe.chains == 1

    pipe.source = make_source(casj_score=150, extra_team=True)  # a new TeamYear
    data_main.update_trailing_year(full=False)
    assert pipe.chains == 2


def test_chain_is_its_own_request(monkeypatch):
    calls = []
    done = threading.Event()

    def fake_get(url, timeout=None):
        calls.append((url, timeout, threading.current_thread().daemon))
        done.set()

    monkeypatch.setattr(data_main.requests, "get", fake_get)
    data_main._schedule_reset_curr_year()
    assert done.wait(5)
    assert calls == [
        (f"{data_main.BACKEND_URL}/v3/data/reset_curr_year", (5, 5), True)
    ]


def test_chain_runs_after_lock_released(pipe, monkeypatch, trailing_open):
    seen = []
    monkeypatch.setattr(
        data_main, "_schedule_reset_curr_year", lambda: seen.append(PIPELINE_LOCK.locked())
    )
    data_main.update_trailing_year(full=True)
    assert seen == [False]


def test_publish_after_close_aborts(pipe, bucket, monkeypatch, trailing_open):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    before = bucket.objects[MANIFEST_OBJECT]

    def close_window_mid_publish(blob):
        if blob.name.startswith("v2/"):
            set_window(monkeypatch, False)

    bucket.before_upload.append(close_window_mid_publish)
    with pytest.raises(tp.TrailingClosed):
        data_main.update_trailing_year(full=True)

    # The manifest was never written, and no snapshot followed.
    assert bucket.objects[MANIFEST_OBJECT] is before
    assert MANIFEST_OBJECT not in bucket.uploads
    assert snapshot.snapshot_key(Y) not in bucket.objects
    status = json.loads(bucket.objects[tp.TRAILING_STATUS_OBJECT])
    assert status["state"] == "error" and "TrailingClosed" in status["error"]
    assert pipe.chains == 0


def test_stamp_after_close_aborts(pipe, bucket, monkeypatch, trailing_open):
    objs = make_source()
    tp.trailing_publish(Y, objs, deepcopy(TEAMS))
    set_window(monkeypatch, False)
    with pytest.raises(tp.TrailingClosed):
        tp.stamp_trailing_bootstrap(Y)
    assert stored(bucket).trailing_year is None


def test_update_manifest_mutate_exception_writes_nothing(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bucket.uploads.clear()

    def boom(m):
        m.blobs["x"] = "v2/x.1"
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        storage.update_manifest(boom)
    assert bucket.uploads == [] and stored(bucket).blobs == {}


def test_trailing_cycle_skipped_when_window_closed(pipe, bucket):
    result = data_main.update_trailing_year(full=True)
    assert result["status"] == "skipped"
    assert pipe.tba_calls == [] and bucket.uploads == []


# ------------------------------------------------------------------- lock


def test_busy_when_locked(pipe, monkeypatch, capsys, trailing_open):
    ran = []
    monkeypatch.setattr(
        data_main, "_update_curr_year_held", lambda p, t: ran.append("curr")
    )
    monkeypatch.setattr(data_main, "_refresh_teams_held", lambda: ran.append("teams"))
    monkeypatch.setattr(sweep, "_revalidate_tba_held", lambda: ran.append("sweep"))

    assert PIPELINE_LOCK.acquire(blocking=False)
    try:
        assert data_main.update_trailing_year(full=True) == {"status": "busy"}
        assert data_main.close_trailing_year() == {"status": "busy"}
        data_main.update_curr_year(partial=True, tba_partial=True)
        assert data_main.refresh_teams() == {"status": "busy"}
        assert sweep.revalidate_tba() == {"status": "busy"}
    finally:
        PIPELINE_LOCK.release()

    assert ran == [] and pipe.tba_calls == []
    out = capsys.readouterr().out
    for name in (
        "update_trailing_year",
        "close_trailing_year",
        "update_curr_year",
        "refresh_teams",
        "revalidate_tba",
    ):
        assert f"PIPELINE BUSY: skipped {name}" in out


def test_sweep_reprocess_chain_runs_under_its_own_lock(monkeypatch):
    """The sweep holds the lock while its reprocess chains the current-year
    cycle and refresh_teams in the same thread: those must run, not skip."""
    ran = []
    monkeypatch.setattr(
        data_main, "_update_curr_year_held", lambda p, t: ran.append("curr")
    )
    monkeypatch.setattr(data_main, "_refresh_teams_held", lambda: ran.append("teams") or {})

    def held():
        data_main.update_curr_year(partial=False, tba_partial=False)
        data_main.refresh_teams()
        return {"status": "success"}

    monkeypatch.setattr(sweep, "_revalidate_tba_held", held)
    assert sweep.revalidate_tba() == {"status": "success"}
    assert ran == ["curr", "teams"]
    assert not PIPELINE_LOCK.locked()


# ----------------------------------------------------------- window close


def bootstrap_open(monkeypatch) -> None:
    set_window(monkeypatch, True)
    data_main.update_trailing_year(full=True)
    set_window(monkeypatch, False)


def test_close_not_needed_while_open(pipe, bucket, trailing_open, capsys):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, trailing_year=Y))
    bucket.uploads.clear()

    assert data_main.close_trailing_year()["status"] == "not_needed"
    assert bucket.uploads == [] and pipe.tba_calls == []
    assert f"TRAILING {Y} CLOSE not_needed secs=" in capsys.readouterr().out


def test_close_not_needed_when_done(pipe, bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={f"parquet/{Y}/x.parquet": "v2/x.1"}))
    bucket.uploads.clear()
    assert data_main.close_trailing_year()["status"] == "not_needed"
    assert bucket.uploads == []


def assert_retired(bucket, rendered_paths) -> Manifest:
    m = stored(bucket)
    assert m.trailing_year is None
    assert [
        k for k in m.blobs
        if tp.year_from_path(k) == Y and not k.startswith("parquet/")
    ] == []
    assert f"parquet/{Y}/team_years.parquet" in m.blobs
    e = HIST_EPOCH + 1
    assert m.hist_epochs == {str(Y): e}
    for path in rendered_paths:
        assert historical_key(e, path) in bucket.objects
    return m


def test_close_retires_year(pipe, bucket, monkeypatch, capsys):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    # Stale hist blobs from before the trailing window (the incident).
    bucket.put(historical_key(HIST_EPOCH, f"events/{Y}"), b"stale")
    bootstrap_open(monkeypatch)
    pipe.source = make_source(casj_score=150)
    pipe.tba_calls.clear()
    pipe.load_teams_calls.clear()
    pipe.chains = 0

    result = data_main.close_trailing_year()

    assert result["status"] == "closed"
    # Fresh TBA, past-year semantics (full, uncached).
    assert pipe.tba_calls == [(Y, False, False)]
    assert pipe.load_teams_calls == [False]
    rendered = storage.render_site_blobs(Y, make_source(casj_score=150), TEAMS)
    assert_retired(bucket, rendered)
    for path, data in rendered.items():
        assert bucket.objects[historical_key(HIST_EPOCH + 1, path)] == data
    assert pipe.chains == 0  # team_years unchanged by a match-score change
    out = capsys.readouterr().out
    assert f"TRAILING {Y} CLOSE closed secs=" in out
    assert f"YEAR EPOCH {Y} advanced epoch={HIST_EPOCH + 1}" in out
    status = json.loads(bucket.objects[tp.TRAILING_STATUS_OBJECT])
    assert status["state"] == "ok" and status["full"] is True

    # Done: the next tick has nothing to do.
    assert data_main.close_trailing_year()["status"] == "not_needed"


def test_close_chains_when_team_years_change(pipe, bucket, monkeypatch):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bootstrap_open(monkeypatch)
    pipe.chains = 0
    pipe.source = make_source(extra_team=True)
    assert data_main.close_trailing_year()["chain"] is True
    assert pipe.chains == 1


def _bd():
    return defaultdict(lambda: None)


def match_dict(key, event, comp_level, winner, time):
    return {
        "event": event, "key": key, "comp_level": comp_level, "set_number": 1,
        "match_number": 1, "status": MatchStatus.COMPLETED, "video": None,
        "red_1": 254, "red_2": 1678, "red_3": 971, "red_dq": "",
        "red_surrogate": "", "blue_1": 118, "blue_2": 148, "blue_3": 604,
        "blue_dq": "", "blue_surrogate": "", "winner": winner, "time": time,
        "predicted_time": None, "red_score": 100, "blue_score": 90,
        "red_score_breakdown": _bd(), "blue_score_breakdown": _bd(),
    }


def event_dict(key, week):
    return {
        "year": Y, "key": key, "name": key, "country": "USA", "state": "CA",
        "district": None, "start_date": f"{Y}-10-01", "end_date": f"{Y}-10-02",
        "time": 100, "type": EventType.REGIONAL, "week": week, "video": None,
    }


def test_close_drops_matchless_event_and_completes_no_elim_event(
    pipe, bucket, monkeypatch
):
    """Window close computes the year the way the sweep later will
    (past-year semantics): a matchless event is Invalid (dropped from
    events/Y, no team events) and an event with quals but no elims is
    Completed, not Upcoming/Ongoing as while the window was open."""
    real_tba = data_tba.process_year
    monkeypatch.setattr(data_main, "process_year_tba", real_tba)
    empty, quals = f"{Y}empty", f"{Y}quals"
    monkeypatch.setattr(data_tba, "get_districts_tba", lambda *a, **k: ([], None))
    monkeypatch.setattr(
        data_tba,
        "get_events_tba",
        lambda *a, **k: ([event_dict(empty, 9), event_dict(quals, 9)], None),
    )
    matches = {
        empty: [],
        quals: [match_dict(f"{quals}_qm1", quals, CompLevel.QUAL, MatchWinner.RED, 110)],
    }
    monkeypatch.setattr(
        data_tba,
        "get_event_matches_tba",
        lambda year, key, time, etag, cache: (matches[key], None),
    )
    monkeypatch.setattr(
        data_tba, "get_event_teams_tba", lambda key, etag, cache: ([254, 1678], None)
    )
    monkeypatch.setattr(data_tba, "get_event_rankings_tba", lambda *a: ({}, None))
    monkeypatch.setattr(data_tba, "get_event_alliances_tba", lambda *a: (({}, {}), None))
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, trailing_year=Y))

    assert data_main.close_trailing_year()["status"] == "closed"

    m = stored(bucket)
    e = max(m.hist_epochs.get(str(Y), 0), m.hist_epoch)
    events = decode(bucket.objects[historical_key(e, f"events/{Y}")])
    listed = {ev["key"]: ev["status"] for ev in events["events"]}
    assert listed == {quals: "Completed"}
    assert historical_key(e, f"event/{empty}") in bucket.objects  # rendered...
    page = decode(bucket.objects[historical_key(e, f"event/{empty}")])
    assert page["team_events"] == []  # ...but with no team events
    assert decode(bucket.objects[historical_key(e, f"event/{quals}")])["event"][
        "status"
    ] == "Completed"


@pytest.mark.parametrize("crash", ["after_parquet", "after_hist_uploads", "after_manifest"])
def test_close_rerun_after_crash_at_each_step(pipe, bucket, monkeypatch, crash):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bucket.put(historical_key(HIST_EPOCH, f"events/{Y}"), b"stale")
    bootstrap_open(monkeypatch)
    pipe.source = make_source(casj_score=150)

    hist_seen = []

    def hook(blob):
        if blob.name.startswith("hist/"):
            hist_seen.append(blob.name)
            if crash == "after_parquet":
                raise RuntimeError("crash after Parquet")
        elif blob.name == MANIFEST_OBJECT and hist_seen and crash == "after_hist_uploads":
            raise RuntimeError("crash after hist uploads")

    bucket.before_upload.append(hook)
    if crash == "after_manifest":
        real = data_main.update_manifest

        def crash_clear(mutate, bucket=None):
            raise RuntimeError("crash after manifest write")

        monkeypatch.setattr(data_main, "update_manifest", crash_clear)

    with pytest.raises(RuntimeError):
        data_main.close_trailing_year()
    bucket.before_upload.clear()
    if crash == "after_manifest":
        monkeypatch.setattr(data_main, "update_manifest", real)
        # publish_past_year's write landed: keys gone, marker still set.
        assert stored(bucket).trailing_year == Y
    else:
        assert stored(bucket).hist_epochs == {}
    status = json.loads(bucket.objects[tp.TRAILING_STATUS_OBJECT])
    assert status["state"] == "error"

    assert data_main.close_trailing_year()["status"] == "closed"
    rendered = storage.render_site_blobs(Y, make_source(casj_score=150), TEAMS)
    assert_retired(bucket, rendered)
    assert data_main.close_trailing_year()["status"] == "not_needed"


# ------------------------------------------------------- reprocess delegate


def test_reprocess_year_of_live_year_delegates(monkeypatch, trailing_open):
    calls = []
    monkeypatch.setattr(
        data_main,
        "update_trailing_year",
        lambda full, chain="http": calls.append((full, chain)) or {},
    )

    def boom(*a, **k):
        raise AssertionError("past-year path must not run for a live year")

    monkeypatch.setattr(data_main, "process_year", boom)
    monkeypatch.setattr(data_main, "update_curr_year", boom)
    monkeypatch.setattr(data_main, "refresh_teams", boom)

    data_main.reprocess_year(Y)
    assert calls == [(True, "inline")]


def test_reprocess_year_delegate_chains_inline(pipe, monkeypatch, trailing_open):
    """A job driver's process exits when reprocess_year returns: the chained
    current-year re-render runs synchronously, never as an HTTP self-call."""
    steps = []
    monkeypatch.setattr(
        data_main,
        "update_curr_year",
        lambda partial, tba_partial: steps.append(
            ("update_curr_year", partial, tba_partial, PIPELINE_LOCK.locked())
        )
        or clear_chain_pending(),
    )
    monkeypatch.setattr(
        data_main, "refresh_teams", lambda: steps.append(("refresh_teams",)) or {}
    )

    def no_http():
        raise AssertionError("no HTTP self-call from a job driver")

    monkeypatch.setattr(data_main, "_schedule_reset_curr_year", no_http)

    data_main.reprocess_year(Y)  # first publish: team_years new
    assert steps == [("update_curr_year", False, False, True), ("refresh_teams",)]

    steps.clear()
    pipe.source = make_source(casj_score=150)  # team_years unchanged
    data_main.reprocess_year(Y)
    assert steps == []

    pipe.source = make_source(casj_score=150, extra_team=True)
    data_main.reprocess_year(Y)
    assert steps == [("update_curr_year", False, False, True), ("refresh_teams",)]


def test_close_chains_inline_when_asked(pipe, bucket, monkeypatch):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bootstrap_open(monkeypatch)
    pipe.chains = 0
    steps = []
    monkeypatch.setattr(
        data_main,
        "update_curr_year",
        lambda partial, tba_partial: steps.append("curr") or clear_chain_pending(),
    )
    monkeypatch.setattr(data_main, "refresh_teams", lambda: steps.append("teams") or {})
    pipe.source = make_source(extra_team=True)
    assert data_main.close_trailing_year(chain="inline")["chain"] is True
    assert steps == ["curr", "teams"] and pipe.chains == 0


def test_bad_chain_value_rejected():
    with pytest.raises(ValueError):
        data_main.update_trailing_year(full=True, chain="later")


def test_disable_gcs_skips_trailing_cycle_and_close(pipe, bucket, monkeypatch, trailing_open):
    monkeypatch.setattr(data_main, "DISABLE_GCS", True)
    result = data_main.update_trailing_year(full=True)
    assert result == {"status": "skipped", "reason": "gcs-disabled", "year": Y}
    assert pipe.tba_calls == [] and bucket.uploads == [] and pipe.chains == 0
    set_window(monkeypatch, False)
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, trailing_year=Y))
    bucket.uploads.clear()
    assert data_main.close_trailing_year()["status"] == "not_needed"
    assert bucket.uploads == [] and stored(bucket).trailing_year == Y


def test_process_year_live_branch_publishes_parquet_and_snapshot(pipe, bucket, trailing_open):
    data_main.process_year(Y, False, False, False, deepcopy(TEAMS), make_source(), {})
    m = stored(bucket)
    assert {f"parquet/{Y}/{t}.parquet" for t in ("team_years", "events", "matches")} <= set(m.blobs)
    assert bucket.uploads.count(MANIFEST_OBJECT) == 1  # one manifest write
    assert snapshot.snapshot_key(Y) in bucket.objects


# ------------------------------------------------------------ chain_pending


def test_chain_pending_set_in_the_trailing_publish_write(pipe, bucket, trailing_open):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    tp.trailing_publish(Y, make_source(), deepcopy(TEAMS))
    publish = manifest_versions(bucket)[-1]
    # The write that changed the team_years digest carries the flag.
    assert f"parquet/{Y}/team_years.parquet" in publish.blobs
    assert publish.chain_pending is True


def test_chain_pending_not_set_when_team_years_unchanged(pipe, bucket, trailing_open):
    tp.trailing_publish(Y, make_source(), deepcopy(TEAMS))
    clear_chain_pending()
    tp.trailing_publish(Y, make_source(casj_score=150), deepcopy(TEAMS))
    assert stored(bucket).chain_pending is False


def test_chain_pending_set_in_the_close_parquet_write(pipe, bucket, monkeypatch):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bootstrap_open(monkeypatch)  # chains (fake) and clears
    assert stored(bucket).chain_pending is False
    pipe.source = make_source(extra_team=True)
    n = len(manifest_versions(bucket))
    data_main.close_trailing_year()
    parquet_write = manifest_versions(bucket)[n]  # close's first manifest write
    assert parquet_write.chain_pending is True
    assert parquet_write.trailing_year == Y  # before the marker clear


def test_crash_before_chain_then_gate_still_chains(pipe, bucket, monkeypatch, trailing_open):
    """The process dies after the trailing publish and before the chained
    request: the flag in GCS is what makes the next gate tick chain."""
    import src.data.router as dr
    from fastapi import BackgroundTasks

    def die():
        raise SystemExit("process killed before the chain was sent")

    monkeypatch.setattr(data_main, "_schedule_reset_curr_year", die)
    with pytest.raises(SystemExit):
        data_main.update_trailing_year(full=True)
    assert stored(bucket).chain_pending is True

    # Re-run: a quiet partial cycle still reports (and HTTP-chains) the flag.
    sent = []
    monkeypatch.setattr(data_main, "_schedule_reset_curr_year", lambda: sent.append(1))
    assert data_main.update_trailing_year(full=False)["chain"] is True
    assert sent == [1]

    # The gate, with no trailing action, schedules the chain from the flag.
    tasks = BackgroundTasks()
    assert dr._schedule_chain(tasks, None) is True
    assert [t.func for t in tasks.tasks] == [dr.reset_curr_year_background]


def test_full_curr_year_publish_clears_chain_pending(bucket, monkeypatch):
    from src.constants import CURR_YEAR

    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, chain_pending=True))
    monkeypatch.setattr(data_main, "DISABLE_GCS", False)
    monkeypatch.setattr(data_main, "get_team_event_teams_db", lambda: [])
    monkeypatch.setattr(storage, "get_team_years_db", lambda **kw: [])
    monkeypatch.setattr(storage, "get_events_db", lambda **kw: [])
    objs = (Year(year=CURR_YEAR), {}, {}, {}, {}, {})
    data_main.publish_curr_year(objs, [])
    assert stored(bucket).chain_pending is False


def test_partial_curr_year_publish_keeps_chain_pending(bucket, monkeypatch):
    from src.constants import CURR_YEAR

    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, chain_pending=True))
    monkeypatch.setattr(storage, "get_team_years_db", lambda **kw: [])
    monkeypatch.setattr(storage, "get_events_db", lambda **kw: [])
    objs = (Year(year=CURR_YEAR), {}, {}, {}, {}, {})
    storage.write_objs(objs, deepcopy(objs), [], None, [])
    # A partial cycle does not re-render every team page: the flag stays.
    assert stored(bucket).chain_pending is True


def test_inline_chain_clears_chain_pending(pipe, bucket, monkeypatch, trailing_open):
    """reprocess_year's inline chain runs the real full current-year cycle,
    whose publish clears the flag."""
    from src.constants import CURR_YEAR

    seen = []

    def fake_held(partial, tba_partial):
        seen.append((partial, tba_partial))
        data_main.publish_curr_year((Year(year=CURR_YEAR), {}, {}, {}, {}, {}), [])

    monkeypatch.setattr(data_main, "_update_curr_year_held", fake_held)
    monkeypatch.setattr(data_main, "_refresh_teams_held", lambda: {})
    monkeypatch.setattr(data_main, "get_team_event_teams_db", lambda: [])
    monkeypatch.setattr(storage, "get_team_years_db", lambda **kw: [])
    monkeypatch.setattr(storage, "get_events_db", lambda **kw: [])

    result = data_main.update_trailing_year(full=True, chain="inline")
    assert result["chain"] is True  # the flag the trailing publish set
    assert seen == [(False, False)]
    assert stored(bucket).chain_pending is False
    assert pipe.chains == 0  # no HTTP self-call
