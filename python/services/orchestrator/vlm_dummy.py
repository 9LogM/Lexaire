"""
Dummy VLM: keyword-based rules so the full pipeline is exercisable without
an API key.

Rules (first match wins):
    "abort" / "stop"            -> abort
    "arm"                        -> arm
    "disarm"                     -> disarm
    "takeoff" / "take off"       -> takeoff(altitude_m=1.5 or N from "to N m")
    "land"                       -> land
    "return" / "rtl" / "home"    -> return_to_launch
    "hold" / "hover"             -> hold
    "go to X" / "land on X"      -> goto_ned / land at the nearest detection labeled X
    anything else                -> no tools emitted (VLM shrugs)
"""

from __future__ import annotations

import re
from typing import Optional

from lexaire.messages import ToolCall

from .vlm_base import VLM, VlmContext, VlmDecision


_TAKEOFF_ALT_RE = re.compile(r"\bto\s+([0-9]+(?:\.[0-9]+)?)\s*m", re.IGNORECASE)


class DummyVLM(VLM):
    def decide(self, ctx: VlmContext) -> VlmDecision:
        text = (ctx.user_command or "").lower().strip()
        if not text:
            return VlmDecision(thought="empty command", tool_calls=[])

        if any(k in text for k in ("abort", "stop now")):
            return VlmDecision("pilot requested abort", [self._tc("abort")])

        if "disarm" in text:
            return VlmDecision("disarm on pilot request", [self._tc("disarm")])

        if "arm" in text and "disarm" not in text:
            return VlmDecision("arm on pilot request", [self._tc("arm")])

        if "takeoff" in text or "take off" in text:
            m = _TAKEOFF_ALT_RE.search(text)
            alt = float(m.group(1)) if m else 1.5
            return VlmDecision(f"takeoff to {alt:.1f}m", [self._tc("takeoff", {"altitude_m": alt})])

        if re.search(r"\b(land on|land at|land near)\b", text):
            target = self._find_target_from_command(text, ctx)
            if target is None:
                return VlmDecision("no matching object in scene", [self._tc("hold")])
            n, e, d = self._xyz_to_ned_goto(target)
            return VlmDecision(
                f"land on {target['label']}",
                [self._tc("goto_ned", {"n": n, "e": e, "d": d}), self._tc("land")],
            )

        if "land" in text:
            return VlmDecision("land in place", [self._tc("land")])

        if any(k in text for k in ("return", "rtl", "home")):
            return VlmDecision("return to launch", [self._tc("return_to_launch")])

        if "hold" in text or "hover" in text:
            return VlmDecision("hold at current position", [self._tc("hold")])

        if "go to" in text or "fly to" in text:
            target = self._find_target_from_command(text, ctx)
            if target is None:
                return VlmDecision("no matching object in scene", [self._tc("hold")])
            n, e, d = self._xyz_to_ned_goto(target)
            return VlmDecision(f"go to {target['label']}", [self._tc("goto_ned", {"n": n, "e": e, "d": d})])

        return VlmDecision(f"unrecognized: {text!r}", [])

    # -- helpers --------------------------------------------------------------

    def _tc(self, name: str, args: dict | None = None) -> ToolCall:
        return ToolCall(request_id=self.new_request_id(), name=name, args=args or {})

    def _find_target_from_command(self, text: str, ctx: VlmContext) -> Optional[dict]:
        detections = (ctx.scene or {}).get("detections") or []
        if not detections:
            return None

        # Extract candidate words from the command and pick the nearest
        # detection whose label matches any of them.
        words = {w.strip(",.?!") for w in text.split()}
        labeled = [d for d in detections if d.get("label", "").lower() in words]
        if not labeled:
            return None
        labeled.sort(key=lambda d: (d.get("depth_m") or 1e9))
        return labeled[0]

    def _xyz_to_ned_goto(self, detection: dict) -> tuple[float, float, float]:
        """
        Convert a detection's camera-frame xyz to a NED goto target.

        This is an approximation (no IMU-based pose rotation yet — phase 4
        SLAM will give us proper world coordinates). For now we treat the
        camera as forward-facing: camera X (right) -> NED east, camera Z
        (forward) -> NED north, and hold current altitude.
        """
        xyz = detection.get("xyz_cam_m")
        if not xyz:
            return 0.0, 0.0, 0.0
        x_cam, _y_cam, z_cam = xyz
        n = float(z_cam)
        e = float(x_cam)
        d = 0.0  # hold altitude; caller can choose to descend with a separate tool
        return n, e, d
