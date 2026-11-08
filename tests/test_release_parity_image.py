#!/usr/bin/env python3
"""Dockerfile text checks and replay of its actual parity copy and gate.

Remote checks only. Shell replay is not proof of a real image build or of
access by the image's default user.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import test_release_hpo_image as image_tests
from parity_bundle import sync_parity_bundle
from test_parity_source_proof import ROWS, TaggedEngineFixture, write_bundle
from test_release_parity_wire import digest, fixture_identity


class ParityImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
        cls.stages = image_tests._dockerfile_stages(cls.dockerfile)
        cls.release = next(stage for stage in cls.stages if stage.name == "release")
        mount = "--mount=type=bind,source=.,target=/tmp/pineforge-context,ro "
        copies = [body for op, body in cls.release.instructions
                  if op == "RUN" and body.startswith(mount)]
        if len(copies) != 1:
            raise ValueError("expected exactly one parity context RUN")
        # The parser joins Dockerfile continuations; only the BuildKit mount
        # option is removed to execute the complete shell body in bash.
        cls.copy = copies[0][len(mount):]
        cls.check = image_tests._run_block(
            cls.release.text, "sha256sum -c --strict PARITY-SHA256SUMS")

    def fixture_files(self, root):
        write_bundle(root)
        identity, inventory = fixture_identity(root)
        return {path.name: path.read_bytes() for path in root.iterdir()}, identity, inventory

    def replay(self, root, binding, *, carrier=1, source=None):
        script = (self.check[len("RUN "):].replace("\\\n", "")
                  if source is None else self.copy)
        self.assertNotIn("\n", script, "replay must use the complete logical RUN")
        if source is not None:
            self.assertIn("/tmp/pineforge-context/docker/parity", script)
            script = script.replace(
                "/tmp/pineforge-context/docker/parity", shlex.quote(str(source)))
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

    def assert_copy_control(self, base, client):
        base.mkdir()
        source = base / "source"
        sync_parity_bundle(client, "v" + str(client.v), source)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700,
                         "use the real renamed TemporaryDirectory stage")
        files = {path.name: path.read_bytes() for path in source.iterdir()}
        self.assertEqual(set(files), {"PARITY-MANIFEST.json", "PARITY-SHA256SUMS",
                                     *(row["destination"] for row in ROWS)})
        # Vary file metadata independently of bytes. Executable source files
        # still install as readable data for the image's inventory consumer.
        old_time = 946684800
        for path in source.iterdir():
            path.chmod(0o700 if path.suffix == ".py" else 0o600)
            os.utime(path, (old_time, old_time))
        os.utime(source, (old_time, old_time))
        installed = base / "installed"
        installed.mkdir()
        installed.chmod(0o755)
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
        binding = digest(files["PARITY-MANIFEST.json"])
        done = self.replay(installed, binding, source=source)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in installed.iterdir()}, files,
                         "the first RUN must copy the supplied source bytes")
        for path in (installed, *installed.iterdir()):
            with self.subTest(installed=path.name):
                info = path.stat()
                expected = 0o755 if path == installed else 0o644
                self.assertEqual(stat.S_IMODE(info.st_mode), expected)
                self.assertEqual(info.st_mode & 0o007, 0o005 if path == installed else 0o004,
                                 "other users need directory read/search and file read bits")
                # The replay creates files as its own user. In the real
                # Dockerfile this RUN precedes USER 10001 and creates them as root.
                self.assertEqual((info.st_uid, info.st_gid), (os.geteuid(), os.getegid()))
                self.assertNotEqual(info.st_mtime, old_time,
                                    "staging timestamps must not reach the image")
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o700)
        done = self.replay(installed, binding)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("parity: seven files match PARITY-SHA256SUMS", done.stdout.splitlines())
        return files

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
        self.assertLess(self.release.text.index("RUN --mount="),
                        self.release.text.index("\nUSER 10001"))
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
            done = self.replay(base / "absent-parity", None, carrier=0,
                               source=base / "absent-context")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertFalse((base / "absent-parity").exists())

    def test_first_run_normalizes_private_stage_metadata_and_copies_changed_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = self.assert_copy_control(base / "positive", TaggedEngineFixture())
            client = TaggedEngineFixture()
            row = ROWS[0]
            changed = original[row["destination"]] + b"changed tagged payload\n"
            client.mutate_source_bytes(row["source"], changed)
            updated = self.assert_copy_control(base / "changed", client)
            self.assertEqual(updated[row["destination"]], changed)
            self.assertNotEqual(updated["PARITY-MANIFEST.json"], original["PARITY-MANIFEST.json"])
            self.assertNotEqual(updated["PARITY-SHA256SUMS"], original["PARITY-SHA256SUMS"])

    def test_first_run_refuses_missing_and_symlinked_source_directories(self):
        for fault, message in (
                ("missing", "parity directory is missing required file"),
                ("symlink", "parity directory contains a symlink")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                self.assert_copy_control(base / "positive", TaggedEngineFixture())
                source = base / "missing-or-symlink"
                if fault == "symlink":
                    source.symlink_to(base / "positive/source", target_is_directory=True)
                installed = base / "negative"
                installed.mkdir()
                installed.chmod(0o755)
                done = self.replay(installed, None, source=source)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(done.stderr.splitlines(), [message])
                self.assertEqual(list(installed.iterdir()), [])

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
