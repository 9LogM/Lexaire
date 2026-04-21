"""
Thin ZMQ wrappers matching the patterns described in messages.py.

Purpose: services should read/write messages without re-implementing HWM /
LINGER / SUBSCRIBE boilerplate. Each helper returns a ready-to-use socket.
"""

from __future__ import annotations

import zmq


def _ctx() -> zmq.Context:
    return zmq.Context.instance()


def pub(endpoint: str, *, hwm: int = 4) -> zmq.Socket:
    s = _ctx().socket(zmq.PUB)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(endpoint)
    return s


def sub(endpoint: str, *, hwm: int = 8, topic_filter: bytes = b"") -> zmq.Socket:
    s = _ctx().socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, topic_filter)
    s.setsockopt(zmq.RCVHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(endpoint)
    return s


def push(endpoint: str, *, hwm: int = 16) -> zmq.Socket:
    s = _ctx().socket(zmq.PUSH)
    s.setsockopt(zmq.SNDHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.connect(endpoint)
    return s


def pull(endpoint: str, *, hwm: int = 16) -> zmq.Socket:
    s = _ctx().socket(zmq.PULL)
    s.setsockopt(zmq.RCVHWM, hwm)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(endpoint)
    return s


def req(endpoint: str, *, timeout_ms: int = 2000) -> zmq.Socket:
    s = _ctx().socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    s.setsockopt(zmq.SNDTIMEO, timeout_ms)
    s.connect(endpoint)
    return s


def rep(endpoint: str) -> zmq.Socket:
    s = _ctx().socket(zmq.REP)
    s.setsockopt(zmq.LINGER, 0)
    s.bind(endpoint)
    return s
