"""
Safety envelope — non-LLM-overridable limits checked before any tool call.

The orchestrator never imports this to *ask permission* — the flight bridge
owns enforcement, and a tool call that violates safety is rejected regardless
of what the VLM decided. The types here are shared so the orchestrator can
surface the same numbers in its prompt context (the VLM is told what the
limits are, but can't move them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SafetyEnvelope:
    max_altitude_m: float
    geofence_radius_m: float
    max_velocity_mps: float
    min_obstacle_distance_m: float
    require_spoken_arm: bool
    abort_keyword: str

    @classmethod
    def from_config(cls, cfg) -> "SafetyEnvelope":
        s = cfg.safety
        return cls(
            max_altitude_m=float(s.max_altitude_m),
            geofence_radius_m=float(s.geofence_radius_m),
            max_velocity_mps=float(s.max_velocity_mps),
            min_obstacle_distance_m=float(s.min_obstacle_distance_m),
            require_spoken_arm=bool(s.require_spoken_arm),
            abort_keyword=str(cfg.get("stt.abort_keyword", "abort")).lower(),
        )


@dataclass
class SafetyState:
    """Runtime state that the safety checker reads each tick."""
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    current_rel_alt_m: float = 0.0
    min_obstacle_m: float = float("inf")
    aborted: bool = False
    armed_with_voice: bool = False


@dataclass
class SafetyDecision:
    allow: bool
    reason: str = ""


def check_tool(name: str, args: dict, env: SafetyEnvelope, state: SafetyState) -> SafetyDecision:
    """
    Apply the non-overridable safety envelope to a proposed tool call.

    This is deliberately conservative: unknown tools pass through (the flight
    bridge still has the final say), but the known-risky actions get explicit
    checks. Adding a new risky tool = adding a case here, not editing the VLM.
    """
    if state.aborted:
        return SafetyDecision(False, "abort_active")

    if name in ("arm", "takeoff") and env.require_spoken_arm and not state.armed_with_voice:
        return SafetyDecision(False, "spoken_arm_required")

    if name == "takeoff":
        alt = float(args.get("altitude_m", 0.0))
        if alt > env.max_altitude_m:
            return SafetyDecision(False, f"altitude_exceeds_max ({alt} > {env.max_altitude_m})")

    if name in ("goto_ned", "goto_global"):
        # Height check (NED: positive down, so z < 0 is above home).
        if "d" in args:
            proposed_rel_alt = -float(args["d"])
            if proposed_rel_alt > env.max_altitude_m:
                return SafetyDecision(False, f"altitude_exceeds_max ({proposed_rel_alt:.2f})")

        # Geofence: simple radial check in NED north/east.
        n = float(args.get("n", 0.0))
        e = float(args.get("e", 0.0))
        radius = (n * n + e * e) ** 0.5
        if radius > env.geofence_radius_m:
            return SafetyDecision(False, f"geofence_breach ({radius:.2f}m > {env.geofence_radius_m}m)")

    if name in ("set_velocity", "set_velocity_ned"):
        vx = float(args.get("vx", 0.0))
        vy = float(args.get("vy", 0.0))
        vz = float(args.get("vz", 0.0))
        speed = (vx * vx + vy * vy + vz * vz) ** 0.5
        if speed > env.max_velocity_mps:
            return SafetyDecision(False, f"velocity_exceeds_max ({speed:.2f} > {env.max_velocity_mps})")

    if state.min_obstacle_m < env.min_obstacle_distance_m and name in (
        "goto_ned", "goto_global", "set_velocity", "set_velocity_ned", "takeoff"
    ):
        return SafetyDecision(False, f"obstacle_too_close ({state.min_obstacle_m:.2f}m)")

    return SafetyDecision(True)
