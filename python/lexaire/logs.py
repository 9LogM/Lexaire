"""Shared logging setup. Every service calls configure(name, level) at startup."""

from __future__ import annotations

import logging
import sys


_LEXAIRE_OWNED = "_lexaire_owned"


def configure(service: str, level: str = "INFO") -> logging.Logger:
    fmt = f"%(asctime)s [{service}] %(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    setattr(handler, _LEXAIRE_OWNED, True)

    # Service-named logger gets its own handler; propagate=False so the
    # same record doesn't double-emit through root.
    log = logging.getLogger(service)
    log.handlers[:] = [handler]
    log.setLevel(level.upper())
    log.propagate = False

    # Module loggers (lexaire.*, services.*.*) propagate to root. Replace
    # any prior lexaire-owned handler so a second configure() in the same
    # process picks up the new formatter, without touching pytest's etc.
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers
                     if not getattr(h, _LEXAIRE_OWNED, False)]
    root.addHandler(handler)
    root.setLevel(level.upper())
    return log
