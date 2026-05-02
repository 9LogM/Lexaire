"""
Detectors emit a per-tick scene graph (label, bbox, depth, camera-frame xyz)
that the orchestrator consumes alongside its own VLM reasoning.

Currently shipped: YoloDetector (Ultralytics YOLO11, COCO-80). Requires
`pip install 'lexaire[detector-yolo]'`; downloads weights on first run.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from lexaire.messages import Detection

log = logging.getLogger(__name__)


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


def _center_depth_and_xyz(depth, depth_scale_m, intrinsics, x, y, w, h):
    """Median depth over a bbox; back-project center to 3D camera-frame meters."""
    if depth is None or depth.size == 0 or depth_scale_m <= 0.0:
        return None, None

    # Original bbox center, NOT the clamped-patch center. For partial
    # off-frame detections the patch is shrunken to image bounds; the
    # ray we back-project should still point at where the object's
    # actual center is, otherwise xyz drifts toward the image edge.
    cx = x + w / 2.0
    cy = y + h / 2.0

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

    X = (cx - ppx) * z_m / fx
    Y = (cy - ppy) * z_m / fy
    return z_m, [X, Y, z_m]


class YoloDetector(Detector):
    """Ultralytics YOLO11 (COCO-80). Fast on CPU, faster on CUDA.

    Model weights auto-download on first use; cache at `~/.cache/ultralytics`
    inside the container (mount a volume if that matters for reproducibility).
    Confidence threshold and max detections are config-driven.
    """

    def __init__(
        self,
        weights: str = "yolo11n.pt",
        score_threshold: float = 0.35,
        max_detections: int = 20,
        device: str = "auto",
        classes: Optional[list[str]] = None,
    ):
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "ultralytics not installed. "
                "pip install 'lexaire[detector-yolo]' or `pip install ultralytics`."
            ) from e

        self._model = YOLO(weights)
        self._score = float(score_threshold)
        self._max_det = int(max_detections)
        self._device = None if device == "auto" else device
        self._class_filter: Optional[set[int]] = None
        if classes:
            name_to_id = {v: k for k, v in self._model.names.items()}
            wanted = {name_to_id[c] for c in classes if c in name_to_id}
            missing = [c for c in classes if c not in name_to_id]
            if missing:
                log.warning("YoloDetector: unknown classes ignored: %s", missing)
            # Empty `wanted` after filtering means EVERY requested class
            # was a typo/unknown. The previous `wanted or None` collapsed
            # this to "no filter" — silently emitting all classes, the
            # opposite of what the operator configured. Fail loud instead.
            if not wanted:
                known = sorted(self._model.names.values())
                raise ValueError(
                    "perception.detector.classes: none of the requested "
                    f"classes match the model's label set; got {classes!r}, "
                    f"first known labels: {known[:8]}..."
                )
            self._class_filter = wanted

    def detect(self, inp: DetectorInputs) -> list[Detection]:
        # BGR (our convention) -> RGB for ultralytics.
        rgb = inp.rgb[..., ::-1]
        predict_kwargs = dict(
            source=rgb,
            conf=self._score,
            max_det=self._max_det,
            verbose=False,
        )
        if self._device:
            predict_kwargs["device"] = self._device
        if self._class_filter:
            predict_kwargs["classes"] = sorted(self._class_filter)

        results = self._model.predict(**predict_kwargs)
        if not results:
            return []

        out: list[Detection] = []
        r = results[0]
        boxes = getattr(r, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []

        xyxy = boxes.xyxy.cpu().numpy()
        conf = boxes.conf.cpu().numpy()
        cls  = boxes.cls.cpu().numpy().astype(int)
        names = self._model.names

        for (x0, y0, x1, y1), c, k in zip(xyxy, conf, cls, strict=True):
            bw = float(x1 - x0)
            bh = float(y1 - y0)
            bx = float(x0)
            by = float(y0)
            depth_m, xyz = _center_depth_and_xyz(
                inp.depth, inp.depth_scale_m, inp.intrinsics, bx, by, bw, bh
            )
            out.append(Detection(
                label=str(names.get(int(k), f"id_{int(k)}")),
                bbox_xywh=[bx, by, bw, bh],
                confidence=float(c),
                depth_m=depth_m,
                xyz_cam_m=xyz,
            ))
        return out


def build_detector(cfg) -> Detector:
    model = cfg.get("perception.detector.model", "yolo")
    if model == "yolo":
        return YoloDetector(
            weights=cfg.get("perception.detector.weights", "yolo11n.pt"),
            score_threshold=float(cfg.get("perception.detector.score_threshold", 0.35)),
            max_detections=int(cfg.get("perception.detector.max_detections", 20)),
            device=cfg.get("perception.detector.device", "auto"),
            classes=cfg.get("perception.detector.classes", None),
        )
    raise ValueError(f"unknown perception.detector.model: {model!r}")
