import pytest

import src.tba.cache as tba_cache


@pytest.fixture(autouse=True)
def _clean_tba_cache_state():
    """The TBA cache manifest/dirty/hydrated sets are process-local module
    state written by get_tba; isolate every test from it."""
    tba_cache.reset_state()
    yield
    tba_cache.reset_state()
