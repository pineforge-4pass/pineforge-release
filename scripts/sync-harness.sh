#!/usr/bin/env bash
# Sync docker/run_json.py from the pineforge-engine tag that this release pins.
# usage: scripts/sync-harness.sh <engine-version e.g. 0.13.1>
set -euo pipefail
E="${1:?engine version}"; E="${E#v}"
curl -fsSL "https://raw.githubusercontent.com/pineforge-4pass/pineforge-engine/v${E}/docker/run_json.py" -o docker/run_json.py
abi="$(sed -n 's/^EXPECTED_PF_ABI = \([0-9][0-9]*\).*/\1/p' docker/run_json.py)"
echo "docker/run_json.py synced from pineforge-engine v${E} (EXPECTED_PF_ABI=${abi})"
