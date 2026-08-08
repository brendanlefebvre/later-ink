# Two stages so the build backend never reaches the shipped image.
#
# pip normally installs the PEP 517 backend into a throwaway environment it
# creates itself, resolving build-system.requires fresh — unpinned, unhashed,
# and outside every other guarantee in this repo. --no-build-isolation closes
# that, but it means the backend has to be installed for real, and a
# single-stage build would then ship hatchling in the runtime image. Building
# the wheel here and copying only the wheel across keeps both properties.
FROM python:3.12-slim AS builder

WORKDIR /app

# The build backend, pinned and hashed like everything else.
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt

COPY pyproject.toml README.md ./
COPY src/ src/

# --no-build-isolation: use the backend installed above rather than letting pip
# fetch its own. --no-deps: this builds the wheel, it does not install into it.
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation -w /wheels .


FROM python:3.12-slim

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

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status == 200 else 1)"

CMD ["uvicorn", "later_ink.main:app", "--host", "0.0.0.0", "--port", "8000"]
