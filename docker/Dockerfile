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
# build-args; the component pins are recorded in the release tag message
# (read back by publish.yml) + the io.pineforge.* image labels below. Building
# without the build-args is an error (guards below), never a silent empty value:
#   ENGINE_VERSION   pineforge-engine release whose static-lib tarball is fetched
#   CODEGEN_VERSION  pineforge-codegen PyPI version baked in
# The default target needs only ENGINE_VERSION and CODEGEN_VERSION above.
# --target hpo additionally requires HPO_VERSION (no source default) and the
# six toolchain package version build-args chosen on the signed box.
# HPO_WHEEL_SHA256 checks the downloaded wheel when supplied to that target.
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
FROM debian:bookworm-slim AS release
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
      org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-PineForge-Source-License-1.2" \
      io.pineforge.engine.version="${ENGINE_VERSION}" \
      io.pineforge.codegen.version="${CODEGEN_VERSION}"

# g++ + Eigen compile the transpiled generated.cpp against the static lib;
# python3 runs the transpiler + the JSON harness. No pip, no network at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libeigen3-dev \
        python3 \
    && rm -rf /var/lib/apt/lists/* /usr/share/man /usr/share/locale /usr/share/info

# --- sources notice (begin)
# Keep copyright files and symlinks (including package doc directory aliases).
RUN set -eu; \
    if [ -d /usr/share/doc ]; then \
      find /usr/share/doc -type f ! -name copyright -delete; \
      find /usr/share/doc -xtype l -delete; \
      find /usr/share/doc -depth -type d -empty -delete; \
    fi
COPY docker/SOURCES.txt.in /tmp/pineforge-SOURCES.txt.in
LABEL io.pineforge.sources.notice="/usr/share/doc/pineforge/SOURCES.txt" \
      io.pineforge.sources.url="https://github.com/pineforge-4pass/pineforge-release/releases/tag/${RELEASE_VERSION}"
RUN set -eu; \
    mkdir -p /usr/share/doc/pineforge; \
    if [ "${RELEASE_VERSION}" = unknown ]; then \
      sed 's|/releases/tag/@RELEASE_VERSION@|/releases|g' /tmp/pineforge-SOURCES.txt.in > /usr/share/doc/pineforge/SOURCES.txt; \
      printf '%s\n' 'Local build: use the releases page to locate mirrored Debian sources.' >> /usr/share/doc/pineforge/SOURCES.txt; \
    else \
      sed "s|@RELEASE_VERSION@|${RELEASE_VERSION}|g" /tmp/pineforge-SOURCES.txt.in > /usr/share/doc/pineforge/SOURCES.txt; \
    fi; \
    rm /tmp/pineforge-SOURCES.txt.in
# --- sources notice (end)
RUN mkdir -p /opt/pineforge/bin
COPY --from=engine  /opt/pineforge/lib       /opt/pineforge/lib
COPY --from=engine  /opt/pineforge/include   /opt/pineforge/include
COPY --from=codegen /opt/codegen             /opt/pineforge/pycodegen
# The harness is the engine's runtime files for the pinned engine tag, all eight and nothing
# else: scripts/sync-harness.sh writes them into docker/ and records their sha256 in
# HARNESS-SHA256SUMS, which the build checks below. entrypoint.sh calls
# bind_compiled_inventory.py; run_json.py imports the other modules from its own directory.
COPY docker/run_json.py   /opt/pineforge/bin/run_json.py
COPY docker/entrypoint.sh /opt/pineforge/bin/entrypoint.sh
COPY docker/bind_compiled_inventory.py /opt/pineforge/bin/bind_compiled_inventory.py
COPY docker/request_feed_inventory.py  /opt/pineforge/bin/request_feed_inventory.py
COPY docker/run_execution_observer.py  /opt/pineforge/bin/run_execution_observer.py
COPY docker/run_phase_transport.py     /opt/pineforge/bin/run_phase_transport.py
COPY docker/selected_window_plan.py    /opt/pineforge/bin/selected_window_plan.py
COPY docker/selected_window_report.py  /opt/pineforge/bin/selected_window_report.py
COPY docker/HARNESS-SHA256SUMS         /opt/pineforge/bin/HARNESS-SHA256SUMS
RUN chmod +x /opt/pineforge/bin/entrypoint.sh /opt/pineforge/bin/run_json.py

# Every harness file in the image must be byte for byte the engine's file at the ref that
# scripts/sync-harness.sh synced from: the script recorded the sha256 of each fetched file in
# HARNESS-SHA256SUMS and the build recomputes them here, on the installed files. The sums file
# must name exactly the eight files above, in this order, and nothing else may sit in bin/;
# a missing, extra, changed or empty entry fails the build.
RUN set -eu; \
    cd /opt/pineforge/bin; \
    want="bind_compiled_inventory.py entrypoint.sh request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py selected_window_plan.py selected_window_report.py"; \
    listed="$(sed 's/^[0-9a-f]\{64\}  //' HARNESS-SHA256SUMS | tr '\n' ' ' | sed 's/ $//')"; \
    present="$(LC_ALL=C ls -A | grep -v '^HARNESS-SHA256SUMS$' | tr '\n' ' ' | sed 's/ $//')"; \
    if [ "$listed" != "$want" ] || [ "$present" != "$want" ]; then \
      echo "ERROR: HARNESS-SHA256SUMS lists [${listed}] and bin/ holds [${present}]; both must be exactly [${want}]" >&2; exit 1; \
    fi; \
    sha256sum -c --strict HARNESS-SHA256SUMS; \
    echo "harness: the eight files match HARNESS-SHA256SUMS"

# The harness (run_json.py) mirrors the engine's C structs; it must be the copy
# from the pinned engine tag. Fail the build if its ABI differs from the header
# in the bundled tarball (0.1.23/0.1.24 shipped an ABI-2 harness on ABI-3 libs).
RUN set -eu; \
    want="$(sed -n 's/^#define PF_ABI_VERSION \([0-9][0-9]*\).*/\1/p' /opt/pineforge/include/pineforge/pineforge.h)"; \
    have="$(sed -n 's/^EXPECTED_PF_ABI = \([0-9][0-9]*\).*/\1/p' /opt/pineforge/bin/run_json.py)"; \
    if [ -z "$want" ] || [ -z "$have" ] || [ "$want" != "$have" ]; then \
      echo "ERROR: harness EXPECTED_PF_ABI=${have:-?} != engine PF_ABI_VERSION=${want:-?} for engine ${ENGINE_VERSION}; run scripts/sync-harness.sh ${ENGINE_VERSION}" >&2; exit 1; \
    fi; \
    echo "harness ABI ${have} matches engine ${ENGINE_VERSION}"

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

# --- HPO wheel: resolve with installed codegen before the separate install ---
FROM codegen AS hpo-wheel
ARG CODEGEN_VERSION
ARG HPO_VERSION
ARG HPO_WHEEL_SHA256
RUN test -n "${HPO_VERSION}" && test -n "${CODEGEN_VERSION}" || { echo "ERROR: HPO_VERSION + CODEGEN_VERSION build-args are required (CI resolves them)" >&2; exit 1; }
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/check_hpo_resolution.py /tmp/check_hpo_resolution.py
RUN set -eu; \
    wheel="/tmp/pineforge_hpo-${HPO_VERSION}-py3-none-any.whl"; \
    curl -fsSL "https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v${HPO_VERSION}/pineforge_hpo-${HPO_VERSION}-py3-none-any.whl" -o "$wheel"; \
    if [ -n "${HPO_WHEEL_SHA256}" ]; then \
      printf '%s  %s\n' "${HPO_WHEEL_SHA256}" "$wheel" > /tmp/hpo-wheel.sha256; \
      sha256sum -c --strict /tmp/hpo-wheel.sha256; \
    fi; \
    python3 -I /tmp/check_hpo_resolution.py \
      --hpo-wheel "$wheel" --hpo-version "${HPO_VERSION}" \
      --codegen-root /opt/codegen --codegen-version "${CODEGEN_VERSION}" \
      --report /tmp/hpo-resolution.json; \
    pip3 install --break-system-packages --no-cache-dir --no-deps \
      --target /opt/hpo "$wheel"; \
    mkdir -p /opt/hpo-licenses/pineforge-hpo /opt/hpo-licenses/pineforge-codegen; \
    set -- /opt/hpo/pineforge_hpo-*.dist-info/licenses; \
    test "$#" -eq 1; \
    test -f "$1/LICENSE"; test -f "$1/NOTICE"; \
    test -f "$1/THIRD_PARTY_LICENSES/sobol_joe_kuo.txt"; \
    cp -r "$1/." /opt/hpo-licenses/pineforge-hpo/; \
    set -- /opt/codegen/pineforge_codegen-*.dist-info/licenses; \
    test "$#" -eq 1; test -f "$1/LICENSE"; \
    cp -r "$1/." /opt/hpo-licenses/pineforge-codegen/

# --- HPO toolchain: shared by the native build and the FULL runtime -----------
# No guessed package versions: the box supplies these exact Debian pins for
# both architectures. gcc/g++/cpp are the bookworm GCC-12 defaults; the compiler
# and libstdc++ packages share HPO_GCC_VERSION. CMake/make stay build-only.
FROM debian:bookworm-slim AS hpo-toolchain
ARG HPO_GCC_DEFAULTS_VERSION
ARG HPO_GCC_VERSION
ARG HPO_BINUTILS_VERSION
ARG HPO_EIGEN_VERSION
RUN test -n "${HPO_GCC_DEFAULTS_VERSION}" && test -n "${HPO_GCC_VERSION}" \
    && test -n "${HPO_BINUTILS_VERSION}" && test -n "${HPO_EIGEN_VERSION}" \
    || { echo "ERROR: HPO toolchain package version build-args are required (box supplies them)" >&2; exit 1; }
# Preserve the packages' own copyright files before the existing doc cleanup.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "gcc=${HPO_GCC_DEFAULTS_VERSION}" "g++=${HPO_GCC_DEFAULTS_VERSION}" "cpp=${HPO_GCC_DEFAULTS_VERSION}" \
      "gcc-12=${HPO_GCC_VERSION}" "g++-12=${HPO_GCC_VERSION}" "cpp-12=${HPO_GCC_VERSION}" \
      "libgcc-12-dev=${HPO_GCC_VERSION}" "libstdc++-12-dev=${HPO_GCC_VERSION}" \
      "libgcc-s1=${HPO_GCC_VERSION}" "libstdc++6=${HPO_GCC_VERSION}" \
      "binutils=${HPO_BINUTILS_VERSION}" "libeigen3-dev=${HPO_EIGEN_VERSION}" python3; \
    for pkg in gcc g++ cpp gcc-12 g++-12 cpp-12 libgcc-12-dev libstdc++-12-dev libgcc-s1 libstdc++6 binutils libeigen3-dev; do \
      mkdir -p "/opt/pineforge/licenses/${pkg}"; \
      cp -L "/usr/share/doc/${pkg}/copyright" "/opt/pineforge/licenses/${pkg}/copyright"; \
    done; \
    rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info

# --- HPO native build: public + generated engine headers and Eigen -----------
FROM hpo-toolchain AS hpo-native
ARG HPO_VERSION
ARG HPO_CMAKE_VERSION
ARG HPO_MAKE_VERSION
RUN test -n "${HPO_VERSION}" && test -n "${HPO_CMAKE_VERSION}" && test -n "${HPO_MAKE_VERSION}" \
    || { echo "ERROR: HPO_VERSION + HPO_CMAKE_VERSION + HPO_MAKE_VERSION build-args are required" >&2; exit 1; }
RUN apt-get update && apt-get install -y --no-install-recommends \
      "cmake=${HPO_CMAKE_VERSION}" "make=${HPO_MAKE_VERSION}" curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=engine /opt/pineforge/include /opt/pineforge/include
# The released sdist includes CMake/native sources; its VERSION must match the
# wheel pin. Upstream CMake pins and SHA256-checks its dlib FetchContent archive.
RUN set -eu; \
    curl -fsSL "https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v${HPO_VERSION}/pineforge_hpo-${HPO_VERSION}.tar.gz" -o /tmp/hpo-source.tar.gz; \
    mkdir -p /tmp/hpo-source; \
    tar -xzf /tmp/hpo-source.tar.gz -C /tmp/hpo-source --strip-components=1; \
    test "$(cat /tmp/hpo-source/VERSION)" = "${HPO_VERSION}"; \
    cmake -S /tmp/hpo-source -B /tmp/hpo-build -DCMAKE_BUILD_TYPE=Release \
      -DPINEFORGE_ENGINE_ROOT=/opt/pineforge \
      -DPINEFORGE_HPO_BUILD_TESTS=OFF -DPINEFORGE_HPO_BUILD_EXAMPLES=OFF \
      -DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=ON -DPINEFORGE_HPO_BUILD_NATIVE_CLI=ON; \
    cmake --build /tmp/hpo-build --target pineforge-hpo-native --parallel 2; \
    mkdir -p /opt/hpo-native-licenses; \
    cp /tmp/hpo-source/THIRD_PARTY_LICENSES/dlib.txt /opt/hpo-native-licenses/dlib.txt; \
    cp /tmp/hpo-source/third_party/core_math/UPSTREAM_LICENSE /opt/hpo-native-licenses/core_math.txt

# --- FULL runtime: HPO layers + unchanged engine harness ---------------------
FROM release AS hpo
USER root
ARG RELEASE_VERSION=unknown
ARG HPO_VERSION
ARG HPO_GCC_DEFAULTS_VERSION
ARG HPO_GCC_VERSION
ARG HPO_BINUTILS_VERSION
ARG HPO_EIGEN_VERSION
RUN test -n "${HPO_VERSION}" && test -n "${HPO_GCC_DEFAULTS_VERSION}" && test -n "${HPO_GCC_VERSION}" \
    && test -n "${HPO_BINUTILS_VERSION}" && test -n "${HPO_EIGEN_VERSION}" \
    || { echo "ERROR: HPO_VERSION and HPO runtime toolchain package version build-args are required" >&2; exit 1; }

LABEL org.opencontainers.image.description="Full PineScript v6 -> deterministic backtest and HPO (engine static lib + bundled codegen transpiler + native HPO runner)" \
      io.pineforge.hpo.version="${HPO_VERSION}"

# Pin the runtime compiler and Eigen to the same packages as hpo-toolchain.
# Engine dev files, codegen, Python and the checked harness come from release;
# CMake, make, pip and the native build tree stay in the build stages.
RUN apt-get update && apt-get install -y --no-install-recommends \
      "gcc=${HPO_GCC_DEFAULTS_VERSION}" "g++=${HPO_GCC_DEFAULTS_VERSION}" "cpp=${HPO_GCC_DEFAULTS_VERSION}" \
      "gcc-12=${HPO_GCC_VERSION}" "g++-12=${HPO_GCC_VERSION}" "cpp-12=${HPO_GCC_VERSION}" \
      "libgcc-12-dev=${HPO_GCC_VERSION}" "libstdc++-12-dev=${HPO_GCC_VERSION}" \
      "libgcc-s1=${HPO_GCC_VERSION}" "libstdc++6=${HPO_GCC_VERSION}" \
      "binutils=${HPO_BINUTILS_VERSION}" "libeigen3-dev=${HPO_EIGEN_VERSION}" \
    && rm -rf /var/lib/apt/lists/* /usr/share/man /usr/share/locale /usr/share/info

# --- sources notice (begin)
# Keep copyright files and symlinks (including package doc directory aliases).
RUN set -eu; \
    if [ -d /usr/share/doc ]; then \
      find /usr/share/doc -type f ! -name copyright -delete; \
      find /usr/share/doc -xtype l -delete; \
      find /usr/share/doc -depth -type d -empty -delete; \
    fi
COPY docker/SOURCES.txt.in /tmp/pineforge-SOURCES.txt.in
LABEL io.pineforge.sources.notice="/usr/share/doc/pineforge/SOURCES.txt" \
      io.pineforge.sources.url="https://github.com/pineforge-4pass/pineforge-release/releases/tag/${RELEASE_VERSION}"
RUN set -eu; \
    mkdir -p /usr/share/doc/pineforge; \
    if [ "${RELEASE_VERSION}" = unknown ]; then \
      sed 's|/releases/tag/@RELEASE_VERSION@|/releases|g' /tmp/pineforge-SOURCES.txt.in > /usr/share/doc/pineforge/SOURCES.txt; \
      printf '%s\n' 'Local build: use the releases page to locate mirrored Debian sources.' >> /usr/share/doc/pineforge/SOURCES.txt; \
    else \
      sed "s|@RELEASE_VERSION@|${RELEASE_VERSION}|g" /tmp/pineforge-SOURCES.txt.in > /usr/share/doc/pineforge/SOURCES.txt; \
    fi; \
    rm /tmp/pineforge-SOURCES.txt.in
# --- sources notice (end)
COPY --from=hpo-toolchain /opt/pineforge/licenses /opt/pineforge/licenses
COPY --from=hpo-wheel /opt/hpo              /opt/pineforge/hpo
COPY --from=hpo-wheel /opt/hpo-licenses     /opt/pineforge/licenses
COPY --from=hpo-native /tmp/hpo-build/bin/pineforge-hpo-native /opt/pineforge/pineforge-hpo-native
COPY --from=hpo-native /opt/hpo-native-licenses /opt/pineforge/licenses/pineforge-hpo/THIRD_PARTY_LICENSES
# Hub-owned additions live outside the engine's eight-file bin/ directory.
COPY docker/entrypoint-dispatch.sh /opt/pineforge/entrypoint-dispatch.sh
COPY docker/LICENSE-SHA256SUMS /opt/pineforge/licenses/LICENSE-SHA256SUMS
RUN chmod +x /opt/pineforge/entrypoint-dispatch.sh /opt/pineforge/pineforge-hpo-native

# An incomplete manifest must never validate a subset of the copied licences.
RUN set -eu; \
    cd /opt/pineforge/licenses; \
    if grep -q '^# TO BE FILLED BY THE BOX RUN:' LICENSE-SHA256SUMS; then \
      echo "ERROR: LICENSE-SHA256SUMS still needs approved box-run digests" >&2; exit 1; \
    fi; \
    listed="$(sed -n 's/^[0-9a-f]\{64\}  //p' LICENSE-SHA256SUMS | LC_ALL=C sort)"; \
    present="$(find . -type f ! -name LICENSE-SHA256SUMS -printf '%P\n' | LC_ALL=C sort)"; \
    if [ -z "$listed" ] || [ "$listed" != "$present" ]; then \
      echo "ERROR: LICENSE-SHA256SUMS must list every copied licence exactly once" >&2; exit 1; \
    fi; \
    sha256sum -c --strict LICENSE-SHA256SUMS; \
    echo "licences: every copied file matches LICENSE-SHA256SUMS"

ENV PYTHONPATH=/opt/pineforge/pycodegen:/opt/pineforge/hpo \
    PINEFORGE_HPO_VERSION=${HPO_VERSION} \
    PINEFORGE_ENGINE_ROOT=/opt/pineforge \
    PINEFORGE_HPO_NATIVE=/opt/pineforge/pineforge-hpo-native

# HPO's default artifact cache is ~/.cache/pineforge/hpo; only that new
# directory needs to be writable by the existing unprivileged image user.
RUN install -d -o 10001 /home/pineforge/.cache/pineforge/hpo
USER 10001

ENTRYPOINT ["/opt/pineforge/entrypoint-dispatch.sh"]

# The last stage is the default build: the original runtime, with no HPO layers.
FROM release AS default
