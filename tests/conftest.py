import sys
from pathlib import Path

# Make the repository root importable so tests can import app.py, and src/ so
# the package is importable without installing it.
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
