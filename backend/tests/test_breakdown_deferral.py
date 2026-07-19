from src.tba.read_tba import BREAKDOWN_GRACE_SECONDS, defer_missing_breakdown

NOW = 1_700_000_000
HOUR = 3600


def test_recent_completed_missing_breakdown_is_deferred():
    assert defer_missing_breakdown(2026, True, False, NOW - HOUR, NOW)


def test_completed_with_breakdown_is_not_deferred():
    assert not defer_missing_breakdown(2026, True, True, NOW - HOUR, NOW)


def test_upcoming_match_is_not_deferred():
    assert not defer_missing_breakdown(2026, False, False, NOW, NOW)


def test_pre_2016_never_deferred():
    assert not defer_missing_breakdown(2015, True, False, NOW, NOW)


def test_fallback_releases_stale_missing_breakdown():
    stale = NOW - BREAKDOWN_GRACE_SECONDS - HOUR
    assert not defer_missing_breakdown(2026, True, False, stale, NOW)


def test_boundary_at_grace_window_releases():
    assert not defer_missing_breakdown(
        2026, True, False, NOW - BREAKDOWN_GRACE_SECONDS, NOW
    )
