"""Shared logging setup. Every service calls configure(name, level) at startup."""

from __future__ import annotations

import logging
import sys


def configure(service: str, level: str = "INFO") -> logging.Logger:
    fmt = f"%(asctime)s [{service}] %(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    # Service-scoped, not root: replacing the root logger's handlers
    # silently dropped any handlers other modules (e.g. tests, replay
    # re-init) had attached. propagate=False keeps records from also
    # going up through the unconfigured root.
    log = logging.getLogger(service)
    log.handlers[:] = [handler]
    log.setLevel(level.upper())
    log.propagate = False
    return log
