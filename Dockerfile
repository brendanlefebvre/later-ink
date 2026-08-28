# Two stages so the build backend never reaches the shipped image.
#
# pip normally installs the PEP 517 backend into a throwaway environment it
# creates itself, resolving build-system.requires fresh — unpinned, unhashed,
# and outside every other guarantee in this repo. --no-build-isolation closes
# that, but it means the backend has to be installed for real, and a
# single-stage build would then ship hatchling in the runtime image. Building
# the wheel here and copying only the wheel across keeps both properties.
#
# Both stages pin the base image by digest. Every Python layer above it is
# hash-pinned, but `python:3.12-slim` is a mutable tag that upstream rebuilds
# regularly, so without this the interpreter and the OS packages underneath all
# those hashes still differ between two builds of the same commit. The builder
# is pinned too: it is what produces the wheel.
#
# The digest below is the multi-architecture index, not one platform's manifest,
# which is what keeps the linux/amd64 + linux/arm64 release build working.
# It resolved to Python 3.12.13 on Debian trixie, published 2026-08-05.
#
# The tag stays in the reference even though Docker ignores it once a digest is
# present. It is what Dependabot's `docker` ecosystem matches on to propose a
# new digest, and it tells a reader which stream this pin came from. Freezing
# the OS packages also freezes their CVEs, so the pin is only safe with
# something refreshing it — see .github/dependabot.yml, which also records why
# nothing tries to verify the tag and digest still agree.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS builder

WORKDIR /app

# The build backend, pinned and hashed like everything else.
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt

COPY pyproject.toml README.md ./
COPY src/ src/

# --no-build-isolation: use the backend installed above rather than letting pip
# fetch its own. --no-deps: this builds the wheel, it does not install into it.
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation -w /wheels .


FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

WORKDIR /app

# Dependencies first, from the lockfile, so this layer is cached until the pins
# actually change — and so the image runs the exact versions CI tested rather
# than whatever the >= ranges resolve to on build day. --require-hashes makes a
# substituted artifact a build failure.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# --no-deps: every dependency is already installed above at its pinned version,
# and resolving again could quietly pull something newer.
COPY --from=builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm -rf /tmp/*.whl

# A fixed uid/gid, not whatever useradd picks next. The number is what shows on
# a mounted volume inspected from the host, and it has to survive an image
# rebuild or an existing volume stops matching the user that has to write it.
# 10001 is above Debian's system range and unused in the base image.
#
# /app/data is created here because the app's default DATABASE_PATH is
# ./data/app.db and WORKDIR is root-owned. The entrypoint would fix that, but it
# is skipped entirely when the image is run with an explicit non-root --user,
# and uid 10001 cannot create a directory under /app on its own.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

# Docker sets HOME=/root when an image declares no USER, and setpriv does not
# rewrite the environment — its --reset-env clears the whole environment, which
# would take DATABASE_PATH and the tokens with it. So without this the app runs
# unprivileged while HOME still names root's 0700 home directory, and the XDG
# config lookup in config.py dies on the permission error before serving
# anything. The home directory is created above so the lookup finds a real
# path rather than a missing one.
ENV HOME=/home/app

# No USER instruction, deliberately: the entrypoint has to start as root to
# chown the volume mounted over /data, and drops to `app` itself before exec'ing
# the app. Image scanners flag the missing USER (Trivy DS-0002, Checkov
# CKV_DOCKER_3) — the container does not in fact run the app as root, and a
# USER here would produce one that cannot open its database. See
# docker-entrypoint.sh.
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# Docker runs HEALTHCHECK outside the entrypoint, so this stays root; it only
# makes an HTTP request to the app's own port and needs no more than that.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "later_ink.main:app", "--host", "0.0.0.0", "--port", "8000"]
