import json
import zlib

from src.google.storage import compress
from src.types.enums import CompLevel, EventStatus, MatchWinner


def decode(raw):
    # Mirror the readers: pako.inflate + JSON.parse (frontend) and
    # zlib.decompress + json.loads (smoke suite).
    return json.loads(zlib.decompress(raw))


def test_round_trip_int_keys():
    # team_to_events is keyed by int team numbers; stdlib json coerces int
    # keys to strings and the swapped encoder must do the same.
    data = {254: ["2026cc", "2026iri"], 1678: ["2026cc"]}
    assert decode(compress(data)) == {"254": ["2026cc", "2026iri"], "1678": ["2026cc"]}


def test_round_trip_enum_values():
    data = {
        "status": EventStatus.COMPLETED,
        "winner": MatchWinner.RED,
        "comp_level": CompLevel.QUAL,
    }
    assert decode(compress(data)) == {
        "status": "Completed",
        "winner": "red",
        "comp_level": "qm",
    }


def test_round_trip_nested_structure():
    data = {
        "team": 254,
        "name": "The Cheesy Poofs",
        "norm_epa": 2056.0,
        "team_years": [
            {"year": 2026, "epa": 90.25, "rank": 1, "percentile": 0.999},
            {"year": 2025, "epa": None, "rank": None, "percentile": None},
        ],
    }
    assert decode(compress(data)) == data


def test_nan_serializes_to_null():
    # orjson emits null for NaN where stdlib emits a bare NaN literal that
    # JSON.parse rejects. Live blobs therefore contain no NaN today, and
    # site-blob data must never contain NaN — a NaN would now silently
    # become null on the wire instead of breaking the frontend.
    assert decode(compress({"epa": float("nan")})) == {"epa": None}
