"""
Gemini VLM backend.

Calls a Gemini multimodal model with the current RGB frame, structured
scene+telemetry context, and the tool schema. Emits function-call responses
as ToolCall objects.

Install: `pip install 'lexaire[gemini]'`. Model is set by
`perception.vlm.model` in config.yaml (default `gemini-2.5-flash`).
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
            from google import genai  # type: ignore
            from google.genai import types as genai_types  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-genai not installed. "
                "pip install 'lexaire[gemini]' or `pip install google-genai`."
            ) from e

        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is unset — cannot start Gemini backend")

        self._types = genai_types
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name

        fn_decls = [
            genai_types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=t["parameters"],
            )
            for t in tool_schemas()
        ]
        self._config = genai_types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=temperature,
            tools=[genai_types.Tool(function_declarations=fn_decls)],
        )

    def decide(self, ctx: VlmContext) -> VlmDecision:
        parts: list = []
        img = self._encode_rgb(ctx.rgb)
        if img is not None:
            parts.append(img)

        parts.append(self._build_prompt(ctx))

        thought_parts: list[str] = []
        calls: list[ToolCall] = []

        try:
            resp = self._client.models.generate_content(
                model=self._model_name,
                contents=parts,
                config=self._config,
            )
            # Cap at the first candidate — if candidate_count >1, alternates'
            # tool_calls would concatenate and dispatch together.
            for candidate in (getattr(resp, "candidates", []) or [])[:1]:
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
        except Exception as e:
            log.exception("Gemini call failed: %s", e)
            # str(e) on google-genai errors can include the request URL
            # with the API-key tail; this string flows over the status
            # PUB to the TUI. Publish only the class name.
            return VlmDecision(thought=f"vlm_error: {type(e).__name__}", tool_calls=[])

        thought = " ".join(thought_parts).strip() or "(no text)"
        return VlmDecision(thought=thought, tool_calls=calls)

    # -- helpers --------------------------------------------------------------

    def _encode_rgb(self, rgb: Optional[np.ndarray]):
        if rgb is None:
            return None
        # BGR -> RGB, JPEG-in-memory for Gemini's inline image input.
        pil = Image.fromarray(rgb[..., ::-1].copy())
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=85)
        return self._types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

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

        # Compact recent telemetry trend — only the fields that matter for
        # decision-making, downsampled to ~5 samples to keep the prompt small.
        trend_keys = ("rel_alt_m", "ground_speed_mps", "battery_pct", "flight_mode", "armed")
        history = ctx.telemetry_history or []
        if len(history) > 5:
            step = max(1, len(history) // 5)
            history = history[::step]
        trend = [
            {k: h.get(k) for k in trend_keys if h.get(k) is not None}
            for h in history
        ]

        parts = [
            "You are the pilot. Use the tools to satisfy the pilot's spoken command "
            "while respecting the safety envelope (the flight bridge enforces it "
            "below you; ignore it at your peril).",
            "",
            f"Pilot command: {ctx.user_command!r}",
            "",
            f"Scene (local detector): {_json.dumps(scene_compact)}",
            "",
            f"Telemetry now: {_json.dumps({k: telem.get(k) for k in ('connected','armed','flight_mode','rel_alt_m','yaw_deg','ground_speed_mps','battery_pct')})}",
        ]
        if trend:
            parts.append(f"Telemetry trend (oldest→newest): {_json.dumps(trend)}")
        if ctx.mission:
            parts.append(f"Active mission: {_json.dumps(ctx.mission)}")
        parts.extend([
            "",
            f"Safety envelope: {_json.dumps(safety)}",
            "",
            "If the command is ambiguous or unsafe, call `hold`. If the pilot says anything that "
            "sounds like an emergency stop, call `abort`. Otherwise, reason about the RGB frame "
            "plus the scene list and emit the tool calls needed to satisfy the command. "
            "When a mission is active, prefer one tool call per turn — you'll be re-prompted "
            "after each call so you can react to the result before the next step.",
        ])
        return "\n".join(parts)


_SYSTEM_PROMPT = (
    "You are the in-flight pilot of a quadcopter. Input to you: an RGB frame from the "
    "drone's forward camera, a structured list of detections with camera-frame 3D "
    "coordinates, the current telemetry, and the pilot's latest spoken command. "
    "Output: function calls that satisfy the command. Prefer smaller, incremental "
    "maneuvers over single large jumps. Never emit a command you aren't certain of — "
    "`hold` is always a safe default."
)


def build_gemini(cfg) -> GeminiVLM:
    model = cfg.get("perception.vlm.model", "gemini-2.5-flash")
    api_key_var = cfg.get("perception.vlm.api_key_env", "GEMINI_API_KEY")
    api_key = os.environ.get(api_key_var, "")
    temp = float(cfg.get("perception.vlm.temperature", 0.2))
    return GeminiVLM(model_name=model, api_key=api_key, temperature=temp)
