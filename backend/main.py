import platform
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv  # type: ignore

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pyinstrument import Profiler

load_dotenv()

# flake8: noqa E402
from src.api.router import router as api_router
from src.auth import generate_api_key, get_api_key
from src.constants import HIST_EPOCH, PROD
from src.data.router import (
    data_router as data_data_router,
    site_router as data_site_router,
)
from src.site.router import router as site_router

"""
SETUP
"""

app = FastAPI(
    title="Statbotics REST API",
    description="The REST API for Statbotics. Please be nice to our servers! If you are looking to do large-scale data science projects, use the CSV exports on the GitHub repo.",
    version="3.0.0",
    # dependencies=[Depends(get_api_key)],
    swagger_ui_parameters={"persistAuthorization": True},
)

origins = [
    "http://localhost:3000",
    "https://statbotics.io",
    "https://www.statbotics.io",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if not PROD:

    @app.middleware("http")
    async def profile_request(request: Request, call_next: Callable[[Any], Any]):
        profiling = request.query_params.get("profile", False)
        if profiling:
            profiler = Profiler(interval=0.001, async_mode="enabled")
            profiler.start()
            await call_next(request)
            profiler.stop()
            return HTMLResponse(profiler.output_html())
        else:
            return await call_next(request)


router = APIRouter()


@router.get("/")
async def read_root():
    return {"Hello": "World"}


def hist_epoch_next_min(hist_epochs: Optional[Dict[str, int]]) -> Optional[int]:
    """The smallest global HIST_EPOCH a future bump must use: above every
    year epoch (the year epoch is a max, so a year at or above the new
    HIST_EPOCH keeps serving its older render) and above the deployed
    HIST_EPOCH. None when the manifest is unreadable."""
    if hist_epochs is None:
        return None
    return max([HIST_EPOCH, *hist_epochs.values()]) + 1


def _read_hist_epochs() -> Optional[Dict[str, int]]:
    from src.google import storage as gcs_storage

    try:
        manifest = gcs_storage.read_manifest()
        return dict(manifest.hist_epochs) if manifest is not None else {}
    except Exception:
        return None


@router.get("/info")
def get_info():
    from src.data import main as data_main
    from src.data import trailing
    from src.google import storage as gcs_storage
    from src.google import trailing_publish

    hist_epochs = _read_hist_epochs()
    try:
        trailing_status = trailing_publish.read_trailing_status()
    except Exception:
        trailing_status = None

    return {
        "TRAILING_YEAR": trailing.trailing_year(),
        "TRAILING_WINDOW_OPEN": trailing.trailing_window_open(),
        "TRAILING_STATUS": trailing_status,
        "HIST_EPOCHS": hist_epochs,
        "HIST_EPOCH_NEXT_MIN": hist_epoch_next_min(hist_epochs),
        "MANIFEST_CONFLICTS": gcs_storage.manifest_conflicts,
        "PROD": PROD,
        "PYTHON_VERSION": platform.python_version(),
        "SEED_INCOMPLETE": data_main.seed_incomplete,
        "PUBLISH_SKIPPED": data_main.publish_skipped,
        "PARTIAL_SKIPPED": data_main.partial_skipped,
    }


@router.get("/generate_key/{counter}")
def get_generate_key(counter: int, request: Request):
    origin = request.headers.get("origin", "")
    if origin not in origins:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"key": generate_api_key(counter)}


app.include_router(router, include_in_schema=False)
app.include_router(api_router, prefix="/v3")
app.include_router(data_data_router, prefix="/v3/data", include_in_schema=False)
app.include_router(data_site_router, prefix="/v3/site", include_in_schema=False)
app.include_router(site_router, prefix="/v3/site", include_in_schema=False)
