#!/usr/bin/env python3
"""Frozen sink vectors and converter checks for an already built image.

Run on a build host with PINEFORGE_SINK_TEST_IMAGE set to the image to test.
PINEFORGE_SINK_TEST_PLATFORM optionally selects linux/amd64 or linux/arm64.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sink-vectors"
UPSTREAM_FILES = (
    "native.expected.parse.json",
    "native.pfsink",
    "native.provenance.json",
    "valid.expected.to-json.json",
    "valid.pfsink",
)


class SinkVectorFixtureTest(unittest.TestCase):
    def test_frozen_bytes_match_the_upstream_manifest(self):
        manifest = json.loads((FIXTURES / "MANIFEST.json").read_bytes())
        self.assertEqual(manifest["wire_version"], "pf-report-sink/v1")
        for name in UPSTREAM_FILES:
            with self.subTest(file=name):
                payload = (FIXTURES / name).read_bytes()
                recorded = manifest["files"][name]
                self.assertEqual(len(payload), recorded["bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), recorded["sha256"])
                if name.startswith("native."):
                    self.assertIs(recorded["captured_native"], True)
        self.assertEqual((FIXTURES / "stdout.json").read_bytes(), b"{}\n")

    def test_native_expectation_describes_the_captured_writer(self):
        expected = json.loads((FIXTURES / "native.expected.parse.json").read_bytes())
        provenance = json.loads((FIXTURES / "native.provenance.json").read_bytes())
        self.assertIs(provenance["captured_native"], True)
        self.assertIs(expected["complete"], True)
        self.assertEqual(provenance["header"], {
            name: expected["header"][name] for name in ("engine", "run_id")
        })
        self.assertEqual(provenance["record_kinds"], ["E", "T"])
        for tag in ("E", "T"):
            self.assertGreater(expected["counts"][tag], 0)
            self.assertEqual(sum(row["tag"] == tag for row in expected["records"]),
                             expected["counts"][tag])


class SinkConverterImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ.get("PINEFORGE_SINK_TEST_IMAGE")
        if not cls.image:
            raise unittest.SkipTest("set PINEFORGE_SINK_TEST_IMAGE on a build host to run the image checks")
        if shutil.which("docker") is None:
            raise AssertionError("PINEFORGE_SINK_TEST_IMAGE is set but docker is unavailable")

    def _convert(self, name):
        command = [
            "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
        ]
        platform = os.environ.get("PINEFORGE_SINK_TEST_PLATFORM")
        if platform:
            command.extend(("--platform", platform))
        command.extend((
            "--mount", f"type=bind,src={FIXTURES},dst=/vectors,readonly",
            "--entrypoint", "python3", self.image,
            "-I", "-B", "-S", "/opt/pineforge/bin/pineforge_sink_to_json.py",
            "--in", "/vectors/" + name, "--stdout-json", "/vectors/stdout.json",
            "--out", "-", "--max-bytes", str((FIXTURES / name).stat().st_size),
        ))
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        diagnostics = [json.loads(line) for line in result.stderr.splitlines()]
        self.assertTrue(diagnostics, "converter must report verified completion on stderr")
        completion = diagnostics[-1]
        self.assertEqual(completion["event"], "converter_complete")
        self.assertEqual(completion["exit_code"], 0)
        self.assertIs(completion["sink_verified"], True)
        return result.stdout

    def test_valid_converter_stdout_matches_frozen_engine_bytes(self):
        expected = (FIXTURES / "valid.expected.to-json.json").read_bytes()
        self.assertEqual(self._convert("valid.pfsink"), expected)

    def test_native_writer_equity_and_trades_match_frozen_rows(self):
        expected = json.loads((FIXTURES / "native.expected.parse.json").read_bytes())
        oracle = json.loads((FIXTURES / "valid.expected.to-json.json").read_bytes())
        # The frozen report defines the public trade fields; the captured parse
        # supplies their values independently of the installed converter.
        trade_fields = tuple(oracle["trades"][0])
        rows = {"E": [], "T": []}
        for record in expected["records"]:
            rows[record["tag"]].append(dict(zip(record["fields"], record["values"])))
        trades = [{name: row[name] for name in trade_fields} for row in rows["T"]]
        report = json.loads(self._convert("native.pfsink"))
        self.assertEqual(report["engine"], "pineforge")
        self.assertEqual(report["equity_curve"], rows["E"])
        self.assertEqual(report["trades"], trades)
        self.assertEqual(report["summary"]["total_trades"], expected["counts"]["T"])
        self.assertEqual(report["metrics"]["all"]["num_trades"], expected["counts"]["T"])


if __name__ == "__main__":
    unittest.main()
