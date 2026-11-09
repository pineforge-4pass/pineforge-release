#!/usr/bin/env python3
"""Sync the reviewed engine parity bundle at publish time; release mode uses its tag."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from parity_bundle import sync_parity_bundle
from release_artifacts import Client
from release_contract import Invalid, Pending, canonical
import release_pair as pair


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("engine_tag", help="engine version or v-prefixed tag")
    parser.add_argument("--destination", type=Path, default=Path("docker/parity"))
    parser.add_argument("--proof-only", action="store_true",
                        help="allow a full proof commit before the engine tag exists")
    parser.add_argument("--proof-commit", help="full engine commit; refused unless --proof-only")
    args = parser.parse_args(argv)
    try:
        identity = sync_parity_bundle(
            Client(), "v" + str(pair.parse(args.engine_tag)), args.destination,
            release_mode=not args.proof_only, proof_commit=args.proof_commit,
        )
    except (Invalid, Pending, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(identity))
    return 0


if __name__ == "__main__":
    sys.exit(main())
