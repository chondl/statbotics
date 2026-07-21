import re
import time

import requests
from fastapi import APIRouter, BackgroundTasks, Response

from src.constants import BACKEND_URL, CURR_YEAR
from src.data.main import refresh_teams, reset_all_years, update_curr_year
from src.data.tba import check_year_partial as check_year_partial_tba
from src.db.read import get_etags as get_etags_db
from src.db.read import get_events as get_events_db

data_router = APIRouter()
site_router = APIRouter()


@data_router.get("/")
async def read_root():
    return {"name": "Data Router"}


@data_router.get("/reset_all_years")
async def reset_all_years_endpoint():
    # return {"status": "skipped"}
    reset_all_years()
    return {"status": "success"}


@data_router.get("/reset_curr_year")
async def reset_curr_year_endpoint():
    update_curr_year(partial=False, tba_partial=False)
    refresh_teams()
    return {"status": "success"}


@data_router.get("/update_curr_year")
async def update_curr_year_endpoint():
    update_curr_year(partial=True, tba_partial=True)
    return {"status": "success"}


@data_router.get("/update_curr_year_debug")
async def update_curr_year_debug_endpoint():
    update_curr_year(partial=True, tba_partial=False)
    return {"status": "success"}


@data_router.get("/refresh_teams")
async def refresh_teams_endpoint():
    result = refresh_teams()
    return {"status": "success", **result}


def update_curr_year_background():
    requests.get(f"{BACKEND_URL}/v3/data/update_curr_year")


@site_router.get("/update_curr_year")
async def update_curr_year_site_endpoint(background_tasks: BackgroundTasks):
    event_objs = get_events_db(year=CURR_YEAR)
    etags = get_etags_db(CURR_YEAR)
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
# design (structurally single-writer), so module globals are authoritative.
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
    if not re.fullmatch(rf"{CURR_YEAR}[a-z0-9]+", event_key):
        return Response(status_code=204)
    now = time.monotonic()
    if _ping_inflight or now - _ping_last_probe < PING_COOLDOWN_S:
        return Response(status_code=204)
    _ping_last_probe = now
    _ping_inflight = True
    background_tasks.add_task(_ping_probe)
    return Response(status_code=202)
