from copy import deepcopy

import src.google.storage as storage
from src.db.models import ETag, Event, Match, Team, TeamEvent, TeamYear, Year
from src.google.publish import UploadPlan
from src.google.storage import _team_render_set
from src.types.enums import CompLevel, EventStatus, EventType, MatchStatus, MatchWinner

YEAR = storage.CURR_YEAR


# ------------------------- _team_render_set (unit) ---------------------------


def ty(team, year=YEAR, epa=50.0):
    return TeamYear(team=team, year=year, epa=epa)


def team_row(num, name="Some Team"):
    return Team(team=num, name=name, active=True)


def baseline():
    curr_ty = {254: ty(254, epa=90.25), 1678: ty(1678, epa=88.5)}
    orig_ty = deepcopy(curr_ty)
    teams_by_num = {254: team_row(254), 1678: team_row(1678)}
    orig_teams_by_num = deepcopy(teams_by_num)
    prev_blobs = {f"team/{num}": f"v2/team/{num}.abc" for num in curr_ty}
    return curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs


def test_full_rebuild_renders_all_current_teams():
    curr_ty, _, teams_by_num, _, _ = baseline()
    assert _team_render_set(curr_ty, None, teams_by_num, None, {}) == {254, 1678}


def test_quiet_cycle_renders_nothing():
    assert _team_render_set(*baseline()) == set()


def test_changed_team_year_renders_only_that_team():
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    curr_ty[254].epa = 95.0
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == {254}


def test_missing_from_manifest_renders_even_if_unchanged():
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    del prev_blobs["team/1678"]
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == {1678}


def test_team_row_change_renders():
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    teams_by_num[1678].name = "Citrus Circuits (renamed)"
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == {1678}


def test_removed_team_year_rerenders_that_team():
    # Team present at cycle start but its current-year TeamYear was removed
    # (e.g. TBA registration removal): the blob must drop the removed year.
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    orig_ty[118] = ty(118, epa=40.0)
    teams_by_num[118] = team_row(118)
    orig_teams_by_num[118] = team_row(118)
    prev_blobs["team/118"] = "v2/team/118.abc"
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == {118}


def test_new_team_year_renders_that_team():
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    curr_ty[118] = ty(118, epa=40.0)
    teams_by_num[118] = team_row(118)
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == {118}


def test_no_team_baseline_renders_everything():
    # A partial cycle without an orig_teams baseline cannot prove team rows
    # are unchanged; correctness rule says render.
    curr_ty, orig_ty, teams_by_num, _, prev_blobs = baseline()
    got = _team_render_set(curr_ty, orig_ty, teams_by_num, None, prev_blobs)
    assert got == {254, 1678}


def test_nan_fields_compare_equal():
    curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs = baseline()
    curr_ty[254].norm_epa = float("nan")
    orig_ty[254].norm_epa = float("nan")
    got = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    assert got == set()


# --------------------------- write_objs (integration) ------------------------


def make_cycle():
    year = Year(year=YEAR)
    team_years = {
        f"254_{YEAR}": TeamYear(team=254, year=YEAR, epa=90.25),
        f"1678_{YEAR}": TeamYear(team=1678, year=YEAR, epa=88.5),
    }
    events = {
        f"{YEAR}cc": Event(
            key=f"{YEAR}cc",
            year=YEAR,
            name="Chezy Champs",
            type=EventType.OFFSEASON,
            status=EventStatus.COMPLETED,
            week=9,
        )
    }
    team_events = {
        f"254_{YEAR}cc": TeamEvent(team=254, year=YEAR, event=f"{YEAR}cc"),
        f"1678_{YEAR}cc": TeamEvent(team=1678, year=YEAR, event=f"{YEAR}cc"),
    }
    matches = {
        f"{YEAR}cc_qm1": Match(
            key=f"{YEAR}cc_qm1",
            year=YEAR,
            event=f"{YEAR}cc",
            comp_level=CompLevel.QUAL,
            set_number=1,
            match_number=1,
            status=MatchStatus.COMPLETED,
            winner=MatchWinner.RED,
            red_1=254,
            blue_1=1678,
            red_score=100,
            blue_score=90,
            red_surrogate="",
            blue_surrogate="",
            red_dq="",
            blue_dq="",
        )
    }
    etags = {
        f"/events/{YEAR}": ETag(year=YEAR, path=f"/events/{YEAR}", etag="abc123"),
    }
    objs = (year, team_years, events, team_events, matches, etags)
    teams = [
        Team(team=254, name="The Cheesy Poofs", active=True),
        Team(team=1678, name="Citrus Circuits", active=True),
    ]
    return objs, teams


class Recorder:
    def __init__(self):
        self.rendered = None
        self.plan = None
        self.hist_ty_calls = 0


def run_write_objs(monkeypatch, objs, orig_objs, teams, orig_teams, prev_manifest):
    rec = Recorder()

    def fake_get_team_years_db():
        rec.hist_ty_calls += 1
        # One historical row plus the current-year rows (as the DB would hold).
        return [TeamYear(team=254, year=YEAR - 1, epa=80.0)] + [
            deepcopy(ty) for ty in objs[1].values()
        ]

    def boom(**kwargs):
        raise RuntimeError("db unavailable in tests")

    real_plan_uploads = storage.plan_uploads

    def spy_plan_uploads(rendered, prev, cycle, hist_epoch=None):
        rec.rendered = dict(rendered)
        return real_plan_uploads(rendered, prev, cycle, hist_epoch=hist_epoch)

    def fake_publish(plan: UploadPlan):
        rec.plan = plan

    monkeypatch.setattr(storage, "get_team_years_db", fake_get_team_years_db)
    monkeypatch.setattr(storage, "get_events_db", lambda: list(objs[2].values()))
    monkeypatch.setattr(storage, "get_noteworthy_matches", boom)
    monkeypatch.setattr(storage, "get_upcoming_matches", boom)
    monkeypatch.setattr(storage, "read_manifest", lambda: prev_manifest)
    monkeypatch.setattr(storage, "plan_uploads", spy_plan_uploads)
    monkeypatch.setattr(storage, "_publish", fake_publish)

    storage.write_objs(objs, orig_objs, teams, None, orig_teams)
    return rec


def team_keys(rendered):
    return {k for k in rendered if k.startswith("team/")}


def event_keys(rendered):
    return {k for k in rendered if k.startswith("event/")}


def test_full_cycle_renders_all_team_blobs(monkeypatch):
    objs, teams = make_cycle()
    rec = run_write_objs(monkeypatch, objs, None, teams, None, None)
    assert team_keys(rec.rendered) == {"team/254", "team/1678"}
    assert event_keys(rec.rendered) == {f"event/{YEAR}cc"}
    assert rec.hist_ty_calls == 1


def test_quiet_partial_cycle_renders_zero_team_blobs(monkeypatch):
    objs, teams = make_cycle()
    # Seed the previous manifest from a full cycle over the same data.
    full = run_write_objs(monkeypatch, objs, None, teams, None, None)
    prev = full.plan.manifest

    rec = run_write_objs(
        monkeypatch, objs, deepcopy(objs), teams, deepcopy(teams), prev
    )
    assert team_keys(rec.rendered) == set()
    assert event_keys(rec.rendered) == set()
    # Lazy fetch: the all-years team_years read is skipped entirely.
    assert rec.hist_ty_calls == 0
    # Nothing changed, so nothing uploads and the blob set is unchanged.
    assert rec.plan.uploads == {}
    assert rec.plan.legacy_uploads == {}
    assert rec.plan.manifest.blobs == prev.blobs


def test_partial_cycle_with_changed_team_year_renders_only_that_team(monkeypatch):
    objs, teams = make_cycle()
    full = run_write_objs(monkeypatch, objs, None, teams, None, None)
    prev = full.plan.manifest

    orig_objs = deepcopy(objs)
    # norm_epa is a field _read_team actually emits, so the re-rendered blob's
    # content (and hash) changes and the upload plan must include it.
    objs[1][f"254_{YEAR}"].norm_epa = 1800.0
    rec = run_write_objs(monkeypatch, objs, orig_objs, teams, deepcopy(teams), prev)
    assert team_keys(rec.rendered) == {"team/254"}
    assert rec.hist_ty_calls == 1
    assert "team/254" in rec.plan.legacy_uploads


def test_partial_cycle_with_removed_team_year_rerenders_blob(monkeypatch):
    objs, teams = make_cycle()
    full = run_write_objs(monkeypatch, objs, None, teams, None, None)
    prev = full.plan.manifest

    orig_objs = deepcopy(objs)
    del objs[1][f"1678_{YEAR}"]
    rec = run_write_objs(monkeypatch, objs, orig_objs, teams, deepcopy(teams), prev)
    assert team_keys(rec.rendered) == {"team/1678"}


def test_partial_cycle_with_changed_team_row_renders_blob(monkeypatch):
    objs, teams = make_cycle()
    full = run_write_objs(monkeypatch, objs, None, teams, None, None)
    prev = full.plan.manifest

    orig_teams = deepcopy(teams)
    teams[1].name = "Citrus Circuits (renamed)"
    rec = run_write_objs(monkeypatch, objs, deepcopy(objs), teams, orig_teams, prev)
    assert team_keys(rec.rendered) == {"team/1678"}


def test_partial_snapshot_fallback_renders_all_team_blobs(monkeypatch):
    # Snapshot-fallback partial cycle: the snapshot read failed, so teams were
    # reloaded from the DB and update_curr_year passes orig_teams=None (the
    # DB rows are not a valid baseline — they may already carry refresh_teams
    # / post_process mutations the published blobs predate). With orig_ty
    # present but no team-row baseline, EVERY team blob must render; the event
    # gate (orig_objs, validly reconstructed from the DB) still applies.
    objs, teams = make_cycle()
    full = run_write_objs(monkeypatch, objs, None, teams, None, None)
    prev = full.plan.manifest

    rec = run_write_objs(monkeypatch, objs, deepcopy(objs), teams, None, prev)
    assert team_keys(rec.rendered) == {"team/254", "team/1678"}
    assert event_keys(rec.rendered) == set()
