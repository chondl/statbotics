import re
import time
import traceback
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, Response
from fastapi.responses import JSONResponse

from src.constants import BACKEND_URL, CURR_YEAR
from src.data import main as data_main
from src.data import trailing
from src.data.pipeline_lock import PIPELINE_LOCK
from src.data.main import refresh_teams, reset_all_years, update_curr_year
from src.data.sweep import revalidate_tba
from src.data.tba import check_year_partial as check_year_partial_tba
from src.google.snapshot import read_snapshot
from src.google import storage as gcs_storage
from src.google.storage import GC_GRACE_HOURS, gc_versioned_blobs

data_router = APIRouter()
site_router = APIRouter()


@data_router.get("/")
async def read_root():
    return {"name": "Data Router"}


# refresh_tba=true (TBA cache design §2.3 "manual force") bypasses the cache
# manifest for the run: every TBA path is fetched unconditionally (etag-less)
# and the cache archives are rebuilt. For known upstream corrections.


@data_router.get("/reset_all_years")
async def reset_all_years_endpoint(refresh_tba: bool = False):
    # return {"status": "skipped"}
    reset_all_years(refresh_tba=refresh_tba)
    return {"status": "success"}


# The current-year endpoints answer {"status": "busy"} when another pipeline
# holds PIPELINE_LOCK and the cycle was skipped (update_curr_year returned
# BUSY, or refresh_teams answered busy): a skip is never reported as success.
BUSY_RESPONSE = {"status": "busy"}


@data_router.get("/reset_curr_year")
async def reset_curr_year_endpoint(refresh_tba: bool = False):
    if (
        update_curr_year(partial=False, tba_partial=False, refresh_tba=refresh_tba)
        == data_main.BUSY
    ):
        return dict(BUSY_RESPONSE)
    if refresh_teams().get("status") == "busy":
        return dict(BUSY_RESPONSE)
    return {"status": "success"}


@data_router.get("/update_curr_year")
async def update_curr_year_endpoint():
    if update_curr_year(partial=True, tba_partial=True) == data_main.BUSY:
        return dict(BUSY_RESPONSE)
    return {"status": "success"}


@data_router.get("/update_curr_year_debug")
async def update_curr_year_debug_endpoint():
    if update_curr_year(partial=True, tba_partial=False) == data_main.BUSY:
        return dict(BUSY_RESPONSE)
    return {"status": "success"}


# Daily historical revalidation sweep (TBA cache design §2.3): the
# statbotics-tba-sweep Cloud Scheduler job (05:30 UTC) GETs this once a day.
# One historical year per hit, round-robin, serial conditional GETs; if the
# year actually changed it is fully reprocessed in-request (minutes — same
# in-request model as reset_curr_year). Synchronous and idempotent.
@data_router.get("/revalidate_tba")
async def revalidate_tba_endpoint():
    return revalidate_tba()


@data_router.get("/refresh_teams")
async def refresh_teams_endpoint():
    result = refresh_teams()
    if result.get("status") == "busy":
        return dict(BUSY_RESPONSE)
    return {"status": "success", **result}


@data_router.get("/gc_blobs")
async def gc_blobs_endpoint(grace_hours: int = GC_GRACE_HOURS, dry_run: bool = False):
    result = gc_versioned_blobs(grace_hours=grace_hours, dry_run=dry_run)
    return {"status": "success", **result}


# Trailing-year cycles and window close (trailing-year refresh spec §5, §7).
# Plain `def`: FastAPI runs them in its threadpool, so the event loop keeps
# serving (and gunicorn's worker heartbeat continues) during a long cycle.
# Both take the pipeline lock themselves and return {"status": "busy"} when
# another pipeline holds it.
@data_router.get("/update_trailing_year")
def update_trailing_year_endpoint(full: bool = False):
    return data_main.update_trailing_year(full=full)


@data_router.get("/close_trailing_year")
def close_trailing_year_endpoint():
    return data_main.close_trailing_year()


def update_curr_year_background():
    requests.get(f"{BACKEND_URL}/v3/data/update_curr_year")


def update_trailing_year_background(full: bool):
    requests.get(f"{BACKEND_URL}/v3/data/update_trailing_year?full={int(full)}")


def close_trailing_year_background():
    requests.get(f"{BACKEND_URL}/v3/data/close_trailing_year")


def reset_curr_year_background():
    requests.get(f"{BACKEND_URL}/v3/data/reset_curr_year")


def chain_gate_pending() -> bool:
    """The manifest's chain_pending: a trailing or past-year team_years
    change whose chained current-year re-render has not published (it
    crashed, was skipped as busy, or was never sent). Raises on a manifest
    read failure (the gate catches it)."""
    if data_main.DISABLE_GCS:
        return False
    manifest = gcs_storage.read_manifest()
    return manifest is not None and manifest.chain_pending


def trailing_gate_action() -> Optional[str]:
    """What the gate should do for the trailing year: "close" when window
    close would act; None when the window is closed otherwise; "full" when
    the manifest's trailing_year marker is not the trailing year (bootstrap)
    or the trailing snapshot is unreadable; "partial" when the trailing
    year's TBA probe sees new data; else None. Raises on a manifest read
    failure (the gate catches it)."""
    year = trailing.trailing_year()
    if data_main.DISABLE_GCS:
        return None
    manifest = gcs_storage.read_manifest()
    if not trailing.trailing_window_open():
        return "close" if data_main.close_would_act(manifest, year) else None
    if manifest is None or manifest.trailing_year != year:
        return "full"
    loaded = read_snapshot(year)
    if loaded is None:
        return "full"
    event_objs = list(loaded[0][2].values())
    etags = list(loaded[0][5].values())
    if check_year_partial_tba(year, event_objs, etags):
        return "partial"
    return None


def _schedule_trailing(background_tasks: BackgroundTasks) -> Optional[str]:
    """Evaluate the trailing action and schedule its self-call. Never
    raises: a failure logs and yields None, so a trailing probe failure
    never 500s the gate."""
    try:
        action = trailing_gate_action()
    except Exception as e:
        traceback.print_exc()
        print(f"TRAILING GATE: probe failed ({type(e).__name__}: {e}); no trailing action")
        return None
    if action == "close":
        background_tasks.add_task(close_trailing_year_background)
    elif action in ("full", "partial"):
        background_tasks.add_task(update_trailing_year_background, action == "full")
    return action


def _schedule_chain(background_tasks: BackgroundTasks, trailing_action: Optional[str]) -> bool:
    """Schedule GET /v3/data/reset_curr_year when the manifest's
    chain_pending is set and the pipeline lock is free. Skipped when a
    trailing action was scheduled: that request reads chain_pending after its
    work and chains itself. Never raises."""
    if trailing_action is not None or PIPELINE_LOCK.locked():
        return False
    try:
        pending = chain_gate_pending()
    except Exception as e:
        traceback.print_exc()
        print(f"CHAIN GATE: probe failed ({type(e).__name__}: {e}); no chain")
        return False
    if pending:
        background_tasks.add_task(reset_curr_year_background)
    return pending


def _schedule_trailing_and_chain(background_tasks: BackgroundTasks) -> Dict[str, Any]:
    action = _schedule_trailing(background_tasks)
    return {"trailing": action, "chain": _schedule_chain(background_tasks, action)}


@site_router.get("/update_curr_year")
async def update_curr_year_site_endpoint(background_tasks: BackgroundTasks):
    # A pipeline is running in this process (a trailing cycle, window close,
    # the sweep, a full reset): its publishes are mid-flight, so neither probe
    # can read a settled baseline. Skip without probing; the next tick runs.
    if PIPELINE_LOCK.locked():
        return {
            "status": "skipped",
            "reason": "pipeline-busy",
            "trailing": None,
            "chain": False,
        }
    try:
        result = _curr_year_gate(background_tasks)
    except Exception as e:
        # The trailing half must not starve behind a current-year probe
        # failure. Raising would drop the background tasks (Starlette runs
        # them only after a response is sent), so answer 500 with them
        # attached instead.
        traceback.print_exc()
        body: Dict[str, Any] = {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            **_schedule_trailing_and_chain(background_tasks),
        }
        return JSONResponse(status_code=500, content=body, background=background_tasks)
    # Scheduled after the current-year task: background tasks run in order,
    # so a current-year cycle finishes before the trailing call arrives.
    result.update(_schedule_trailing_and_chain(background_tasks))
    return result


def _curr_year_gate(background_tasks: BackgroundTasks) -> Dict[str, Any]:
    loaded = read_snapshot(CURR_YEAR)
    if loaded is None:
        # Snapshot unreadable (read_snapshot never raises): assume no new
        # data and skip. Probing TBA with an empty etag baseline would
        # always report "new data" and trigger a partial cycle that the
        # pipeline's snapshot-miss guard (_update_curr_year) would skip
        # anyway — so don't spend the TBA calls or the cycle. Surface the
        # degraded state: a persistent snapshot failure (e.g. fingerprint
        # mismatch after a model change) would otherwise starve updates
        # silently behind an ordinary-looking "skipped".
        data_main.partial_skipped = True
        return {"status": "skipped", "reason": "snapshot-unreadable"}
    event_objs = list(loaded[0][2].values())
    etags = list(loaded[0][5].values())
    is_new_data = check_year_partial_tba(CURR_YEAR, event_objs, etags)
    if not is_new_data:
        return {"status": "skipped"}

    background_tasks.add_task(update_curr_year_background)
    return {"status": "backgrounded"}


# Read-triggered freshness ping.
#
# Event pages fire-and-forget GET /v3/site/ping/event/{key} while an event is
# live. The hot path below is pure in-process memory (no DB, no GCS, no TBA):
# during the cooldown or while a probe is in flight, a ping costs a regex, a
# float compare, and a 204. The data service runs a single gunicorn worker by
# design, so these module globals are authoritative for the process. That one
# process still runs several pipelines at once (threadpool endpoints and
# background self-calls); PIPELINE_LOCK (src/data/pipeline_lock.py)
# serialises them, and the gate skips its probes while the lock is held.
#
# A cold ping schedules a background self-HTTP to /v3/site/update_curr_year —
# the existing cheap probe (TBA etag pre-check, then a backgrounded partial
# cycle only if something actually changed). The 300s cooldown bounds TBA
# traffic to one probe per 5 minutes no matter how many viewers pile on.
PING_COOLDOWN_S = 300

_ping_last_probe: float = float("-inf")
_ping_inflight: bool = False


def _ping_probe():
    global _ping_inflight
    try:
        # Bounded timeout so a hung probe can never wedge _ping_inflight (and
        # thus disable the fast path) permanently: (connect, read) seconds.
        requests.get(f"{BACKEND_URL}/v3/site/update_curr_year", timeout=(5, 30))
    finally:
        _ping_inflight = False


@site_router.get("/ping/event/{event_key}")
async def ping_event_endpoint(event_key: str, background_tasks: BackgroundTasks):
    global _ping_last_probe, _ping_inflight
    keys = [str(CURR_YEAR)]
    if trailing.trailing_window_open():
        keys.append(str(trailing.trailing_year()))
    if not re.fullmatch(rf"(?:{'|'.join(keys)})[a-z0-9]+", event_key):
        return Response(status_code=204)
    now = time.monotonic()
    if _ping_inflight or now - _ping_last_probe < PING_COOLDOWN_S:
        return Response(status_code=204)
    _ping_last_probe = now
    _ping_inflight = True
    background_tasks.add_task(_ping_probe)
    return Response(status_code=202)
