"""VLM interface. Backends subclass this."""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from lexaire.messages import ToolCall


@dataclass
class VlmContext:
    """Everything the VLM sees when asked to make a decision."""

    user_command: str
    telemetry: dict                 # decoded TelemetryHeader (latest sample)
    scene: dict                     # decoded SceneHeader (latest)
    rgb: Optional[np.ndarray] = None  # (H, W, 3) BGR, current frame — may be None
    history: list[str] = field(default_factory=list)
    safety: dict = field(default_factory=dict)
    # Recent telemetry samples (oldest -> newest) so the VLM can reason about
    # trends — battery dropping, approaching geofence, altitude unstable.
    # Phase 2A; depth controlled by orchestrator.telemetry_history_seconds.
    telemetry_history: list[dict] = field(default_factory=list)
    # Active multi-step mission, if any. None when the orchestrator is idle.
    # Phase 2A; populated when the orchestrator is running a re-prompt loop.
    mission: Optional[dict] = None


@dataclass
class VlmDecision:
    thought: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class VLM(abc.ABC):
    @abc.abstractmethod
    def decide(self, ctx: VlmContext) -> VlmDecision: ...

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex[:12]
