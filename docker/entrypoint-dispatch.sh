#!/bin/sh
set -eu
if [ "${1-}" = hpo ]; then
    shift
    exec python3 -m pineforge_hpo.cli "$@"
fi
exec /opt/pineforge/bin/entrypoint.sh "$@"
