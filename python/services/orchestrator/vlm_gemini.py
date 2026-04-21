"""
Gemini VLM backend.

Uses google-generativeai to call a Gemini multimodal model with the current
RGB frame, structured scene+telemetry context, and the tool schema. Emits
function-call responses as ToolCall objects.

Install: `pip install google-generativeai`
Model default: `gemini-2.0-flash-exp` (cheap/fast, good for dev). Config
`perception.vlm.model` overrides.
"""

from __future__ import annotations

import io
import json as _json
import logging
import os
from typing import Optional

import numpy as np
from PIL import Image

from lexaire.messages import ToolCall

from .tools import tool_schemas
from .vlm_base import VLM, VlmContext, VlmDecision

log = logging.getLogger(__name__)


class GeminiVLM(VLM):
    def __init__(self, model_name: str, api_key: str, temperature: float = 0.2):
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-generativeai not installed. "
                "pip install 'lexaire[gemini]' or `pip install google-generativeai`."
            ) from e

        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is unset — cannot start Gemini backend")

        genai.configure(api_key=api_key)
        self._genai = genai
        self._temperature = temperature

        tools = [{"function_declarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            } for t in tool_schemas()
        ]}]

        self._model = genai.GenerativeModel(
            model_name=model_name,
            tools=tools,
            system_instruction=_SYSTEM_PROMPT,
        )

    def decide(self, ctx: VlmContext) -> VlmDecision:
        parts: list = []
        img = self._encode_rgb(ctx.rgb)
        if img is not None:
            parts.append(img)

        prompt = self._build_prompt(ctx)
        parts.append(prompt)

        try:
            resp = self._model.generate_content(
                parts,
                generation_config={"temperature": self._temperature},
            )
        except Exception as e:
            log.exception("Gemini call failed: %s", e)
            return VlmDecision(thought=f"vlm_error: {e}", tool_calls=[])

        thought_parts: list[str] = []
        calls: list[ToolCall] = []

        for candidate in getattr(resp, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", []) or []:
                fn = getattr(part, "function_call", None)
                if fn is not None:
                    args = dict(fn.args) if fn.args is not None else {}
                    calls.append(ToolCall(
                        request_id=self.new_request_id(),
                        name=str(fn.name),
                        args=args,
                    ))
                    continue
                text = getattr(part, "text", None)
                if text:
                    thought_parts.append(str(text))

        thought = " ".join(thought_parts).strip() or "(no text)"
        return VlmDecision(thought=thought, tool_calls=calls)

    # -- helpers --------------------------------------------------------------

    def _encode_rgb(self, rgb: Optional[np.ndarray]):
        if rgb is None:
            return None
        # BGR -> RGB, to JPEG-in-memory for Gemini's inline image input.
        pil = Image.fromarray(rgb[..., ::-1].copy())
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=85)
        return {"mime_type": "image/jpeg", "data": buf.getvalue()}

    def _build_prompt(self, ctx: VlmContext) -> str:
        scene_compact = []
        for d in (ctx.scene or {}).get("detections", []) or []:
            scene_compact.append({
                "label": d.get("label"),
                "bbox": d.get("bbox_xywh"),
                "depth_m": d.get("depth_m"),
                "xyz_cam_m": d.get("xyz_cam_m"),
            })

        telem = ctx.telemetry or {}
        safety = ctx.safety or {}

        return (
            "You are the pilot. Use the tools to satisfy the pilot's spoken command "
            "while respecting the safety envelope (the flight bridge enforces it "
            "below you; ignore it at your peril).\n\n"
            f"Pilot command: {ctx.user_command!r}\n\n"
            f"Scene (local detector): {_json.dumps(scene_compact)}\n\n"
            f"Telemetry: {_json.dumps({k: telem.get(k) for k in ('connected','armed','flight_mode','rel_alt_m','yaw_deg','ground_speed_mps')})}\n\n"
            f"Safety envelope: {_json.dumps(safety)}\n\n"
            "If the command is ambiguous or unsafe, call `hold`. If the pilot says anything that "
            "sounds like an emergency stop, call `abort`. Otherwise, reason about the RGB frame "
            "plus the scene list and emit the tool calls needed to satisfy the command."
        )


_SYSTEM_PROMPT = (
    "You are the in-flight pilot of a quadcopter. Input to you: an RGB frame from the "
    "drone's forward camera, a structured list of detections with camera-frame 3D "
    "coordinates, the current telemetry, and the pilot's latest spoken command. "
    "Output: function calls that satisfy the command. Prefer smaller, incremental "
    "maneuvers over single large jumps. Never emit a command you aren't certain of — "
    "`hold` is always a safe default."
)


def build_gemini(cfg) -> GeminiVLM:
    model = cfg.get("perception.vlm.model", "gemini-2.0-flash-exp")
    api_key_var = cfg.get("perception.vlm.api_key_env", "GEMINI_API_KEY")
    api_key = os.environ.get(api_key_var, "")
    temp = float(cfg.get("perception.vlm.temperature", 0.2))
    return GeminiVLM(model_name=model, api_key=api_key, temperature=temp)
