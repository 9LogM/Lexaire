"""
VLA backend.

Concrete VLA model wired in here. Inputs: latest RGB frame, operator's
most recent instruction (held in context), telemetry, safety envelope.
Output: at most one tool call per tick.

This file is intentionally a thin wrapper. The orchestrator handles the
control loop, voice intake, and dispatch. The VLA's only job is to map
(observation, language) -> action.

Pick your model:

    OpenVLA      — github.com/openvla/openvla       (HF transformers, ~7B params)
    π0           — github.com/Physical-Intelligence/openpi
    Octo         — github.com/octo-models/octo      (~93M, lightweight)
    RT-2 family  — closed-source; you'd need a custom inference server
    custom       — your fine-tune

Local vs remote inference is your call: instantiate the model in
__init__ for local, or treat decide() as a thin RPC for remote.
"""

from __future__ import annotations

import logging
from typing import Optional

from lexaire.messages import ToolCall

from .vla_base import VLA, VlaContext, VlaDecision

log = logging.getLogger(__name__)


class _NoOpVLA(VLA):
    """Default backend until a real VLA is wired in. Always returns no
    action. Lets the orchestrator's control loop exercise end-to-end on
    bench without a model loaded."""

    def decide(self, ctx: VlaContext) -> VlaDecision:
        return VlaDecision(thought="no_op", tool_call=None)


class GenericVlaBackend(VLA):
    """Concrete VLA wrapper.

    Replace `_load_model` and `_infer` with your model's load/forward
    calls. The control loop's tick rate, frame staleness gate, and
    voice-arm wiring are owned by the orchestrator — this class only
    needs to be deterministic given (rgb, instruction, telemetry).
    """

    def __init__(self, model_path: str, device: str = "auto",
                  control_rate_hz: float = 10.0):
        self._model_path = model_path
        self._device = device
        self._control_rate_hz = control_rate_hz
        self._model = self._load_model()
        log.info("VLA backend ready  model=%s device=%s rate=%.1fHz",
                 model_path, device, control_rate_hz)

    def _load_model(self):
        # Replace with the chosen VLA's loader (HF transformers, local
        # checkpoint, gRPC client, etc.). Returning None for now so the
        # class is a clear "TODO: drop the model in here" landmark.
        raise NotImplementedError(
            "GenericVlaBackend._load_model: drop your VLA here. "
            "See module docstring for candidate models."
        )

    def _infer(self, rgb, instruction: str, telemetry: dict) -> Optional[dict]:
        # Replace with the chosen VLA's forward pass. Return action dict
        # in the shape `{vx, vy, vz, yaw_deg}` (omit any of vx/vy/vz that
        # the model doesn't emit), or None for no-op.
        raise NotImplementedError("GenericVlaBackend._infer: forward pass goes here.")

    def decide(self, ctx: VlaContext) -> VlaDecision:
        # No instruction yet → idle. Avoids commanding random setpoints
        # before the operator has said anything.
        if not ctx.instruction:
            return VlaDecision(thought="idle (no instruction)", tool_call=None)
        # No fresh frame → idle. The orchestrator already age-gates rgb;
        # if it's None, the publisher is dead and the VLA shouldn't
        # extrapolate from staleness.
        if ctx.rgb is None:
            return VlaDecision(thought="idle (no fresh frame)", tool_call=None)
        action = self._infer(ctx.rgb, ctx.instruction, ctx.telemetry)
        if action is None:
            return VlaDecision(thought="model emitted no-op", tool_call=None)
        return VlaDecision(
            thought=f"vla -> {action}",
            tool_call=ToolCall(
                request_id=self.new_request_id(),
                name="set_velocity_ned",
                args=action,
            ),
        )


def build_vla(cfg) -> VLA:
    """Factory. Reads `perception.vla.*` from config; defaults to no-op
    so a config without a model path doesn't crash startup."""
    model_path = cfg.get("perception.vla.model_path")
    if not model_path:
        log.warning("perception.vla.model_path not set — using no-op VLA")
        return _NoOpVLA()
    device = cfg.get("perception.vla.device", "auto")
    rate = float(cfg.get("orchestrator.control_hz", 10.0))
    return GenericVlaBackend(model_path=model_path, device=device,
                              control_rate_hz=rate)
