#!/usr/bin/env python3
"""Current release-chain replay; external services are nonempty fixtures only."""
from __future__ import annotations

import argparse
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", default="", help="select test names containing this text")
    parser.add_argument("-v", action="store_true")
    parser.add_argument("--out", help="optional transcript directory")
    parser.add_argument("--checkout", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args(argv)
    root = Path(args.checkout).resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "tests"))
    sys.path.insert(0, str(root / "scripts"))
    loader = unittest.TestLoader()
    if args.k:
        loader.testNamePatterns = ["*" + args.k + "*"]
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in
                               ("test_release_binding", "test_release_assets", "test_release_receipts",
                                "test_release_binding_workflows", "test_release_runtime_smoke"))
    if not suite.countTestCases():
        parser.error("no replay cases selected")
    stream = sys.stderr
    if args.out:
        output = Path(args.out)
        output.mkdir(parents=True, exist_ok=True)
        stream = (output / "ready-replay.txt").open("w")
    # Any accidental real networking is a replay failure. No production token,
    # target name or infrastructure credential reaches a fixture shell step.
    def no_network(*args, **kwargs):
        raise AssertionError("real networking forbidden in ready replay")
    with patch.object(socket, "create_connection", side_effect=no_network):
        result = unittest.TextTestRunner(stream=stream, verbosity=2 if args.v else 1).run(suite)
    if stream is not sys.stderr:
        stream.close()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
