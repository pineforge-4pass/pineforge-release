#!/usr/bin/env python3
"""Dockerfile text checks and replay of its actual parity gate.

Remote checks only. Shell replay is not proof of a real image build; the
Dockerfile and publish workflow integration are owned by the shared-file lane.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import test_release_hpo_image as image_tests
from test_parity_source_proof import ROWS, write_bundle
from test_release_parity_wire import digest, fixture_identity


class ParityImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
        cls.stages = image_tests._dockerfile_stages(cls.dockerfile)
        cls.release = next(stage for stage in cls.stages if stage.name == "release")
        cls.check = image_tests._run_block(
            cls.release.text, "sha256sum -c --strict PARITY-SHA256SUMS")

    def fixture_files(self, root):
        write_bundle(root)
        identity, inventory = fixture_identity(root)
        return {path.name: path.read_bytes() for path in root.iterdir()}, identity, inventory

    def replay(self, root, binding, *, carrier=1):
        script = self.check[len("RUN "):].replace("\\\n", "")
        self.assertNotIn("\n", script, "replay must use the complete logical RUN")
        self.assertIn("/opt/pineforge/parity", script)
        script = script.replace("/opt/pineforge/parity", shlex.quote(str(root)))
        environment = dict(os.environ)
        environment.pop("PARITY_DESCRIPTOR_SHA256", None)
        environment["PARITY_CARRIER"] = str(carrier)
        if binding is not None:
            environment["PARITY_DESCRIPTOR_SHA256"] = binding
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            cwd=root.parent, env=environment,
            input="", capture_output=True, text=True,
        )

    def layout(self, root, files, *, omit=(), directory=None, symlink=None):
        root.mkdir()
        for name, raw in files.items():
            if name not in omit and name not in (directory, symlink):
                (root / name).write_bytes(raw)
        if directory is not None:
            (root / directory).mkdir()
        if symlink is not None:
            target = root.parent / (root.name + "-symlink-target")
            target.write_bytes(files[symlink])
            (root / symlink).symlink_to(target)
        return root

    def assert_control(self, root, files, identity):
        installed = self.layout(root, files)
        done = self.replay(installed, identity["descriptor_sha256"])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        names = ["PARITY-MANIFEST.json", *sorted(row["destination"] for row in ROWS)]
        self.assertEqual(done.stdout.splitlines(),
                         [name + ": OK" for name in names]
                         + ["parity: seven files match PARITY-SHA256SUMS"])
        self.assertEqual({p.name: p.read_bytes() for p in installed.iterdir()}, files)

    def assert_refuses(self, root, binding, message):
        done = self.replay(root, binding)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(message, done.stderr.splitlines())
        self.assertNotIn("parity: seven files match PARITY-SHA256SUMS", done.stdout)

    def test_release_stage_carries_parity_and_retains_harness_and_abi_checks(self):
        copies = [body for op, body in self.release.instructions if op == "COPY"
                  and "/opt/pineforge/parity" in body]
        self.assertEqual(copies, [])
        self.assertIn(("ARG", "PARITY_DESCRIPTOR_SHA256"), self.release.instructions)
        self.assertIn(("ARG", "PARITY_CARRIER=0"), self.release.instructions)
        self.assertIn(
            "RUN --mount=type=bind,source=.,target=/tmp/pineforge-context,ro",
            self.release.text)
        self.assertIn('if [ "${PARITY_CARRIER:-0}" = 1 ]', self.release.text)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.release.text)
        expected = " ".join(["PARITY-MANIFEST.json", *sorted(row["destination"] for row in ROWS)])
        self.assertIn('want="' + expected + '";', self.check)
        self.assertIn("sha256sum -c --strict PARITY-SHA256SUMS", self.check)
        self.assertNotIn("|| true", self.check)
        self.assertIn("PARITY_DESCRIPTOR_SHA256", self.check)
        self.assertEqual(
            image_tests._run_block(self.release.text, "sha256sum -c --strict HARNESS-SHA256SUMS"),
            image_tests.DockerfileHarnessTest.CHECK,
        )
        self.assertIn("PF_ABI_VERSION", image_tests._run_block(self.release.text, 'echo "harness ABI'))
        for target in ("hpo", None):
            self.assertIn("release", image_tests._reachable_stages(self.stages, target)
                          if target else image_tests._reachable_stages(self.stages))

    def test_below_cutoff_without_carrier_skips_gate_and_keeps_context_optional(self):
        self.assertNotIn("COPY docker/parity/", self.release.text)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            done = self.replay(base / "absent-parity", None, carrier=0)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_complete_fixture_passes_the_actual_run_instruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            files, identity, _ = self.fixture_files(base / "source")
            self.assert_control(base / "installed", files, identity)

    def test_missing_extra_directory_symlink_and_empty_entries_refuse(self):
        rows = (
            ("missing", "parity directory is missing required file"),
            ("extra", "parity directory contains unexpected files"),
            ("hidden_extra", "parity directory contains unexpected files"),
            ("directory", "parity directory is missing required file"),
            ("symlink", "parity directory contains a symlink"),
            ("empty", "parity payload is empty"),
            ("missing_sums", "parity directory is missing required file"),
            ("sums_directory", "parity directory is missing required file"),
            ("sums_symlink", "parity directory contains a symlink"),
            ("empty_sums", "parity payload is empty"),
        )
        for fault, message in rows:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                files, identity, _ = self.fixture_files(base / "source")
                self.assert_control(base / "positive", files, identity)
                victim = "PARITY-SHA256SUMS" if "sums" in fault else "run_strategy.py"
                kwargs = {}
                if fault in ("missing", "missing_sums"):
                    kwargs["omit"] = (victim,)
                elif fault in ("directory", "sums_directory"):
                    kwargs["directory"] = victim
                elif fault in ("symlink", "sums_symlink"):
                    kwargs["symlink"] = victim
                elif fault in ("empty", "empty_sums"):
                    files[victim] = b""
                else:
                    files[".unapproved" if fault == "hidden_extra" else "unapproved.py"] = b"extra\n"
                root = self.layout(base / "negative", files, **kwargs)
                self.assert_refuses(root, identity["descriptor_sha256"], message)

    def test_duplicate_missing_extra_and_malformed_sums_rows_refuse(self):
        for fault in ("duplicate", "missing", "extra", "malformed"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                files, identity, _ = self.fixture_files(base / "source")
                self.assert_control(base / "positive", files, identity)
                lines = files["PARITY-SHA256SUMS"].splitlines(keepends=True)
                if fault == "duplicate":
                    lines.append(lines[-1])
                elif fault == "missing":
                    lines.pop()
                elif fault == "extra":
                    lines.append((digest(b"unapproved\n") + "  unapproved.py\n").encode())
                else:
                    lines.append(b"this is not a checksum row\n")
                files["PARITY-SHA256SUMS"] = b"".join(lines)
                root = self.layout(base / "negative", files)
                self.assert_refuses(root, identity["descriptor_sha256"], "parity SUMS verification failed")

    def test_changed_payload_bytes_fail_strict_sums_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            files, identity, _ = self.fixture_files(base / "source")
            self.assert_control(base / "positive", files, identity)
            files["run_strategy.py"] += b"changed payload bytes\n"
            root = self.layout(base / "negative", files)
            self.assert_refuses(root, identity["descriptor_sha256"], "parity SUMS verification failed")

    def test_descriptor_binding_mismatch_and_missing_binding_refuse(self):
        for binding in (digest(b"a different descriptor\n"), "", None):
            with self.subTest(binding=binding), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                files, identity, _ = self.fixture_files(base / "source")
                self.assert_control(base / "positive", files, identity)
                root = self.layout(base / "negative", files)
                self.assert_refuses(root, binding, "parity descriptor hash differs from binding")

    def test_every_inspect_and_push_build_uses_the_same_producer_binding(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        blocks = workflow.split("          build-args: |\n")[1:]
        self.assertEqual(len(blocks), 6)
        for block in blocks:
            args = block.split("          cache-from:", 1)[0]
            self.assertIn(
                "PARITY_DESCRIPTOR_SHA256=${{ steps.binding.outputs.parity_descriptor_sha256 }}", args)
            self.assertIn("${{ steps.binding.outputs.parity_carrier }}", args)
        self.assertIn("obj['versions']['engine']['parity']['descriptor_sha256']", workflow)
        self.assertIn("parity_carrier=PARITY_CARRIER=1", workflow)
        self.assertLess(workflow.index("Sync parity from the immutable engine tag"),
                        workflow.index("Bind producer source and package bytes"))


if __name__ == "__main__":
    unittest.main()
