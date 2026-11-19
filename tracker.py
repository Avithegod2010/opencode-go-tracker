#!/usr/bin/env python3
"""Compatibility shim: the CLI lives in ``src/opencode_tracker/cli.py``.

``python tracker.py ...`` keeps working from a checkout, and installing the
package exposes the same entry point as ``opencode-tracker``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from opencode_tracker.cli import main

if __name__ == "__main__":
    main()
