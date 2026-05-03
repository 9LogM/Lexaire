"""Shared logging setup. Every service calls configure(name, level) at startup."""

from __future__ import annotations

import logging
import sys


_ROOT_HANDLER_ATTR = "_lexaire_handler_attached"


def configure(service: str, level: str = "INFO") -> logging.Logger:
    fmt = f"%(asctime)s [{service}] %(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    # Service-named logger gets its own handler; propagate=False so the
    # same record doesn't double-emit through root.
    log = logging.getLogger(service)
    log.handlers[:] = [handler]
    log.setLevel(level.upper())
    log.propagate = False

    # Module loggers (lexaire.*, services.*.*) propagate to root, not to
    # the service-named logger. Attach the same handler to root once
    # (idempotent across re-init) without disturbing pytest / other handlers.
    root = logging.getLogger()
    if not getattr(root, _ROOT_HANDLER_ATTR, False):
        root.addHandler(handler)
        root.setLevel(level.upper())
        setattr(root, _ROOT_HANDLER_ATTR, True)
    return log
