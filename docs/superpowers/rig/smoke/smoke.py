#!/usr/bin/env python3
"""Shared rig smoke suite — fast pass/fail, stdlib only (no test framework).

Point it at any environment via flags. Non-zero exit if any check fails.

    python3 smoke.py \
        --base-url http://127.0.0.1:8000 \
        --data-url http://127.0.0.1:8001 \
        --gcs http://localhost:4443 \
        --bucket site_dev_v1 \
        --year 2026 \
        [--run-update]      # flag-gated: trigger one cycle, assert blobs advanced

Checks:
  1. liveness            /  and /info  -> 200
  2. db-backed reads     /v3/team/254 (team==254 + EPA fields),
                         /v3/site/team_years/{year} (non-empty)
  3. blob reads          teams/all, team_years/{year}, one event/{key}
                         (fetch, zlib-decompress, parse, non-trivial)
  4. consistency probe   a sampled team's EPA in an event/{key} BLOB matches
                         the same team_event EPA from the DB-backed site API
                         (regression guard for the stale-event-blob bug).
                         Also: team_years BLOB epa == team_years API epa.
                         NB: static seasons have no "upcoming, no matches"
                         events, so event-epa == year-epa only holds for a
                         constructed fixture; blob-vs-API is the equivalent
                         guard that works on any data and catches the same class.
  5. --run-update        trigger a cycle on --data-url, then assert the publish
                         landed via EITHER:
                           (a) legacy: team_years/{year} blob generation advanced
                           (b) manifest: manifest.json exists and advanced
                               (object generation changed, or content changed).
                         (b) exists because Track 2's manifest-based copy-on-
                         write publishing intentionally does NOT re-upload
                         unchanged blobs each cycle; only the manifest advances.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
import zlib

# Send a browser-like User-Agent on every request. Cloudflare (which fronts the
# staging mirror) returns 403 to the default Python-urllib UA as suspected-bot
# traffic, which otherwise fails every https check against the live mirror.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) statbotics-smoke/1.0"
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", _UA)]
urllib.request.install_opener(_opener)

TOL = 0.5  # EPA point tolerance for blob-vs-API equality

_results = []


def check(name, fn):
    try:
        detail = fn()
        _results.append((True, name, detail))
        print(f"  PASS  {name}  {detail or ''}")
    except Exception as e:  # noqa: BLE001
        _results.append((False, name, str(e)))
        print(f"  FAIL  {name}  {e}")


def http_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        assert r.status == 200, f"HTTP {r.status} for {url}"
        return json.loads(r.read())


def http_status(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.status


def gcs_media(gcs, bucket, name):
    url = f"{gcs}/storage/v1/b/{bucket}/o/{urllib.parse.quote(name, safe='')}?alt=media"
    with urllib.request.urlopen(url, timeout=60) as r:
        assert r.status == 200, f"HTTP {r.status} for blob {name}"
        return json.loads(zlib.decompress(r.read()))


def gcs_meta(gcs, bucket, name):
    url = f"{gcs}/storage/v1/b/{bucket}/o/{urllib.parse.quote(name, safe='')}"
    return http_json(url)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--data-url", default="http://127.0.0.1:8001")
    p.add_argument("--gcs", default="http://localhost:4443")
    p.add_argument("--bucket", default="site_dev_v1")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--run-update", action="store_true")
    a = p.parse_args()

    print(f"Smoke suite -> base={a.base_url} gcs={a.gcs}/{a.bucket} year={a.year}")

    # 1. liveness
    print("[1] liveness")
    check("GET /", lambda: _eq(http_json(a.base_url + "/"), {"Hello": "World"}))
    check("GET /info 200", lambda: _is(http_status(a.base_url + "/info"), 200))

    # 2. db-backed reads
    print("[2] db-backed reads")

    def _team254():
        d = http_json(a.base_url + "/v3/team/254")
        assert d["team"] == 254, f"team != 254: {d.get('team')}"
        assert "norm_epa" in d and d["norm_epa"].get("current") is not None, "no EPA"
        return f"254={d['name']} norm={d['norm_epa']['current']}"

    check("/v3/team/254 sane", _team254)

    def _team_years():
        d = http_json(f"{a.base_url}/v3/site/team_years/{a.year}")
        tys = d["team_years"] if isinstance(d, dict) else d
        assert len(tys) > 100, f"only {len(tys)} team_years"
        return f"{len(tys)} team_years"

    check(f"/v3/site/team_years/{a.year} non-empty", _team_years)

    # 3. blob reads
    print("[3] blob reads")
    check(
        "blob teams/all",
        lambda: _nonempty(gcs_media(a.gcs, a.bucket, "teams/all"), "teams/all"),
    )

    def _blob_ty():
        d = gcs_media(a.gcs, a.bucket, f"team_years/{a.year}")
        tys = d["team_years"]
        assert len(tys) > 100, f"only {len(tys)}"
        return f"{len(tys)} team_years"

    check(f"blob team_years/{a.year}", _blob_ty)

    # discover an event key from the events/{year} blob
    event_key = _discover_event(a)

    def _blob_event():
        d = gcs_media(a.gcs, a.bucket, f"event/{event_key}")
        assert d["team_events"], "no team_events in event blob"
        return f"{event_key}: {len(d['team_events'])} team_events"

    check(f"blob event/{event_key}", _blob_event)

    # 4. consistency probe
    print("[4] consistency probe (blob vs DB-backed API)")

    def _event_consistency():
        blob = gcs_media(a.gcs, a.bucket, f"event/{event_key}")
        api = http_json(f"{a.base_url}/v3/site/event/{event_key}")
        b = {te["team"]: te for te in blob["team_events"]}
        s = {te["team"]: te for te in api["team_events"]}
        common = sorted(set(b) & set(s))[:25]
        assert common, "no common teams between blob and API"
        worst = 0.0
        worst_team = None
        for t in common:
            be = _epa_total(b[t]["epa"])
            se = _epa_total(s[t]["epa"])
            if abs(be - se) > worst:
                worst, worst_team = abs(be - se), t
        assert worst <= TOL, f"team {worst_team} blob/API epa diff {worst:.3f} > {TOL}"
        return f"{len(common)} teams checked, max diff {worst:.4f}"

    check(f"event blob epa == API epa ({event_key})", _event_consistency)

    def _ty_consistency():
        blob = gcs_media(a.gcs, a.bucket, f"team_years/{a.year}")
        api = http_json(f"{a.base_url}/v3/site/team_years/{a.year}")
        b = {t["team"]: t for t in blob["team_years"]}
        s = {t["team"]: t for t in (api["team_years"] if isinstance(api, dict) else api)}
        common = sorted(set(b) & set(s))[:50]
        worst = max(abs(_epa_total(b[t]["epa"]) - _epa_total(s[t]["epa"])) for t in common)
        assert worst <= TOL, f"team_years blob/API epa diff {worst:.3f} > {TOL}"
        return f"{len(common)} teams checked, max diff {worst:.4f}"

    check(f"team_years blob epa == API epa ({a.year})", _ty_consistency)

    # 5. optional update trigger
    if a.run_update:
        print("[5] update trigger + publish advance (legacy blob OR manifest)")

        def _update_advances():
            legacy_name = f"team_years/{a.year}"
            before_legacy = _gen_or_none(a, legacy_name)
            before_manifest = _manifest_state(a)

            code = http_status(f"{a.data_url}/v3/data/update_curr_year")
            assert code == 200, f"update endpoint HTTP {code}"

            after_legacy = _gen_or_none(a, legacy_name)
            after_manifest = _manifest_state(a)

            legacy_ok = (
                before_legacy is not None
                and after_legacy is not None
                and after_legacy != before_legacy
            )
            # Manifest path: manifest.json exists after the cycle and advanced —
            # object generation changed, or (same generation semantics unknown)
            # its raw bytes changed. A manifest appearing for the first time
            # also counts as an advance.
            manifest_ok = after_manifest is not None and after_manifest != before_manifest

            assert legacy_ok or manifest_ok, (
                "publish did not advance: "
                f"legacy generation {before_legacy} -> {after_legacy}; "
                f"manifest {'absent' if after_manifest is None else 'unchanged'}"
            )
            if legacy_ok:
                return f"legacy generation {before_legacy} -> {after_legacy}"
            return (
                "manifest advanced: "
                f"{'(new)' if before_manifest is None else before_manifest[0]}"
                f" -> {after_manifest[0]}"
            )

        check("update cycle advances publish (legacy blob or manifest)", _update_advances)

    # summary
    failed = [r for r in _results if not r[0]]
    print(f"\n{'FAILED' if failed else 'OK'}: "
          f"{len(_results) - len(failed)}/{len(_results)} checks passed")
    sys.exit(1 if failed else 0)


def _gen_or_none(a, name):
    """GCS object generation for a blob, or None if it doesn't exist."""
    try:
        return gcs_meta(a.gcs, a.bucket, name)["generation"]
    except Exception:  # noqa: BLE001  (404 etc.)
        return None


def _manifest_state(a, name="manifest.json"):
    """(generation, raw_bytes) for manifest.json, or None if absent.

    Comparing the tuple detects an advance whether the publisher rewrites the
    object (generation changes) or the content/version stamp changes. Raw bytes
    are compared as-is (works for plain JSON or zlib-compressed payloads).
    """
    try:
        gen = gcs_meta(a.gcs, a.bucket, name)["generation"]
        url = (
            f"{a.gcs}/storage/v1/b/{a.bucket}/o/"
            f"{urllib.parse.quote(name, safe='')}?alt=media"
        )
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read()
        return (gen, raw)
    except Exception:  # noqa: BLE001
        return None


def _discover_event(a):
    try:
        d = gcs_media(a.gcs, a.bucket, f"events/{a.year}")
        events = d["events"] if isinstance(d, dict) else d
        # prefer an event that has team_events in its own blob
        for e in events:
            key = e["key"] if isinstance(e, dict) else e
            try:
                b = gcs_media(a.gcs, a.bucket, f"event/{key}")
                if b.get("team_events"):
                    return key
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return f"{a.year}cancmp"  # sensible fallback


def _epa_total(epa):
    if isinstance(epa, dict):
        for k in ("total_points", "total", "norm", "unitless"):
            if k in epa and isinstance(epa[k], (int, float)):
                return float(epa[k])
    if isinstance(epa, (int, float)):
        return float(epa)
    raise AssertionError(f"cannot extract epa total from {type(epa)}")


def _eq(got, want):
    assert got == want, f"got {got} != {want}"
    return "ok"


def _is(got, want):
    assert got == want, f"got {got} != {want}"
    return str(got)


def _nonempty(obj, label):
    n = len(obj) if hasattr(obj, "__len__") else 0
    assert n > 0, f"{label} empty"
    return f"{n} items"


if __name__ == "__main__":
    main()
