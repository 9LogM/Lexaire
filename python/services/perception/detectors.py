"""
Detector backends for the perception service.

Two abstractions coexist by design:

* VLM backends — the model itself emits detections given the frame. A VLM
  orchestrator may bypass this step entirely and reason on pixels directly,
  but for quick-inner-loop reporting to the orchestrator we still produce a
  list of (label, bbox, depth) so downstream logic has something to reason
  about.
* Detector backends — a purpose-built open-vocab detector (GroundingDINO,
  YOLO-World) runs locally and returns bboxes. Later phases can wire one in.

For now, only the dummy detector is implemented; it fabricates a single
"table" detection at the center of the frame so the rest of the pipeline is
exercised end-to-end without a real model.
"""

from __future__ import annotations

import abc
import random
from dataclasses import dataclass

import numpy as np

from lexaire.messages import Detection


@dataclass
class DetectorInputs:
    rgb: np.ndarray          # (H, W, 3) uint8 BGR
    depth: np.ndarray        # (H, W) uint16
    depth_scale_m: float
    intrinsics: dict


class Detector(abc.ABC):
    @abc.abstractmethod
    def detect(self, inp: DetectorInputs) -> list[Detection]:
        ...


class DummyDetector(Detector):
    """Fabricates a detection at the frame center so the pipeline has data."""

    LABELS = ["table", "chair", "doorway", "person", "box"]

    def __init__(self, seed: int = 0, label_cycle: bool = True):
        self._rng = random.Random(seed)
        self._i = 0
        self._cycle = label_cycle

    def detect(self, inp: DetectorInputs) -> list[Detection]:
        h, w = inp.rgb.shape[:2]
        bw, bh = int(w * 0.3), int(h * 0.3)
        bx, by = (w - bw) // 2, (h - bh) // 2

        label = self.LABELS[self._i % len(self.LABELS)] if self._cycle else "object"
        self._i += 1

        depth_m, xyz = _center_depth_and_xyz(inp.depth, inp.depth_scale_m, inp.intrinsics, bx, by, bw, bh)
        return [Detection(
            label=label,
            bbox_xywh=[float(bx), float(by), float(bw), float(bh)],
            confidence=0.9,
            depth_m=depth_m,
            xyz_cam_m=xyz,
        )]


def _center_depth_and_xyz(depth, depth_scale_m, intrinsics, x, y, w, h):
    """Median depth over a bbox; back-project center to 3D camera-frame meters."""
    if depth is None or depth.size == 0 or depth_scale_m <= 0.0:
        return None, None

    x0 = max(0, int(x))
    y0 = max(0, int(y))
    x1 = min(depth.shape[1], int(x + w))
    y1 = min(depth.shape[0], int(y + h))
    if x1 <= x0 or y1 <= y0:
        return None, None

    patch = depth[y0:y1, x0:x1]
    nz = patch[patch > 0]
    if nz.size == 0:
        return None, None
    z_m = float(np.median(nz)) * float(depth_scale_m)

    fx = float(intrinsics.get("fx", 0.0))
    fy = float(intrinsics.get("fy", 0.0))
    ppx = float(intrinsics.get("ppx", 0.0))
    ppy = float(intrinsics.get("ppy", 0.0))
    if fx <= 0 or fy <= 0:
        return z_m, None

    cx = x0 + (x1 - x0) / 2.0
    cy = y0 + (y1 - y0) / 2.0
    X = (cx - ppx) * z_m / fx
    Y = (cy - ppy) * z_m / fy
    return z_m, [X, Y, z_m]


def build_detector(cfg) -> Detector:
    backend = cfg.get("perception.backend", "vlm")
    if backend == "vlm":
        # In VLM mode, the orchestrator owns reasoning about pixels directly.
        # The perception service still runs a cheap local detector so it can
        # publish an approximate scene graph on its own tick.
        return DummyDetector()
    if backend == "detector":
        model = cfg.get("perception.detector.model", "dummy")
        if model == "grounding-dino":
            raise NotImplementedError("grounding-dino detector not wired yet (phase 1 work)")
        return DummyDetector()
    raise ValueError(f"unknown perception backend: {backend}")
