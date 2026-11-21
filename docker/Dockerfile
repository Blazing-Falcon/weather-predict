FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM python:3.12.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-default-groups

FROM python:3.12.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 nowcast \
    && useradd --uid 1000 --gid 1000 --no-create-home nowcast
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ src/
COPY config/ config/
COPY web/ web/
ENV PATH="/app/.venv/bin:$PATH" TZ=UTC PYTHONUNBUFFERED=1 DATA_ROOT=/data \
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache MPLCONFIGDIR=/tmp/matplotlib \
    NUMBA_CACHE_DIR=/tmp/numba PYTHONPYCACHEPREFIX=/tmp/pycache \
    NOWCAST_CONFIG_DIR=/app/config NOWCAST_WEB_DIR=/app/web
USER 1000:1000
CMD ["nowcast", "runner"]
