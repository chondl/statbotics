"""Process-wide lock serialising the heavy data pipelines.

Trailing endpoints acquire it non-blocking and answer {"status": "busy"} if
held; the current-year cycle, the sweep and refresh_teams acquire it
non-blocking and skip with a log line if held.

``try_hold`` is the one way pipeline code takes the lock. It is re-entrant
for the thread that took it through ``try_hold``: the daily sweep holds the
lock and its reprocess chains the current-year cycle and refresh_teams in
the same thread, which must run rather than skip as busy. Any other holder
(another thread, or a direct ``PIPELINE_LOCK.acquire``) makes it yield False.

INVARIANT: a held region must never ``await``. Re-entrancy is keyed on the
thread, and every ``async def`` handler runs on the one event-loop thread: a
coroutine that awaited while holding the lock would let a second coroutine
on that thread pass ``try_hold`` as the "owner" and run concurrently. Hold
the lock only around synchronous pipeline code.
"""

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

PIPELINE_LOCK = threading.Lock()

# Thread ident of the try_hold caller that holds PIPELINE_LOCK, if any.
_owner: Optional[int] = None


@contextmanager
def try_hold() -> Iterator[bool]:
    """Yield True while this thread holds PIPELINE_LOCK, False (without
    waiting) when someone else holds it."""
    global _owner
    me = threading.get_ident()
    if _owner == me:
        yield True
        return
    if not PIPELINE_LOCK.acquire(blocking=False):
        yield False
        return
    _owner = me
    try:
        yield True
    finally:
        _owner = None
        PIPELINE_LOCK.release()
