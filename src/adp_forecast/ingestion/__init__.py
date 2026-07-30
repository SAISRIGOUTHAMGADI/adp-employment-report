"""Ingestion layer: retrieving raw observations from an upstream source.

Import the ports, not the adapters, in downstream code. ``FredAdapter`` is
re-exported here only so entry points have one obvious place to construct it.
"""

from .fred import FredAdapter
from .port import IngestionPort, ReleaseCalendarPort, observations_known_on

__all__ = [
    "FredAdapter",
    "IngestionPort",
    "ReleaseCalendarPort",
    "observations_known_on",
]
