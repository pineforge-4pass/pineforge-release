# pineforge-release — the combined PineScript -> backtest product image.
#
#   = the pineforge-engine pure runtime  +  the pinned pineforge-codegen transpiler.
#
# This repo is the SINGLE place where the engine version and the codegen version
# are pinned INDEPENDENTLY; it owns its own semver. Downstream (the MCP servers
# and end users) consume THIS image for full `.pine` -> trades — not the bare
# engine image.
#
# Build args (handle-upstream.yml bumps the pins; publish.yml passes RELEASE_*):
#   ENGINE_VERSION   ghcr pineforge-engine tag to build on
#   CODEGEN_VERSION  pineforge-codegen PyPI version to bake in
#
# Published as ghcr.io/pineforge-4pass/pineforge-release:<own-semver>.

ARG ENGINE_VERSION=0.10.10
ARG CODEGEN_VERSION=0.7.5

# --- Stage 1: stage the pure-Python transpiler (final image carries no pip) ---
FROM debian:bookworm-slim AS codegen
ARG CODEGEN_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir -U \
        --target /opt/codegen "pineforge-codegen==${CODEGEN_VERSION}"

# --- Stage 2: engine pure runtime + bundled codegen = full Pine->backtest -----
FROM ghcr.io/pineforge-4pass/pineforge-engine:${ENGINE_VERSION}
ARG ENGINE_VERSION
ARG CODEGEN_VERSION
ARG RELEASE_VERSION=unknown
ARG RELEASE_GIT_SHA=unknown
ARG RELEASE_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="pineforge-release" \
      org.opencontainers.image.description="Full PineScript v6 -> deterministic backtest (engine runtime + bundled codegen transpiler)" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${RELEASE_GIT_SHA}" \
      org.opencontainers.image.created="${RELEASE_BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/pineforge-4pass/pineforge-release" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.pineforge.engine.version="${ENGINE_VERSION}" \
      io.pineforge.codegen.version="${CODEGEN_VERSION}"

# Layer the transpiler onto the engine runtime at the same path the engine
# entrypoint expects. PYTHONPATH makes `from pineforge_codegen import transpile`
# resolve, so the inherited entrypoint accepts strategy.pine directly.
# NOTE: on a codegen-free engine base (post-shed) this is the SOLE codegen; on a
# still-bundled base it overwrites /opt/pineforge/pycodegen with the same tree.
USER root
COPY --from=codegen /opt/codegen /opt/pineforge/pycodegen
# Own the Pine->backtest harness HERE rather than inheriting it from the engine
# base — Phase 2 turns the engine into a pure base (no entrypoint/run_json), so
# this image must carry the harness itself. (On a still-bundled engine base these
# overwrite the inherited copies with identical content.)
COPY docker/run_json.py   /opt/pineforge/bin/run_json.py
COPY docker/entrypoint.sh /opt/pineforge/bin/entrypoint.sh
RUN chmod +x /opt/pineforge/bin/entrypoint.sh /opt/pineforge/bin/run_json.py
ENV PYTHONPATH=/opt/pineforge/pycodegen \
    PINEFORGE_ENGINE_VERSION=${ENGINE_VERSION} \
    PINEFORGE_CODEGEN_VERSION=${CODEGEN_VERSION} \
    PINEFORGE_RELEASE_VERSION=${RELEASE_VERSION}
USER 10001

# This image OWNS the entrypoint (the engine base may shed it in Phase 2).
ENTRYPOINT ["/opt/pineforge/bin/entrypoint.sh"]
