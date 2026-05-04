# Phase 2 — First flight: multi-step missions and real-drone verification

Phase 1 proved the perception → reasoning → tool dispatch loop end-to-end against a desk autopilot. The orchestrator can take a voice command, fuse it with a YOLO scene + a Gemini-reasoned RGB frame, and dispatch a tool call that the flight bridge executes against MAVSDK.

Phase 2 takes the same loop into actual flight in a controlled indoor environment.

## Phase 2A vs 2B — what's blocked on positioning

Indoor flight needs a position source. The L515 publishes RGB + depth + IMU but doesn't run a SLAM/VIO solver, so PX4 has no way to hold position from those streams without one. That's a significant chunk of work (essentially most of Phase 4).

We split Phase 2 into two halves so the no-positioning work isn't gated on the positioning decision:

- **Phase 2A — safety + recovery shakedown.** Real-flight verification of the parts that don't need positional autonomy: arm, auto-takeoff to fixed altitude, hover via internal IMU stabilization, abort, auto-land, RTL. The safety envelope ([`include/lexaire/safety.hpp`](../include/lexaire/safety.hpp)) is exercised airborne. The bridge heartbeat + orchestrator mission-state work is testable here. **Goes first.**
- **Phase 2B — voice-named navigation.** "Fly to the doorway and hold there." Requires position holding indoors. Gated on the positioning choice below.

### Indoor positioning options (Phase 2B prerequisite)

| Option | What it needs | Effort | Trade-offs |
|---|---|---|---|
| **A. L515 + RTAB-Map / VINS-Fusion** | A SLAM/VIO solver consuming the existing L515 streams; a `VISION_POSITION_ESTIMATE` MAVLink bridge into PX4; `EKF2_AID_MASK` configured for vision pose. | 1–2 weeks focused | This is most of Phase 4 SLAM pulled forward. End state is the right architecture. Risk: VIO drift / latency / camera-FC calibration. |
| **B. PMW3901 optical flow + lidar range finder** | ~$30 of hardware on the airframe + PX4 params (`SENS_FLOW_*`, `EKF2_AID_MASK`). | A weekend | Quick win; PX4's flow stack is mature. Doesn't reuse the L515. Stops working over visually-uniform floors. |
| **C. External motion capture (Vicon / OptiTrack / OpenVR)** | A mocap rig and a `VISION_POSITION_ESTIMATE` bridge. | A day if you have the rig | Lab-only. |
| **D. Defer 2B** | Phase 2A only; 2B waits until either the L515 SLAM stack lands as part of Phase 4, or you decide on flow hardware. | Zero | Loses voice-named navigation but unblocks safety/recovery testing today. |

The decision lives outside this doc — once you pick, Phase 2B's scope and prereqs lock in.

## Goals

### 1. Live flight  — Phase 2A

The flight bridge talks to the PX4 FC via MAVSDK on every run.

- Validate the safety envelope **in flight**: takeoff capped at `safety.max_altitude_m`, geofence rejecting `goto_ned` outside `safety.geofence_radius_m` (Phase 2B only — needs position), velocity rejection on `set_velocity_ned` over `safety.max_velocity_mps` (Phase 2B). The altitude cap and `require_spoken_arm` gates are testable in 2A.
- Verify `abort` lands a flying drone via `Action.land()` (controlled descent + disarm-on-touchdown), and that the separate `kill` tool reserves `Action.kill()` for true emergencies.

### 2. Multi-step mission orchestration  — Phase 2A scaffold, 2B exercises

The orchestrator currently handles one tool call per voice command, then returns to idle. That's enough for "arm" or "takeoff" but breaks for anything compositional like "fly to the doorway and hold there" — the VLM has to emit goto + hold in a single response and the orchestrator dispatches them sequentially with no state tracking between.

Phase 2 adds an explicit mission state machine:

- `OrchestratorMission` dataclass: `goal_text`, `current_step`, `step_history`, `started_ts_ns`.
- The VLM sees the active mission as part of `VlmContext` so it can reason about progress.
- When a tool call returns ok, the orchestrator advances the mission step and re-prompts the VLM with the new state. Loop until VLM emits a terminal call (`hold`, `land`, `abort`) or the user gives a new command.
- Mission abort (user voice or safety trip) cleanly cancels and returns to idle.

The state machine itself is testable in 2A with a stub VLM and a fake bridge in unit/integration tests. 2B exercises it under real flight.

### 3. Telemetry-aware reasoning  — Phase 2A

`safety_snapshot` is currently a flat dict of envelope limits. The VLM gets a one-shot telemetry snapshot but no history.

Phase 2 widens the context:

- Maintain a 5-second ring buffer of telemetry samples in the orchestrator.
- Expose to the VLM as `ctx.telemetry_history` so it can reason about "approaching geofence", "battery dropping", "altitude unstable".
- Add a tick-based orchestrator wakeup (currently the orchestrator only wakes on a voice command — it can't proactively call `hold` if it sees something concerning).

### 4. Recovery / connection loss  — Phase 2A

Right now if the flight bridge loses the MAVSDK connection mid-flight, handlers return `connection_error` and the orchestrator just moves on. If the orchestrator dies, the bridge has no fallback policy.

Phase 2 adds an explicit recovery layer:

- Bridge: if airborne and the autopilot heartbeat drops for >2 s, command RTL (configurable to HOLD via `safety.heartbeat_loss_action`).
- Orchestrator: if it loses the bridge REQ socket for >3 s, surface it on the status PUB so the TUI can show it red, and reject any further tool calls until reconnect.
- TUI: a single banner row that calls out "FLIGHT BRIDGE OFFLINE" or similar — currently the services panel just goes stale silently.

## Out of scope (still later)

- **Live microphone capture for STT.** The pipeline accepts text or pre-recorded WAV; mic-in-Docker is host-specific and is a Phase 3 concern.
- **SLAM persistent memory.** "Go back to the table you saw earlier" needs RTAB-Map and a map store; Phase 4. (Note: option A above does *one* component of Phase 4 — the live pose estimate — but not the persistence/mapping layer.)
- **Outdoor / GPS-denied flight.** L515 is IR-based, doesn't work outdoors. Future hardware swap (D435i) and an RTK-style position source come with that.
- **Local LLM (Ollama).** Gemini works fine; offline operation is deferred until there's a concrete reason to swap.

## Acceptance criteria

### Phase 2A — done when, in a controlled indoor space:

1. The drone arms, auto-takes-off to a fixed altitude (≤ `safety.max_altitude_m`), hovers, and lands — entirely via voice — with the safety envelope active.
2. The altitude cap and spoken-arm gate are exercised airborne (a takeoff over the cap is rejected; an LLM-only arm without a voice-confirmed flag is rejected).
3. Mid-flight `abort` voice command produces an `Action.land()` (not motor-kill).
4. A simulated bridge-side MAVSDK heartbeat drop for >2 s triggers an automatic RTL.
5. A simulated orchestrator-side bridge-REQ timeout >3 s flips the TUI offline banner and prevents new tool calls until the bridge comes back.
6. The TUI services panel shows the orchestrator's mission state in real time.

### Phase 2B — done when (gated on the positioning choice above):

7. The drone arms, takes off, navigates to a voice-named object ("the chair"), hovers, and returns to launch — entirely via voice.
8. Geofence rejection fires airborne when the VLM tries to fly outside `safety.geofence_radius_m`.
9. Velocity-cap rejection fires airborne on `set_velocity_ned` over the limit.

## Defaults baked in by Phase 2A code (overridable via config)

- `safety.heartbeat_loss_action: rtl` — bridge auto-action on autopilot heartbeat drop. `hold` also valid.
- `safety.heartbeat_loss_threshold_s: 2.0` — how long to wait before triggering.
- `orchestrator.telemetry_history_seconds: 5.0` — telemetry ring-buffer depth in seconds.
- `orchestrator.mission_max_steps: 10` — hard cap on mission re-prompt loop to bound Gemini calls per mission.
