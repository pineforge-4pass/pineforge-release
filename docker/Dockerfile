# pineforge-release — the combined PineScript->backtest product image.
#
#   = the pineforge-engine static library (C ABI)  +  the pinned
#     pineforge-codegen transpiler  +  the Pine->backtest harness.
#
# The engine no longer ships a Docker image; it is a C++ library delivered as a
# per-arch GitHub Release tarball (libpineforge.a + headers). This image fetches
# that tarball, lays a g++/Eigen/python3 toolchain + the codegen transpiler +
# the entrypoint/run_json harness on top, and IS the standalone `.pine -> trades`
# tool. Downstream (the MCP servers, end users) consume THIS image.
#
# Versions are NOT pinned in this file. CI resolves them and supplies them as
# build-args; the (engine, codegen) pair is recorded in the release tag message
# (read back by publish.yml) + the io.pineforge.* image labels below. Building
# without the build-args is an error (guards below), never a silent empty value:
#   ENGINE_VERSION   pineforge-engine release whose static-lib tarball is fetched
#   CODEGEN_VERSION  pineforge-codegen PyPI version baked in
#
# Published as ghcr.io/pineforge-4pass/pineforge-release:<own-semver>.

ARG ENGINE_VERSION
ARG CODEGEN_VERSION

# --- Stage 1: stage the pure-Python transpiler (final image carries no pip) ---
FROM debian:bookworm-slim AS codegen
ARG CODEGEN_VERSION
RUN test -n "${CODEGEN_VERSION}" || { echo "ERROR: CODEGEN_VERSION build-arg is required (CI resolves it)" >&2; exit 1; }
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir -U \
        --target /opt/codegen "pineforge-codegen==${CODEGEN_VERSION}"

# --- Stage 2: fetch + verify the engine static-lib tarball (per build arch) ---
FROM debian:bookworm-slim AS engine
ARG ENGINE_VERSION
ARG TARGETARCH
RUN test -n "${ENGINE_VERSION}" || { echo "ERROR: ENGINE_VERSION build-arg is required (CI resolves it)" >&2; exit 1; }
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) triple=linux-x86_64 ;; \
      arm64) triple=linux-aarch64 ;; \
      *) echo "unsupported TARGETARCH='${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    base="pineforge-v${ENGINE_VERSION}-${triple}"; \
    url="https://github.com/pineforge-4pass/pineforge-engine/releases/download/v${ENGINE_VERSION}"; \
    cd /tmp; \
    curl -fsSL "${url}/${base}.tar.gz"        -o "${base}.tar.gz"; \
    curl -fsSL "${url}/${base}.tar.gz.sha256" -o "${base}.tar.gz.sha256"; \
    sha256sum -c "${base}.tar.gz.sha256"; \
    tar -xzf "${base}.tar.gz"; \
    mkdir -p /opt/pineforge; \
    cp -r "/tmp/${base}/lib"     /opt/pineforge/lib; \
    cp -r "/tmp/${base}/include" /opt/pineforge/include

# --- Stage 3: final = toolchain + engine lib + codegen + harness --------------
FROM debian:bookworm-slim
ARG ENGINE_VERSION
ARG CODEGEN_VERSION
ARG RELEASE_VERSION=unknown
ARG RELEASE_GIT_SHA=unknown
ARG RELEASE_BUILD_DATE=unknown
RUN test -n "${ENGINE_VERSION}" && test -n "${CODEGEN_VERSION}" || { echo "ERROR: ENGINE_VERSION + CODEGEN_VERSION build-args are required (CI resolves them)" >&2; exit 1; }

LABEL org.opencontainers.image.title="pineforge-release" \
      org.opencontainers.image.description="Full PineScript v6 -> deterministic backtest (engine static lib + bundled codegen transpiler)" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${RELEASE_GIT_SHA}" \
      org.opencontainers.image.created="${RELEASE_BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/pineforge-4pass/pineforge-release" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.pineforge.engine.version="${ENGINE_VERSION}" \
      io.pineforge.codegen.version="${CODEGEN_VERSION}"

# g++ + Eigen compile the transpiled generated.cpp against the static lib;
# python3 runs the transpiler + the JSON harness. No pip, no network at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libeigen3-dev \
        python3 \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info

RUN mkdir -p /opt/pineforge/bin
COPY --from=engine  /opt/pineforge/lib       /opt/pineforge/lib
COPY --from=engine  /opt/pineforge/include   /opt/pineforge/include
COPY --from=codegen /opt/codegen             /opt/pineforge/pycodegen
COPY docker/run_json.py   /opt/pineforge/bin/run_json.py
COPY docker/entrypoint.sh /opt/pineforge/bin/entrypoint.sh
RUN chmod +x /opt/pineforge/bin/entrypoint.sh /opt/pineforge/bin/run_json.py

ENV PINEFORGE_PREFIX=/opt/pineforge \
    PYTHONPATH=/opt/pineforge/pycodegen \
    PINEFORGE_ENGINE_VERSION=${ENGINE_VERSION} \
    PINEFORGE_CODEGEN_VERSION=${CODEGEN_VERSION} \
    PINEFORGE_RELEASE_VERSION=${RELEASE_VERSION} \
    PINEFORGE_VERSION=${RELEASE_VERSION}

# Drop root: the entrypoint compiles + runs everything under mktemp dirs.
RUN useradd -r -u 10001 -s /usr/sbin/nologin pineforge
USER 10001

ENTRYPOINT ["/opt/pineforge/bin/entrypoint.sh"]
