"""
Perception service.

Subscribes to the RGB and depth streams from the Pi-side sensor publisher,
throttles to `perception.tick_hz`, runs a detector on the latest
synchronized frameset, and publishes a SceneHeader on
`services.perception_scene_pub`. Provides a cheap, always-available scene
graph at a stable tick rate; the orchestrator may still reason on pixels
directly via its own VLM.
"""

from __future__ import annotations

import argparse
import dataclasses
import signal
import time

import zmq

from lexaire import logs, transport
from lexaire.config import load_config
from lexaire.messages import SceneHeader, encode_header
from lexaire.subscriber import SensorSubscriber

from .detectors import DetectorInputs, build_detector


def cli() -> int:
    p = argparse.ArgumentParser(description="Lexaire perception service")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--once", action="store_true", help="produce one scene then exit (for tests)")
    args = p.parse_args()

    cfg = load_config(args.config)
    log = logs.configure("perception", cfg.get("logging.level", "INFO"))

    tick_hz = float(cfg.get("perception.tick_hz", 2.0))
    scene_pub_ep = cfg.require("services.perception_scene_pub")

    detector = build_detector(cfg)

    ctx = zmq.Context()
    sub = SensorSubscriber(
        ctx,
        rgb_endpoint=cfg.require("sensor.channels.rgb"),
        depth_endpoint=cfg.require("sensor.channels.depth"),
    )
    pub = transport.pub(ctx, scene_pub_ep)

    stop = False
    def _stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    sub.start()
    log.info("perception up  tick=%.1fHz  scene_pub=%s", tick_hz, scene_pub_ep)

    interval_s = 1.0 / tick_hz
    last_tick = 0.0
    latest_fs = None
    # --once bails after 10s with no frame so a missing publisher
    # doesn't hang CI runs.
    once_deadline = time.monotonic() + 10.0 if args.once else None

    try:
        while not stop:
            fs = sub.get_nowait()
            if fs is not None:
                latest_fs = fs

            now = time.monotonic()
            if once_deadline is not None and latest_fs is None and now >= once_deadline:
                log.error("--once: no frame from publisher within 10s; exiting")
                return 2
            if now - last_tick < interval_s:
                time.sleep(min(interval_s - (now - last_tick), 0.05))
                continue
            last_tick = now

            if latest_fs is None:
                continue

            inp = DetectorInputs(
                rgb=latest_fs.rgb,
                depth=latest_fs.depth,
                depth_scale_m=latest_fs.depth_scale_m,
                intrinsics=latest_fs.intrinsics,
            )
            try:
                detections = detector.detect(inp)
            except Exception as e:
                log.exception("detector error: %s", e)
                continue

            header = SceneHeader(
                ts_ns=latest_fs.ts_ns,
                frame_seq=latest_fs.seq,
                detections=[dataclasses.asdict(d) for d in detections],
            )
            pub.send(encode_header(header))
            log.debug("scene ts_ns=%d seq=%d n=%d", header.ts_ns, header.frame_seq, len(header.detections))

            if args.once:
                # transport.pub() sets LINGER=0 by default — fine for the
                # streaming case where missing one frame doesn't matter,
                # but for --once mode we need a graceful drain or the
                # one-and-only frame can vanish on close.
                pub.setsockopt(zmq.LINGER, 500)
                break
    finally:
        sub.stop()
        pub.close()
        ctx.term()
        log.info("perception shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
