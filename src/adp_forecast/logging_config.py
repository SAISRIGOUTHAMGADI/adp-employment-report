"""Central logging setup.

Library modules never configure logging; they only call :func:`get_logger`. Entry
points (scripts, the CLI, the future API) call :func:`configure_logging` exactly once.
That keeps this package importable from a notebook or a web server without hijacking
the host application's logging config.
"""

from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_ROOT_LOGGER_NAME = "adp_forecast"

# Guards against duplicate handlers when an entry point is invoked more than once
# (e.g. pytest importing a script module in several test sessions).
_configured = False


def configure_logging(level: int | str | None = None, *, stream=sys.stderr) -> None:
    """Attach a single stream handler to the package logger.

    Idempotent: repeat calls only adjust the level, they never stack handlers.

    Args:
        level: Log level as an int or name. Defaults to the ``ADP_LOG_LEVEL``
            environment variable, then ``INFO``.
        stream: Destination stream. Defaults to stderr so that stdout stays clean
            for machine-readable command output.
    """
    global _configured

    resolved = level if level is not None else os.getenv("ADP_LOG_LEVEL", "INFO")
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(resolved)

    if not _configured:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)
        # Keep our records out of the root logger to avoid double emission when the
        # host application has its own handlers.
        logger.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return the package-scoped logger for ``name``.

    Args:
        name: Usually ``__name__``. A bare module name is namespaced under the
            package logger so that one level setting controls the whole package.
    """
    if name == _ROOT_LOGGER_NAME or name.startswith(f"{_ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
