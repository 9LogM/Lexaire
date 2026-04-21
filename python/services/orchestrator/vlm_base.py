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
    telemetry: dict                 # decoded TelemetryHeader
    scene: dict                     # decoded SceneHeader (latest)
    rgb: Optional[np.ndarray] = None  # (H, W, 3) BGR, current frame — may be None
    history: list[str] = field(default_factory=list)
    safety: dict = field(default_factory=dict)


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
