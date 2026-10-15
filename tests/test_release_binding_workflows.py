#!/usr/bin/env python3
"""CI/cloud replay of actual workflow shell and finalizer wiring, without network."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import release_pair as pair
import release_pipeline as pipeline
import release_contract as contract
import test_release_workflows as wiring
from test_release_binding import (FinalizeClient, MemoryRegistry, MemoryStore,
                                  ProducerFixture, fixture_manifest)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def assert_ready_wiring(case, publish, finalizer):
    steps = publish["jobs"]["publish"]["steps"]
    named = {row.get("name"): row for row in steps}
    order = ["Check Debian source mirror", "Check pushed Debian source mirror",
             "Verify real candidate runtime on both architectures", "GitHub Release"]
    positions = [steps.index(named[name]) for name in order]
    case.assertEqual(positions, sorted(positions))
    for name in order:
        case.assertNotIn("continue-on-error", named[name])
        case.assertNotIn("if", named[name])
    tags = named["Image metadata"]["with"]["tags"].splitlines()
    case.assertEqual(len(tags), 1)
    case.assertTrue(tags[0].startswith("type=raw,value=candidate-"))
    case.assertIn("org.opencontainers.image.version=${{ github.ref_name }}", named["Image metadata"]["with"]["labels"])
    case.assertIn("org.opencontainers.image.revision=${{ github.sha }}", named["Image metadata"]["with"]["labels"])
    case.assertEqual(publish["jobs"]["notify-consumers"]["uses"], "./.github/workflows/release-finalize.yml")
    finalize = finalizer["jobs"]["finalize"]
    case.assertTrue(any("scripts/release_pipeline.py finalize" in row.get("run", "") for row in finalize["steps"]))
    dispatch = finalizer["jobs"]["notify-consumers"]
    case.assertEqual(dispatch["needs"], "finalize")
    case.assertEqual(dispatch["strategy"]["fail-fast"], "false")
    case.assertEqual(dispatch["strategy"]["matrix"]["include"], "${{ fromJSON(needs.finalize.outputs.deliveries) }}")
    case.assertEqual(finalizer["concurrency"]["group"], "release-finalize")


class ReadyWiringTests(unittest.TestCase):
    def test_actual_workflows_require_verified_candidate_before_ready_fanout(self):
        assert_ready_wiring(self, workflow("publish.yml"), workflow("release-finalize.yml"))

    def test_negative_controls_detect_removed_runtime_gate_early_latest_and_skipped_fanout(self):
        original = workflow("publish.yml")
        for mutation in ("runtime", "latest", "fanout"):
            obj, finalizer = copy.deepcopy(original), workflow("release-finalize.yml")
            if mutation == "runtime":
                obj["jobs"]["publish"]["steps"] = [s for s in obj["jobs"]["publish"]["steps"]
                                                         if s.get("name") != "Verify real candidate runtime on both architectures"]
            elif mutation == "latest":
                step = next(s for s in obj["jobs"]["publish"]["steps"] if s.get("name") == "Image metadata")
                step["with"]["tags"] += "\ntype=raw,value=latest"
            else:
                finalizer["jobs"]["notify-consumers"]["needs"] = "publish"
            with self.subTest(mutation=mutation), self.assertRaises((AssertionError, KeyError)):
                assert_ready_wiring(self, obj, finalizer)

    def test_aggregate_inventory_rejects_failed_cancelled_skipped_or_missing_job(self):
        wf = workflow("python-test.yml")
        job = wf["jobs"]["release-ready"]
        self.assertEqual(job["name"], "release/ready")
        self.assertEqual(set(job["needs"]), {"fingerprint-canonical", "release-rules", "description-renderer", "release-ready-replay"})
        script = job["steps"][0]["run"]
        # Execute the workflow's actual aggregate program in a child process.
        import subprocess
        expected = {name: {"result": "success"} for name in job["needs"]}
        for bad in (None, "failure", "cancelled", "skipped", "missing"):
            results = copy.deepcopy(expected)
            if bad == "missing":
                results.pop("release-rules")
            elif bad:
                results["release-rules"]["result"] = bad
            done = subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                                  env={"PATH": os.environ.get("PATH", ""), "RESULTS": json.dumps(results)},
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode == 0, bad is None, (bad, done.stderr))

    def test_facts_license_extension_keeps_exact_three_field_payload(self):
        wf = workflow("facts-fanout.yml")
        self.assertEqual(set(wf["jobs"]), {"web", "license"})
        self.assertEqual(wf["jobs"]["license"]["env"]["FACTS_CONSUMER_ROLE"], "license")
        from facts_fanout import payload_fields
        good = {"commit": "a" * 40, "sha256": "b" * 64, "dry_run": False}
        self.assertEqual(payload_fields(good), good)
        with self.assertRaises(ValueError):
            payload_fields({**good, "release_version": "1.6.0"})


class ArrivalReplayTests(unittest.TestCase):
    def test_all_arrival_orders_replay_real_handler_shell_then_finalize_and_repeat(self):
        handler = (ROOT / ".github/workflows/handle-upstream.yml").read_text()
        runner = wiring.HpoWorkflowTest()
        for ver in ("1.6.0-rc.1", "1.6.0"):
            v = pair.parse(ver)
            for order in permutations(("engine", "codegen", "hpo")):
                with self.subTest(version=ver, order=order), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    runner.fixture(root)
                    http, published = {}, set()
                    for index, role in enumerate(order):
                        published.add(role)
                        if role == "engine":
                            for arch in ("x86_64", "aarch64"):
                                http[f"https://github.com/pineforge-4pass/pineforge-engine/releases/download/v{v}/pineforge-v{v}-linux-{arch}.tar.gz"] = {"status": 200}
                        elif role == "codegen":
                            http[f"https://pypi.org/pypi/pineforge-codegen/{v.pep440}/json"] = {"status": 200, "body": "{}"}
                        else:
                            http[f"https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v{v.pep440}/pineforge_hpo-{v.pep440}-py3-none-any.whl"] = {"status": 200}
                        http["https://pypi.org/pypi/pineforge-codegen/json"] = {"status": 200, "body": json.dumps({"releases": [v.pep440] if "codegen" in published else ["1.5.9"]})}
                        (root / "state/http.json").write_text(json.dumps(http))
                        for kind in ("engine", "hpo"):
                            tag = v.pep440 if kind == "hpo" else str(v)
                            rows = [{"tag_name": "v" + tag, "draft": False, "prerelease": v.prerelease}] if kind in published else []
                            (root / f"state/releases-pineforge-{kind}.json").write_text(json.dumps(rows))
                        # The producer itself must pass its real artifact verifier.
                        import release_artifacts
                        release_artifacts.producer(ProducerFixture(ver), role, ver)
                        done, incoming = runner.shell(root, handler, "Validate payload", {
                            "EVENT": role + "-release", "RAW_VERSION": v.pep440 if role == "hpo" else str(v),
                            "RAW_PRERELEASE": str(v.prerelease).lower(), "RUN_ID": "42"})
                        self.assertEqual(done.returncode, 0, done.stderr)
                        done, partners = runner.shell(root, handler, "Probe the partner", {
                            "COMPONENT": role, "VER": str(v), "PYPI": v.pep440, "HPO_PAIRING": "true", "HPO_VERSION": v.pep440})
                        self.assertEqual(done.returncode, 0, done.stderr)
                        values = {"COMPONENT": role, "VER": str(v), "PAYLOAD_PRERELEASE": str(v.prerelease).lower(),
                                  "EPREV": "1.5.9", "CPREV": "1.5.9", "HPREV": "", "HPO_PAIRING": "true",
                                  "PUBLISHED": "false", "PARTNER_LATEST": ""}
                        for kind in ("engine", "codegen", "hpo"):
                            values[kind.upper() + "_PUBLISHED"] = partners.get(kind + "_published", "false")
                            values[kind.upper() + "_LATEST"] = partners.get(kind + "_latest", "")
                        done, decision = runner.shell(root, handler, "Decide action", values)
                        self.assertEqual(done.returncode, 0, done.stderr)
                        self.assertEqual(decision["mode"], "bump" if index == 2 else "wait")
                    manifest = fixture_manifest(ver)
                    client, store, registry = FinalizeClient(manifest), MemoryStore(manifest), MemoryRegistry()
                    tag = "v" + ver
                    with patch.object(pipeline, "npm", side_effect=lambda c, pin, inv: {
                            **pin, "npm_version": ver, "npm_integrity": manifest["versions"]["codegen"]["npm_integrity"]}):
                        event = pipeline.finalize(client, store, registry, store.get(tag))
                        raw = store.read(store.get(tag), contract.ASSET)
                        self.assertEqual(event, pipeline.finalize(client, store, registry, store.get(tag)))
                        self.assertEqual(store.read(store.get(tag), contract.ASSET), raw)


if __name__ == "__main__":
    unittest.main()
