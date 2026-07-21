"""GCS-persisted TBA cache tests (design §2.1, §2.2, §2.4, §3.3).

Covers: year attribution of cache keys, archive pack/extract round-trip,
manifest recording in get_tba (200 / 304 / cache-hit), the hydrate-once
guard, dirty-only persist, and GCS-failure honesty (the pipeline must be
indifferent to cache subsystem failure). No real GCS or TBA access.
"""

import io
import os
import pickle
import tarfile

import pytest

import src.data.tba as data_tba
import src.tba.cache as tba_cache
import src.tba.main as tba_main


def _write_pickle(root, url, data):
    path = os.path.join(root, url)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "data.p"), "wb") as f:
        pickle.dump(data, f)


def _read_pickle(root, url):
    with open(os.path.join(root, url, "data.p"), "rb") as f:
        return pickle.load(f)


"""
1. Year attribution (design §2.1)
"""


@pytest.mark.parametrize(
    "url,expected",
    [
        ("events/2025", "2025"),
        ("districts/2024", "2024"),
        ("event/2025iri/matches", "2025"),
        ("event/2026cc/teams/simple", "2026"),
        ("event/2025iri/rankings", "2025"),
        ("event/2025iri/alliances", "2025"),
        ("district/2023fim/teams", "2023"),
        ("teams/0", "global"),
        ("teams/49", "global"),
    ],
)
def test_archive_attribution(url, expected):
    assert tba_cache.archive_for(url) == expected


def test_unknown_key_falls_back_to_global(capsys):
    assert tba_cache.archive_for("status/whatever") == "global"
    assert "status/whatever" in capsys.readouterr().out


"""
2. Archive pack/extract round-trip
"""


def test_archive_pack_extract_round_trip(tmp_path):
    src_root = str(tmp_path / "src")
    _write_pickle(src_root, "events/2025", ["ev-2025"])
    _write_pickle(src_root, "event/2025iri/matches", ["m1", "m2"])
    _write_pickle(src_root, "events/2024", ["ev-2024"])  # other year: excluded
    _write_pickle(src_root, "teams/0", ["t0"])  # global: excluded
    tba_cache._manifest.update(
        {
            "events/2025": {"etag": "E1", "last_validated": "2026-07-20T14:00:00Z"},
            "teams/0": {"etag": "E2", "last_validated": "2026-07-20T14:00:00Z"},
        }
    )

    raw = tba_cache.pack_archive("2025", src_root)

    dest_root = str(tmp_path / "dest")
    manifest = tba_cache.extract_archive(raw, dest_root)

    assert _read_pickle(dest_root, "events/2025") == ["ev-2025"]
    assert _read_pickle(dest_root, "event/2025iri/matches") == ["m1", "m2"]
    assert not os.path.exists(os.path.join(dest_root, "events/2024"))
    assert not os.path.exists(os.path.join(dest_root, "teams/0"))
    # manifest carries only this archive's keys
    assert manifest == {
        "events/2025": {"etag": "E1", "last_validated": "2026-07-20T14:00:00Z"}
    }


def _tar_with(extra_members):
    """A tar.gz holding one benign pickle plus the given hostile TarInfo
    members (crafted in-test; tarfile.add would refuse to create them)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = pickle.dumps(["ok"])
        info = tarfile.TarInfo("events/2025/data.p")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        for info, payload in extra_members:
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload) if payload else None)
    return buf.getvalue()


def test_extract_skips_parent_traversal_members(tmp_path):
    raw = _tar_with(
        [
            (tarfile.TarInfo("../escape"), b"evil"),
            (tarfile.TarInfo("events/../../escape2"), b"evil"),
        ]
    )
    dest = tmp_path / "dest"

    tba_cache.extract_archive(raw, str(dest))

    assert _read_pickle(str(dest), "events/2025") == ["ok"]  # benign sibling
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "escape2").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["dest"]


def test_extract_skips_absolute_path_member(tmp_path):
    outside = tmp_path / "outside" / "evil"
    raw = _tar_with([(tarfile.TarInfo(str(outside)), b"evil")])
    dest = tmp_path / "dest"

    tba_cache.extract_archive(raw, str(dest))

    assert _read_pickle(str(dest), "events/2025") == ["ok"]  # benign sibling
    assert not outside.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["dest"]


def test_extract_skips_symlink_member(tmp_path):
    link = tarfile.TarInfo("events/2025/link")
    link.type = tarfile.SYMTYPE
    link.linkname = str(tmp_path / "target")
    raw = _tar_with([(link, b"")])
    dest = tmp_path / "dest"

    tba_cache.extract_archive(raw, str(dest))

    assert _read_pickle(str(dest), "events/2025") == ["ok"]  # benign sibling
    # No symlink (or anything else) at the member's path.
    assert not os.path.lexists(os.path.join(str(dest), "events/2025/link"))


"""
3. get_tba manifest integration (design §2.2, §3.3)
"""


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    return str(tmp_path)


def test_get_tba_records_manifest_on_200(cache_dir, monkeypatch):
    url = "event/2026iri/matches"
    monkeypatch.setattr(tba_main, "_get_tba", lambda u, etag=None: ([1], 'W/"a"'))

    data, etag = tba_main.get_tba(url, etag=None, cache=False)

    assert data == [1] and etag == 'W/"a"'
    assert tba_cache.stored_etag(url) == 'W/"a"'
    assert tba_cache._manifest[url]["last_validated"].endswith("Z")
    assert "2026" in tba_cache._dirty


def test_get_tba_dump_failure_leaves_manifest_unchanged(cache_dir, monkeypatch, capsys):
    """Failed pickle write must not record the new etag: the pickle still
    holds the OLD data, and a manifest claiming the new etag for it would
    make later manifest-backed 304s serve stale data."""
    url = "event/2026iri/matches"
    _write_pickle(cache_dir, url, ["old"])
    tba_cache._manifest[url] = {
        "etag": 'W/"old"',
        "last_validated": "2020-01-01T00:00:00Z",
    }
    pickle_path = os.path.join(cache_dir, url, "data.p")
    os.chmod(pickle_path, 0o444)  # the pickle rewrite will fail
    monkeypatch.setattr(tba_main, "_get_tba", lambda u, etag=None: (["new"], 'W/"new"'))

    try:
        data, etag = tba_main.get_tba(url, etag=None, cache=False)
    finally:
        os.chmod(pickle_path, 0o644)

    assert data == ["new"] and etag == 'W/"new"'  # DB/objs[5] flow untouched
    assert tba_cache._manifest[url] == {
        "etag": 'W/"old"',
        "last_validated": "2020-01-01T00:00:00Z",
    }
    assert not tba_cache._dirty
    assert "WARNING" in capsys.readouterr().out


def test_get_tba_no_pickle_no_etag_is_unconditional(cache_dir, monkeypatch):
    """etag=None and no cached pickle: nothing could satisfy a 304, so the
    request must carry no If-None-Match header at all."""
    url = "event/2026iri/matches"

    class FakeSession:
        calls = []
        headers = {}

        def get(self, u, headers=None, **kwargs):
            self.calls.append(dict(headers or {}))

            class Resp:
                status_code = 200
                headers = {"ETag": 'W/"a"'}

                def json(self):
                    return [1]

            return Resp()

    monkeypatch.setattr(tba_main, "session", FakeSession())

    data, etag = tba_main.get_tba(url, etag=None, cache=False)

    assert data == [1] and etag == 'W/"a"'
    assert FakeSession.calls == [{}]  # no If-None-Match sent


def test_get_tba_manifest_etag_304_serves_pickle_and_refreshes(cache_dir, monkeypatch):
    url = "event/2026iri/matches"
    _write_pickle(cache_dir, url, [42])
    tba_cache._manifest[url] = {
        "etag": 'W/"a"',
        "last_validated": "2020-01-01T00:00:00Z",
    }

    sent = {}

    def fake(u, etag=None):
        sent["etag"] = etag
        return True, etag  # 304

    monkeypatch.setattr(tba_main, "_get_tba", fake)

    data, etag = tba_main.get_tba(url, etag=None, cache=False)

    assert sent["etag"] == 'W/"a"'  # manifest-backed conditional GET
    assert data == [42]  # the validated pickle, not a bare True
    assert etag == 'W/"a"'
    assert tba_cache._manifest[url]["last_validated"] != "2020-01-01T00:00:00Z"
    # A 304 changes only last_validated: metadata-dirty, not content-dirty.
    assert "2026" in tba_cache._meta_dirty
    assert "2026" not in tba_cache._dirty


def test_get_tba_cache_hit_touches_nothing(cache_dir, monkeypatch):
    url = "events/2026"
    _write_pickle(cache_dir, url, [7])
    tba_cache._manifest[url] = {
        "etag": 'W/"a"',
        "last_validated": "2020-01-01T00:00:00Z",
    }

    def boom(u, etag=None):
        raise AssertionError("network hit on cache hit")

    monkeypatch.setattr(tba_main, "_get_tba", boom)

    data, etag = tba_main.get_tba(url, cache=True)

    assert data == [7] and etag is None
    assert tba_cache._manifest[url]["last_validated"] == "2020-01-01T00:00:00Z"
    assert not tba_cache._dirty
    assert not tba_cache._meta_dirty


def test_get_tba_explicit_etag_flow_unchanged(cache_dir, monkeypatch):
    """Explicit-etag callers (partial cycles, check_year_partial) keep today's
    bool/etag contract, and a 304 against a foreign etag must not claim our
    stored pickle was validated."""
    url = "event/2026iri/matches"
    _write_pickle(cache_dir, url, [42])
    tba_cache._manifest[url] = {
        "etag": 'W/"stored"',
        "last_validated": "2020-01-01T00:00:00Z",
    }

    sent = {}

    def fake(u, etag=None):
        sent["etag"] = etag
        return True, etag  # 304

    monkeypatch.setattr(tba_main, "_get_tba", fake)

    data, etag = tba_main.get_tba(url, etag='W/"db"', cache=False)

    assert sent["etag"] == 'W/"db"'  # explicit etag wins over manifest
    assert data is True and etag == 'W/"db"'
    assert tba_cache._manifest[url]["last_validated"] == "2020-01-01T00:00:00Z"


def test_get_tba_explicit_etag_matching_stored_refreshes(cache_dir, monkeypatch):
    url = "event/2026iri/matches"
    _write_pickle(cache_dir, url, [42])
    tba_cache._manifest[url] = {
        "etag": 'W/"a"',
        "last_validated": "2020-01-01T00:00:00Z",
    }
    monkeypatch.setattr(tba_main, "_get_tba", lambda u, etag=None: (True, etag))

    data, etag = tba_main.get_tba(url, etag='W/"a"', cache=False)

    assert data is True and etag == 'W/"a"'
    assert tba_cache._manifest[url]["last_validated"] != "2020-01-01T00:00:00Z"


"""
4. Hydrate (design §2.2): once per process, zero TBA requests
"""


def test_hydrate_downloads_each_archive_once(monkeypatch, tmp_path):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(tba_cache, "_download_archive", lambda name: calls.append(name))
    tba_cache.hydrate(2025)
    tba_cache.hydrate(2025)
    tba_cache.hydrate()  # global-only entry point
    assert calls == ["global", "2025"]


def test_hydrate_extracts_archive_and_merges_manifest(tmp_path, monkeypatch):
    packed_root = str(tmp_path / "packed")
    _write_pickle(packed_root, "events/2025", ["ev"])
    tba_cache._manifest["events/2025"] = {
        "etag": "E1",
        "last_validated": "2026-07-20T14:00:00Z",
    }
    raw = tba_cache.pack_archive("2025", packed_root)
    tba_cache.reset_state()

    cache_root = tmp_path / "cache"
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(
        tba_cache,
        "_download_archive",
        lambda name: raw if name == "2025" else None,
    )

    tba_cache.hydrate(2025)

    assert (cache_root / "events" / "2025" / "data.p").exists()
    assert tba_cache.stored_etag("events/2025") == "E1"
    assert not tba_cache._dirty  # hydration alone dirties nothing


def test_hydrate_missing_archive_is_cold_start(tmp_path, monkeypatch):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tba_cache, "_download_archive", lambda name: None)
    tba_cache.hydrate(2025)  # must not raise
    assert "2025" not in tba_cache._blocked  # missing != failed: may persist


"""
5. Persist (design §2.2): dirty archives only, atomic per-archive upload
"""


def test_persist_uploads_only_dirty_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    _write_pickle(str(tmp_path), "events/2025", [1])
    _write_pickle(str(tmp_path), "teams/0", [2])  # global exists but is clean
    tba_cache._manifest["events/2025"] = {
        "etag": "E1",
        "last_validated": "2026-07-20T14:00:00Z",
    }
    tba_cache._dirty.add("2025")

    uploads = {}
    monkeypatch.setattr(
        tba_cache,
        "_upload_archive",
        lambda name, data: uploads.__setitem__(name, data),
    )

    tba_cache.persist()

    assert set(uploads) == {"2025"}
    assert not tba_cache._dirty  # cleared after successful upload
    manifest = tba_cache.extract_archive(uploads["2025"], str(tmp_path / "out"))
    assert "events/2025" in manifest
    assert _read_pickle(str(tmp_path / "out"), "events/2025") == [1]


"""
6. Failure honesty: cache subsystem failure never fails the pipeline
"""


def test_hydrate_failure_blocks_persist_but_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))

    def boom(name):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(tba_cache, "_download_archive", boom)
    tba_cache.hydrate(2025)  # must not raise

    tba_cache._dirty.update({"2025", "global"})

    def boom_up(name, data):
        raise AssertionError("must not upload an archive whose hydrate failed")

    monkeypatch.setattr(tba_cache, "_upload_archive", boom_up)
    tba_cache.persist()  # must not raise

    assert "WARNING" in capsys.readouterr().out


def test_persist_upload_failure_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    _write_pickle(str(tmp_path), "events/2025", [1])
    tba_cache._dirty.add("2025")

    def boom(name, data):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(tba_cache, "_upload_archive", boom)
    tba_cache.persist()  # must not raise

    assert "2025" in tba_cache._dirty  # stays dirty for a later retry
    assert "WARNING" in capsys.readouterr().out


"""
7. Metadata-only dirty debounce (design §2.2 refinement): last_validated-only
   changes re-upload an archive at most once per 6h; content changes always
   upload. last_validated is an optimization hint, so deferring it only costs
   extra 304s, never correctness.
"""


def _hours_ago(hours):
    from datetime import datetime, timedelta, timezone

    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _persist_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    _write_pickle(str(tmp_path), "events/2025", [1])
    tba_cache._manifest["events/2025"] = {
        "etag": "E1",
        "last_validated": _hours_ago(0),
    }
    uploads = {}
    monkeypatch.setattr(
        tba_cache,
        "_upload_archive",
        lambda name, data: uploads.__setitem__(name, data),
    )
    return uploads


def test_meta_only_persist_debounced_within_interval(tmp_path, monkeypatch):
    uploads = _persist_setup(tmp_path, monkeypatch)
    tba_cache._meta_dirty.add("2025")
    stamp = _hours_ago(1)
    tba_cache._meta_persisted["2025"] = stamp

    tba_cache.persist()

    assert not uploads  # debounced
    assert "2025" in tba_cache._meta_dirty  # retained for a later cycle
    assert tba_cache._meta_persisted["2025"] == stamp


def test_meta_only_persist_uploads_after_interval(tmp_path, monkeypatch):
    uploads = _persist_setup(tmp_path, monkeypatch)
    tba_cache._meta_dirty.add("2025")
    old_stamp = _hours_ago(7)
    tba_cache._meta_persisted["2025"] = old_stamp

    tba_cache.persist()

    assert set(uploads) == {"2025"}
    assert not tba_cache._meta_dirty
    # The stamp refreshed and travels inside the archive's manifest.
    manifest = tba_cache.extract_archive(uploads["2025"], str(tmp_path / "out"))
    assert manifest["__archive__"]["meta_persisted"] > old_stamp
    assert "events/2025" in manifest


def test_meta_only_persist_uploads_when_never_persisted(tmp_path, monkeypatch):
    uploads = _persist_setup(tmp_path, monkeypatch)
    tba_cache._meta_dirty.add("2025")

    tba_cache.persist()

    assert set(uploads) == {"2025"}
    assert "2025" in tba_cache._meta_persisted


def test_content_dirty_persist_ignores_debounce(tmp_path, monkeypatch):
    uploads = _persist_setup(tmp_path, monkeypatch)
    tba_cache._dirty.add("2025")
    tba_cache._meta_persisted["2025"] = _hours_ago(0)  # just persisted

    tba_cache.persist()

    assert set(uploads) == {"2025"}
    assert not tba_cache._dirty


def test_persist_failure_restores_meta_stamp(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    _write_pickle(str(tmp_path), "events/2025", [1])
    tba_cache._meta_dirty.add("2025")
    old_stamp = _hours_ago(7)
    tba_cache._meta_persisted["2025"] = old_stamp

    def boom(name, data):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(tba_cache, "_upload_archive", boom)
    tba_cache.persist()  # must not raise

    # The stamp rolls back so the retry is not debounced for 6h.
    assert tba_cache._meta_persisted["2025"] == old_stamp
    assert "2025" in tba_cache._meta_dirty
    assert "WARNING" in capsys.readouterr().out


def test_hydrate_restores_meta_stamp_and_keeps_it_out_of_manifest(
    tmp_path, monkeypatch
):
    packed_root = str(tmp_path / "packed")
    _write_pickle(packed_root, "events/2025", ["ev"])
    tba_cache._manifest["events/2025"] = {
        "etag": "E1",
        "last_validated": _hours_ago(0),
    }
    stamp = _hours_ago(2)
    tba_cache._meta_persisted["2025"] = stamp
    raw = tba_cache.pack_archive("2025", packed_root)
    tba_cache.reset_state()

    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        tba_cache,
        "_download_archive",
        lambda name: raw if name == "2025" else None,
    )
    tba_cache.hydrate(2025)

    assert tba_cache._meta_persisted["2025"] == stamp
    assert "__archive__" not in tba_cache._manifest  # reserved key stripped


"""
8. Pipeline wiring: load_teams hydrates the global archive
"""


def test_load_teams_hydrates_global(monkeypatch):
    called = []
    monkeypatch.setattr(tba_cache, "hydrate", lambda year=None: called.append(year))
    monkeypatch.setattr(data_tba, "get_teams_tba", lambda cache=True: [])

    assert data_tba.load_teams() == []
    assert called == [None]
