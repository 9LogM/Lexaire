# Phase 2 — First flight: multi-step missions and real-drone verification

Phase 1 proved the perception → reasoning → tool dispatch loop on a desk in `--dummy` mode. The orchestrator can take a voice command, fuse it with a YOLO scene + a Gemini-reasoned RGB frame, and dispatch a tool call that the flight bridge would execute against MAVSDK.

Phase 2 takes the same loop into actual flight in a controlled indoor environment.

## Goals

### 1. Live flight (drop `--dummy`)

The flight bridge currently defaults to `--dummy` in `docker-compose.yaml:18`. Phase 2 flips that to live MAVSDK against the connected PX4 FC.

- Add a `docker-compose.live.yaml` overlay that drops `--dummy` and adds `extra_hosts: ["drone.local:<pi-ip>"]` for the bridge.
- Validate the safety envelope **in flight**: takeoff capped at `safety.max_altitude_m`, geofence rejecting `goto_ned` outside `safety.geofence_radius_m`, velocity rejection on `set_velocity_ned` over `safety.max_velocity_mps`. These are all unit-tested today; Phase 2 is the airborne sanity check.
- Verify `abort` actually disarms a flying drone (right now it `Action.kill()`s in code, but we've never observed it in real flight).

### 2. Multi-step mission orchestration

The orchestrator currently handles one tool call per voice command, then returns to idle. That's enough for "arm" or "takeoff" but breaks for anything compositional like "fly to the doorway and hold there" — the VLM has to emit goto + hold in a single response and the orchestrator dispatches them sequentially with no state tracking between.

Phase 2 adds an explicit mission state machine:

- `OrchestratorMission` dataclass: `goal_text`, `current_step`, `step_history`, `started_ts_ns`.
- The VLM sees the active mission as part of `VlmContext` so it can reason about progress.
- When a tool call returns ok, the orchestrator advances the mission step and re-prompts the VLM with the new state. Loop until VLM emits a terminal call (`hold`, `land`, `abort`) or the user gives a new command.
- Mission abort (user voice or safety trip) cleanly cancels and returns to idle.

### 3. Telemetry-aware reasoning

`safety_snapshot` is currently a flat dict of envelope limits. The VLM gets a one-shot telemetry snapshot but no history.

Phase 2 widens the context:

- Maintain a 5-second ring buffer of telemetry samples in the orchestrator.
- Expose to the VLM as `ctx.telemetry_history` so it can reason about "approaching geofence", "battery dropping", "altitude unstable".
- Add a tick-based perception/orchestrator awakening (currently the orchestrator only wakes on a voice command — it can't proactively call `hold` or `rtl` if it sees something concerning).

### 4. Recovery / connection loss

Right now if the flight bridge loses the MAVSDK connection mid-flight, handlers return `connection_error` and the orchestrator just moves on. If the orchestrator dies, the bridge has no fallback policy.

Phase 2 adds an explicit recovery layer:

- Bridge: if airborne and the autopilot heartbeat drops for >2 s, command RTL.
- Orchestrator: if it loses the bridge REQ socket for >3 s, surface it on the status PUB so the TUI can show it red, and reject any further tool calls until reconnect.
- TUI: a single banner row that calls out "FLIGHT BRIDGE OFFLINE" or similar — currently the services panel just goes stale silently.

## Out of scope (still later)

- **Live microphone capture for STT.** The pipeline accepts text or pre-recorded WAV; mic-in-Docker is host-specific and is a Phase 3 concern.
- **SLAM persistent memory.** "Go back to the table you saw earlier" needs RTAB-Map and a map store; Phase 4.
- **Outdoor / GPS-denied flight.** L515 is IR-based, doesn't work outdoors. Future hardware swap (D435i) and an RTK-style position source come with that.
- **Local LLM (Ollama).** Gemini works fine; offline operation is deferred until there's a concrete reason to swap.

## Acceptance criteria

Phase 2 is complete when, in a controlled indoor space:

1. The drone arms, takes off, navigates to a voice-named object ("the chair"), hovers, and returns to launch — entirely via voice — with the safety envelope active.
2. Mid-mission abort works: speaking "abort" while the drone is airborne kills motors.
3. A simulated bridge crash mid-flight triggers an RTL within 2 s.
4. The TUI services panel shows the orchestrator's mission state in real time.
