"""ZMQ socket helpers. Each call takes an explicit zmq.Context so the
caller owns lifetime — no process-wide singleton."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

import zmq


def _bind_ep(endpoint: str) -> str:
    """Rewrite a tcp endpoint's host to wildcard so binding works regardless
    of how the service's own hostname resolves inside the container."""
    if not endpoint.startswith("tcp://"):
        return endpoint
    u = urlparse(endpoint)
    if u.port is None:
        return endpoint
    return f"tcp://*:{u.port}"


def pub(ctx: zmq.Context, endpoint: str, *, hwm: int = 4) -> zmq.Socket:
    s = ctx.socket(zmq.PUB)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(_bind_ep(endpoint))
    return s


def sub(ctx: zmq.Context, endpoint: str, *,
        hwm: int = 8, topic_filter: bytes = b"",
        connect_timeout_ms: Optional[int] = None) -> zmq.Socket:
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, topic_filter)
    s.setsockopt(zmq.RCVHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    if connect_timeout_ms is not None:
        s.setsockopt(zmq.CONNECT_TIMEOUT, connect_timeout_ms)
    s.connect(endpoint)
    return s


def push(ctx: zmq.Context, endpoint: str, *,
         hwm: int = 16, linger_ms: int = 500) -> zmq.Socket:
    # Non-zero LINGER so one-shot senders (e.g. STT --once) don't drop the
    # payload on close if the PUSH/PULL TCP handshake hasn't fully settled.
    s = ctx.socket(zmq.PUSH)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, linger_ms)
    s.connect(endpoint)
    return s


def pull(ctx: zmq.Context, endpoint: str, *, hwm: int = 16) -> zmq.Socket:
    s = ctx.socket(zmq.PULL)
    s.setsockopt(zmq.RCVHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(_bind_ep(endpoint))
    return s
