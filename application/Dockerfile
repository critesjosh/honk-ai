# Builder stage — installs Python deps into a venv.
#
# This fork is for deployments that use REMOTE embeddings (OpenAI or similar
# via EMBEDDINGS_BASE_URL). torch / transformers / sentence-transformers have
# been removed, which drops ~5GB of CUDA/triton wheels and the 400MB
# mpnet-base-v2 model the upstream image bundled for local embeddings.
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-only toolchain (dropped from the final image).
# gcc/g++/libc6-dev: some pure-Python wheels need a C compiler if no wheel
# is published for python 3.12 bookworm — defensive but cheap.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install -r requirements.txt


# Final stage — runtime only.
FROM python:3.12-slim-bookworm AS final

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/venv/bin:$PATH \
    FLASK_APP=app.py \
    FLASK_DEBUG=false

# Runtime system libs:
# - poppler-utils: used by pdf2image / PDFParser for PDF text extraction.
# libgl1 / libglib2.0-0 dropped: they were transitive opencv deps pulled
# by docling/rapidocr, which are no longer in this image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /sbin/nologin -c "Docker image user" appuser

COPY --from=builder /venv /venv

# --chown in the COPY avoids the ~475MB duplicate layer that a separate
# `RUN chown -R` would create (every file gets a new metadata record).
COPY --chown=appuser:appuser . /app/application

# inputs/ is excluded from the build context (.dockerignore): it's dev
# upload junk locally and a volume mountpoint at runtime (named `uploads`
# volume in the hub compose, bind mount in dev). Recreate it empty and
# appuser-owned — `-R` matters: a fresh named volume copies the image
# directory's ownership, so a root-owned inputs/ would break uploads.
RUN mkdir -p /app/application/inputs/local && \
    chown -R appuser:appuser /app/application/inputs

EXPOSE 7091

USER appuser

# gthread with 4 workers × 8 threads → 32 concurrent requests. The workload
# is I/O-bound (Postgres, OpenAI, pgvector retrieval), so threads fit well
# and avoid the monkey-patching risk of gevent/eventlet. Celery runs
# heavy/long work out-of-band — the Gunicorn process just serves API + SSE.
# Bumped from 2 → 4 workers to better absorb spikes in concurrent widget
# users; each /stream call holds a slot for 5–15s.
#
# --keep-alive 75: governs the Caddy→Gunicorn connection reuse window.
# --graceful-timeout 30: drain in-flight SSE on SIGTERM. Requires a
#   matching stop_grace_period on the backend compose service; default
#   Compose SIGKILL at 10s would otherwise cut streams short.
# --timeout 120: worker-stall detection (not stream duration). Long
#   streams stay healthy because the SSE heartbeat yields every ~15s.
CMD ["gunicorn", \
     "-k", "gthread", \
     "-w", "4", \
     "--threads", "8", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--keep-alive", "75", \
     "--bind", "0.0.0.0:7091", \
     "--preload", \
     "application.wsgi:app"]
