#!/usr/bin/env python3
"""CI/cloud checks for nonempty image smoke results and per-child execution."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import release_contract as contract
import release_smoke as smoke
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

    def test_runtime_pulls_the_recorded_product_repository_for_either_name(self):
        for repository in contract.IMAGES:
            with self.subTest(repository=repository):
                self.calls.clear()
                self.m["image"]["repository"] = repository
                with patch.object(smoke, "command", side_effect=self.command):
                    proof = self.verify()
                self.assertEqual(set(proof), {"amd64", "arm64"})
                for arch in proof:
                    self.assertTrue(any(call[:2] == ("docker", "pull")
                                        and repository + "@" + self.m["image"][arch + "_digest"] in call
                                        for call in self.calls))

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


if __name__ == "__main__":
    unittest.main()
