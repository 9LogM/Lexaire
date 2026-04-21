# Lexaire Engineering Report

A running record of design decisions, architecture trade-offs, and implementation milestones. This is the "why" narrative — git log is the "what."

---

## 2026-04-20 — Phase 0: Design lock-in

### Context

Lexaire is a natural language control and autonomy system for drones. Speak into a mic → drone interprets intent via an AI pilot → executes MAVLink commands with spatial awareness. Current state before this session: ground station TUI (C++/ncurses/MAVSDK/Boost.Asio) with telemetry monitor and relay deployer. No perception, LLM, voice, or SLAM yet.

### Hardware

- Drone: Pixhawk 6C mini flight controller running PX4, Raspberry Pi 4 companion, Intel RealSense L515 (RGB + depth + IMU)
- Ground station: laptop with RTX 4070 (12 GB VRAM)
- Network: Pi will broadcast its own AP; laptop connects direct to Pi (low-latency, no general WiFi)
- Indoor-only operation (L515 is IR-based, fails in direct sunlight; also officially discontinued by Intel — outdoor plans would need a different depth sensor later)

### Decisions locked

**1. Transport: ZeroMQ, not ROS.**

The RS-L515-Docker container currently uses ROS 2 Galactic + `realsense2_camera`. We're ripping ROS out.

- RTAB-Map (the SLAM framework we'll use in phase 4) has a standalone C++ API (`librtabmap`). `rtabmap_ros` is only a thin wrapper — not required.
- ROS 2 on the Pi is heavy (DDS daemon, large runtime) and would lock us into version churn (galactic/humble/iron) for no gain.
- ZeroMQ + a minimal publisher (~100 lines of Python) is enough. Publish RGB, depth, IMU as separate PUB sockets with shared hardware timestamps from librealsense for frame sync.

**2. Pilot model: System 2 (strategic VLM) + System 1 (tactical deterministic loop).**

Gemini's "VLA world model" framing is accurate for the research frontier, but true VLA models (pi-0, OpenVLA, Gemini Robotics-ER) are trained on manipulation data, don't speak MAVLink, and aren't accessible as general APIs. Not viable for a drone project in 2026.

Practical split instead:
- **System 2 — Strategic pilot (1–2 Hz):** VLM (Claude via API) sees RGB frame + depth visualization + telemetry + SLAM excerpt, emits MAVLink tool calls against the full MAVSDK surface. No curated vocabulary, no hardcoded command selection.
- **System 1 — Tactical reflexes (50 Hz, deterministic C++):** executes the current strategic goal via offboard setpoints and applies reactive depth-based obstacle brake. This is a reflex layer, not a command choice, so it doesn't violate "no hardcoded directions."
- **Safety envelope (non-LLM-overridable):** max_altitude, geofence radius, max_velocity, min_obstacle_distance, "abort" keyword force-disarm. Hard limits enforced below the tool-call layer.

This preserves the "AI is the pilot" principle while being buildable on a 4070. Swap-in path to a true drone-trained VLA when one ships.

**3. VLM choice: Claude Haiku 4.5 (cheapest 4.x) via API, configurable.**

- `claude-haiku-4-5-20251001` — full vision + tool use, ~1/10th the cost of Sonnet, fine for testing.
- Architecture is provider-agnostic: config field `perception.vlm.provider` lets us swap between Claude, Gemini, OpenAI, local (Qwen2.5-VL, Gemma 3) without changing any other code.
- API key via `ANTHROPIC_API_KEY` env var, read from gitignored `.env`. Never committed.

**4. Process topology: separate processes, ZMQ between them.**

- Sensor publisher (Pi, Python, librealsense2 → ZMQ)
- Perception service (laptop, Python, CUDA)
- SLAM service (laptop, C++ wrapping librtabmap)
- Orchestrator / LLM (laptop, Python — where the AI ecosystem lives)
- Flight controller (laptop, C++ MAVSDK — the deterministic inner loop)
- TUI (existing C++ ncurses app, extended with status panels)
- STT (laptop, Python — whisper.cpp or faster-whisper)

Rationale: crash isolation, right language for each job (Python for AI glue, C++ for MAVSDK and tight loops), no Python MAVSDK bindings needed.

**5. Other locked calls**

- Flight stack: PX4 on Pixhawk 6C mini. OFFBOARD mode supported, standard PX4 FC.
- STT: dummy for design/testing; push-to-talk trigger; "abort" keyword = kill switch.
- LLM: dummy provider for design/testing; Claude Haiku 4.5 as real backend.
- SLAM: 1 map per session (no cross-session resume in scope).
- No SITL — test on real drone with props off when needed. User has the drone on and accessible at `orbis@drone.local`.
- Frame rate: 30 Hz RGB+depth (low-latency AP link makes this feasible without aggressive throttling).

### Draft config schema

```yaml
drone:
  host: orbis@drone.local
  serial_device: /dev/ttyACM0
  serial_baud: 57600

sensor:
  repo: ./RS-L515-Docker
  host: orbis@drone.local
  channels:
    rgb:   tcp://drone.local:5555
    depth: tcp://drone.local:5556
    imu:   tcp://drone.local:5557
  framerate_hz: 30
  resolution: [640, 480]

perception:
  backend: vlm
  vlm:
    provider: dummy                 # dummy | claude | gemini | openai
    model: claude-haiku-4-5-20251001
    api_key_env: ANTHROPIC_API_KEY

slam:
  backend: rtabmap
  map_path: ./maps

stt:
  backend: dummy
  trigger: push_to_talk
  abort_keyword: abort

control:
  tick_hz: 50
  llm_tick_hz: 2

safety:
  max_altitude_m: 5.0
  geofence_radius_m: 10.0
  max_velocity_mps: 1.5
  min_obstacle_distance_m: 0.5
  require_spoken_arm: true

services:
  orchestrator_endpoint: tcp://localhost:6000
  flight_controller_endpoint: tcp://localhost:6001
  perception_endpoint: tcp://localhost:6002
  slam_endpoint: tcp://localhost:6003
```

### Roadmap

Unchanged from the original 4-phase plan:

1. **Phase 1 — Perception** (desk, no flying): L515 streaming over ZMQ → perception service on laptop → object coordinates from bbox + depth + intrinsics.
2. **Phase 2 — LLM + drone control**: orchestrator with Claude Haiku 4.5 → MAVSDK tool definitions → flight controller with reactive safety layer.
3. **Phase 3 — Voice**: Whisper push-to-talk replaces typed commands.
4. **Phase 4 — SLAM**: RTAB-Map session-scoped mapping; spatial memory ("go back to where we were").

Sensor pipeline built from day one with RTAB-Map compatibility (synced RGB+depth+IMU with hardware timestamps) so phase 4 doesn't require rework.

### Open questions

None. All design forks are resolved.

### Next milestone

De-ROSify `RS-L515-Docker/`: replace the ROS 2 Galactic image with a slim Python publisher using `pyrealsense2` + `pyzmq`, emitting RGB / depth / IMU on three ZMQ PUB sockets with librealsense hardware timestamps. Keep the `docker-compose.yaml` shape stable so the existing `DOCKER_HOST=ssh://` relay-deployer pattern continues to work.

---

## 2026-04-20 — Milestone 1: De-ROSification of RS-L515-Docker

### What landed

- `RS-L515-Docker/Dockerfile` rewritten. Base is now `python:3.11-slim-bookworm`. Builds `librealsense` `v2.54.2` from source with Python bindings, installs `pyzmq`, `numpy`, `zstandard`, `pillow`. No ROS. `librealsense` is pinned to v2.54.2 because that was the last release with reliable L515 support — the L515 line is officially discontinued by Intel.
- `RS-L515-Docker/publisher.py` added (~170 lines). Single `pyrealsense2` pipeline with a callback handler that dispatches by frame type: framesets (RGB+depth aligned to color) publish on two sockets, individual motion frames (accel + gyro) publish on a third. Every message is a two-frame ZMQ multipart with a JSON header + binary payload.
- `RS-L515-Docker/docker-compose.yaml` simplified. `network_mode: host` replaces bridged networking + port-mapping so the three ZMQ PUB ports are directly accessible on the Pi's IP (matters because the Pi broadcasts its own AP, and we want zero port-mapping overhead). Dropped X11 volumes — no rviz anymore. Added env-var knobs for ports, resolution, frame rate, JPEG quality, IMU rates.
- `RS-L515-Docker/README.md` rewritten to describe the new ZMQ interface and drop all ROS references.

### Design notes

**Wire format.** Each topic publishes ZMQ multipart messages:

- Frame 0 — UTF-8 JSON header: `{"ts_ns": <librealsense hw timestamp, ns>, "seq": <per-stream counter>, "w": ..., "h": ..., "encoding": ..., "intrinsics": {fx, fy, ppx, ppy, model, coeffs}, "depth_scale_m": ...}`
- Frame 1 — binary payload.

Rationale: JSON header is self-describing, trivially parseable in any language, and adds only tens of bytes per frame. Binary payload keeps heavy data out of the header and avoids re-encoding costs. Sequence numbers enable drop detection on the subscriber.

**Timestamps.** Every message is tagged with `frame.get_timestamp() * 1e6` (ns). This is librealsense's hardware timestamp (domain `HARDWARE_CLOCK` when supported), which lets the subscriber correlate RGB frame N, depth frame N, and nearby IMU samples into a synced frameset for SLAM consumption. Using wall-clock timestamps on the Pi would lose that precision.

**Depth alignment.** Depth is server-side aligned to color via `rs.align(rs.stream.color)`. This costs GPU/CPU time on the Pi but means subscribers can use pixel coordinates from the RGB frame to index directly into the depth frame — a natural API for "center of bounding box → 3D point" perception. Intrinsics published with the depth header reflect the aligned (color) intrinsics.

**Encoding choices.** JPEG for RGB (standard lossy compression, ~20× reduction at quality 85). zstd for depth (lossless, ~3-5× reduction on typical z16 data; zstd level 1 is nearly free on a Pi). IMU is three float32s per message — trivially small.

**Threading.** librealsense's callback fires on its own C++ thread, which calls back into the Python publisher. ZMQ sockets are not thread-safe, so a `threading.Lock` gates every send. Lock contention is minimal because sends are fast and motion frames (the high-rate producer) dominate. No per-thread sockets needed.

**Why `network_mode: host`.** Docker port-mapping adds latency and requires explicit `-p` declarations per port. Since the Pi is a dedicated companion computer, host networking is safe and removes friction. The publisher binds to `0.0.0.0` on the configured ports; the laptop connects to `tcp://<pi-ip>:<port>`.

### Open issues / deferred

- **First-build time on Pi 4 is ~30–40 min** (librealsense from source). Not a correctness issue, just slow. Once built, subsequent deploys reuse the image layer. A future optimization is to publish a prebuilt ARM64 image to a registry, but we're not there yet.
- **Publisher is not yet tested against a real L515** — I don't have the hardware in my sandbox, and the user is away. Build syntax is correct and the librealsense API usage follows documented patterns, but real-hardware verification happens when the user returns.

### Submodule situation (needs user decision when back)

`RS-L515-Docker/` is a git repository cloned in-place from `https://github.com/9LogM/RS-L515`. My de-ROSification commit (`82a5e71` — "De-ROSify: replace ROS 2 Galactic with Python + pyrealsense2 + ZMQ publisher") landed in the **inner repo** only — it has not been pushed upstream.

I did not flatten the inner repo into Lexaire because that would destroy the inner `.git/` history irreversibly, which the sandbox correctly refused to authorize without explicit user approval.

`RS-L515-Docker/` is gitignored from Lexaire for now so the outer repo doesn't track a fragile gitlink to a local-only commit. **When you're back, pick one:**

- **Option A (recommended): publish + submodule.** Push the inner repo to `9LogM/RS-L515` (your own), then add it to Lexaire as a proper submodule: `git submodule add https://github.com/9LogM/RS-L515.git RS-L515-Docker && git rm .gitignore rule for RS-L515-Docker/`. Clean separation, reusable for other drone projects.
- **Option B: flatten.** `rm -rf RS-L515-Docker/.git` and treat it as a plain subdirectory of Lexaire. Loses the inner history (original RS-L515 commits remain on GitHub), but simpler.
- **Option C: leave as is.** Works locally; new checkouts of Lexaire would need to `git clone https://github.com/9LogM/RS-L515 RS-L515-Docker` separately.

---

## 2026-04-20 — Secrets and provider switch

- Added `.gitignore` covering `.env`, build artifacts, editor/OS cruft, runtime logs, and local config overrides.
- Added `.env.example` documenting the three provider-key slots (Gemini, Anthropic, OpenAI).
- Wrote actual `.env` with the Gemini API key the user provided. Verified git ignores it (`git check-ignore` confirms, `git status` does not list `.env` as untracked).
- **Provider switch:** user clarified that Claude API is separately billed from their Max subscription, so default VLM provider is now Gemini (using AI Studio's free tier). The config schema already had `provider: dummy | claude | gemini | openai`, so this is a config flip, not a code change. The orchestrator will implement both Gemini and Claude backends at the same time — swap is a one-line config change.
- **Security note for user:** Gemini key was pasted in-chat; rotate after return. Dev use this week is fine.

---

## 2026-04-20 — Plan for the week

User is away for ~7 days. Scope of autonomous work:

1. **Common infrastructure** (config loader, ZMQ message schema library, logging utilities).
2. **Subscriber library** (Python + C++) for the sensor streams, with a sync buffer that joins RGB+depth+nearest IMU into framesets.
3. **Perception service** — Python, dummy detector that returns canned bounding boxes + depth lookup → 3D points. Swappable for real YOLO/GroundingDINO later.
4. **Flight controller service** — C++, MAVSDK-backed, exposes a ZMQ REP socket with JSON tool-call interface. Dummy mode that logs intent without commanding the drone, for development without risk.
5. **Orchestrator** — Python, takes user command + perception + telemetry, calls VLM (dummy or Gemini), dispatches tool calls to flight controller. Dummy VLM returns canned tool-call scripts for testing.
6. **STT stub** — Python, reads text from stdin for now (push-to-talk upgrade later).
7. **TUI extension** — add status panels for perception, orchestrator, services. Keep single-binary shape but split into modules.
8. **Safety layer** — non-LLM-overridable checks applied in flight controller before every tool call (altitude, geofence, velocity, obstacle distance, abort keyword).
9. **Integration test harness** — replay mode that feeds recorded sensor frames into the pipeline so end-to-end tests don't need hardware.
10. **Documentation** — each service gets its own README; REPORT.md tracks milestones.

Constraints for this week:
- **No flying.** Don't deploy the sensor container to the Pi without user present (build is long and the container's visible at `orbis@drone` could affect the user's other work).
- **No destructive git ops.** No force-push, no `git reset --hard`.
- **No external message sends** (no PRs, no issues created, no chat platform posts).
- **Local-only testing.** Build on the Windows dev box, run dummy-mode integration tests.
- **Commit often and write clear messages.** User will read git log + REPORT.md when back.

---

## 2026-04-20 — Milestone 2: Services landed

Picking up from the orchestrator write. This session closed the remaining
items from the week plan — the dummy pipeline is now exercisable end-to-end
without any hardware.

### What landed

**STT stub** (`python/services/stt/main.py`)
- Three modes: `--once TEXT`, `--from-file PATH`, interactive stdin.
- PUSHes `VoiceCommand` to the orchestrator's `command_pull` endpoint.
- Sets `is_abort=True` when the configured abort keyword (default "abort")
  appears anywhere in the command text — the orchestrator short-circuits on
  that flag before the VLM is even consulted, so a stuck VLM can't swallow
  the kill switch.
- `stt.backend = whisper` raises `NotImplementedError` with a clear message
  for future-me. Phase-3 upgrade is a backend swap, no service rework.

**Replay harness** (`python/services/replay/`)
- `record` — SUB the live sensor channels, write a JSONL recording with
  base64-encoded payloads and a monotonic receiver timestamp per frame. The
  receiver timestamp is what we use to schedule playback; the sensor's own
  `ts_ns` stays in the header so downstream consumers can't tell the
  difference from a live stream.
- `play` — BIND the sensor channels (with a `--bind-host` rewrite so
  `tcp://drone.local:5555` → `tcp://0.0.0.0:5555` for local playback) and
  replay at `--speed` multiplier, optionally looping forever.
- `synth` — bypass the file entirely: cook up a uniform-gray JPEG, a
  constant-depth uint16 buffer, and zero-motion IMU; publish at
  configurable rates. This is what the integration test suite uses when it
  needs the pipeline to believe a sensor exists.
- File format is deliberately JSONL with base64 payloads — bloats JPEG ~30%
  but is dead-simple to inspect, diff, and edit. Phase-1 dev recordings
  won't be huge.

**Python Dockerfile + extended compose**
- `python/Dockerfile` — slim Python 3.11, installs `lexaire[gemini,perception]`
  editable. Build context is repo root so `python/` and `common/` are both
  visible.
- `docker-compose.yaml` now spins up the full stack:
  - `lexaire` (TUI, existing) and `flight-bridge` share one C++ image.
  - `perception` + `orchestrator` share one Python image (different `command`).
  - `stt` and `replay` live behind `profiles: ["tools"]` because they're
    interactive/on-demand — putting them in the default up-set would crash-
    loop on stdin EOF or collide on sensor ports.
- All services use `network_mode: host` so the intra-service ZMQ URLs
  (`tcp://127.0.0.1:6100…6300`) Just Work without bridge-network plumbing.
- Secrets flow via `env_file: .env` — image stays clean.

**TUI extension** (`include/services_panel.hpp`, `src/services_panel.cpp`,
`src/main.cpp`)
- New main-menu option **4. Service status monitor**. Spawns a
  `ServicesWatcher` background thread that SUBs the three status channels
  (perception scene, flight-bridge telemetry, orchestrator status) and
  keeps a mutex-protected snapshot.
- Panel renders each service with last-seen age (green < 500 ms, yellow
  ≤ 2 s, red > 2 s or never) plus a one-line summary parsed from the header:
  detection count+labels for perception, `mode/armed/alt/battery` for
  telemetry, `state — thought` for the orchestrator.
- Ages auto-refresh via a 500 ms boost::asio timer that's only armed while
  the panel is on screen; the lambda captures itself via `weak_ptr` to
  avoid a shared_ptr cycle.
- CMake wires libzmq into the `lexaire` target (previously only the
  flight-bridge linked it).

### Deferred / known gaps

- **Whisper STT, real sensor runtime, real MAVSDK connection.** All three
  need hardware or user-side setup that can't happen this week.
- **No real L515 verification.** Publisher is deployed to the Pi on the
  user's return; all downstream work was designed against the documented
  wire format, not a live stream.
- **C++ code only compiles inside the Docker image.** The Windows dev box
  has neither MAVSDK nor yaml-cpp nor libzmq — `cmake` would fail before
  the first translation unit. `docker compose build` is the way.
- **Tests are local-only scaffolding.** A Python test suite lives under
  `python/tests/` but is gitignored — unit coverage for the library and
  services plus an integration harness with a fake REP bridge. Written
  but not executed (no Python / Docker on this dev box), and not part of
  the tracked deliverable. Treat as a local sketch, not a contract.

### Status snapshot

All 10 items from the original week plan are complete or reasonably stubbed:

| # | Item                         | Status |
|---|------------------------------|--------|
| 1 | Common infra                 | done   |
| 2 | Subscriber library           | done   |
| 3 | Perception service           | done (dummy detector) |
| 4 | Flight controller service    | done (dummy mode wired) |
| 5 | Orchestrator                 | done (dummy + Gemini backends) |
| 6 | STT stub                     | **done this session** |
| 7 | TUI extension                | **done this session** |
| 8 | Safety layer                 | done (both languages) |
| 9 | Integration test harness     | local-only scaffolding (gitignored) |
| 10 | Documentation                | done (REPORT.md + service docstrings) |
