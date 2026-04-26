"""ZMQ socket helpers: pub/sub/push with consistent HWM, LINGER, and bind
behavior. Services use these to avoid re-implementing boilerplate."""

from __future__ import annotations

from urllib.parse import urlparse

import zmq


def _ctx() -> zmq.Context:
    return zmq.Context.instance()


def _bind_ep(endpoint: str) -> str:
    """Rewrite a tcp endpoint's host to the wildcard so a service binds on
    every interface. Lets config.yaml use the same string for bind and
    connect even when the service's own hostname resolves to a private
    container-bridge IP only."""
    if not endpoint.startswith("tcp://"):
        return endpoint
    u = urlparse(endpoint)
    if u.port is None:
        return endpoint
    return f"tcp://*:{u.port}"


def pub(endpoint: str, *, hwm: int = 4) -> zmq.Socket:
    s = _ctx().socket(zmq.PUB)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(_bind_ep(endpoint))
    return s


def sub(endpoint: str, *, hwm: int = 8, topic_filter: bytes = b"") -> zmq.Socket:
    s = _ctx().socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, topic_filter)
    s.setsockopt(zmq.RCVHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(endpoint)
    return s


def push(endpoint: str, *, hwm: int = 16, linger_ms: int = 500) -> zmq.Socket:
    # Non-zero LINGER so one-shot senders (e.g. STT --once) don't drop the
    # payload on close if the PUSH/PULL TCP handshake hasn't fully settled.
    s = _ctx().socket(zmq.PUSH)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, linger_ms)
    s.connect(endpoint)
    return s
