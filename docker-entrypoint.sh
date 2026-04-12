#!/bin/bash
set -e

# SSH files mounted from Windows have incorrect permissions.
# Copy to a fresh directory with correct Unix permissions before the SSH client sees them.
if [ -d /mnt/ssh ]; then
    mkdir -p /root/.ssh
    cp -r /mnt/ssh/. /root/.ssh/
    chmod 700 /root/.ssh
    find /root/.ssh -type f -exec chmod 600 {} \;
    find /root/.ssh -name "*.pub" -exec chmod 644 {} \;
fi

exec /workspace/build/orbis "$@"
