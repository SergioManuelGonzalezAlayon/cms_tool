# syntax=docker/dockerfile:1.7

FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/current-measurement/.venv \
    CURRENT_MEASUREMENT_IMAGE_REPO=/opt/current-measurement-build \
    CURRENT_MEASUREMENT_WORKSPACE=/workspace \
    SILABS_HOME=/opt/silabs \
    PATH="/opt/current-measurement/.venv/bin:/opt/silabs/bin:/usr/local/bin:${PATH}"

WORKDIR /opt/current-measurement-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libusb-1.0-0 \
        ninja-build \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /usr/local/bin/uv
COPY container/vendor-tools/ /opt/silabs/

COPY . /opt/current-measurement-build

RUN mkdir -p /opt/silabs/bin \
    && if [ -d /opt/silabs/bin ]; then find /opt/silabs/bin -maxdepth 1 -type f -exec chmod +x {} +; fi
RUN test -f external/power_profiling_tools_val/pyproject.toml
RUN uv sync --frozen
RUN mkdir -p /workspace/artifacts

WORKDIR /workspace

ENTRYPOINT ["python", "/opt/current-measurement-build/scripts/container_entrypoint.py"]
