# Lexaire
Natural Language Control and Autonomy for Drone Systems.

---

## Setup

### Requirements

**Hardware**
- MAVLink-compatible flight controller
- Companion computer with serial connection to flight controller
- Depth sensor
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

The TUI brings up the GCS stack via `depends_on` and auto-deploys the relay to the Pi if it's not already running.

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
 QGC         MAVSDK (lexaire)
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

The `relay/` directory contains the relay's Dockerfile and entrypoint. When you select **Start relay** in the Lexaire menu, it runs:

```bash
DOCKER_HOST=ssh://<drone_host> docker compose -f relay/docker-compose.yaml up -d --build
```

Docker streams the `relay/` build context over SSH to the companion computer's daemon, which builds and starts the container natively. The companion computer never needs the repo cloned. `restart: unless-stopped` keeps the relay running across reboots.

---

## Services

The ground station's `docker compose up -d` brings up four services beyond the C++ TUI. They communicate over ZMQ on the compose network using the schema in [`python/lexaire/messages.py`](python/lexaire/messages.py) and [`include/lexaire/messages.hpp`](include/lexaire/messages.hpp).

| Service | Source | Role |
|---|---|---|
| `perception` | [`python/services/perception/`](python/services/perception/) | Subscribes to the L515 RGB+depth streams, runs YOLO11 on each frame, publishes a scene graph (label + bbox + camera-frame xyz) at `perception.tick_hz`. |
| `orchestrator` | [`python/services/orchestrator/`](python/services/orchestrator/) | Pulls voice commands from the STT service, fuses them with the latest scene + telemetry + RGB frame, calls the Gemini 2.5 Flash VLM for a tool-call decision, dispatches the calls to the flight bridge over REQ/REP. |
| `flight-bridge` (C++) | [`src/flight_bridge/`](src/flight_bridge/) | MAVSDK-backed tool dispatcher. Enforces the non-overridable safety envelope ([`include/lexaire/safety.hpp`](include/lexaire/safety.hpp)) below the tool-call layer. |
| `stt` | [`python/services/stt/`](python/services/stt/) | Voice command source. Modes: text-input via stdin / `--once` / `--from-file`, or `--audio-file` for pre-recorded WAV (uses `faster-whisper`). Mic capture is a follow-up. Profile-gated: `docker compose --profile tools run --rm stt --once "land"`. |
| `replay` | [`python/services/replay/`](python/services/replay/) | Field-debug tool: SUBs the live sensor channels and writes a JSONL recording (`record`), or replays one back as PUBs (`play`). Profile-gated. |

The L515 publisher itself lives in a separate repo, [`RS-L515-Docker`](https://github.com/9LogM/RS-L515-Docker), and runs on the Raspberry Pi.

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

Everything lives in [`common/config.yaml`](common/config.yaml). Key fields:

- `sensor.channels.{rgb,depth,imu}` — endpoints the perception/orchestrator subscribe to. Defaults assume the L515 publisher at `tcp://drone.local:<port>`. The Pi's IP comes from `DRONE_PI_IP` in `.env`; compose substitutes it into `extra_hosts` for every service that needs to resolve `drone.local`.
- `perception.vlm.{provider,model,api_key_env,temperature}` — currently `gemini` with `gemini-2.5-flash`. Requires `GEMINI_API_KEY` in `.env`.
- `perception.detector.{model,weights,score_threshold,...}` — YOLO11; `yolo11n.pt` is auto-downloaded on first run.
- `safety.{max_altitude_m,geofence_radius_m,max_velocity_mps,require_spoken_arm}` — non-overridable bridge-side gate.
- `stt.{abort_keyword,whisper_model,whisper_device,...}` — voice command settings; the abort keyword (default `"abort"`) short-circuits the VLM and goes straight to the abort tool.

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
