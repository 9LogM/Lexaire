"""
Tool schema exposed to the VLA. Mirrors the handler set in
`src/flight_bridge/handlers.cpp` — keep the two in sync. Safety limits are
enforced in the flight bridge; the VLA sees them as context, not as gates
it can negotiate.

Pure-VLA action space: continuous velocity setpoints + a small set of
discrete state transitions and emergency primitives. No `takeoff`,
`goto_ned`, `return_to_launch`, or `hold` — those are emergent from
velocity setpoints under closed-loop control.
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
            "name": "set_velocity_ned",
            "description": (
                "Continuous control setpoint: NED-frame velocity (m/s). "
                "At least one of vx/vy/vz must be set — the bridge rejects "
                "a no-arg call to avoid silently stopping an active "
                "autonomous mode. {vx:0, vy:0, vz:0} is a legitimate hover."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vx": {"type": "number", "description": "North velocity (m/s)."},
                    "vy": {"type": "number", "description": "East velocity (m/s)."},
                    "vz": {"type": "number", "description": "Down velocity (m/s); negative = ascend."},
                    "yaw_deg": {
                        "type": "number",
                        "description": (
                            "Absolute heading in degrees (0=North, CW positive). "
                            "Omit to hold current heading."
                        ),
                    },
                },
                "required": [],
                "anyOf": [
                    {"required": ["vx"]},
                    {"required": ["vy"]},
                    {"required": ["vz"]},
                ],
            },
        },
        {
            "name": "land",
            "description": "Controlled landing in place (kept as a safety primitive).",
            "parameters": {"type": "object", "properties": {}, "required": []},
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
