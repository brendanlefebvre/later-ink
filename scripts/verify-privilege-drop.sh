#!/usr/bin/env bash
# Verify that the built image drops privileges correctly against a root-owned
# volume — the shape Fly presents, and the failure mode that would otherwise
# appear only as a crash loop after deploy.
#
#   ./scripts/verify-privilege-drop.sh [image]        # default later-ink:test
#   PORT=18080 ./scripts/verify-privilege-drop.sh
#
# Exits non-zero if any check fails, so it can gate a release rather than
# needing someone to read a process table and spot a wrong number.
set -euo pipefail

IMAGE="${1:-later-ink:test}"
PORT="${PORT:-18080}"
APP_UID=10001

# $$ keeps a run from colliding with a volume that already exists and matters:
# docker volume create reuses one of the same name rather than refusing, and
# this script chowns and then deletes what it names.
vol="later-ink-privdrop-$$"
ctr="later-ink-privdrop-$$"

failures=0

check() {
    # check <description> <command...>
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf 'ok:   %s\n' "$description"
    else
        printf 'FAIL: %s\n' "$description"
        failures=$((failures + 1))
    fi
}

cleanup() {
    local status=$?
    docker rm -f "$ctr" >/dev/null 2>&1 || true
    docker volume rm "$vol" >/dev/null 2>&1 || true
    return $status
}
trap cleanup EXIT

printf 'image %s, host port %s\n\n' "$IMAGE" "$PORT"

# A volume owned by root, which is how Fly creates them and where a bare USER
# instruction would leave the app unable to open its database.
docker volume create "$vol" >/dev/null
docker run --rm -v "$vol":/data alpine chown -R 0:0 /data

docker run -d --name "$ctr" -p "$PORT":8000 \
    -v "$vol":/data -e DATABASE_PATH=/data/app.db \
    -e EPUB_CACHE_DIR=/data/epub-cache "$IMAGE" >/dev/null

# docker run -d returns before uvicorn is listening, so poll rather than
# asking straight away.
for _ in $(seq 30); do
    if curl -fsS --max-time 5 "localhost:$PORT/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

check "starts and answers /healthz" \
    curl -fsS --max-time 5 "localhost:$PORT/healthz"

# Matched inside the container so the pattern syntax is always GNU grep's,
# whatever the host ships. docker exec runs as the image's user (root, since
# there is no USER) and reads pid 1, which is the app itself.
# All four fields of each line, not just the first: Uid and Gid carry real,
# effective, saved and filesystem ids, and a process that kept an effective or
# filesystem identity of 0 would sail past a check on the real uid alone.
ids="[[:space:]]+$APP_UID[[:space:]]+$APP_UID[[:space:]]+$APP_UID[[:space:]]+$APP_UID$"
check "pid 1 runs as uid and gid $APP_UID, all four fields of each" \
    docker exec "$ctr" sh -c \
        "grep -Eq '^Uid:$ids' /proc/1/status && grep -Eq '^Gid:$ids' /proc/1/status"
check "no_new_privs is set" \
    docker exec "$ctr" grep -Eq '^NoNewPrivs:[[:space:]]*1$' /proc/1/status
check "inheritable capabilities are empty" \
    docker exec "$ctr" grep -Eq '^CapInh:[[:space:]]*0+$' /proc/1/status
check "the database was created" \
    docker exec "$ctr" test -f /data/app.db

# The mount itself rather than its contents: the entrypoint has to have taken
# ownership of the directory the volume put there, not just the file in it.
check "/data is owned by $APP_UID" \
    bash -c "docker run --rm -v '$vol':/data alpine stat -c '%u:%g' /data \
        | grep -qx '$APP_UID:$APP_UID'"

# The cache directory the entrypoint was asked to create, checked the same way
# as /data: the mount itself, from outside, since the app must own the
# directory and not merely be able to write a file into it.
check "/data/epub-cache is owned by $APP_UID" \
    bash -c "docker run --rm -v '$vol':/data alpine stat -c '%u:%g' /data/epub-cache \
        | grep -qx '$APP_UID:$APP_UID'"

printf '\n--- for the eye: docker top reads the host process table, and the\n'
printf -- '--- base image has no ps of its own\n'
docker top "$ctr" || true
docker exec "$ctr" grep -E '^(Uid|Gid|NoNewPrivs|CapInh):' /proc/1/status || true

if [ "$failures" -ne 0 ]; then
    printf '\n%s check(s) failed. docker logs %s for the reason.\n' "$failures" "$ctr" >&2
    # The container is about to be removed by the trap, so surface its logs
    # first — an unreadable database shows up here and nowhere else.
    docker logs "$ctr" 2>&1 | tail -20 >&2 || true
    exit 1
fi

printf '\nall checks passed\n'
