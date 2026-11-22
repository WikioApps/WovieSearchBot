FROM python:3.13-slim

ENV MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/var/lib/wovie/tmp

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=pip_ca if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 wovie \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app wovie \
    && mkdir -p /var/lib/wovie/tmp \
    && chown -R 10001:10001 /var/lib/wovie

COPY --chown=10001:10001 wovie ./wovie
COPY --chown=10001:10001 scripts ./scripts
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "-m", "wovie"]
