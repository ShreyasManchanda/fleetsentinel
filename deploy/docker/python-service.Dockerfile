# One Dockerfile for every Python service.  docker build -f deploy/docker/python-service.Dockerfile \
#   --build-arg PACKAGE=fs-gateway --build-arg MODULE=fs_gateway.main -t fleetsentinel/gateway .
FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ARG PACKAGE
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY simulator simulator
COPY ml ml
RUN uv sync --frozen --no-dev --package ${PACKAGE}

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app /app
ARG MODULE
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SERVICE_MODULE=${MODULE}
USER 10001
# Read-only root filesystem friendly: writable paths are mounted as tmpfs (/tmp) by compose/Helm.
CMD ["sh", "-c", "exec python -m ${SERVICE_MODULE}"]
