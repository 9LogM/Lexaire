"""
Sensor subscriber.

Subscribes to the RGB and depth ZMQ publishers configured in `sensor.channels`,
decodes each frame, and yields synchronized framesets joined on the publisher's
sequence number. The publisher must emit matching seq values for the rgb and
depth frames captured together so the matcher can pair them one-to-one.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import zmq
import zstandard as zstd
from PIL import Image

from . import messages as msg
from . import transport

log = logging.getLogger(__name__)


@dataclass
class Frameset:
    ts_ns: int
    seq: int
    rgb: np.ndarray              # (H, W, 3) uint8, BGR
    depth: np.ndarray            # (H, W) uint16
    depth_scale_m: float
    intrinsics: dict             # color intrinsics (depth aligned to color)


class SensorSubscriber:
    """Background RGB+depth subscriber with matcher. Call start() then poll
    get_nowait()."""

    def __init__(
        self,
        ctx: zmq.Context,
        rgb_endpoint: str,
        depth_endpoint: str,
        *,
        buffer_size: int = 8,
    ):
        self.rgb_ep = rgb_endpoint
        self.depth_ep = depth_endpoint
        self.buffer_size = buffer_size

        self._ctx = ctx
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._zdecomp = zstd.ZstdDecompressor()

        # Keyed by seq. Bounded via _trim_locked.
        self._rgb_buf: dict[int, tuple[dict, np.ndarray]] = {}
        self._depth_buf: dict[int, tuple[dict, np.ndarray]] = {}
        self._buf_lock = threading.Lock()
        self._new_frame = threading.Condition(self._buf_lock)

        self._out: queue.Queue[Frameset] = queue.Queue(maxsize=16)

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._start_thread(self._run_rgb, "lexaire-sub-rgb")
        self._start_thread(self._run_depth, "lexaire-sub-depth")
        self._start_thread(self._run_matcher, "lexaire-sub-matcher")
        log.info("SensorSubscriber started rgb=%s depth=%s", self.rgb_ep, self.depth_ep)

    def stop(self) -> None:
        self._stop.set()
        with self._new_frame:
            self._new_frame.notify_all()
        for t in self._threads:
            t.join(timeout=2.0)

    def _start_thread(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # -- Consumer API ---------------------------------------------------------

    def get_nowait(self) -> Optional[Frameset]:
        try:
            return self._out.get_nowait()
        except queue.Empty:
            return None

    def get(self, timeout: float) -> Optional[Frameset]:
        """Block up to `timeout` seconds for the next Frameset, None on timeout."""
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- Stream threads -------------------------------------------------------

    def _run_rgb(self) -> None:
        sock = transport.sub(self._ctx, self.rgb_ep, connect_timeout_ms=1000)
        try:
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self._stop.is_set():
                events = dict(poller.poll(250))
                if sock not in events:
                    continue
                try:
                    hdr_bytes, payload = sock.recv_multipart(copy=False)
                    hdr = msg.decode_header(bytes(hdr_bytes))
                    img = np.asarray(Image.open(io.BytesIO(bytes(payload))).convert("RGB"))
                    bgr = img[..., ::-1].copy()
                except Exception as e:
                    log.warning("rgb decode error: %s", e)
                    continue
                with self._new_frame:
                    self._rgb_buf[int(hdr["seq"])] = (hdr, bgr)
                    self._trim_locked(self._rgb_buf)
                    self._new_frame.notify()
        finally:
            sock.close()

    def _run_depth(self) -> None:
        sock = transport.sub(self._ctx, self.depth_ep, connect_timeout_ms=1000)
        try:
            poller = zmq.Poller()
            poller.register(sock, zmq.POLLIN)
            while not self._stop.is_set():
                events = dict(poller.poll(250))
                if sock not in events:
                    continue
                try:
                    hdr_bytes, payload = sock.recv_multipart(copy=False)
                    hdr = msg.decode_header(bytes(hdr_bytes))
                    raw = self._zdecomp.decompress(bytes(payload))
                    depth = np.frombuffer(raw, dtype="<u2").reshape(hdr["h"], hdr["w"])
                except Exception as e:
                    log.warning("depth decode error: %s", e)
                    continue
                with self._new_frame:
                    self._depth_buf[int(hdr["seq"])] = (hdr, depth)
                    self._trim_locked(self._depth_buf)
                    self._new_frame.notify()
        finally:
            sock.close()

    # -- Matching -------------------------------------------------------------

    def _trim_locked(self, buf: dict) -> None:
        while len(buf) > self.buffer_size:
            buf.pop(next(iter(buf)))

    def _run_matcher(self) -> None:
        while not self._stop.is_set():
            with self._new_frame:
                matched = self._try_match_locked()
                if not matched:
                    # Wait inside the lock — a notify between unlock and
                    # wait would otherwise be lost.
                    self._new_frame.wait(timeout=0.1)
                    matched = self._try_match_locked()
            for fs in matched:
                try:
                    self._out.put(fs, timeout=0.1)
                except queue.Full:
                    # Drop oldest; sustained drops mean the consumer can't keep up.
                    try:
                        dropped = self._out.get_nowait()
                        self._out.put_nowait(fs)
                        log.warning("frameset queue full; dropped seq=%d for seq=%d",
                                    dropped.seq, fs.seq)
                    except (queue.Empty, queue.Full) as e:
                        log.warning("frameset queue churn (seq=%d): %s; frame dropped",
                                    fs.seq, e.__class__.__name__)

    def _try_match_locked(self) -> list[Frameset]:
        out: list[Frameset] = []
        common = sorted(set(self._rgb_buf.keys()) & set(self._depth_buf.keys()))
        for seq in common:
            rgb_hdr, rgb = self._rgb_buf.pop(seq)
            depth_hdr, depth = self._depth_buf.pop(seq)
            out.append(Frameset(
                ts_ns=int(rgb_hdr["ts_ns"]),
                seq=seq,
                rgb=rgb,
                depth=depth,
                depth_scale_m=float(depth_hdr.get("depth_scale_m") or 0.0),
                intrinsics=rgb_hdr["intrinsics"],
            ))
        return out
