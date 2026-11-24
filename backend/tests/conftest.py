import pytest

import src.tba.cache as tba_cache
from src.data import trailing


@pytest.fixture(autouse=True)
def _trailing_window_closed(monkeypatch):
    """Pin the trailing window closed so tests do not depend on the date."""
    monkeypatch.setattr(trailing, "trailing_window_open", lambda now=None: False)


@pytest.fixture
def trailing_open(monkeypatch):
    """Opt-in: pin the trailing window open."""
    monkeypatch.setattr(trailing, "trailing_window_open", lambda now=None: True)


@pytest.fixture(autouse=True)
def _clean_tba_cache_state():
    """The TBA cache manifest/dirty/hydrated sets are process-local module
    state written by get_tba; isolate every test from it."""
    tba_cache.reset_state()
    yield
    tba_cache.reset_state()
