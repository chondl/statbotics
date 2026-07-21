"""Before/after correctness comparison for the 2026-07-21 perf deploy.

Compares the pre-deploy baseline (baseline-pre-perf/) against live production
(API + site bucket). Historical artifacts must match to ~1e-9; current-year
2026 is checked by invariants. Writes verification-results.md.

Usage: python3 compare_verification.py
"""

import gzip
import json
import math
import os
import statistics
import urllib.request
import zlib

SC = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(SC, "baseline-pre-perf")
OUT = os.path.join(SC, "verification-results.md")
API = "https://api-statbotics.iterativerefinement.com"
BUCKET = "https://storage.googleapis.com/statbotics-staging-site"
TOL = 1e-9

results = []  # rows: (artifact, fields_compared, max_abs_diff, n_diffs, verdict, notes)
defects = []
notes_lines = []


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sb-verify"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def fetch_json(path):
    return json.loads(fetch(API + path))


_manifest = None


def manifest():
    global _manifest
    if _manifest is None:
        raw = fetch(BUCKET + "/manifest.json")
        try:
            m = json.loads(raw)
        except Exception:
            m = json.loads(gzip.decompress(raw))
        _manifest = m.get("blobs", m)
    return _manifest


def fetch_blob(logical):
    m = manifest()
    path = m[logical] if logical in m else logical
    return json.loads(zlib.decompress(fetch(BUCKET + "/" + path)))


def load_baseline(name):
    with open(os.path.join(BASE, name)) as f:
        return json.load(f)


class DiffStats:
    def __init__(self):
        self.fields = 0
        self.max_abs = 0.0
        self.diffs = []  # (path, old, new)

    def record_cmp(self, path, a, b):
        self.fields += 1
        if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None \
           or isinstance(a, str) or isinstance(b, str):
            if a != b:
                self.diffs.append((path, a, b))
            return
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if math.isnan(a) and math.isnan(b):
                return
            d = abs(a - b)
            if d > self.max_abs:
                self.max_abs = d
            if d > TOL:
                self.diffs.append((path, a, b))
            return
        if a != b:
            self.diffs.append((path, a, b))


def deep_diff(a, b, ds, path="", skip=None):
    """Recursively compare parsed JSON. skip: predicate(path) -> True to ignore."""
    if skip and skip(path):
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if skip and skip(p):
                continue
            if k not in a:
                ds.diffs.append((p, "<absent>", "<present>"))
            elif k not in b:
                ds.diffs.append((p, "<present>", "<absent>"))
            else:
                deep_diff(a[k], b[k], ds, p, skip)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            ds.diffs.append((path + ".<len>", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            deep_diff(x, y, ds, f"{path}[{i}]", skip)
    else:
        ds.record_cmp(path, a, b)


def key_by(rows, keyfn):
    return {keyfn(r): r for r in rows}


def add_result(artifact, ds, verdict=None, note=""):
    if verdict is None:
        verdict = "PASS" if not ds.diffs else "DEFECT"
    if verdict == "DEFECT":
        defects.append((artifact, ds.diffs[:20]))
    results.append((artifact, ds.fields, ds.max_abs, len(ds.diffs), verdict, note))
    if ds.diffs:
        notes_lines.append(f"### Diffs in {artifact} (first 20)")
        for p, o, n in ds.diffs[:20]:
            notes_lines.append(f"- `{p}`: baseline=`{o}` live=`{n}`")
        notes_lines.append("")
    return verdict


# ---------------------------------------------------------------- Part A: historical years
def check_year(year):
    base = load_baseline(f"api_v3_year_{year}.json")
    live = fetch_json(f"/v3/year/{year}")
    ds = DiffStats()
    deep_diff(base, live, ds)
    add_result(f"/v3/year/{year}", ds)


def check_team_years(year):
    base = load_baseline(f"api_v3_team_years_year-{year}_limit-50_metric-epa.json")
    live = fetch_json(f"/v3/team_years?year={year}&limit=50&metric=epa")
    ds = DiffStats()
    bk, lk = key_by(base, lambda r: r["team"]), key_by(live, lambda r: r["team"])
    if set(bk) != set(lk):
        ds.diffs.append(("<team-set>", sorted(set(bk) - set(lk)), sorted(set(lk) - set(bk))))
    for t in sorted(set(bk) & set(lk)):
        deep_diff(bk[t], lk[t], ds, f"team{t}")
    add_result(f"/v3/team_years?year={year} top-50", ds)


def check_matches(event):
    base = load_baseline(f"api_v3_matches_event-{event}.json")
    live = fetch_json(f"/v3/matches?event={event}")
    ds = DiffStats()
    bk, lk = key_by(base, lambda m: m["key"]), key_by(live, lambda m: m["key"])
    note = ""
    if set(bk) != set(lk):
        note = (f"match set changed: baseline-only={sorted(set(bk)-set(lk))} "
                f"live-only={sorted(set(lk)-set(bk))} (data-driven)")
    for k in sorted(set(bk) & set(lk)):
        deep_diff(bk[k], lk[k], ds, k)
    add_result(f"/v3/matches?event={event}", ds, note=note)


def check_site_team_years_hist(year):
    base = load_baseline(f"api_v3_site_team_years_{year}.json")
    live = fetch_json(f"/v3/site/team_years/{year}")
    ds = DiffStats()
    brows = key_by(base["team_years"], lambda r: r["team"])
    lrows = key_by(live["team_years"], lambda r: r["team"])
    if set(brows) != set(lrows):
        ds.diffs.append(("<team-set>", len(brows), len(lrows)))
    skip = lambda p: p.endswith(".competing") or ".competing." in p
    for t in sorted(set(brows) & set(lrows)):
        deep_diff(brows[t], lrows[t], ds, f"team{t}", skip=skip)
    add_result(f"/v3/site/team_years/{year} (full, competing skipped)", ds)


def check_team_page_blob(team):
    base = load_baseline(f"blob_team_{team}.json")
    live = fetch_blob(f"team/{team}")
    ds = DiffStats()
    bty = key_by([r for r in base["team_years"] if r["year"] != 2026], lambda r: r["year"])
    lty = key_by([r for r in live["team_years"] if r["year"] != 2026], lambda r: r["year"])
    if set(bty) != set(lty):
        ds.diffs.append(("<year-set>", sorted(bty), sorted(lty)))
    for y in sorted(set(bty) & set(lty)):
        deep_diff(bty[y], lty[y], ds, f"y{y}")
    add_result(f"blob team/{team} team_years (hist, !=2026)", ds)


def check_team_summary(team):
    # /v3/team summary depends on 2026 (norm_epa, record) — informational
    base = load_baseline(f"api_v3_team_{team}.json")
    live = fetch_json(f"/v3/team/{team}")
    ds = DiffStats()
    deep_diff(base, live, ds)
    verdict = "PASS" if not ds.diffs else "INFO (2026-coupled fields)"
    results.append((f"/v3/team/{team} summary", ds.fields, ds.max_abs,
                    len(ds.diffs), verdict, "career fields may move with 2026 data"))
    if ds.diffs:
        notes_lines.append(f"### Diffs in /v3/team/{team} summary (informational)")
        for p, o, n in ds.diffs[:10]:
            notes_lines.append(f"- `{p}`: baseline=`{o}` live=`{n}`")
        notes_lines.append("")


# ---------------------------------------------------------------- Part B: 2026 invariants
def inv_blob_vs_api_2026():
    blob = fetch_blob("team_years/2026")
    api = fetch_json("/v3/site/team_years/2026")
    ds = DiffStats()
    ds.record_cmp("year", blob.get("year"), api.get("year"))
    bk = key_by(blob["team_years"], lambda r: r["team"])
    ak = key_by(api["team_years"], lambda r: r["team"])
    if set(bk) != set(ak):
        ds.diffs.append(("<team-set>", sorted(set(bk) - set(ak)), sorted(set(ak) - set(bk))))
    for t in sorted(set(bk) & set(ak)):
        deep_diff(bk[t], ak[t], ds, f"team{t}")
    add_result("INVARIANT blob team_years/2026 == /v3/site/team_years/2026 (keyed by team; "
               "list order differs by design)", ds)
    return api


def inv_percentile_monotone(site2026):
    rows = site2026["team_years"]
    ds = DiffStats()
    for scope in ("total", "country", "state", "district"):
        groups = {}
        for r in rows:
            rk = r.get("epa", {}).get("ranks", {}).get(scope)
            if not rk or rk.get("rank") is None:
                continue
            gkey = (r.get("country") if scope == "country" else
                    r.get("state") if scope == "state" else
                    r.get("district") if scope == "district" else "all")
            groups.setdefault(gkey, []).append((rk["rank"], rk["percentile"], r["team"]))
        for g, lst in groups.items():
            lst.sort()
            for (r1, p1, t1), (r2, p2, t2) in zip(lst, lst[1:]):
                ds.fields += 1
                if r2 > r1 and p2 > p1 + 1e-9:
                    ds.diffs.append((f"{scope}/{g}: rank {r1}(t{t1})->{r2}(t{t2})", p1, p2))
    add_result("INVARIANT 2026 percentile monotone non-increasing vs rank", ds)


def inv_teams_all_norm(site2026):
    # teams/all blob carries only {active, name, team} (same shape as baseline);
    # norm_epa lives on the /v3/team API. Check: (a) every active 2026 team with
    # matches is present+active in teams/all; (b) API norm_epa non-null for a
    # 60-team sample spanning top/middle/bottom of the 2026 rank distribution.
    teams_all = fetch_blob("teams/all")
    ranked = sorted(
        (r for r in site2026["team_years"]
         if r.get("record", {}).get("count", 0) > 0),
        key=lambda r: r["epa"]["ranks"]["total"]["rank"])
    ds = DiffStats()
    by_team = {t["team"]: t for t in teams_all}
    for r in ranked:
        t = r["team"]
        ds.fields += 1
        if t not in by_team:
            ds.diffs.append((f"team{t}", "in 2026 team_years w/ matches", "missing from teams/all"))
    n = len(ranked)
    sample = ranked[:20] + ranked[n // 2 - 10: n // 2 + 10] + ranked[-20:]
    checked = 0
    for r in sample:
        t = r["team"]
        if not by_team.get(t, {}).get("active"):
            continue
        api_team = fetch_json(f"/v3/team/{t}")
        ds.fields += 1
        checked += 1
        if api_team.get("norm_epa") is None:
            ds.diffs.append((f"/v3/team/{t}.norm_epa", "expected non-null", None))
    add_result("INVARIANT active 2026 teams present in teams/all; API norm_epa non-null "
               "(60-team rank-stratified sample)", ds,
               note=f"teams/all blob has no norm_epa field by design; sampled {checked} via API")


def inv_event_parity(event_key):
    blob = fetch_blob(f"event/{event_key}")
    api_te = fetch_json(f"/v3/team_events?event={event_key}")
    ds = DiffStats()
    bk = key_by(blob["team_events"], lambda r: r["team"])
    ak = key_by(api_te, lambda r: r["team"])
    if set(bk) != set(ak):
        ds.diffs.append(("<team-set>", sorted(set(bk) - set(ak)), sorted(set(ak) - set(bk))))
    for t in sorted(set(bk) & set(ak)):
        b_epa, a_epa = bk[t].get("epa", {}), ak[t].get("epa", {})
        for f in ("total_points", "unitless", "norm"):
            bv = b_epa.get(f)
            av = a_epa.get(f)
            if isinstance(av, dict):  # api may nest e.g. total_points under stats
                av = av.get("mean", av)
            ds.record_cmp(f"t{t}.epa.{f}", bv, av)
        deep_diff(b_epa.get("breakdown"), a_epa.get("breakdown"), ds, f"t{t}.breakdown")
    add_result(f"INVARIANT event blob EPA == API EPA ({event_key})", ds)


def inv_norm_sanity(site2026):
    rows = site2026["team_years"]
    norms = sorted(r["epa"]["norm"] for r in rows if r.get("epa", {}).get("norm") is not None)
    unitless = [r["epa"]["unitless"] for r in rows if r.get("epa", {}).get("unitless") is not None]
    n = len(norms)
    mid = norms[n // 4: 3 * n // 4]  # middle 50%
    mid_mean = statistics.fmean(mid)
    med = norms[n // 2]
    ds = DiffStats()
    ds.fields = 4
    if not (1400 <= mid_mean <= 1600):
        ds.diffs.append(("norm mid-50% mean", "1400..1600", mid_mean))
    if not (1400 <= med <= 1600):
        ds.diffs.append(("norm median", "1400..1600", med))
    if not (1000 <= norms[0] and norms[-1] <= 2400):
        ds.diffs.append(("norm range", "1000..2400", (norms[0], norms[-1])))
    if not (800 <= min(unitless) and max(unitless) <= 2600):
        ds.diffs.append(("unitless range", "800..2600", (min(unitless), max(unitless))))
    # cross-check against the pre-deploy baseline distribution (same day)
    bl = load_baseline("blob_team_years_2026.json")["team_years"]
    bl_norms = sorted(r["epa"]["norm"] for r in bl if r.get("epa", {}).get("norm") is not None)
    bl_med = bl_norms[len(bl_norms) // 2]
    ds.fields += 1
    if abs((norms[n // 2]) - bl_med) > 25:
        ds.diffs.append(("norm median vs baseline", bl_med, norms[n // 2]))
    note = (f"n={n} median={med} mid50-mean={mid_mean:.1f} "
            f"norm=[{norms[0]},{norms[-1]}] unitless=[{min(unitless)},{max(unitless)}]")
    add_result("INVARIANT 2026 norm/unitless EPA ranges sane", ds, note=note)


def main():
    for y in (2005, 2016, 2024, 2025):
        check_year(y)
    for y in (2016, 2025):
        check_team_years(y)
    for e in ("2025casj", "2016nytr"):
        check_matches(e)
    for y in (2024, 2025):
        check_site_team_years_hist(y)
    for t in (254, 1678):
        check_team_page_blob(t)
    for t in (254, 1678, 2056, 148):
        check_team_summary(t)

    site2026 = inv_blob_vs_api_2026()
    inv_percentile_monotone(site2026)
    inv_teams_all_norm(site2026)
    for ev in ("2026kylou", "2026iri"):
        inv_event_parity(ev)
    inv_norm_sanity(site2026)

    # write report
    lines = [
        "# Correctness verification — perf deploy 2026-07-21",
        "",
        f"Baseline: `{BASE}` (captured 2026-07-21 ~07:00Z, pre-deploy). "
        f"Live: `{API}` + `{BUCKET}` (db-less since 16:07Z). Tolerance: {TOL}.",
        "",
        "| Artifact | Fields compared | Max abs diff | # diffs | Verdict | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for a, f, m, nd, v, note in results:
        lines.append(f"| {a} | {f} | {m:.3g} | {nd} | **{v}** | {note} |")
    lines.append("")
    n_defect = sum(1 for r in results if r[4] == "DEFECT")
    lines.append(f"**Summary: {n_defect} defect(s) across {len(results)} checks; "
                 f"{sum(r[1] for r in results)} fields compared total.**")
    lines.append("")
    if notes_lines:
        lines.append("## Diff details")
        lines.append("")
        lines.extend(notes_lines)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}")
    for a, f_, m, nd, v, note in results:
        print(f"{v:8} {a}  fields={f_} maxdiff={m:.3g} ndiffs={nd} {note}")


if __name__ == "__main__":
    main()
