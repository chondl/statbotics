"""Rig bootstrap — import FIRST in any rig script.

Imports the entity models (still load-bearing db-less: the Parquet writer, the
state snapshot, and the DuckDB schemas all introspect them) and, if a real TBA
key file is present, injects it into the TBA HTTP session at runtime WITHOUT
editing any tracked file (src/tba/constants.py hardcodes a public key; we
override the live header).

Key resolution is ENV-FIRST: $TBA_AUTH_KEY when set (agent containers have
only env vars), else the Mac key file below (`X-TBA-Auth-Key=<value>`,
chmod 600). The value is never logged. If neither exists the hardcoded public
key is used (it works).
"""
import os

import src.db.models  # noqa: F401  register entity models on Base.metadata

TBA_KEY_FILE = os.path.expanduser("~/thebluealliance_api_key.txt")


def _load_tba_key() -> str | None:
    env = os.environ.get("TBA_AUTH_KEY")
    if env:
        return env
    if not os.path.exists(TBA_KEY_FILE):
        return None
    with open(TBA_KEY_FILE) as f:
        line = f.readline().strip()
    if not line:
        return None
    # Accept "X-TBA-Auth-Key=VALUE", "KEY=VALUE", or a raw key.
    return line.split("=", 1)[1] if "=" in line else line


def apply() -> None:
    key = _load_tba_key()
    if key:
        from src.tba.main import session

        session.headers.update({"X-TBA-Auth-Key": key})
        print("[rig] TBA session using real key ($TBA_AUTH_KEY or key file)")
    else:
        print("[rig] TBA session using hardcoded public key")


apply()
