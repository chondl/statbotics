"""Rig bootstrap — import FIRST in any rig script.

Imports the entity models (still load-bearing db-less: the Parquet writer, the
state snapshot, and the DuckDB schemas all introspect them) and, if a real TBA
key file is present, injects it into the TBA HTTP session at runtime WITHOUT
editing any tracked file (src/tba/constants.py hardcodes a public key; we
override the live header).

The real key comes ONLY from the $TBA_AUTH_KEY environment variable (provided
by the operator's environment). The value is never logged. If the variable is
unset the hardcoded public key is used (it works).
"""
import os

import src.db.models  # noqa: F401  register entity models on Base.metadata


def apply() -> None:
    key = os.environ.get("TBA_AUTH_KEY")
    if key:
        from src.tba.main import session

        session.headers.update({"X-TBA-Auth-Key": key})
        print("[rig] TBA session using real key from $TBA_AUTH_KEY")
    else:
        print("[rig] TBA session using hardcoded public key")


apply()
