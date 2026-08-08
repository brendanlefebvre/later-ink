FROM python:3.12-slim

WORKDIR /app

# Dependencies first, from the lockfile, so this layer is cached until the
# pins actually change — and so the image is built from the exact versions CI
# tested rather than whatever the >= ranges resolve to on build day.
# --require-hashes makes a substituted artifact a build failure.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ src/

# --no-deps: everything is already installed above at its pinned version, and
# resolving again could quietly pull something newer.
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status == 200 else 1)"

CMD ["uvicorn", "later_ink.main:app", "--host", "0.0.0.0", "--port", "8000"]
