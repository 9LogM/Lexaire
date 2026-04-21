"""
Replay harness.

Three subcommands:

    record  — SUB to the live sensor channels, write a JSONL recording to disk.
    play    — read a JSONL recording and re-PUB it on the sensor channels,
              preserving the original inter-message timing.
    synth   — generate a stream of synthetic frames (static gray RGB, constant
              depth, zero IMU) to exercise the pipeline without a real sensor.

By design, this service *replaces* the drone's publisher in dev. The endpoints
are read from `sensor.channels.*`; `play` and `synth` bind, `record` connects.
`--bind-host` rewrites the bind side so `tcp://drone.local:5555` becomes
`tcp://0.0.0.0:5555` for local playback.
"""

from __future__ import annotations

import argparse
import io
import json
import signal
import struct
import sys
import threading
import time
from dataclasses import asdict
from typing import Optional
from urllib.parse import urlparse

import numpy as np
import zmq
import zstandard as zstd
from PIL import Image

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import now_ns

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


def _run_record(cfg, args, log, stop: threading.Event) -> int:
    channels = {
        "rgb":   cfg.get("sensor.channels.rgb"),
        "depth": cfg.get("sensor.channels.depth"),
        "imu":   cfg.get("sensor.channels.imu"),
    }
    subs = {name: transport.sub(ep) for name, ep in channels.items()}
    poller = zmq.Poller()
    for s in subs.values():
        poller.register(s, zmq.POLLIN)

    log.info("recording -> %s (Ctrl-C to stop)", args.path)
    count = 0
    start = time.monotonic_ns()
    deadline_ns = start + int(args.max_seconds * 1e9) if args.max_seconds > 0 else 0

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

    log.info("recorded %d frames", count)
    for s in subs.values():
        s.close()
    return 0


# -- Play ---------------------------------------------------------------------


def _run_play(cfg, args, log, stop: threading.Event) -> int:
    endpoints = {
        "rgb":   _rewrite_host(cfg.get("sensor.channels.rgb"),   args.bind_host),
        "depth": _rewrite_host(cfg.get("sensor.channels.depth"), args.bind_host),
        "imu":   _rewrite_host(cfg.get("sensor.channels.imu"),   args.bind_host),
    }
    pubs = {name: transport.pub(ep) for name, ep in endpoints.items()}
    log.info("replay bound %s", endpoints)

    # SUB connect is racy-on-open; give subscribers a beat to connect before we
    # start replaying. (Pattern: slow-joiner problem.)
    time.sleep(0.25)

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

    for s in pubs.values():
        s.close()
    return 0


# -- Synth --------------------------------------------------------------------


_SYNTH_W = 640
_SYNTH_H = 480
_SYNTH_INTRINSICS = {
    "width":  _SYNTH_W,
    "height": _SYNTH_H,
    "fx": 600.0,
    "fy": 600.0,
    "ppx": _SYNTH_W / 2,
    "ppy": _SYNTH_H / 2,
    "model": "brown_conrady",
    "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
}


def _synth_rgb_jpeg() -> bytes:
    # Mid-gray BGR so it compresses small and looks obviously synthetic.
    arr = np.full((_SYNTH_H, _SYNTH_W, 3), 128, dtype=np.uint8)
    pil = Image.fromarray(arr[..., ::-1])  # BGR -> RGB
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _synth_depth_zstd(zc: zstd.ZstdCompressor, range_m: float = 2.0) -> bytes:
    # depth_scale = 1mm; 2m -> 2000 raw units.
    value = int(round(range_m * 1000))
    arr = np.full((_SYNTH_H, _SYNTH_W), value, dtype=np.uint16)
    return zc.compress(arr.tobytes())


def _run_synth(cfg, args, log, stop: threading.Event) -> int:
    endpoints = {
        "rgb":   _rewrite_host(cfg.get("sensor.channels.rgb"),   args.bind_host),
        "depth": _rewrite_host(cfg.get("sensor.channels.depth"), args.bind_host),
        "imu":   _rewrite_host(cfg.get("sensor.channels.imu"),   args.bind_host),
    }
    pubs = {name: transport.pub(ep) for name, ep in endpoints.items()}
    log.info("synth bound %s", endpoints)
    time.sleep(0.25)

    zc = zstd.ZstdCompressor(level=1)
    jpeg_cached = _synth_rgb_jpeg()
    depth_cached = _synth_depth_zstd(zc)
    depth_scale_m = 0.001

    video_seq = 0
    accel_seq = 0
    gyro_seq = 0

    period = 1.0 / max(args.fps, 1e-3)
    imu_period = 1.0 / max(args.imu_hz, 1e-3)
    next_video = time.monotonic()
    next_imu = time.monotonic()

    while not stop.is_set():
        now = time.monotonic()
        fired = False

        if now >= next_video:
            ts = now_ns()
            rgb_hdr = {
                "ts_ns": ts, "seq": video_seq,
                "w": _SYNTH_W, "h": _SYNTH_H,
                "encoding": "jpeg",
                "intrinsics": _SYNTH_INTRINSICS,
            }
            depth_hdr = {
                "ts_ns": ts, "seq": video_seq,
                "w": _SYNTH_W, "h": _SYNTH_H,
                "encoding": "zstd_z16_le",
                "depth_scale_m": depth_scale_m,
                "intrinsics": _SYNTH_INTRINSICS,
            }
            pubs["rgb"].send_multipart(
                [json.dumps(rgb_hdr, separators=(",", ":")).encode("utf-8"), jpeg_cached],
                copy=False,
            )
            pubs["depth"].send_multipart(
                [json.dumps(depth_hdr, separators=(",", ":")).encode("utf-8"), depth_cached],
                copy=False,
            )
            video_seq += 1
            next_video += period
            fired = True

        if now >= next_imu:
            ts = now_ns()
            accel_hdr = {"ts_ns": ts, "seq": accel_seq, "type": "accel", "units": "m/s^2"}
            gyro_hdr  = {"ts_ns": ts, "seq": gyro_seq,  "type": "gyro",  "units": "rad/s"}
            # Zero motion: drone sitting still.
            payload_a = struct.pack("<fff", 0.0, 0.0, 9.81)
            payload_g = struct.pack("<fff", 0.0, 0.0, 0.0)
            pubs["imu"].send_multipart(
                [json.dumps(accel_hdr, separators=(",", ":")).encode("utf-8"), payload_a],
                copy=False,
            )
            pubs["imu"].send_multipart(
                [json.dumps(gyro_hdr, separators=(",", ":")).encode("utf-8"), payload_g],
                copy=False,
            )
            accel_seq += 1
            gyro_seq += 1
            next_imu += imu_period
            fired = True

        if not fired:
            time.sleep(min(next_video - now, next_imu - now, 0.05))

    for s in pubs.values():
        s.close()
    log.info("synth stopped  video_frames=%d", video_seq)
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

    p_syn = sub.add_parser("synth", help="generate synthetic frames forever")
    p_syn.add_argument("--fps", type=float, default=15.0,
                       help="RGB+depth frames per second (default 15)")
    p_syn.add_argument("--imu-hz", type=float, default=100.0,
                       help="IMU sample rate (default 100)")
    p_syn.add_argument("--bind-host", default="0.0.0.0",
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
    if args.mode == "synth":
        return _run_synth(cfg, args, log, stop)
    log.error("unknown mode: %s", args.mode)
    return 2


if __name__ == "__main__":
    sys.exit(cli())
