# Orbis
An autonomous indoor drone that responds to natural language voice commands.

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

Edit `common/config.yaml`:
```yaml
serial_device: /dev/ttyACM0       # serial port to flight controller (companion computer side)
serial_baud: 57600                # baud rate of the serial connection
drone_host: user@companion.local  # SSH connection to companion computer
```

### Build

```bash
docker compose build
```

### Deploy relay

On first run, deploy the MAVLink relay to the companion computer from the Orbis menu:

```
1. Start relay
```

This uses Docker's remote daemon over SSH. After the first deploy, the relay auto-starts on every reboot.

### Run

```bash
docker compose run --rm orbis
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
 QGC         MAVSDK (Orbis)
```

The companion computer is a dumb MAVLink bridge. All logic — telemetry, commands, SLAM, AI — runs on the ground station.

### Stack

| Component | Version | Runs on | Role |
|---|---|---|---|
| Debian slim | 13 (Trixie) | Ground station (amd64) | Base image for Orbis |
| Debian slim | 13 (Trixie) | Companion computer (native arch) | Base image for relay |
| MAVSDK | 3.17.0 | Ground station | High-level MAVLink SDK |
| mavlink-router | v4 | Companion computer | MAVLink packet forwarder |
| Boost.Asio | system | Ground station | Async event loop |
| ncurses | system | Ground station | Terminal UI |

### Relay deployment

The `relay/` directory contains the relay's Dockerfile and entrypoint. When you select **Start relay** in the Orbis menu, it runs:

```bash
DOCKER_HOST=ssh://<drone_host> docker compose -f relay/docker-compose.yaml up -d --build
```

Docker streams the `relay/` build context over SSH to the companion computer's daemon, which builds and starts the container natively. The companion computer never needs the repo cloned. `restart: unless-stopped` keeps the relay running across reboots.

---

## Discussions

Have questions, ideas, or want to follow along? Join the conversation:
[github.com/9LogM/Orbis/discussions](https://github.com/9LogM/Orbis/discussions)

---

## Special Thanks

This project was built with the help of [Claude](https://claude.ai) — cheers for the pair programming.
