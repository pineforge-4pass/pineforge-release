#!/usr/bin/env bash
# Sync the engine's runtime harness into docker/ from the pineforge-engine tag that this release
# pins, and record the sha256 of every synced file in docker/HARNESS-SHA256SUMS (the image build
# checks the installed files against it).
# usage: scripts/sync-harness.sh <engine-version e.g. 1.5.0>
#        scripts/sync-harness.sh --files    print the files a sync writes, one per line (no network)
#
# The files are fetched from the tag v<engine-version>. PF_ENGINE_REF names another ref of the
# engine (a branch, a tag or a commit) and exists ONLY for a proof before the tag exists; the
# release workflows never set it, so a release always syncs from its pinned tag.
set -euo pipefail

# The ONE list of the harness files, sorted: the entrypoint and every file it runs or imports.
# The Dockerfile installs exactly these; tests/test_release_workflows.py holds the two together.
FILES=(
  bind_compiled_inventory.py
  entrypoint.sh
  request_feed_inventory.py
  run_execution_observer.py
  run_json.py
  run_phase_transport.py
  selected_window_plan.py
  selected_window_report.py
)
SUMS=HARNESS-SHA256SUMS

if [ "${1:-}" = "--files" ]; then
  printf 'docker/%s\n' "${FILES[@]}" "${SUMS}"
  exit 0
fi

E="${1:?engine version}"; E="${E#v}"
REF="${PF_ENGINE_REF:-v${E}}"
BASE="https://raw.githubusercontent.com/pineforge-4pass/pineforge-engine/${REF}/docker"

# The sums file is written in the order of the list, so the list must be sorted and without repeats.
[ "$(printf '%s\n' "${FILES[@]}")" = "$(printf '%s\n' "${FILES[@]}" | LC_ALL=C sort -u)" ] \
  || { echo "ERROR: FILES must be sorted and without repeats" >&2; exit 1; }

for f in "${FILES[@]}"; do
  curl -fsSL "${BASE}/${f}" -o "docker/${f}"
  [ -s "docker/${f}" ] || { echo "ERROR: docker/${f} came back empty from pineforge-engine ${REF}" >&2; exit 1; }
done

if command -v sha256sum >/dev/null 2>&1; then sha256=(sha256sum); else sha256=(shasum -a 256); fi
( cd docker && "${sha256[@]}" -- "${FILES[@]}" ) > "docker/${SUMS}"

echo "synced ${#FILES[@]} harness files from pineforge-engine ${REF}:"
printf '  docker/%s\n' "${FILES[@]}"
echo "sha256 of each, written to docker/${SUMS}:"
sed 's/^/  /' "docker/${SUMS}"
abi="$(sed -n 's/^EXPECTED_PF_ABI = \([0-9][0-9]*\).*/\1/p' docker/run_json.py)"
echo "docker/run_json.py synced from pineforge-engine ${REF} (EXPECTED_PF_ABI=${abi})"
