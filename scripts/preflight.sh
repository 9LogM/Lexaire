#!/usr/bin/env bash
# Lexaire pre-flight check.
#
# Verifies the local environment, the companion-computer link, and the
# stack's container health before a flight. Designed to be run either
# directly from the host or from inside the lexaire TUI container.
#
# Output is plain text with [OK]/[WARN]/[FAIL] markers — no ANSI, since
# the TUI renders this verbatim into an ncurses subview.
#
# Exit codes:
#   0  every check passed (or only [WARN])
#   1  at least one [FAIL]

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FAIL=0

ok()   { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

section() {
    printf '\n  %s\n' "$1"
}

printf '  PRE-FLIGHT CHECK  (repo: %s)\n' "$REPO_ROOT"

# ---- Local config ---------------------------------------------------------
section "Local config"

ENV_FILE="$REPO_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    ok ".env present"
    if grep -Eq '^GEMINI_API_KEY=.+' "$ENV_FILE"; then
        ok "GEMINI_API_KEY set in .env"
    else
        fail "GEMINI_API_KEY missing or empty in .env"
    fi
else
    # Fall back to the live process env — works when the script runs inside
    # a container that received GEMINI_API_KEY via env_file or -e.
    if [ -n "${GEMINI_API_KEY:-}" ]; then
        warn ".env file not found at $ENV_FILE, but GEMINI_API_KEY is set in environment"
    else
        fail ".env not found at $ENV_FILE and GEMINI_API_KEY not in environment"
    fi
fi

CFG="$REPO_ROOT/common/config.yaml"
if [ -f "$CFG" ]; then
    ok "common/config.yaml present"
else
    fail "common/config.yaml missing — orchestrator/perception/bridge will fail to start"
fi

# ---- Tooling --------------------------------------------------------------
section "Tooling"

if command -v docker >/dev/null 2>&1; then
    ok "docker on PATH"
    if docker info >/dev/null 2>&1; then
        ok "docker daemon reachable"
    else
        warn "docker daemon not reachable from here (expected when running inside the lexaire container without /var/run/docker.sock mounted)"
    fi
else
    fail "docker not found"
fi

# `docker compose config -q` parses the file without contacting the daemon.
if command -v docker >/dev/null 2>&1; then
    if (cd "$REPO_ROOT" && docker compose config -q >/dev/null 2>&1); then
        ok "docker-compose.yaml validates"
    else
        fail "docker compose config rejected docker-compose.yaml"
    fi
fi

# ---- Companion link -------------------------------------------------------
section "Companion link"

# Pull drone.host out of config without requiring a YAML parser.
DRONE_HOST=""
if [ -f "$CFG" ]; then
    DRONE_HOST="$(awk '
        /^drone:/      { in_drone=1; next }
        /^[a-zA-Z]/    { in_drone=0 }
        in_drone && /^[[:space:]]+host:/ {
            sub(/^[[:space:]]+host:[[:space:]]*/, "")
            sub(/[[:space:]]*#.*$/, "")
            print
            exit
        }
    ' "$CFG")"
fi

if [ -z "$DRONE_HOST" ]; then
    warn "could not parse drone.host from $CFG; skipping companion-link checks"
else
    # drone.host is `user@hostname` — strip the user prefix for ping/nc.
    HOST="${DRONE_HOST#*@}"
    ok "drone.host = $DRONE_HOST"

    if command -v ping >/dev/null 2>&1; then
        if ping -c 1 -W 2 "$HOST" >/dev/null 2>&1; then
            ok "$HOST reachable (ping)"
        else
            warn "$HOST not reachable via ping (host may block ICMP)"
        fi
    else
        warn "ping not available; skipping reachability test"
    fi

    if command -v ssh >/dev/null 2>&1; then
        if ssh -o BatchMode=yes -o ConnectTimeout=3 \
              -o StrictHostKeyChecking=accept-new \
              "$DRONE_HOST" true 2>/dev/null; then
            ok "SSH to $DRONE_HOST works (BatchMode)"
        else
            fail "SSH to $DRONE_HOST failed — check key with 'ssh-copy-id $DRONE_HOST'"
        fi
    else
        warn "ssh not available; skipping SSH test"
    fi

    # Sensor publisher ports — read from sensor.channels in the config so
    # this tracks whatever publisher the operator pointed at. Skip blank
    # channels (operator can disable a stream by leaving it empty). If the
    # publisher isn't running on the Pi, perception will SUB silently and
    # never see a frame. Use bash /dev/tcp; portable to slim images without nc.
    PUBLISHER_REPO="$(awk '
        /^sensor:/      { in_sensor=1; next }
        /^[a-zA-Z]/     { in_sensor=0 }
        in_sensor && /^[[:space:]]+publisher_repo:/ {
            sub(/^[[:space:]]+publisher_repo:[[:space:]]*/, "")
            sub(/[[:space:]]*#.*$/, "")
            print
            exit
        }
    ' "$CFG")"
    PUBLISHER_HINT="see ${PUBLISHER_REPO:-sensor.publisher_repo in $CFG} for the publisher"

    CHANNEL_PORTS="$(awk '
        /^sensor:/                  { in_sensor=1; next }
        /^[a-zA-Z]/                 { in_sensor=0; in_channels=0 }
        in_sensor && /^  channels:/ { in_channels=1; next }
        in_sensor && /^  [a-zA-Z]/  { in_channels=0 }
        in_channels && /^    [a-zA-Z_]+:[[:space:]]+tcp:\/\// {
            v=$0
            sub(/^    [a-zA-Z_]+:[[:space:]]+/, "", v)
            sub(/[[:space:]]*#.*$/, "", v)
            n=split(v, parts, ":")
            if (n>=3) print parts[n]
        }
    ' "$CFG")"

    if [ -z "$CHANNEL_PORTS" ]; then
        warn "no sensor channels configured in $CFG"
    else
        for port in $CHANNEL_PORTS; do
            if (exec 3<>"/dev/tcp/$HOST/$port") 2>/dev/null; then
                exec 3<&-; exec 3>&-
                ok "sensor publisher port $port open on $HOST"
            else
                warn "sensor publisher port $port not reachable on $HOST ($PUBLISHER_HINT)"
            fi
        done
    fi
fi

# ---- Summary --------------------------------------------------------------
section "Summary"
if [ "$FAIL" -eq 0 ]; then
    ok "Pre-flight passed (warnings non-blocking)"
    exit 0
else
    fail "Pre-flight blocked by [FAIL] above — fix before bringing up the stack"
    exit 1
fi
