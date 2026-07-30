"""Storage layer: persisting observations, release dates.

Import the port in downstream code. ``SqliteStorage`` is re-exported so entry points
have one obvious place to construct it.
"""

from .port import StoragePort
from .sqlite import SqliteStorage

__all__ = ["SqliteStorage", "StoragePort"]
