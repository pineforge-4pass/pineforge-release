#!/usr/bin/env python3
"""Child smoke regressions that execute the actual inventory checker on fixtures.

The existing smoke fake supplies pulls, labels, backtest and HPO transport.
These tests do not execute a real container or prove a real image build.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import release_contract as contract
import release_smoke as smoke
import test_release_runtime_smoke as existing_smoke
from test_release_binding import candidate, fixture_manifest
from test_release_parity_wire import alternate_identity, canonical, digest, wire_case


PACKAGE_BYTES = {
    "codegen": {"pineforge_codegen/__init__.py": b"# codegen runtime fixture\n"},
    "hpo": {"pineforge_hpo/__init__.py": b"# hpo runtime fixture\n",
            "pineforge_hpo/hpo_failure_codes.json": b'{"fixture":"failure codes"}\n'},
}
IMAGE_ROOTS = {
    "codegen": "/opt/pineforge/pycodegen",
    "hpo": "/opt/pineforge/hpo",
    "parity": "/opt/pineforge/parity",
}


def runtime_case(root):
    manifest, built, identity, inventory = wire_case(root)
    files = {**copy.deepcopy(PACKAGE_BYTES),
             "parity": {path.name: path.read_bytes() for path in root.iterdir()}}
    for role, entries in PACKAGE_BYTES.items():
        built["inventories"][role] = {name: digest(raw) for name, raw in entries.items()}
    children = {arch: copy.deepcopy(files) for arch in ("amd64", "arm64")}
    return manifest, built, identity, inventory, children


class ParityRuntimeFake:
    def __init__(self, root, manifest, children):
        self.manifest = manifest
        self.legacy = existing_smoke.RuntimeSmokeTests()
        self.legacy.setUp()
        self.legacy.m = manifest
        self.calls = self.legacy.calls
        self.paths = {}
        self.missing_output_arch = None
        self.reported_identity_arch = None
        self.reported_inventory_arch = None
        self.inputs = {}
        for arch, roles in children.items():
            self.paths[arch] = {}
            for role, files in roles.items():
                directory = root / arch / role
                self.paths[arch][role] = directory
                if files is None:
                    continue
                directory.mkdir(parents=True)
                for name, raw in files.items():
                    path = directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(raw, tuple):
                        kind, value = raw
                        if kind == "directory":
                            path.mkdir()
                        elif kind == "symlink":
                            target = directory.parent / (role + "-symlink-target")
                            target.write_bytes(value)
                            path.symlink_to(target)
                        else:
                            raise AssertionError("unknown fixture entry kind")
                    else:
                        path.write_bytes(raw)

    def command(self, *args, input=None):
        if args[:3] == ("docker", "image", "inspect"):
            result = json.loads(self.legacy.command(*args, input=input))
            labels = result[0]["Config"]["Labels"]
            versions = self.manifest["versions"]
            labels.update({
                "org.opencontainers.image.version": "v" + self.manifest["release_version"],
                "io.pineforge.engine.version": versions["engine"]["version"],
                "io.pineforge.codegen.version": versions["codegen"]["version"],
                "io.pineforge.hpo.version": versions["hpo"]["package_version"],
            })
            return canonical(result)
        if "--entrypoint" in args and args[args.index("--entrypoint") + 1] == "python3":
            self.calls.append(args)
            arch = args[args.index("--platform") + 1].split("/", 1)[1]
            ref = contract.IMAGE + "@" + self.manifest["image"][arch + "_digest"]
            if ref not in args or args[-2] != "-c":
                raise AssertionError("inventory command is not bound to the expected child")
            script = args[-1]
            self.inputs[arch] = json.loads(input)
            for role, image_root in IMAGE_ROOTS.items():
                script = script.replace(image_root, str(self.paths[arch][role]))
            done = subprocess.run(
                [sys.executable, "-c", script], input=input, capture_output=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if done.returncode:
                raise AssertionError(done.stderr.decode())
            if self.missing_output_arch == arch:
                return b""  # Deliberate dropped-proof transport fault.
            observed = json.loads(done.stdout)
            if "parity" in observed:
                if self.reported_identity_arch == arch:
                    observed["parity"]["identity"] = alternate_identity(
                        self.manifest["versions"]["engine"]["parity"])
                if self.reported_inventory_arch == arch:
                    observed["parity"]["inventory"]["run_strategy.py"] = digest(b"wrong returned inventory\n")
            return canonical(observed)
        return self.legacy.command(*args, input=input)


class ParityRuntimeTests(unittest.TestCase):
    def verify(self, fake, manifest, built):
        with patch.object(smoke, "command", side_effect=fake.command):
            return smoke.verify_runtime(
                built["image"], built["versions"], manifest["hub_commit"],
                manifest["release_version"], built["inventories"])

    def assert_control(self, root, manifest, built, identity, inventory, children):
        fake = ParityRuntimeFake(root, manifest, children)
        proof = self.verify(fake, manifest, built)
        self.assertEqual(set(proof), {"amd64", "arm64"})
        for arch in ("amd64", "arm64"):
            self.assertEqual(proof[arch]["parity"], {
                "image_digest": built["image"][arch + "_digest"],
                "identity": identity, "inventory": inventory,
            })
            self.assertEqual(fake.inputs[arch]["inventories"], built["inventories"])
            self.assertEqual(fake.inputs[arch]["parity"], identity)
            ref = contract.IMAGE + "@" + built["image"][arch + "_digest"]
            checks = [index for index, args in enumerate(fake.calls)
                      if ref in args and "--entrypoint" in args
                      and args[args.index("--entrypoint") + 1] == "python3"]
            runs = [index for index, args in enumerate(fake.calls)
                    if ref in args and args[:2] == ("docker", "run") and "--entrypoint" not in args]
            self.assertEqual(len(checks), 1)
            self.assertTrue(runs)
            self.assertLess(checks[0], min(runs))
            for args in fake.calls:
                if ref in args and args[:2] == ("docker", "run"):
                    self.assertIn("--network=none", args)
                    self.assertIn("PYTHONDONTWRITEBYTECODE=1", args)
        inventory_checks = [index for index, args in enumerate(fake.calls)
                            if "--entrypoint" in args
                            and args[args.index("--entrypoint") + 1] == "python3"]
        smoke_commands = [index for index, args in enumerate(fake.calls)
                          if args[:2] == ("docker", "run") and "--entrypoint" not in args]
        self.assertLess(max(inventory_checks), min(smoke_commands))
        completed = copy.deepcopy(built)
        completed["verification"] = proof
        self.assertEqual(contract.validate_candidate(completed), completed)
        return fake

    def test_both_children_compare_real_fixture_bytes_to_producer_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            manifest, built, identity, inventory, children = runtime_case(base / "source")
            self.assert_control(base / "positive", manifest, built, identity, inventory, children)

    def test_legacy_runtime_needs_no_parity_root_or_child_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = fixture_manifest("1.10.0")
            built = candidate(manifest)
            built["inventories"] = {
                role: {name: digest(raw) for name, raw in files.items()}
                for role, files in PACKAGE_BYTES.items()
            }
            children = {arch: {**copy.deepcopy(PACKAGE_BYTES), "parity": None}
                        for arch in ("amd64", "arm64")}
            fake = ParityRuntimeFake(Path(temporary), manifest, children)
            proof = self.verify(fake, manifest, built)
            for arch in ("amd64", "arm64"):
                self.assertFalse(fake.paths[arch]["parity"].exists())
                self.assertIsNone(fake.inputs[arch]["parity"])
                self.assertNotIn("parity", fake.inputs[arch]["inventories"])
                self.assertEqual(set(proof[arch]), {
                    "backtest_sha256", "trades", "hpo_sha256", "hpo_trials"})
                self.assertGreater(proof[arch]["trades"], 0)

    def test_unknown_inventory_roles_refuse_before_any_pull(self):
        for role in ("engine", "unknown"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                manifest, built, identity, inventory, children = runtime_case(base / "source")
                self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                built["inventories"][role] = {"unexpected.py": digest(b"unknown role\n")}
                fake = ParityRuntimeFake(base / "negative", manifest, children)
                with self.assertRaisesRegex(contract.Invalid, "^unknown runtime inventory role$"):
                    self.verify(fake, manifest, built)
                self.assertEqual(fake.calls, [])

    def test_installed_faults_refuse_on_either_child_before_that_child_smokes(self):
        rows = (
            ("missing_root", "parity root is missing"),
            ("missing_file", "parity installed file inventory differs"),
            ("extra_file", "parity installed file inventory differs"),
            ("directory", "parity installed file inventory differs"),
            ("symlink", "parity installed file inventory differs"),
            ("descriptor_bytes", "parity descriptor identity differs"),
            ("payload_bytes", "parity payload hash differs"),
            ("empty_payload", "parity payload hash differs"),
            ("sums_bytes", "parity SUMS verification failed"),
            ("duplicate_sums", "parity SUMS verification failed"),
        )
        for arch in ("amd64", "arm64"):
            for fault, message in rows:
                with self.subTest(arch=arch, fault=fault), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    manifest, built, identity, inventory, children = runtime_case(base / "source")
                    self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                    damaged = copy.deepcopy(children)
                    files = damaged[arch]["parity"]
                    if fault == "missing_root":
                        damaged[arch]["parity"] = None
                    elif fault == "missing_file":
                        del files["run_strategy.py"]
                    elif fault == "extra_file":
                        files[".unexpected"] = b"extra\n"
                    elif fault in ("directory", "symlink"):
                        files["run_strategy.py"] = (fault, files["run_strategy.py"])
                    elif fault == "descriptor_bytes":
                        files["PARITY-MANIFEST.json"] += b"\n"
                    elif fault == "payload_bytes":
                        files["run_strategy.py"] += b"changed bytes\n"
                    elif fault == "empty_payload":
                        files["run_strategy.py"] = b""
                    elif fault == "sums_bytes":
                        files["PARITY-SHA256SUMS"] += b"\n"
                    else:
                        files["PARITY-SHA256SUMS"] += files["PARITY-SHA256SUMS"].splitlines(keepends=True)[-1]
                    fake = ParityRuntimeFake(base / "negative", manifest, damaged)
                    with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
                        self.verify(fake, manifest, built)
                    self.assertFalse(any(args[:2] == ("docker", "run")
                                         and "--entrypoint" not in args for args in fake.calls))

    def test_empty_child_proof_is_a_refusal_on_each_architecture(self):
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                manifest, built, identity, inventory, children = runtime_case(base / "source")
                self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                fake = ParityRuntimeFake(base / "negative", manifest, children)
                fake.missing_output_arch = arch
                with self.assertRaisesRegex(contract.Invalid, "^parity child proof missing$"):
                    self.verify(fake, manifest, built)

    def test_returned_identity_must_match_binding_on_each_child(self):
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                manifest, built, identity, inventory, children = runtime_case(base / "source")
                self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                fake = ParityRuntimeFake(base / "negative", manifest, children)
                fake.reported_identity_arch = arch
                with self.assertRaisesRegex(contract.Invalid, "^parity descriptor identity differs$"):
                    self.verify(fake, manifest, built)

    def test_returned_inventory_must_match_binding_on_each_child(self):
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                manifest, built, identity, inventory, children = runtime_case(base / "source")
                self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                fake = ParityRuntimeFake(base / "negative", manifest, children)
                fake.reported_inventory_arch = arch
                with self.assertRaisesRegex(contract.Invalid, "^parity installed file inventory differs$"):
                    self.verify(fake, manifest, built)

    def test_missing_duplicate_or_index_child_digest_cannot_authorize_runtime(self):
        for fault, message in (
                ("missing", "parity child proof missing"),
                ("duplicate", "parity child digest not verified"),
                ("index", "parity child digest not verified"),
                ("missing_index", "parity child digest not verified"),
                ("malformed", "parity child digest not verified")):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                manifest, built, identity, inventory, children = runtime_case(base / "source")
                self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                if fault == "missing":
                    del built["image"]["arm64_digest"]
                elif fault == "duplicate":
                    built["image"]["arm64_digest"] = built["image"]["amd64_digest"]
                elif fault == "index":
                    built["image"]["arm64_digest"] = built["image"]["digest"]
                elif fault == "missing_index":
                    del built["image"]["digest"]
                else:
                    built["image"]["arm64_digest"] = "invalid-child"
                fake = ParityRuntimeFake(base / "negative", manifest, children)
                with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
                    self.verify(fake, manifest, built)
                self.assertEqual(fake.calls, [])

    def test_candidate_requires_matching_proof_for_each_child(self):
        for arch in ("amd64", "arm64"):
            for fault, message in (
                    ("missing_arch", "parity child proof missing"),
                    ("null_record", "parity child proof missing"),
                    ("missing_proof", "parity child proof missing"),
                    ("wrong_digest", "parity child digest not verified"),
                    ("wrong_identity", "parity descriptor identity differs"),
                    ("wrong_inventory", "parity installed file inventory differs")):
                with self.subTest(arch=arch, fault=fault), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    manifest, built, identity, inventory, children = runtime_case(base / "source")
                    self.assert_control(base / "positive", manifest, built, identity, inventory, children)
                    if fault == "missing_arch":
                        del built["verification"][arch]
                    elif fault == "null_record":
                        built["verification"][arch] = None
                    elif fault == "missing_proof":
                        del built["verification"][arch]["parity"]
                    elif fault == "wrong_digest":
                        other = "arm64" if arch == "amd64" else "amd64"
                        built["verification"][arch]["parity"]["image_digest"] = built["image"][other + "_digest"]
                    elif fault == "wrong_identity":
                        built["verification"][arch]["parity"]["identity"] = alternate_identity(identity)
                    else:
                        built["verification"][arch]["parity"]["inventory"]["run_strategy.py"] = digest(b"wrong proof inventory\n")
                    with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
                        contract.validate_candidate(built)

    def test_workflow_uses_pushed_index_and_preserves_verified_child_proofs(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        self.assertIn("DIGEST: ${{ steps.push_release.outputs.digest || steps.push_hpo.outputs.digest }}", workflow)
        self.assertIn('python3 scripts/release_smoke.py --bindings "$RUNNER_TEMP/release-bindings.json"', workflow)
        self.assertIn('--digest "$DIGEST" --output "$RUNNER_TEMP/release-verified.json"', workflow)
        self.assertIn('candidate --file "$RUNNER_TEMP/release-verified.json"', workflow)


if __name__ == "__main__":
    unittest.main()
