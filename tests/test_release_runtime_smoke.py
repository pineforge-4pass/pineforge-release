#!/usr/bin/env python3
"""CI/cloud checks for nonempty image smoke results and per-child execution."""
import copy
import hashlib
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
import test_release_workflows as workflow_tests
from test_release_binding import fixture_manifest


class RuntimeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.m = fixture_manifest()
        self.calls = []
        self.trades = [{"entry": 1, "exit": 2}]
        self.hpo = {"ok": True, "trials_completed": 9, "full_parameter_coverage": True}

    def command(self, *args, input=None):
        self.calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            labels = {"org.opencontainers.image.revision": self.m["hub_commit"],
                      "org.opencontainers.image.version": "v1.6.0", "io.pineforge.engine.version": "1.6.0",
                      "io.pineforge.codegen.version": "1.6.0", "io.pineforge.hpo.version": "1.6.0"}
            return contract.canonical([{"Config": {"Labels": labels}}])
        if args[:2] == ("docker", "pull") or "--entrypoint" in args:
            return b""
        if "hpo" in args:
            return contract.canonical(self.hpo)
        return contract.canonical({"engine": "pineforge", "trades": self.trades, "metrics": {"all": {"num_trades": len(self.trades)}}})

    def verify(self):
        return smoke.verify_runtime(self.m["image"], self.m["versions"], self.m["hub_commit"], "1.6.0", {
            "codegen": {"pineforge_codegen/__init__.py": "0" * 64}})

    def test_both_child_digests_run_offline_real_fixture_commands(self):
        with patch.object(smoke, "command", side_effect=self.command):
            proof = self.verify()
        self.assertEqual(set(proof), {"amd64", "arm64"})
        for arch in proof:
            self.assertGreater(proof[arch]["trades"], 0)
            self.assertEqual(proof[arch]["hpo_trials"], 9)
            ref = contract.IMAGE + "@" + self.m["image"][arch + "_digest"]
            self.assertTrue(any(ref in call and "linux/" + arch in call and "hpo" in call for call in self.calls))
            helper_calls = [call for call in self.calls
                            if ref in call and "--entrypoint" in call
                            and call[call.index("--entrypoint") + 1] == "/usr/bin/python3"]
            self.assertEqual(len(helper_calls), 1, "each child must run the helper runtime check")
            self.assertEqual(helper_calls[0][-3:-1], ("-I", "-c"))
            self.assertEqual(helper_calls[0][-1], smoke.selected_admit_runtime_script())
            harness_check = next(index for index, call in enumerate(self.calls)
                                 if ref in call and "--entrypoint" in call
                                 and call[call.index("--entrypoint") + 1] == "bash")
            self.assertLess(harness_check, self.calls.index(helper_calls[0]))
            backtest = next(index for index, call in enumerate(self.calls)
                            if ref in call and call[:2] == ("docker", "run") and "--entrypoint" not in call)
            self.assertLess(self.calls.index(helper_calls[0]), backtest)
        for call in self.calls:
            if call[:2] == ("docker", "run"):
                self.assertIn("--network=none", call)
                self.assertNotIn(contract.IMAGE + "@" + self.m["image"]["digest"], call)

    def test_zero_trades_and_incomplete_hpo_cannot_produce_proof(self):
        for field, value in (("trades", []), ("hpo", {"ok": True, "trials_completed": 8, "full_parameter_coverage": False})):
            old = copy.deepcopy(getattr(self, field))
            setattr(self, field, value)
            with self.subTest(field=field), patch.object(smoke, "command", side_effect=self.command), self.assertRaises(contract.Invalid):
                self.verify()
            setattr(self, field, old)

    def test_index_requires_unique_distinct_architectures_and_matching_original_bytes(self):
        index = {"manifests": [{"digest": self.m["image"][arch + "_digest"], "platform": {"os": "linux", "architecture": arch}}
                               for arch in ("amd64", "arm64")]}
        raw = contract.canonical(index)
        digest = "sha256:" + contract.sha256(raw)
        with patch("release_pipeline.Registry.raw", return_value=raw):
            self.assertEqual(smoke.inspect_index(digest)["digest"], digest)
            with self.assertRaises(contract.Invalid):
                smoke.inspect_index("sha256:" + "0" * 64)
        index["manifests"].append(index["manifests"][0])
        raw = contract.canonical(index)
        with patch("release_pipeline.Registry.raw", return_value=raw), self.assertRaises(contract.Invalid):
            smoke.inspect_index("sha256:" + contract.sha256(raw))


class HelperRuntimeSmokeTests(unittest.TestCase):
    """Execute the same in-container Python source on an isolated helper layout."""

    SUCCESS = "helper: installed files and --describe match the manifest"

    def _run(self, prefix: Path) -> subprocess.CompletedProcess:
        self.assertTrue(hasattr(smoke, "selected_admit_runtime_script"),
                        "release smoke must provide the selected-admit runtime check")
        source = smoke.selected_admit_runtime_script()
        self.assertEqual(source.count('"/opt/pineforge"'), 1)
        source = source.replace('"/opt/pineforge"', repr(str(prefix)))
        # Root ownership cannot be created or changed by ordinary test users.
        # Normalize only fixture ownership after a real stat; keep file types,
        # modes and bytes real. The actual image command has no such adapter.
        # This exact path list refuses every protected path and all env files.
        adapter = (
            "import os,pathlib,sys\n"
            f"fixture_paths={[str(prefix / name) for name in workflow_tests.SELECTED_ADMIT_PATHS]!r}\n"
            f"fixture_owner={(os.geteuid(), os.getegid())!r}\n"
            "real_stat=pathlib.Path.stat\n"
            "def fixture_stat(path,*args,**kwargs):\n"
            " if str(path) not in fixture_paths: sys.exit('refused fixture stat path: '+str(path))\n"
            " print('stat follows: '+str(path),file=sys.stderr,flush=True)\n"
            " info=real_stat(path,*args,**kwargs)\n"
            " if (info.st_uid,info.st_gid)==fixture_owner:\n"
            "  values=list(info)\n"
            "  values[4]=values[5]=0\n"
            "  return os.stat_result(values)\n"
            " return info\n"
            "pathlib.Path.stat=fixture_stat\n"
        )
        return subprocess.run([sys.executable, "-I", "-c", adapter + source],
                              cwd=prefix.parent, env={"PATH": os.environ.get("PATH", os.defpath)},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def _passes(self, prefix: Path) -> None:
        done = self._run(prefix)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines(), [self.SUCCESS])
        for name in workflow_tests.SELECTED_ADMIT_PATHS:
            self.assertIn("stat follows: " + str(prefix / name), done.stderr.splitlines())

    def _fails(self, prefix: Path, message: str) -> None:
        done = self._run(prefix)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("ERROR: " + message, done.stderr.splitlines())
        self.assertNotIn(self.SUCCESS, done.stdout)

    def _bind_helper_bytes(self, prefix: Path) -> None:
        helper, path = (prefix / name for name in workflow_tests.SELECTED_ADMIT_PATHS)
        manifest = json.loads(path.read_bytes())
        manifest["helper"]["bytes"] = str(helper.stat().st_size)
        manifest["helper"]["sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
        path.write_text(json.dumps(manifest, ensure_ascii=True, separators=(",", ":")) + "\n",
                        encoding="ascii")

    def test_a_one_byte_description_difference_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _ = workflow_tests.selected_admit_layout(Path(tmp))
            self._passes(prefix)
            helper = prefix / workflow_tests.SELECTED_ADMIT_PATHS[0]
            before = helper.read_bytes()
            after = before.replace(b'"version_full":"1.0.0"', b'"version_full":"1.0.1"', 1)
            self.assertEqual(len(before), len(after))
            self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
            helper.write_bytes(after)
            self._bind_helper_bytes(prefix)
            self._fails(prefix, "helper --describe differs from the canonical manifest projection")

    def test_a_helper_that_exits_nonzero_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _ = workflow_tests.selected_admit_layout(Path(tmp))
            self._passes(prefix)
            (prefix / workflow_tests.SELECTED_ADMIT_PATHS[0]).write_bytes(b"#!/bin/sh\nexit 7\n")
            self._bind_helper_bytes(prefix)
            self._fails(prefix, "helper --describe must exit zero")

    def test_an_incorrect_manifest_helper_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, _ = workflow_tests.selected_admit_layout(Path(tmp))
            self._passes(prefix)
            path = prefix / workflow_tests.SELECTED_ADMIT_PATHS[1]
            manifest = json.loads(path.read_bytes())
            manifest["helper"]["sha256"] = "0" * 64
            path.write_text(json.dumps(manifest, ensure_ascii=True, separators=(",", ":")) + "\n",
                            encoding="ascii")
            self._fails(prefix, "helper manifest sha256 differs from the helper")

    def test_runtime_rejects_extra_files_and_directories_in_each_exact_inventory(self):
        for name, directory, message in (
                ("bin/pineforge-selected-admit.bak", False, workflow_tests.HelperBuildCheckTest.BIN_ERROR),
                ("bin/.x", False, workflow_tests.HelperBuildCheckTest.BIN_ERROR),
                ("share/another-file", False, workflow_tests.HelperBuildCheckTest.SHARE_ERROR),
                ("share/another-directory", True, workflow_tests.HelperBuildCheckTest.SHARE_ERROR),
                ("share/pineforge/another-file", False, workflow_tests.HelperBuildCheckTest.MANIFEST_ERROR)):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as tmp:
                prefix, _ = workflow_tests.selected_admit_layout(Path(tmp))
                self._passes(prefix)
                path = prefix / name
                if directory:
                    path.mkdir()
                else:
                    path.write_bytes(b"extra\n")
                self._fails(prefix, message.removeprefix("ERROR: "))

    def test_runtime_rejects_wrong_modes_and_symlinks_for_both_assets(self):
        for name, mode in zip(workflow_tests.SELECTED_ADMIT_PATHS, (0o755, 0o644)):
            for fault in ("mode", "symlink"):
                with self.subTest(asset=name, fault=fault), tempfile.TemporaryDirectory() as tmp:
                    prefix, _ = workflow_tests.selected_admit_layout(Path(tmp))
                    self._passes(prefix)
                    path = prefix / name
                    if fault == "mode":
                        path.chmod(0o777)
                        message = "helper " + name + " must have mode " + format(mode, "o")
                    else:
                        target = Path(tmp) / "symlink-target"
                        path.rename(target)
                        path.symlink_to(target)
                        message = "helper file " + name + " must be a regular file without symlinks"
                    self._fails(prefix, message)


if __name__ == "__main__":
    unittest.main()
