"""VLA backend interface.

Backends subclass `VLA`, receive a `VlaContext` per tick, and return a
`VlaDecision` (zero or one tool call). Continuous control: one decide()
per orchestrator control tick (config: orchestrator.control_hz).

The shape is deliberately narrow — the orchestrator does not run a
mission state machine. The VLA holds whatever planning state it needs
internally (or operates purely reactively).
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from lexaire.messages import ToolCall


@dataclass
class VlaContext:
    """Per-tick input to the VLA.

    `instruction` is the operator's most recent voice/text command, held
    in context across ticks until the operator updates it. None means the
    VLA is idle (no instruction → emit no tool call).
    """

    instruction: Optional[str]
    rgb: Optional[np.ndarray]        # (H, W, 3) BGR; None when frame is stale
    telemetry: dict                   # latest TelemetryHeader
    safety: dict                      # safety envelope (max alt / geofence / max v)


@dataclass
class VlaDecision:
    """Per-tick output. `tool_call` is None when the VLA elects to do
    nothing this tick (operator hasn't said anything, or model is still
    warming up, or VLA explicitly emits a no-op).

    `thought` is opaque debug text surfaced in the TUI — backends may
    leave it empty.
    """

    thought: str = ""
    tool_call: Optional[ToolCall] = None


class VLA(abc.ABC):
    @abc.abstractmethod
    def decide(self, ctx: VlaContext) -> VlaDecision: ...

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex[:12]
