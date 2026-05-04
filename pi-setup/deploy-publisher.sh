#!/usr/bin/env bash
# Lexaire sensor publisher deploy.
#
# Pipe over SSH from the GCS:
#
#   ssh orbis@drone.local 'bash -s -- <repo_url>' < pi-setup/deploy-publisher.sh
#
# The TUI's ensure_publisher_running calls this on startup and on
# "Restart publisher" from the menu. Idempotent and SD-card-friendly:
# only rebuilds the image when origin/HEAD actually moved.
#
# Behavior:
#   - First run (no checkout)      → git clone, then `up -d --build`.
#   - URL changed since last run   → bring old project down, rm -rf, re-clone.
#   - HEAD == origin/HEAD          → `up -d` only (no rebuild).
#   - HEAD != origin/HEAD          → `git reset --hard origin/HEAD`, then
#                                    `up -d --build`. The hard reset is
#                                    deliberate: this dir is Lexaire-owned;
#                                    operators must not hand-edit it, and a
#                                    forward-only reset enforces that.
#
# Why no `|| true` around `docker compose down` in the URL-change branch:
# if `down` fails, the old containers keep holding the configured ZMQ ports
# and the rm -rf below would orphan them. We must abort and surface the
# error to the operator instead of silently leaving a broken state.

set -euo pipefail

REPO_URL="${1:?repo URL required as first arg}"
DIR="$HOME/lexaire-publisher"

# ---- URL-change detection -------------------------------------------------

if [ -d "$DIR/.git" ]; then
    have="$(git -C "$DIR" remote get-url origin 2>/dev/null || echo "")"
    if [ "$have" != "$REPO_URL" ]; then
        echo "publisher_repo changed ($have -> $REPO_URL); resetting $DIR"
        if ! ( cd "$DIR" && docker compose down ); then
            echo "ERROR: failed to bring down old publisher project at $DIR" >&2
            echo "       containers are still running and would orphan if we rm -rf'd." >&2
            echo "       fix manually on the Pi, then re-run." >&2
            exit 1
        fi
        rm -rf "$DIR"
    fi
fi

# ---- Clone / smart-build --------------------------------------------------

if [ ! -d "$DIR/.git" ]; then
    # Fresh checkout: must build, no image exists yet.
    git clone "$REPO_URL" "$DIR"
    cd "$DIR"
    docker compose up -d --build
    exit 0
fi

# Existing checkout: only rebuild if origin/HEAD moved. `--build` recompiles
# every layer's RUN cache against the source tree, which on a Pi means
# minutes of CPU + non-trivial SD-card writes. Skip it on the no-op path.
git -C "$DIR" fetch --all --prune

local_head="$(git -C "$DIR" rev-parse HEAD)"
remote_head="$(git -C "$DIR" rev-parse origin/HEAD)"

if [ "$local_head" = "$remote_head" ]; then
    echo "publisher up-to-date at ${local_head:0:12}; reconciling without rebuild"
    cd "$DIR"
    docker compose up -d
else
    echo "publisher updating: ${local_head:0:12} -> ${remote_head:0:12}"
    # Hard reset on purpose: the dir is Lexaire-managed and any local edits
    # are operator drift we want to flatten so the deployed image always
    # matches the declared upstream.
    git -C "$DIR" reset --hard origin/HEAD
    cd "$DIR"
    docker compose up -d --build
fi
