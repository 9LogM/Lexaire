# Lexaire
Natural Language Control and Autonomy for Drone Systems.

---

## Setup

### Requirements

**Hardware**
- MAVLink-compatible flight controller (PX4 tested)
- Companion computer with serial connection to flight controller
- Depth + RGB sensor with a docker-based ZMQ publisher (Intel RealSense L515 via [`RS-L515-Docker`](https://github.com/9LogM/RS-L515-Docker) is the documented default; any publisher matching the channel encoding works)
- Ubuntu x86_64 ground station

**Ground station software**
- Docker with Compose v2
- SSH key configured to companion computer

**Companion computer software**
- Docker with Compose v2
- SSH server enabled

**Optional**
- QGroundControl *(manual control and parameter tuning during development)*

### Configuration

Project-shared defaults live in `common/config.yaml` — edit `drone.host`, `drone.serial_device`, and `drone.serial_baud` to match your hardware:

```yaml
drone:
  host:          user@companion.local   # SSH target for the companion computer
  serial_device: /dev/ttyACM0           # FC serial port on the companion computer
  serial_baud:   57600                  # baud rate of the FC link
```

Per-machine values (secrets and the Pi's IP) live in `.env`. Copy the example and fill in:

```bash
cp .env.example .env
# Edit .env: set GEMINI_API_KEY and DRONE_PI_IP
```

### Pi setup

The MAVLink relay and the L515 publisher both run on the Pi.

- **MAVLink relay** lives in this repo (`relay/`). Lexaire deploys it automatically the first time the TUI starts. After that, `restart: unless-stopped` keeps it up across reboots.
- **L515 publisher** is a separate repo: [`RS-L515-Docker`](https://github.com/9LogM/RS-L515-Docker). Clone it on the Pi and `docker compose up -d`. Independent of this repo's release cadence.

### SSH key setup

Lexaire deploys the relay over SSH. Run once from the ground station:

```bash
ssh-keygen -t ed25519 -C "lexaire"      # skip if you already have a key
ssh-copy-id user@companion.local        # use drone.host from config.yaml
```

### Build and run

```bash
docker compose build
docker compose run --rm lexaire
```

The TUI brings up the GCS stack via `depends_on` and auto-deploys the relay to the Pi if it's not already running. Header indicators (`Stack`, `Relay`, `QGC`) reflect live state; menu options:

```
1. Pre-flight check        # scripts/preflight.sh: .env, config, drone link, L515 ports
2. QGroundControl setup    # how to point QGC at the relay
3. Live telemetry monitor  # reads from the bridge's published telemetry
4. Service status monitor  # per-service freshness + last state
5. Restart relay           # force-redeploy on the Pi
6. Restart GCS stack       # rebuild + restart local containers
```

Voice commands go through the `stt` service:

```bash
docker compose --profile tools run --rm stt --once "takeoff to 2 meters"
```

---

## Architecture

```
[ Drone ]
  Flight controller
        │ serial
        ▼
  Companion computer — mavlink-router (relay container)
        │ UDP over WiFi
        ▼
[ Ground station ]
  ┌─────┴──────┐
  ▼            ▼
:14550       :14551
 QGC         MAVSDK (flight-bridge container)
```

The companion computer is a dumb MAVLink bridge. All logic — telemetry, commands, SLAM, AI — runs on the ground station.

### Stack

| Component | Version | Runs on | Role |
|---|---|---|---|
| Debian slim | 13 (Trixie) | Ground station (amd64) | Base image for Lexaire |
| Debian slim | 13 (Trixie) | Companion computer (native arch) | Base image for relay |
| MAVSDK | 3.17.0 | Ground station | High-level MAVLink SDK |
| mavlink-router | v4 | Companion computer | MAVLink packet forwarder |
| Boost.Asio | system | Ground station | Async event loop |
| ncurses | system | Ground station | Terminal UI |

### Relay deployment

The `relay/` directory contains the relay's Dockerfile and entrypoint. The TUI auto-deploys it on first launch via:

```bash
DOCKER_HOST=ssh://<drone_host> docker compose -f relay/docker-compose.yaml up -d --build
```

Docker streams the `relay/` build context over SSH to the companion computer's daemon, which builds and starts the container natively. The companion computer never needs the repo cloned. `restart: unless-stopped` keeps the relay running across reboots; menu option **5. Restart relay** force-redeploys when needed.

---

## Services

`docker compose run --rm lexaire` brings up the TUI plus three always-on services (`perception`, `orchestrator`, `flight-bridge`) via `depends_on`. Two more (`stt`, `replay`) are profile-gated tools. They communicate over ZMQ on the compose network using the schema in [`python/lexaire/messages.py`](python/lexaire/messages.py) and [`include/lexaire/messages.hpp`](include/lexaire/messages.hpp).

| Service | Source | Role |
|---|---|---|
| `perception` | [`python/services/perception/`](python/services/perception/) | Subscribes to the sensor publisher's RGB+depth streams, runs YOLO11 on each frame, publishes a scene graph (label + bbox + camera-frame xyz) at `perception.tick_hz`. |
| `orchestrator` | [`python/services/orchestrator/`](python/services/orchestrator/) | Pulls voice commands from the STT service, fuses them with the latest scene + telemetry + RGB frame, calls the Gemini 2.5 Flash VLM for a tool-call decision, dispatches the calls to the flight bridge over REQ/REP. Owns the mission state machine and re-prompt loop. |
| `flight-bridge` (C++) | [`src/flight_bridge/`](src/flight_bridge/) | The system's only MAVSDK consumer. Enforces the non-overridable safety envelope ([`include/lexaire/safety.hpp`](include/lexaire/safety.hpp)) below the tool-call layer; runs the heartbeat-loss watchdog (auto RTL/HOLD); publishes telemetry + QGC liveness on the PUB stream the TUI reads. |
| `stt` | [`python/services/stt/`](python/services/stt/) | Voice command source. Modes: text-input via stdin / `--once` / `--from-file`, or `--audio-file` for pre-recorded WAV (uses `faster-whisper`). Mic capture is a follow-up. Profile-gated: `docker compose --profile tools run --rm stt --once "land"`. |
| `replay` | [`python/services/replay/`](python/services/replay/) | Field-debug tool: SUBs the live sensor channels and writes a JSONL recording (`record`), or replays one back as PUBs (`play`). Profile-gated. |

The sensor publisher (default: [`RS-L515-Docker`](https://github.com/9LogM/RS-L515-Docker)) lives in a separate repo and runs on the Raspberry Pi. Any docker-based ZMQ publisher that matches the channel encoding works — point `sensor.publisher_repo` at it and adjust `sensor.channels`.

### Wiring at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   STT ──── PUSH ────► orchestrator ──── REQ/REP ────► flight-bridge  │
│   (voice)             │   ▲     ▲                       │            │
│                       │   │     │                       │ MAVSDK     │
│                       │  scene  telemetry               ▼            │
│                       │   │     │                    autopilot       │
│                       │  PUB   PUB                                   │
│                       │   │     │                                    │
│                       ▼   │     │                                    │
│                    Gemini  │     │                                    │
│                       (RGB)│     │                                    │
│                            │     │                                    │
│  L515 ──► perception ──────┘     │                                    │
│                                  │                                    │
│  flight-bridge ──────────────────┘                                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Configuration

Project-shared defaults live in [`common/config.yaml`](common/config.yaml); secrets and per-machine values live in `.env`. Key fields:

- `sensor.publisher_repo` — URL of the sensor publisher project deployed on the Pi (default L515).
- `sensor.channels.{rgb,depth,imu,infrared,confidence}` — ZMQ endpoints the publisher exposes. Each can be left blank to disable that stream; perception/orchestrator require `rgb` and `depth` and fail at startup if either is blank, replay subscribes to whichever are non-empty.
- `perception.vlm.{provider,model,api_key_env,temperature}` — currently `gemini` with `gemini-2.5-flash`. Requires `GEMINI_API_KEY` in `.env`.
- `perception.detector.{model,weights,score_threshold,...}` — YOLO11; `yolo11n.pt` is auto-downloaded on first run.
- `safety.{max_altitude_m,geofence_radius_m,max_velocity_mps,require_spoken_arm,heartbeat_loss_action,heartbeat_loss_threshold_s}` — non-overridable bridge-side gate plus heartbeat-loss recovery thresholds.
- `orchestrator.{mission_max_steps,telemetry_history_seconds}` — mission re-prompt loop cap and telemetry ring-buffer depth fed to the VLM.
- `stt.{abort_keyword,whisper_model,whisper_device,...}` — voice command settings; the abort keyword (default `"abort"`) short-circuits the VLM and goes straight to the abort tool.

`.env` (copy from `.env.example`):

- `GEMINI_API_KEY` — required when `perception.vlm.provider == "gemini"`.
- `DRONE_PI_IP` — the Pi's IP address. Compose substitutes it into `extra_hosts` so every container resolves `drone.local`.

### Roadmap

Lexaire ships in phases:

- **Phase 1** — Perception + orchestrator + flight bridge end-to-end against a desk autopilot. ✅
- **Phase 2** — First flight: multi-step missions, telemetry-aware reasoning, recovery on connection loss. See [`docs/phase-2.md`](docs/phase-2.md).
- **Phase 3** — Live microphone capture for STT.
- **Phase 4** — RTAB-Map SLAM for persistent spatial memory ("go back to the table you saw earlier").

---

## Discussions

Have questions, ideas, or want to follow along? Join the conversation:
[github.com/9LogM/Lexaire/discussions](https://github.com/9LogM/Lexaire/discussions)

---

## Special Thanks

This project was built with the help of [Claude](https://claude.ai) — cheers for the pair programming.
