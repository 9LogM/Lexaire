# Phase 2 prep — running log

Working branch: `feat/phase-2-prep` (off `main`). Nothing pushed; all commits are local. Discardable via `git reset --hard main` if you'd rather restart.

This file is a chronological log of what I did and why, so a future-you can scan it when you're back. Each section corresponds to a logical commit on the branch.

## 0. Phase 2 doc updates

`docs/phase-2.md` now reflects the indoor-positioning conversation:

- Phase 2 is split into **2A (safety + recovery shakedown — no positioning needed)** and **2B (voice-named navigation — gated on positioning choice)**.
- Four positioning options documented (A: L515 + RTAB-Map / VINS-Fusion, B: PMW3901 flow + lidar, C: external mocap, D: defer 2B).
- Acceptance criteria are split between the two halves so 2A can ship without 2B's positioning prerequisite.
- Default config knobs Phase 2A introduces are listed at the bottom (heartbeat threshold, mission max steps, telemetry history seconds, etc.).

## 1. Phase-2-readiness audit findings

Reading every file with Phase 2 eyes — what'd block or complicate goals 1–4.

### Flight bridge (`src/flight_bridge/main.cpp`, `handlers.cpp`)

**Blocker / behavior change needed:**

- **`abort` calls `Action.kill()` (`handlers.cpp:159`).** Motors instant-off → drone falls. Indoors that's risky. Phase 2A: rewire the `abort` tool to `Action.land()` for a controlled descent, and add a separate `kill` tool for true emergency motor-cut. The non-overridable safety gate (`require_spoken_arm`, altitude cap, etc.) stays on top of `kill` so the LLM can't hallucinate it without a voice-confirmed flag.
- **No heartbeat watchdog.** If MAVSDK's autopilot heartbeat drops mid-flight, handlers eventually return `connection_error` but the bridge does nothing proactive. Phase 2A: dedicated thread polls `ctx.system->is_connected()` (or subscribes to the connection-state callback), and if it sees disconnect for >`safety.heartbeat_loss_threshold_s` seconds while `armed && !on_ground`, calls `Action.return_to_launch()` (or `Action.hold()`, configurable).
- **5 s autopilot-discovery timeout** (`main.cpp:67`) — fine but the bridge silently keeps running with `ctx.system == nullptr` after that, returning `action_not_initialized` for every tool. For Phase 2A live mode it should refuse to start (return non-zero from `run()`) unless `--dummy` is set.

**OK as-is:**

- ZMQ topology, telemetry broadcaster cadence, ToolResult schema.
- Spoken-arm gate logic in `safety.hpp` (already correctly split per-tool).
- `state.armed_with_voice` lifecycle: set on arm-with-voice-confirmed, cleared on disarm-success. Stays sticky across an armed session, which is what we want.

### Orchestrator (`python/services/orchestrator/main.py`)

**Phase 2 work needed:**

- **No mission state machine.** `_on_command` (line 206) is single-pass: dispatch all tool calls in one VLM response, then idle. Phase 2 needs an `OrchestratorMission` dataclass + a re-prompt loop that calls the VLM again after each tool result until a terminal call (`hold`, `land`, `abort`, `return_to_launch`) or a new user command.
- **`_dispatch_tool_call` return is discarded** (line 224 — `for tc in decision.tool_calls: self._dispatch_tool_call(tc)`). Phase 2 must consume the result so the mission loop can react to failures (safety_denied, connection_error, etc.).
- **No telemetry history.** `latest_telem` (line 62) is a single sample. Phase 2 needs a `collections.deque` ring buffer keyed by configurable depth-in-seconds.
- **No bridge-offline tracking.** `_dispatch_tool_call` swallows REQ timeouts (line 284) and returns failure without updating any service-wide state. Phase 2 needs a counter / last-success-ts that flips an `OrchestratorStatus.state = "bridge_offline"` if no successful call within `orchestrator.bridge_offline_threshold_s` seconds, and refuses new tool calls until clear.
- **No tick-based wakeup.** Orchestrator only acts on a voice command. For Phase 2 telemetry-aware reasoning it'd be useful to wake the orchestrator periodically (say every 1 s) when a mission is active, so it can react to telemetry trends (geofence approach, low battery) without a user prompt.

**OK as-is:**

- ZMQ-context-per-socket workaround (commented as such; Phase 1 finding).
- Voice command queue, scene/telemetry/frame thread structure.
- Abort short-circuit (`_on_command` line 208) — bypasses the VLM correctly.

### VLM context (`vlm_base.py`, `vlm_gemini.py`)

- `VlmContext` (`vlm_base.py:14`) currently holds `user_command, telemetry, scene, rgb, history, safety`. Phase 2 adds `mission: Optional[dict]` and `telemetry_history: list[dict]`. `vlm_gemini._build_prompt` needs to surface them in the prompt.
- Mission re-prompts will increase Gemini calls per session. `safety_snapshot` is sent verbatim each time; could trim to only changed fields, but that's an optimization, not correctness.

### Config (`common/config.yaml`)

New keys to add:

```yaml
safety:
  # New in Phase 2A:
  heartbeat_loss_action:    rtl    # rtl | hold — bridge action when autopilot heartbeat drops
  heartbeat_loss_threshold_s: 2.0

orchestrator:
  bridge_offline_threshold_s: 3.0
  telemetry_history_seconds:  5.0
  mission_max_steps:          10
  mission_tick_hz:            1.0  # tick-based wakeup rate while a mission is active
```

### TUI (`src/main.cpp`, `src/services_panel.cpp`)

- Services panel already colors stale rows red. Bridge offline currently shows up as a red `telemetry` row, which is fine but not loud.
- Phase 2 adds an explicit `bridge_offline` state from the orchestrator (via the existing `OrchestratorStatus.state` field). The services panel just needs to color the `orchestrator` row red when `state == "bridge_offline"` and show the state string clearly. Minimal C++ change.

### Tests / docs

- No tests cover heartbeat-loss-triggers-RTL, bridge-offline-detection, or the mission state machine. Phase 2A adds them.
- `docs/phase-2.md` now reflects 2A/2B split (commit 0).

## 2. Build plan (in order, additive, each its own commit)

1. **Config knobs** — add the new safety/orchestrator keys with defaults; update tests that load config so they don't break.
2. **Telemetry ring buffer** — `collections.deque` with timestamp-based eviction; expose via `VlmContext.telemetry_history`.
3. **Mission state machine** — `OrchestratorMission` dataclass; re-prompt loop in `_on_command`; cap by `mission_max_steps`; emit per-step status on the status PUB.
4. **Bridge offline detection** — orchestrator-side last-success timestamp on each tool dispatch; flip status to `bridge_offline` when stale; reject further tool calls until clear.
5. **Bridge heartbeat watchdog** — C++ thread on the flight bridge that triggers RTL/HOLD on heartbeat-drop while armed.
6. **Abort split: land vs kill** — flight-bridge `abort` → `Action.land()`; add new `kill` tool for `Action.kill()`. Update `tool_schemas()` and the C++ dispatch table.
7. **TUI bridge-offline coloring** — minor services-panel tweak.
8. **Tests** — unit + integration coverage for each.

(filled in below as I build each piece)
