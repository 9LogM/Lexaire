# Orbis
An autonomous drone that maps and navigates its environment using SLAM.

---

## Requirements

### Hardware
- **Drone side:** Any ARM64 onboard computer acting as a MAVLink bridge, with any MAVLink-compatible flight controller connected via serial
- **Ground station:** Any machine capable of running Orbis and QGroundControl, networked to the drone

### Software
- Docker with Compose v2
- QGroundControl

---

## Configuration
Edit `common/config.yaml` before building:
```yaml
serial_device: /dev/ttyACM0  # serial device path to the flight controller
serial_baud: 57600           # baud rate of the serial connection
```

---

## Commands

**Build:**
```bash
docker compose build orbis
```

**Run** (interactive, removed on exit):
```bash
docker compose run --rm orbis
```

**Stop and remove containers:**
```bash
docker compose down --remove-orphans
```

**Nuke all unused Docker resources:**
```bash
docker system prune -a --volumes
```

**Force-kill all running containers:**
```bash
docker kill $(docker ps -q)
```

---

## QGroundControl

1. Make sure Orbis is running on the ground station and the drone is powered on.
2. In QGroundControl: **Application Settings → Comm Links → Add → UDP**.
3. Set the server address to the drone-side bridge computer's IP and port **14550**.
4. Click Connect — QGC will link up and the header bar in Orbis will turn green.

---

## Architecture

> This section documents how the system is built. It will grow as the project expands.

### Stack

| Component | Version | Role |
|---|---|---|
| Debian slim | 12 (Bookworm) | Base Docker image (`arm64v8`) |
| MAVSDK | 3.17.0 | High-level MAVLink SDK — telemetry, commands |
| mavlink-router | v4 | Low-level MAVLink packet forwarder |
| Boost.Asio | system | Async event loop, timers, signal handling |
| ncurses | system | Terminal UI |

---

### MAVLink routing

The flight controller speaks MAVLink over a serial connection. Two consumers need that stream simultaneously — MAVSDK (for telemetry and control) and QGroundControl (for full ground station visibility). A single serial port can only be opened once, so **mavlink-router** acts as a transparent byte-level forwarder.

The Pi runs only mavlink-router — it is a dumb MAVLink bridge. All application logic (Orbis, QGC, and eventually SLAM) runs on the ground station.

```
[ Drone ]
  Flight controller
        │ serial
        ▼
  Pi — mavlink-router
        │ UDP (network)
        ▼
[ Ground station ]
  ┌─────┴──────┐
  ▼            ▼
:14550       :14551
 QGC         MAVSDK (Orbis)
```

---

### MAVSDK

MAVSDK connects to mavlink-router's output on `udpin://0.0.0.0:14551`. It is configured as a `CompanionComputer` (not a GCS), which keeps it from interfering with QGC's GCS role.

---

## Discussions

Have questions, ideas, or want to follow along? Join the conversation:
[github.com/9LogM/Orbis/discussions](https://github.com/9LogM/Orbis/discussions)

---

## Special Thanks

This project was built with the help of [Claude](https://claude.ai) — cheers for the pair programming.