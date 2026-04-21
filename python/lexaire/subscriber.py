"""
Sensor subscriber.

Subscribes to the three ZMQ publishers the Pi's L515 container emits (RGB,
depth, IMU), decodes each frame, and yields synchronized framesets joined on
the librealsense sequence number.

Synchronization strategy: the Pi publishes color and depth from the same
`pipeline.wait_for_frames()` so their sequence numbers align one-to-one. We
buffer up to N unmatched frames per stream (bounded) and emit when both are
present for the same seq, or on timeout. IMU is attached as the set of samples
whose ts_ns falls in [prev_frame_ts, this_frame_ts].
"""

from __future__ import annotations

import io
import logging
import queue
import struct
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np
import zmq
import zstandard as zstd
from PIL import Image

from . import messages as msg

log = logging.getLogger(__name__)


@dataclass
class ImuSample:
    ts_ns: int
    type: str       # "accel" | "gyro"
    xyz: tuple[float, float, float]


@dataclass
class Frameset:
    ts_ns: int
    seq: int
    rgb: np.ndarray              # (H, W, 3) uint8, BGR
    depth: np.ndarray            # (H, W) uint16
    depth_scale_m: float
    intrinsics: dict             # color intrinsics (depth aligned to color)
    imu_since_last: list[ImuSample]  # accel + gyro samples between prev and this frame


class SensorSubscriber:
    """
    Runs a background thread per stream + a matcher thread. Use as an iterable:

        sub = SensorSubscriber(cfg)
        sub.start()
        for fs in sub.framesets():
            ...

    Call `stop()` to shut down cleanly.
    """

    def __init__(
        self,
        rgb_endpoint: str,
        depth_endpoint: str,
        imu_endpoint: str,
        *,
        buffer_size: int = 8,
        imu_window_ns: int = 200_000_000,   # 200 ms of IMU history kept
        match_timeout_ns: int = 100_000_000,  # 100 ms to wait for the twin frame
    ):
        self.rgb_ep = rgb_endpoint
        self.depth_ep = depth_endpoint
        self.imu_ep = imu_endpoint
        self.buffer_size = buffer_size
        self.imu_window_ns = imu_window_ns
        self.match_timeout_ns = match_timeout_ns

        self._ctx = zmq.Context.instance()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._zdecomp = zstd.ZstdDecompressor()

        # Keyed by seq. Bounded via _trim().
        self._rgb_buf: dict[int, tuple[dict, np.ndarray]] = {}
        self._depth_buf: dict[int, tuple[dict, np.ndarray]] = {}
        self._buf_lock = threading.Lock()
        self._new_frame = threading.Condition(self._buf_lock)

        self._imu_buf: deque[ImuSample] = deque(maxlen=4096)
        self._imu_lock = threading.Lock()

        self._out: queue.Queue[Frameset] = queue.Queue(maxsize=16)
        self._last_emitted_ts_ns: int = 0

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._start_thread(self._run_rgb, "lexaire-sub-rgb")
        self._start_thread(self._run_depth, "lexaire-sub-depth")
        self._start_thread(self._run_imu, "lexaire-sub-imu")
        self._start_thread(self._run_matcher, "lexaire-sub-matcher")
        log.info("SensorSubscriber started rgb=%s depth=%s imu=%s",
                 self.rgb_ep, self.depth_ep, self.imu_ep)

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

    # -- Public iterator ------------------------------------------------------

    def framesets(self, timeout: float = 1.0) -> Iterator[Frameset]:
        while not self._stop.is_set():
            try:
                yield self._out.get(timeout=timeout)
            except queue.Empty:
                continue

    def get_nowait(self) -> Optional[Frameset]:
        try:
            return self._out.get_nowait()
        except queue.Empty:
            return None

    # -- Stream threads -------------------------------------------------------

    def _subscribe(self, endpoint: str) -> zmq.Socket:
        s = self._ctx.socket(zmq.SUB)
        s.setsockopt(zmq.SUBSCRIBE, b"")
        s.setsockopt(zmq.RCVHWM, 8)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.CONNECT_TIMEOUT, 1000)
        s.connect(endpoint)
        return s

    def _run_rgb(self) -> None:
        sock = self._subscribe(self.rgb_ep)
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
        sock.close()

    def _run_depth(self) -> None:
        sock = self._subscribe(self.depth_ep)
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
        sock.close()

    def _run_imu(self) -> None:
        sock = self._subscribe(self.imu_ep)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self._stop.is_set():
            events = dict(poller.poll(250))
            if sock not in events:
                continue
            try:
                hdr_bytes, payload = sock.recv_multipart(copy=False)
                hdr = msg.decode_header(bytes(hdr_bytes))
                xyz = struct.unpack("<fff", bytes(payload))
            except Exception as e:
                log.warning("imu decode error: %s", e)
                continue
            sample = ImuSample(ts_ns=int(hdr["ts_ns"]), type=hdr["type"], xyz=xyz)
            with self._imu_lock:
                self._imu_buf.append(sample)
                cutoff = sample.ts_ns - self.imu_window_ns
                while self._imu_buf and self._imu_buf[0].ts_ns < cutoff:
                    self._imu_buf.popleft()
        sock.close()

    # -- Matching -------------------------------------------------------------

    def _trim_locked(self, buf: dict) -> None:
        while len(buf) > self.buffer_size:
            buf.pop(next(iter(buf)))

    def _run_matcher(self) -> None:
        while not self._stop.is_set():
            with self._new_frame:
                matched = self._try_match_locked()
            for fs in matched:
                try:
                    self._out.put(fs, timeout=0.1)
                except queue.Full:
                    # downstream too slow; drop oldest
                    try:
                        self._out.get_nowait()
                        self._out.put_nowait(fs)
                    except Exception:
                        pass
            with self._new_frame:
                self._new_frame.wait(timeout=0.1)

    def _try_match_locked(self) -> list[Frameset]:
        out: list[Frameset] = []
        common = sorted(set(self._rgb_buf.keys()) & set(self._depth_buf.keys()))
        for seq in common:
            rgb_hdr, rgb = self._rgb_buf.pop(seq)
            depth_hdr, depth = self._depth_buf.pop(seq)
            ts = int(rgb_hdr["ts_ns"])
            imu = self._slice_imu(self._last_emitted_ts_ns, ts)
            out.append(Frameset(
                ts_ns=ts,
                seq=seq,
                rgb=rgb,
                depth=depth,
                depth_scale_m=float(depth_hdr.get("depth_scale_m") or 0.0),
                intrinsics=rgb_hdr["intrinsics"],
                imu_since_last=imu,
            ))
            self._last_emitted_ts_ns = ts
        return out

    def _slice_imu(self, lo_ns: int, hi_ns: int) -> list[ImuSample]:
        with self._imu_lock:
            return [s for s in self._imu_buf if lo_ns < s.ts_ns <= hi_ns]
