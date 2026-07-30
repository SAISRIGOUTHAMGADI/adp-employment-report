"""Command-line interface.

``app`` is the typer application; ``main`` is the console-script entry point declared
in ``pyproject.toml``.
"""

from .app import app, main

__all__ = ["app", "main"]
