# syntax=docker/dockerfile:1
# Official manifest: amd64, arm64v8, arm32v7 (among other architectures).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="mycameratele" \
    org.opencontainers.image.description="Camera archive dashboard and Telegram calendar; exported-file ingest" \
    org.opencontainers.image.source="https://github.com/bscongluanbui/mycameratele"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data \
    CACHE_DIR=/cache \
    INPUT_DIR=/input \
    DISPLAY_TIMEZONE=Asia/Ho_Chi_Minh \
    ENABLE_UPLOAD=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 archive \
    && useradd --uid 10001 --gid archive --no-create-home --shell /usr/sbin/nologin archive \
    && mkdir -p /app /data /cache /input \
    && chown 10001:10001 /data /cache

WORKDIR /app
COPY --chown=10001:10001 archive_app/ ./archive_app/
COPY --chown=10001:10001 tests/ ./tests/
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "archive_app", "health"]
ENTRYPOINT ["python", "-m", "archive_app"]
CMD ["run"]

# Building target test executes the suite for the target architecture.
FROM runtime AS test
RUN python -m unittest discover -s tests -v

# Default image: lean runtime, not the test build stage.
FROM runtime AS final
