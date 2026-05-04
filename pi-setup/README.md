# pi-setup

Pi-side bash scripts that the ground-station-side TUI pipes over SSH. Three live here:

- `setup-ap.sh` — one-time AP-mode bring-up (operator runs once per Pi build, see below)
- `deploy-publisher.sh` — sensor-publisher clone+up; called automatically by the TUI on launch and from the "Restart publisher" menu, never run manually
- `check-publisher.sh` — sensor-publisher liveness probe; called automatically by the TUI on launch, never run manually

The TUI bakes this directory into the lexaire image (Dockerfile `COPY . .`) and references the scripts at `/workspace/pi-setup/*.sh`. Edits here ship with the next image build.

## `setup-ap.sh`

Turns the Pi's `wlan0` into a WPA2 access point using NetworkManager (Pi OS Bookworm+). The ground station connects directly to this AP — no router involved in production. Run once per Pi build.

Remote invocation (from the ground station) pipes the script over SSH so nothing needs pre-copying:

```bash
ssh orbis@drone.local "sudo bash -s" < pi-setup/setup-ap.sh
```

Overrides via env vars:

| Var | Default | What |
| --- | --- | --- |
| `SSID` | `drone-ap` | AP name |
| `PASSWORD` | `lexaire-drone` | WPA2 passphrase |
| `CON_NAME` | `drone-ap` | NetworkManager connection name |
| `CHANNEL` | `6` | 2.4 GHz channel |
| `IFACE` | `wlan0` | Radio interface |

After a successful run, NetworkManager's built-in dnsmasq hands out DHCP on `10.42.0.0/24` with the Pi at `10.42.0.1`. Set the drone hostname in `common/config.yaml` to that IP.

**Important:** running this over a WiFi SSH session kills the session when AP mode comes up, because `wlan0` can only be in one mode at a time. Prefer ethernet or a console to run it.
