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

set -u

DIR="$HOME/lexaire-publisher"

if [ ! -d "$DIR" ]; then
    exit 1
fi

cd "$DIR" || exit 1

if ! out="$(docker compose ps -q --status running 2>/dev/null)"; then
    exit 2
fi

if [ -z "$out" ]; then
    exit 1
fi

exit 0
