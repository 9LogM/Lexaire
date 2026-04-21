"""Shared logging setup. Every service calls configure(name) at startup."""

from __future__ import annotations

import logging
import os
import sys


def configure(service: str, level: str | None = None) -> logging.Logger:
    lvl = (level or os.environ.get("LEXAIRE_LOG", "INFO")).upper()
    fmt = f"%(asctime)s [{service}] %(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    return logging.getLogger(service)
