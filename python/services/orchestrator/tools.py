"""
Tool schema exposed to the VLM. Mirrors the handler set in
`src/flight_bridge/handlers.cpp` — keep the two in sync. Safety limits are
enforced in the flight bridge; the VLM sees them as context, not as gates
it can negotiate.
"""

from __future__ import annotations

from typing import Any


def tool_schemas() -> list[dict[str, Any]]:
    """Vendor-neutral tool descriptions; each backend maps to its own format."""
    return [
        {
            "name": "arm",
            "description": "Arm the vehicle (requires the pilot's spoken arm confirmation).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "disarm",
            "description": "Disarm the vehicle. Only on the ground.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "takeoff",
            "description": "Take off to the given altitude (meters above home).",
            "parameters": {
                "type": "object",
                "properties": {
                    "altitude_m": {
                        "type": "number",
                        "description": "Target altitude AGL in meters. Must be <= safety.max_altitude_m.",
                    },
                },
                "required": ["altitude_m"],
            },
        },
        {
            "name": "land",
            "description": "Land in place at the current position.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "return_to_launch",
            "description": "Return to the home position and land.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "hold",
            "description": "Switch to HOLD — hover at the current position.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "goto_ned",
            "description": (
                "Fly to a position in the local NED (north-east-down) frame relative to home. "
                "Negative `d` means above home (d = -height_m)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "number", "description": "North offset, meters."},
                    "e": {"type": "number", "description": "East offset, meters."},
                    "d": {"type": "number", "description": "Down offset, meters (negative = above)."},
                    "yaw_deg": {"type": "number", "description": "Heading, deg."},
                },
                "required": ["n", "e", "d"],
            },
        },
        {
            "name": "set_velocity_ned",
            "description": "Command a NED velocity setpoint (m/s) for smooth motion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {"type": "number", "description": "North velocity (m/s)."},
                    "vy": {"type": "number", "description": "East velocity (m/s)."},
                    "vz": {"type": "number", "description": "Down velocity (m/s); negative = ascend."},
                    "yaw_rate_deg_s": {"type": "number", "description": "Yaw rate (deg/s)."},
                },
                "required": [],
            },
        },
        {
            "name": "abort",
            "description": (
                "Safe stop: trigger an immediate controlled landing. Use this when the pilot "
                "says 'abort'/'stop' or you detect an unsafe situation that needs the vehicle "
                "on the ground now. This is the right tool for almost all emergencies."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "kill",
            "description": (
                "Emergency motor-off: cuts power to the motors instantly. The drone falls. "
                "Use ONLY when a controlled landing is unsafe (e.g. the drone is about to "
                "strike a person and you must drop it now). For everything else, use `abort`."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    ]
