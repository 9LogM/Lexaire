"""
Wire format between Lexaire services.

All messages are ZMQ multipart. Frame 0 is a UTF-8 JSON header; frame 1, when
present, is a binary payload. This module defines the header schemas and small
helpers to encode/decode them consistently.

Transport pattern per channel:
    sensor.rgb        PUB/SUB   header + jpeg bytes
    sensor.depth      PUB/SUB   header + zstd(z16 LE) bytes
    sensor.imu        PUB/SUB   header + 3xf32 LE bytes
    perception.scene  PUB/SUB   header only (JSON contains the detection list)
    telemetry         PUB/SUB   header only
    orch.status       PUB/SUB   header only
    orch.command      PUSH/PULL header only  (STT -> orchestrator)
    flight.tool       REQ/REP   header only
    flight.result     REQ/REP   header only (reply to tool)
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# -- Sensor streams -----------------------------------------------------------


@dataclass
class Intrinsics:
    width: int
    height: int
    fx: float
    fy: float
    ppx: float
    ppy: float
    model: str = "brown_conrady"
    coeffs: list[float] = field(default_factory=list)


@dataclass
class RgbHeader:
    ts_ns: int
    seq: int
    w: int
    h: int
    encoding: str          # "jpeg"
    intrinsics: dict       # Intrinsics dict


@dataclass
class DepthHeader:
    ts_ns: int
    seq: int
    w: int
    h: int
    encoding: str          # "zstd_z16_le"
    depth_scale_m: float
    intrinsics: dict


@dataclass
class ImuHeader:
    ts_ns: int
    seq: int
    type: str              # "accel" | "gyro"
    units: str             # "m/s^2" | "rad/s"


# -- Perception ---------------------------------------------------------------


@dataclass
class Detection:
    label: str
    bbox_xywh: list[float]       # [x, y, w, h] in RGB pixels
    confidence: float
    depth_m: Optional[float]     # median depth in bbox, or None if depth missing
    xyz_cam_m: Optional[list[float]]  # [x, y, z] in camera frame, meters


@dataclass
class SceneHeader:
    ts_ns: int
    frame_seq: int
    detections: list[dict]       # list of Detection dicts


# -- Telemetry ----------------------------------------------------------------


@dataclass
class TelemetryHeader:
    ts_ns: int
    connected: bool
    armed: bool
    flight_mode: str
    battery_pct: Optional[int]
    battery_v: Optional[float]
    lat: Optional[float]
    lon: Optional[float]
    abs_alt_m: Optional[float]
    rel_alt_m: Optional[float]
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    ground_speed_mps: float


# -- Orchestrator / tool calls ------------------------------------------------


@dataclass
class VoiceCommand:
    ts_ns: int
    text: str
    is_abort: bool = False


@dataclass
class ToolCall:
    request_id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    request_id: str
    ok: bool
    error: Optional[str] = None
    data: Any = None


@dataclass
class OrchestratorStatus:
    ts_ns: int
    state: str                 # "idle" | "thinking" | "executing" | "aborted"
    last_thought: str = ""
    last_action: str = ""


# -- Wire helpers -------------------------------------------------------------


def now_ns() -> int:
    return time.time_ns()


def encode_header(obj: Any) -> bytes:
    """Serialize a dataclass or dict to a UTF-8 JSON header frame."""
    if hasattr(obj, "__dataclass_fields__"):
        obj = asdict(obj)
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def decode_header(frame: bytes) -> dict:
    return json.loads(frame.decode("utf-8"))


def pack_imu(x: float, y: float, z: float) -> bytes:
    return struct.pack("<fff", x, y, z)


def unpack_imu(payload: bytes) -> tuple[float, float, float]:
    return struct.unpack("<fff", payload)
