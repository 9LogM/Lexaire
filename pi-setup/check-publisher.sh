#!/usr/bin/env bash
# Lexaire sensor publisher liveness check.
#
# Pipe over SSH from the GCS:
#
#   ssh orbis@drone.local 'bash -s' < pi-setup/check-publisher.sh
#
# Container-name detection isn't usable here — different publisher repos
# name their containers differently — so we probe by directory: if
# `docker compose ps -q --status running` returns non-empty inside the
# auto-cloned dir, at least one publisher container is up.
#
# Exit codes match the ps_query_shell convention used elsewhere in the TUI:
#   0 = Up      (at least one container running in the publisher project)
#   1 = Down    (no project / no containers / all stopped)
#   2 = Unknown (docker daemon unreachable or unexpected failure)

set -euo pipefail

DIR="$HOME/lexaire-publisher"

# Down: directory not present yet.
[ -d "$DIR" ] || exit 1

cd "$DIR" || exit 1

# Capture-or-Unknown: a non-zero from `docker compose ps` is the
# daemon-unreachable signal, distinct from "no containers running".
out="$(docker compose ps -q --status running 2>/dev/null)" || exit 2

# No running containers in the project = Down.
[ -n "$out" ] || exit 1

exit 0
