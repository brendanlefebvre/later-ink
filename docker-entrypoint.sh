#!/bin/sh
# Fix ownership on the database directory as root, then drop privileges and
# exec the app.
#
# Why this exists rather than a bare `USER app` in the Dockerfile: fly.toml
# mounts a volume at /data and Fly creates volumes root-owned, while
# docker-compose bind-mounts ./data, where ownership comes from the host. The
# mount is laid over the image's filesystem at runtime, so a build-time chown is
# invisible and a plain USER produces an image that starts, cannot open its
# SQLite database, and takes the deployed instance down. Fixing ownership has to
# happen at runtime, as root, before the drop — which is what an entrypoint is.
set -eu

APP_USER=app
APP_UID=10001

# The app defaults to ./data/app.db relative to WORKDIR and creates the parent
# itself (store.py), so this has to cover the unmounted case as well as the
# mounted one. Derived from the same variable the app reads rather than
# hardcoded to /data, or a custom DATABASE_PATH silently gets no chown.
DATABASE_PATH="${DATABASE_PATH:-./data/app.db}"
DATA_DIR="$(dirname "$DATABASE_PATH")"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"

    # Conditional rather than an unconditional recursive walk. The contents are
    # a SQLite file plus its WAL, so the walk is cheap here, but it runs on
    # every start and its cost scales with whatever else lands on the volume.
    # The directory's own owner is the signal: it is what the mount sets, and
    # what a previous start would already have corrected.
    if [ "$(stat -c %u "$DATA_DIR")" != "$APP_UID" ]; then
        chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
    fi

    # setpriv comes from util-linux, already in the Debian base image — no
    # gosu or su-exec to install and keep pinned. --init-groups gives the
    # process the app user's supplementary groups rather than root's leftovers;
    # --inh-caps=-all and --no-new-privs stop anything regaining privilege
    # through a setuid binary after the drop. exec so the app replaces this
    # shell rather than running under it, and receives stop signals directly.
    exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
        --inh-caps=-all --no-new-privs -- "$@"
fi

# Already unprivileged, because someone passed `--user` / compose `user:`. There
# is no privilege to drop and no ownership we could fix; the mount has to line
# up with the uid they chose. Run the app and let it report the problem if not.
exec "$@"
