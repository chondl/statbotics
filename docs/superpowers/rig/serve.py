"""Rig backend server launcher.

    PORT=8000 PYTHONPATH=$PWD .venv/bin/python .../serve.py   # api/site server
    PORT=8001 PYTHONPATH=$PWD .venv/bin/python .../serve.py   # data server

Loads main:app, injects the real TBA key into the shared session, and serves.
Both ports serve the same app (all routers mounted); dev convention is 8000 =
api/site, 8001 = data (the site router backgrounds update calls to 8001).
"""
import os

import uvicorn

import rig_bootstrap  # noqa: F401  registers models + patches TBA session
from main import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
