# Agent House add-on image.
# Since Supervisor 2026.04.0 BUILD_FROM is no longer passed and build.yaml is deprecated, so the
# base image is named here (multi-arch manifest: aarch64 + amd64). See docs/RESEARCH.md §1.
# [UNVERIFIED-ON-HW] built for aarch64 in CI under QEMU only; first real run is OWNER_CHECKLIST step M1.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/agent_house

COPY requirements.txt .
# Prefer wheels: every dependency ships musllinux wheels for aarch64 and amd64, so no compiler is
# needed. If a wheel is missing the build fails loudly here instead of compiling for an hour on a Pi.
RUN pip install --only-binary=:all: -r requirements.txt

COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ARG BUILD_ARCH
ARG BUILD_VERSION
LABEL \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Agent House" \
    org.opencontainers.image.source="https://github.com/vvvlladimir/agent-home"

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4).status == 200 else 1)"

CMD ["/run.sh"]
