import os
import sys
from pathlib import Path

import pytest

# Make the repository root importable so tests can import app.py, and src/ so
# the package is importable without installing it.
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def isolated_tracker_token(tmp_path, monkeypatch):
    """Keep the generated /api/* token out of the real user config dir.

    app.py persists a generated token under the user config dir; point that at
    the test's tmp_path and restore the module-level token afterwards.
    """
    monkeypatch.setenv("OPENCODE_TRACKER_CONFIG_DIR", str(tmp_path / "tracker-config"))
    import app as module

    monkeypatch.setattr(module, "API_TOKEN", os.getenv("TRACKER_API_TOKEN", "").strip())
    monkeypatch.setattr(module, "_TOKEN_PRINTED", False)
