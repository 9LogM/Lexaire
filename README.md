# Orbis
A drone using SLAM to map its surroundings, all controlled using a ROG Ally.

## Configuration
Edit `common/config.yaml` before running:
```yaml
connection_url: serial:///dev/ttyACM0:57600  # USB path to Pixhawk
qgc_ip: 192.168.1.x                          # IP of the machine running QGroundControl
```

## Features
- **Connect QGroundControl** — bridges MAVLink from the Pixhawk over UDP to QGroundControl on your laptop (port 14550)
- **Telemetry monitoring** — live flight mode and battery readouts via MAVSDK

## Instructions
1. Set `qgc_ip` in `common/config.yaml` to your laptop's IP.
2. Run Orbis and select **1. Connect QGroundControl**.
3. Open QGroundControl — it will connect automatically over UDP port 14550.

## Build
Build the `orbis` service image:
```bash
docker compose build orbis
```

## Down
Stop and remove containers, networks, and orphans:
```bash
docker compose down --remove-orphans
```

## Run
Run the main service interactively (removes container after exit):
```bash
docker compose run --rm orbis
```

## Prune
Nuke everything unused:
```bash
docker system prune -a --volumes
```

## Force Stop
Kill docker:
```bash
docker kill $(docker ps -q)
```
