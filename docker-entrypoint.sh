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

# A CDPATH inherited from the environment makes `cd` to a relative path both
# print where it landed and potentially land somewhere else, either of which
# corrupts the resolution below. Clear it rather than work around it.
CDPATH=''

APP_USER=app

# The app defaults to ./data/app.db relative to WORKDIR and creates the parent
# itself (store.py), so this has to cover the unmounted case as well as the
# mounted one. Derived from the same variable the app reads rather than
# hardcoded to /data, or a custom DATABASE_PATH silently gets no chown.
#
# Every path-taking command below gets a -- terminator: a DATABASE_PATH whose
# first character is a dash would otherwise be parsed as options.
DATABASE_PATH="${DATABASE_PATH:-./data/app.db}"
DATA_DIR="$(dirname -- "$DATABASE_PATH")"

if [ "$(id -u)" = "0" ]; then
    mkdir -p -- "$DATA_DIR"
    # Resolve before doing anything with it, so a relative path or one with a
    # .. in it is the same path here as the app will open. -P also means no
    # component of what follows is a symlink.
    DATA_DIR="$(cd -P -- "$DATA_DIR" && pwd -P)"

    # DATABASE_PATH=/app.db would otherwise put the whole filesystem in scope
    # below. Refuse rather than proceed: 78 is EX_CONFIG, and this is one.
    if [ "$DATA_DIR" = "/" ]; then
        echo "docker-entrypoint: DATABASE_PATH must sit in a directory, not at /" >&2
        exit 78
    fi

    # Named paths rather than a recursive walk. The walk was cheap here — the
    # directory holds a SQLite database and its sidecars and nothing else
    # writes there — but its cost would scale with whatever later lands on the
    # volume, and rooted at an unexpected DATA_DIR it is a large mistake to
    # make on every start.
    #
    # Not conditional on the directory's owner either: the directory is a poor
    # proxy for the files in it. Restoring a backup as root leaves an app-owned
    # directory holding a root-owned database, which the app still cannot open.
    # WAL is on (store.py), and -journal covers the rollback-mode fallback.
    #
    # -h, because these are the one set of paths an already-compromised app
    # process could have replaced. It owns this directory, so it can put a
    # symlink where the database goes; a dereferencing chown would then hand it
    # ownership of the link's target — this script, say — and root runs that on
    # the next start. -h changes the link rather than what it points at, which
    # also makes the -e test's race harmless: whatever is there at chown time
    # gets its own ownership changed and nothing else does. The directory needs
    # no -h, since resolving it above left it free of symlink components.
    # A trailing . or .. survives the check above — DATABASE_PATH=/data/..
    # gives DATA_DIR=/data, which is fine, and DB_FILE=/data/.., which names /.
    # -h does not help: .. is a real directory entry, not a symlink to refuse
    # to follow. So the basename has to name a file.
    DB_NAME="$(basename -- "$DATABASE_PATH")"
    case "$DB_NAME" in
        "" | "." | "..")
            echo "docker-entrypoint: DATABASE_PATH must name a file, not a directory" >&2
            exit 78
            ;;
    esac

    DB_FILE="$DATA_DIR/$DB_NAME"
    chown -- "$APP_USER:$APP_USER" "$DATA_DIR"
    for db_file in "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm" "$DB_FILE-journal"; do
        [ ! -e "$db_file" ] || chown -h -- "$APP_USER:$APP_USER" "$db_file"
    done

    # The EPUB cache, when configured. Same problem as DATA_DIR: a fresh
    # volume arrives root-owned and the app cannot create or write it once
    # unprivileged. Unset means no cache and nothing to do.
    CACHE_DIR="${EPUB_CACHE_DIR:-}"
    if [ -n "$CACHE_DIR" ]; then
        mkdir -p -- "$CACHE_DIR"
        CACHE_DIR="$(cd -P -- "$CACHE_DIR" && pwd -P)"
        if [ "$CACHE_DIR" = "/" ]; then
            echo "docker-entrypoint: EPUB_CACHE_DIR must be a directory, not /" >&2
            exit 78
        fi
        # The directory only, not its contents. A recursive walk would scale
        # with the number of cached books, and it is not needed: the app owns
        # the directory, so it can replace a stale root-owned entry, and one it
        # cannot read is treated as a cache miss and rebuilt.
        chown -- "$APP_USER:$APP_USER" "$CACHE_DIR"
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
