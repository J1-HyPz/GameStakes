#!/bin/sh
# GameStakes container entrypoint.
# Creates a runtime user matching PUID/PGID (TrueNAS dataset ownership),
# runs database migrations, then starts the app as that user.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
PORT="${PORT:-8080}"

if [ "$(id -u)" = "0" ]; then
    # Create or align the runtime user/group with the requested IDs.
    if ! getent group gamestakes >/dev/null 2>&1; then
        groupadd -o -g "$PGID" gamestakes
    else
        groupmod -o -g "$PGID" gamestakes
    fi
    if ! getent passwd gamestakes >/dev/null 2>&1; then
        useradd -o -u "$PUID" -g gamestakes -d /app -s /sbin/nologin gamestakes
    else
        usermod -o -u "$PUID" gamestakes
    fi

    # Tolerate chown failure (read-only or root-squashed mounts): the app only
    # hard-requires /data to be writable, and migrations fail loudly if not.
    chown -R gamestakes:gamestakes /config /data /logs \
        || echo "[entrypoint] warning: could not chown some of /config /data /logs (read-only or root-squashed mount?); continuing"
    RUN_AS="gosu gamestakes"
else
    # Container was started with --user; run as-is without privilege dropping.
    RUN_AS=""
fi

echo "[entrypoint] running database migrations"
$RUN_AS python -m app.db.migrate

echo "[entrypoint] seeding sports and leagues"
$RUN_AS python -m app.ingest.seed

echo "[entrypoint] starting GameStakes on port $PORT"
exec $RUN_AS python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
