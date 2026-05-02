"""
Wire format between Lexaire services.

All messages are ZMQ multipart. Frame 0 is a UTF-8 JSON header; frame 1, when
present, is a binary payload.

Channels (all sensor.* channels emit a 2-frame multipart so consumers
can recv_multipart uniformly; IMU's payload frame is empty):
    sensor.rgb        PUB/SUB   header + jpeg bytes
    sensor.depth      PUB/SUB   header + zstd(z16 LE) bytes
    sensor.imu        PUB/SUB   header (accel + gyro samples in JSON) + empty
    sensor.infrared   PUB/SUB   header + zstd(y8) bytes
    sensor.confidence PUB/SUB   header + zstd(raw8) bytes
    perception.scene  PUB/SUB   header only (JSON contains the detection list)
    telemetry         PUB/SUB   header only
    orch.status       PUB/SUB   header only
    orch.command      PUSH/PULL header only  (STT -> orchestrator)
    flight.toolcall   REQ/REP   ToolCall request -> ToolResult reply
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Detection:
    label: str
    bbox_xywh: list[float]           # [x, y, w, h] in RGB pixels
    confidence: float
    depth_m: Optional[float]         # median depth in bbox, or None if depth missing
    xyz_cam_m: Optional[list[float]] # [x, y, z] in camera frame, meters


@dataclass
class SceneHeader:
    ts_ns: int
    frame_seq: int
    detections: list[dict]           # list of Detection dicts


@dataclass
class VoiceCommand:
    ts_ns: int
    text: str
    is_abort: bool = False


@dataclass
class ToolCall:
    request_id: str
    name: str
    # Match the C++ side (lexaire/messages.hpp:ToolCall::from_json), which
    # decodes a missing `args` field as an empty object. Without the default
    # here, constructing a no-arg tool call (e.g. arm/disarm/abort/kill)
    # would TypeError on the keyword.
    args: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    request_id: str
    ok: bool
    error: Optional[str] = None
    data: Any = None


@dataclass
class OrchestratorStatus:
    ts_ns: int
    state: str                       # "idle" | "thinking" | "executing" | "aborted" | "bridge_offline" | "vlm_error"
    last_thought: str = ""


def now_ns() -> int:
    return time.time_ns()


def encode_header(obj: Any) -> bytes:
    """Serialize a dataclass or dict to a UTF-8 JSON header frame."""
    if hasattr(obj, "__dataclass_fields__"):
        obj = asdict(obj)
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def decode_header(frame: bytes) -> dict:
    return json.loads(frame.decode("utf-8"))
