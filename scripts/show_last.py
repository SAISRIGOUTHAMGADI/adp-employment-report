#!/usr/bin/env python3
"""Deprecated shim: use `adp-forecast history` instead.

Kept so documented paths keep working. All logic lives in the CLI, so there is exactly
one implementation to maintain.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adp_forecast.cli.app import app  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "history", *sys.argv[1:]]
    app()
