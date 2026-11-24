import json
from pathlib import Path

import pytest

from src.google.year_path import year_from_path

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "year_from_path_vectors.json").read_text()
)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["path"] for v in VECTORS])
def test_year_from_path_vectors(vector):
    assert year_from_path(vector["path"]) == vector["year"]


def test_year_path_is_stdlib_only():
    """An ops script loads year_path.py by file path, outside the package:
    it must import nothing from src.*."""
    import importlib.util

    path = Path(__file__).parent.parent / "src" / "google" / "year_path.py"
    assert "from src" not in path.read_text() and "import src" not in path.read_text()
    spec = importlib.util.spec_from_file_location("standalone_year_path", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.year_from_path("event/2025casj") == 2025
    assert mod.effective_hist_epoch({"hist_epoch": 2, "hist_epochs": {"2026": 3}}, 2026) == 3
