"""Shared logging setup. Every service calls configure(name, level) at startup."""

from __future__ import annotations

import logging
import sys


def configure(service: str, level: str = "INFO") -> logging.Logger:
    fmt = f"%(asctime)s [{service}] %(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    return logging.getLogger(service)
