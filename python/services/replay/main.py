"""
Replay harness — field-debug tool, not part of the live pipeline.

    record  — SUB to the live sensor channels, write a JSONL recording to disk.
    play    — read a JSONL recording and re-PUB it on the sensor channels,
              preserving the original inter-message timing.

The endpoints are read from `sensor.channels.*`; `play` binds, `record`
connects. `--bind-host` rewrites the bind side so `tcp://drone.local:5555`
becomes `tcp://0.0.0.0:5555` for local playback.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import zmq

from lexaire import logs, transport
from lexaire.config import load_config

from .store import Record, read_all, write


# -- Helpers ------------------------------------------------------------------


def _rewrite_host(endpoint: str, bind_host: Optional[str]) -> str:
    """Turn a tcp://host:port endpoint into tcp://<bind_host>:port.

    If bind_host is None or the endpoint already uses a wildcard, leave it.
    """
    if not bind_host:
        return endpoint
    u = urlparse(endpoint)
    if u.scheme != "tcp":
        return endpoint
    port = u.port
    if port is None:
        return endpoint
    return f"tcp://{bind_host}:{port}"


def _sleep_monotonic(target_ns: int, stop: threading.Event) -> None:
    while not stop.is_set():
        now = time.monotonic_ns()
        remaining = (target_ns - now) / 1e9
        if remaining <= 0:
            return
        # Cap sleep so stop events are responsive.
        time.sleep(min(remaining, 0.1))


# -- Record -------------------------------------------------------------------


def _enabled_channels(cfg) -> dict[str, str]:
    """Return {name: endpoint} for every sensor channel with a non-empty
    endpoint. Channels left blank/null in config are skipped."""
    raw = cfg.get("sensor.channels") or {}
    return {name: ep for name, ep in raw.items() if ep}


def _run_record(cfg, args, log, stop: threading.Event) -> int:
    channels = _enabled_channels(cfg)
    if not channels:
        log.error("no sensor channels enabled in config")
        return 2
    ctx = zmq.Context()
    subs = {name: transport.sub(ctx, ep) for name, ep in channels.items()}
    poller = zmq.Poller()
    for s in subs.values():
        poller.register(s, zmq.POLLIN)

    log.info("recording -> %s (Ctrl-C to stop)", args.path)
    count = 0
    start = time.monotonic_ns()
    deadline_ns = start + int(args.max_seconds * 1e9) if args.max_seconds > 0 else 0

    try:
        with open(args.path, "w", encoding="utf-8") as fp:
            while not stop.is_set():
                if deadline_ns and time.monotonic_ns() >= deadline_ns:
                    log.info("max-seconds reached — stopping")
                    break
                events = dict(poller.poll(250))
                for name, sock in subs.items():
                    if sock in events:
                        parts = sock.recv_multipart()
                        if len(parts) < 2:
                            continue
                        header = json.loads(parts[0].decode("utf-8"))
                        rec = Record(
                            channel=name,
                            ts_ns=time.monotonic_ns(),
                            header=header,
                            payload=bytes(parts[1]),
                        )
                        write(fp, rec)
                        count += 1
                        if count % 30 == 0:
                            fp.flush()
    finally:
        log.info("recorded %d frames", count)
        for s in subs.values():
            s.close()
        ctx.term()
    return 0


# -- Play ---------------------------------------------------------------------


def _run_play(cfg, args, log, stop: threading.Event) -> int:
    endpoints = {
        name: _rewrite_host(ep, args.bind_host)
        for name, ep in _enabled_channels(cfg).items()
    }
    if not endpoints:
        log.error("no sensor channels enabled in config")
        return 2
    ctx = zmq.Context()
    pubs = {name: transport.pub(ctx, ep) for name, ep in endpoints.items()}
    log.info("replay bound %s", endpoints)

    # SUB connect is racy-on-open; give subscribers a beat to connect before we
    # start replaying. (Pattern: slow-joiner problem.)
    time.sleep(0.25)

    try:
        records = list(read_all(args.path))
        if not records:
            log.warning("no records in %s", args.path)
            return 0

        if args.loop:
            log.info("looping replay (%d records per pass)", len(records))
        log.info("replaying %d records at speed=%.2fx", len(records), args.speed)

        pass_num = 0
        while not stop.is_set():
            pass_num += 1
            t_zero_file = records[0].ts_ns
            t_zero_wall = time.monotonic_ns()
            speed = max(args.speed, 1e-6)
            sent = 0
            for rec in records:
                if stop.is_set():
                    break
                offset_ns = int((rec.ts_ns - t_zero_file) / speed)
                _sleep_monotonic(t_zero_wall + offset_ns, stop)
                if stop.is_set():
                    break
                sock = pubs.get(rec.channel)
                if sock is None:
                    continue
                header_bytes = json.dumps(rec.header, separators=(",", ":")).encode("utf-8")
                sock.send_multipart([header_bytes, rec.payload], copy=False)
                sent += 1
            log.info("pass %d: sent %d records", pass_num, sent)
            if not args.loop:
                break
    finally:
        for s in pubs.values():
            s.close()
        ctx.term()
    return 0


# -- CLI ----------------------------------------------------------------------


def cli() -> int:
    p = argparse.ArgumentParser(description="Lexaire sensor replay harness")
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="mode", required=True)

    p_rec = sub.add_parser("record", help="SUB live sensor, write JSONL")
    p_rec.add_argument("path", help="output .jsonl path")
    p_rec.add_argument("--max-seconds", type=float, default=0.0,
                       help="stop after N seconds (0 = no limit)")

    p_play = sub.add_parser("play", help="replay a recorded .jsonl")
    p_play.add_argument("path", help="input .jsonl path")
    p_play.add_argument("--speed", type=float, default=1.0,
                        help="playback speed multiplier (default 1.0)")
    p_play.add_argument("--loop", action="store_true",
                        help="loop forever instead of stopping at end")
    p_play.add_argument("--bind-host", default="0.0.0.0",
                        help="host to bind PUB sockets on (default 0.0.0.0)")

    args = p.parse_args()

    cfg = load_config(args.config)
    log = logs.configure("replay", cfg.get("logging.level", "INFO"))

    stop = threading.Event()
    def _stop(*_):
        stop.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.mode == "record":
        return _run_record(cfg, args, log, stop)
    if args.mode == "play":
        return _run_play(cfg, args, log, stop)
    log.error("unknown mode: %s", args.mode)
    return 2


if __name__ == "__main__":
    sys.exit(cli())
