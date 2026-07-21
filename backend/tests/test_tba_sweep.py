"""Daily historical-year revalidation sweep tests (TBA cache design §2.3).

Covers: the round-robin cursor (2021 skip, wraparound, persistence across
processes via the global archive's manifest), serial conditional-GET request
shapes, 304 vs 200 handling, the changed-year reprocess trigger (exactly
once; deferred etag recording so a failed reprocess is retried), and the
cold-year skip. No real TBA or GCS access.
"""

import os
import pickle

import pytest

import src.data.main as data_main
import src.data.sweep as sweep
import src.tba.cache as tba_cache
import src.tba.main as tba_main
from src.constants import CURR_YEAR

OLD_TS = "2020-01-01T00:00:00Z"


@pytest.fixture
def gcs(monkeypatch, tmp_path):
    """Fake GCS archive store + isolated local cache dir."""
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path / "cache"))
    store = {}
    monkeypatch.setattr(tba_cache, "_download_archive", lambda name: store.get(name))
    monkeypatch.setattr(
        tba_cache, "_upload_archive", lambda name, data: store.__setitem__(name, data)
    )
    return store


@pytest.fixture
def reprocess_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(data_main, "reprocess_year", lambda year: calls.append(year))
    return calls


@pytest.fixture
def no_tba(monkeypatch):
    def boom(url, etag=None):
        raise AssertionError("unexpected TBA request: " + url)

    monkeypatch.setattr(tba_main, "_get_tba", boom)


class FakeTBA:
    """Recording _get_tba double: responses[url] is "304", "error", or a
    (data, etag) tuple for a 200."""

    def __init__(self, monkeypatch, responses=None):
        self.calls = []
        self.responses = responses or {}
        monkeypatch.setattr(tba_main, "_get_tba", self)

    def __call__(self, url, etag=None):
        self.calls.append((url, etag))
        resp = self.responses.get(url, "304")
        if resp == "304":
            return True, etag
        if resp == "error":
            return False, None
        return resp


def _seed_path(url, data=("cached",)):
    path = os.path.join(tba_main.TBA_CACHE_DIR, url)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "data.p"), "wb") as f:
        pickle.dump(list(data), f)
    tba_cache._manifest[url] = {"etag": 'W/"' + url + '"', "last_validated": OLD_TS}


def _seed_year(year):
    urls = [
        "events/%d" % year,
        "event/%daaa/matches" % year,
        "event/%daaa/rankings" % year,
    ]
    for url in urls:
        _seed_path(url)
    return urls


def _read_pickle(url):
    with open(os.path.join(tba_main.TBA_CACHE_DIR, url, "data.p"), "rb") as f:
        return pickle.load(f)


"""
1. Cursor round-robin: 2021 skip, wraparound, restart on garbage
"""


def test_next_sweep_year_round_robin():
    assert sweep.next_sweep_year(2002) == 2003
    assert sweep.next_sweep_year(2020) == 2022  # 2021 never existed
    assert sweep.next_sweep_year(CURR_YEAR - 2) == CURR_YEAR - 1
    assert sweep.next_sweep_year(CURR_YEAR - 1) == 2002  # wraparound


def test_normalize_cursor():
    assert sweep.normalize_cursor(None) == 2002  # first ever run
    assert sweep.normalize_cursor(1999) == 2002
    assert sweep.normalize_cursor(CURR_YEAR) == 2002  # never the current year
    assert sweep.normalize_cursor(2021) == 2022
    assert sweep.normalize_cursor(2015) == 2015


"""
2. Cold-year skip: no manifest -> no TBA requests, cursor still advances
"""


def test_cold_year_skips_without_tba_traffic(gcs, no_tba, reprocess_calls):
    result = sweep.revalidate_tba()

    assert result["status"] == "skipped"  # nothing to revalidate yet
    assert result["year"] == 2002
    assert result["next_year"] == 2003
    assert result["reprocessed"] is False
    assert reprocess_calls == []


def test_cursor_persists_across_processes(gcs, no_tba):
    sweep.revalidate_tba()  # 2002 -> cursor 2003, persisted to GCS
    assert "global" in gcs

    tba_cache.reset_state()  # simulate a process restart

    result = sweep.revalidate_tba()

    assert result["year"] == 2003  # cursor came back from the global archive
    assert result["next_year"] == 2004


def test_cursor_skip_and_wraparound_in_sweep(gcs, no_tba):
    tba_cache.set_sweep_cursor(2020)
    assert sweep.revalidate_tba()["next_year"] == 2022  # 2021 skipped

    tba_cache.reset_state()
    tba_cache.set_sweep_cursor(CURR_YEAR - 1)
    result = sweep.revalidate_tba()
    assert result["year"] == CURR_YEAR - 1
    assert result["next_year"] == 2002  # wraps to the oldest year


"""
3. Serial conditional GETs; 304 -> last_validated only, no reprocess
"""


def test_all_304_refreshes_last_validated_only(gcs, reprocess_calls, monkeypatch):
    urls = _seed_year(2019)
    tba_cache.set_sweep_cursor(2019)
    fake = FakeTBA(monkeypatch)

    result = sweep.revalidate_tba()

    # One conditional GET per manifest-tracked path, serially, each carrying
    # the stored etag as If-None-Match.
    assert fake.calls == [(url, 'W/"' + url + '"') for url in sorted(urls)]
    for url in urls:
        assert tba_cache._manifest[url]["last_validated"] != OLD_TS
        assert tba_cache.stored_etag(url) == 'W/"' + url + '"'  # unchanged
        assert _read_pickle(url) == ["cached"]  # pickle untouched
    assert result["status"] == "success"
    assert result["changed"] == 0
    assert result["reprocessed"] is False
    assert reprocess_calls == []  # unchanged year: no reprocess
    assert "2019" not in tba_cache._dirty  # no content change


"""
4. 200 -> pickle + etag rewritten, year marked changed, reprocess exactly once
"""


def test_200_rewrites_pickle_and_reprocesses_once(gcs, reprocess_calls, monkeypatch):
    _seed_year(2019)
    tba_cache.set_sweep_cursor(2019)
    changed_url = "event/2019aaa/matches"
    fake = FakeTBA(monkeypatch, {changed_url: (["fresh"], 'W/"new"')})

    result = sweep.revalidate_tba()

    assert _read_pickle(changed_url) == ["fresh"]
    assert tba_cache.stored_etag(changed_url) == 'W/"new"'
    assert reprocess_calls == [2019]  # exactly once
    assert result["status"] == "success"
    assert result["changed"] == 1
    assert result["changed_paths"] == [changed_url]
    assert result["reprocessed"] is True
    # The rewritten year archive was persisted with the new pickle + etag.
    assert "2019" in gcs
    out = os.path.join(tba_main.TBA_CACHE_DIR, "unpacked")
    manifest = tba_cache.extract_archive(gcs["2019"], out)
    assert manifest[changed_url]["etag"] == 'W/"new"'
    with open(os.path.join(out, changed_url, "data.p"), "rb") as f:
        assert pickle.load(f) == ["fresh"]
    assert len(fake.calls) == 3  # still one serial GET per path


def test_failed_reprocess_keeps_old_etag_for_retry(gcs, monkeypatch):
    """Revalidation must never update stored etag state while published data
    is stale: if the reprocess fails, the old etag stays so the next visit of
    this year re-detects the change and retries."""
    _seed_year(2019)
    tba_cache.set_sweep_cursor(2019)
    changed_url = "event/2019aaa/matches"
    FakeTBA(monkeypatch, {changed_url: (["fresh"], 'W/"new"')})

    def boom(year):
        raise RuntimeError("reprocess failed")

    monkeypatch.setattr(data_main, "reprocess_year", boom)

    result = sweep.revalidate_tba()

    assert result["status"] == "error"  # loud
    assert "reprocess:2019" in result["errors"]
    assert result["reprocessed"] is False
    assert tba_cache.stored_etag(changed_url) == 'W/"' + changed_url + '"'
    assert "2019" not in tba_cache._dirty  # stored archive keeps old etags
    # The cursor still advances: one year per day, always.
    assert result["next_year"] == 2020


def test_tba_error_is_loud_but_never_corrupts(gcs, reprocess_calls, monkeypatch):
    _seed_year(2019)
    tba_cache.set_sweep_cursor(2019)
    bad_url = "event/2019aaa/rankings"
    FakeTBA(monkeypatch, {bad_url: "error"})

    result = sweep.revalidate_tba()

    assert result["status"] == "error"
    assert result["errors"] == [bad_url]
    assert tba_cache.stored_etag(bad_url) == 'W/"' + bad_url + '"'
    assert _read_pickle(bad_url) == ["cached"]
    assert reprocess_calls == []  # errors alone never trigger a reprocess


"""
5. Cursor storage plumbing (reserved __sweep__ manifest key)
"""


def test_sweep_cursor_round_trips_through_global_archive(gcs, tmp_path):
    tba_cache.set_sweep_cursor(2013)
    raw = tba_cache.pack_archive("global", tba_main.TBA_CACHE_DIR)
    tba_cache.reset_state()
    assert tba_cache.sweep_cursor() is None

    gcs["global"] = raw
    tba_cache.hydrate()

    assert tba_cache.sweep_cursor() == 2013
    assert "__sweep__" not in tba_cache._manifest  # reserved key stripped


def test_set_sweep_cursor_marks_global_dirty(gcs):
    tba_cache.set_sweep_cursor(2013)
    assert "global" in tba_cache._dirty  # persist() will upload it


"""
6. Endpoint wiring
"""


def test_router_exposes_revalidate_tba():
    from src.data.router import data_router

    assert any(
        getattr(route, "path", None) == "/revalidate_tba"
        for route in data_router.routes
    )
