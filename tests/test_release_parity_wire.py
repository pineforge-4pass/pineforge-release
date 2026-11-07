#!/usr/bin/env python3
"""Parity wire regressions using real builders and independently hashed bytes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import release_artifacts as artifacts
import release_contract as contract
import release_pair as pair
import release_pipeline as pipeline
from parity_bundle import PARITY_CAPABILITY_FROM
from test_parity_source_proof import TaggedEngineFixture, write_bundle
from test_release_binding import (
    FinalizeClient, MemoryRegistry, MemoryStore, ProducerFixture, candidate,
    expected_pair_payload, fixture_manifest,
)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture_identity(root):
    """Read fixture input bytes, never a producer/validator result."""
    files = {path.name: path.read_bytes() for path in root.iterdir()}
    descriptor = json.loads(files["PARITY-MANIFEST.json"])
    inventory = {name: digest(raw) for name, raw in files.items()}
    identity = {
        **descriptor,
        "descriptor_sha256": digest(files["PARITY-MANIFEST.json"]),
        "sums_sha256": digest(files["PARITY-SHA256SUMS"]),
    }
    return identity, inventory


def alternate_identity(identity):
    """A different, internally consistent identity with the same engine commit."""
    descriptor = {key: copy.deepcopy(identity[key]) for key in
                  ("schema", "source_manifest_sha256", "engine_commit", "payload_sha256")}
    descriptor["source_manifest_sha256"] = digest(b"another reviewed source declaration\n")
    raw = canonical(descriptor)
    listed = [("PARITY-MANIFEST.json", digest(raw)), *sorted(descriptor["payload_sha256"].items())]
    sums = "".join(f"{value}  {name}\n" for name, value in listed).encode()
    return {**descriptor, "descriptor_sha256": digest(raw), "sums_sha256": digest(sums)}


def wire_case(root):
    manifest = fixture_manifest(PARITY_CAPABILITY_FROM)
    write_bundle(root, descriptor_commit=manifest["versions"]["engine"]["commit"])
    identity, inventory = fixture_identity(root)
    manifest["versions"]["engine"]["parity"] = copy.deepcopy(identity)
    built = candidate(manifest)
    built["inventories"]["parity"] = copy.deepcopy(inventory)
    for arch, record in built["verification"].items():
        record["parity"] = {
            "image_digest": built["image"][arch + "_digest"],
            "identity": copy.deepcopy(identity), "inventory": copy.deepcopy(inventory),
        }
    return manifest, built, identity, inventory


def finalization_case(manifest, built, *, published=None):
    store = MemoryStore(manifest)
    row = store.get("v" + manifest["release_version"])
    # Seed immutable fixture assets directly; put() refuses replacement of the
    # constructor's original candidate, just as the real store does.
    store.assets[(row["tag_name"], contract.CANDIDATE)] = canonical(built)
    asset = next(item for item in row["assets"] if item["name"] == contract.CANDIDATE)
    asset["digest"] = "sha256:" + digest(canonical(built))
    if published is not None:
        store.put(row, contract.ASSET, canonical(published))
    store.operations.clear()
    return FinalizeClient(manifest), store, MemoryRegistry(), row


def finish(client, store, registry, row, manifest):
    with patch.object(pipeline, "npm", side_effect=lambda c, pin, inventory: {
            **pin, "npm_version": pin["version"],
            "npm_integrity": manifest["versions"]["codegen"]["npm_integrity"]}):
        return pipeline.finalize(client, store, registry, row)


class ParityWireTests(unittest.TestCase):
    def assert_control(self, manifest, built, identity, inventory):
        self.assertEqual(contract.validate(copy.deepcopy(manifest)), manifest)
        self.assertEqual(contract.validate_candidate(copy.deepcopy(built)), built)
        event = contract.pair_payload(canonical(manifest), canonical(built))
        self.assertEqual(event, expected_pair_payload(canonical(manifest), canonical(built)))
        self.assertEqual(event["source_sets"], [built["sources"]])
        ready = contract.payload(canonical(manifest))
        self.assertEqual(ready["versions"]["engine"]["parity"], identity)
        contract.validate_payload(ready, canonical(manifest))
        client, store, registry, row = finalization_case(manifest, built)
        result = finish(client, store, registry, row, manifest)
        self.assertEqual(result["versions"]["engine"]["parity"], identity)
        self.assertEqual(json.loads(store.read(row, contract.CANDIDATE))["inventories"]["parity"], inventory)
        self.assertTrue(registry.operations)
        self.assertFalse(row["draft"])
        raw, candidate_raw = store.read(row, contract.ASSET), store.read(row, contract.CANDIDATE)
        replay = finish(client, store, registry, row, manifest)
        self.assertEqual(replay, result)
        self.assertEqual(store.read(row, contract.ASSET), raw)
        self.assertEqual(store.read(row, contract.CANDIDATE), candidate_raw)

    def assert_candidate_refuses(self, manifest, built, message):
        with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
            contract.validate_candidate(built)
        with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
            contract.pair_payload(canonical(manifest), canonical(built))
        client, store, registry, row = finalization_case(manifest, built)
        with self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
            finish(client, store, registry, row, manifest)
        self.assertEqual(registry.operations, [])
        self.assertTrue(row["draft"])
        self.assertIsNone(store.read(row, contract.ASSET))

    def test_bindings_carry_tagged_proof_and_eight_entry_parity_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            write_bundle(root, descriptor_commit=client.source_commit)
            identity, inventory = fixture_identity(root)
            with patch.object(artifacts, "PARITY_BUNDLE_ROOT", root):
                bound = pipeline.bindings(
                    client, PARITY_CAPABILITY_FROM, PARITY_CAPABILITY_FROM,
                    PARITY_CAPABILITY_FROM, PARITY_CAPABILITY_FROM,
                    fixture_manifest(PARITY_CAPABILITY_FROM)["hub_commit"])
            self.assertEqual(bound["versions"]["engine"], {
                "version": PARITY_CAPABILITY_FROM, "tag": "v" + PARITY_CAPABILITY_FROM,
                "commit": client.source_commit, "parity": identity,
            })
            self.assertEqual(bound["inventories"]["parity"], inventory)
            self.assertEqual(len(inventory), 8)
            self.assertNotIn("engine", bound["inventories"])

    def test_historical_bindings_keep_their_pin_shape_and_inventory_roles(self):
        prerelease = PARITY_CAPABILITY_FROM + "-rc.1"
        for release in ("1.8.0", "1.9.0", "1.9.1", "1.10.0", "1.10.1", prerelease):
            with self.subTest(release=release):
                self.assertLess(pair.parse(release), pair.parse(PARITY_CAPABILITY_FROM))
                client = ProducerFixture(release)
                with patch.object(artifacts, "parity_source_proof",
                                  side_effect=AssertionError("legacy producer fetched parity")):
                    bound = pipeline.bindings(client, release, release, release, release,
                                              fixture_manifest(release)["hub_commit"])
                self.assertEqual(bound["versions"]["engine"], {
                    "version": release, "tag": "v" + release, "commit": client.commits["engine"],
                })
                self.assertEqual(set(bound["inventories"]), {"codegen", "hpo"})

    def test_bindings_from_an_unrelated_cwd_use_the_repo_anchored_bundle_root(self):
        self.assertEqual(artifacts.PARITY_BUNDLE_ROOT, ROOT / "docker/parity")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "parity"
            client = TaggedEngineFixture()
            write_bundle(root, descriptor_commit=client.source_commit)
            identity, inventory = fixture_identity(root)
            unrelated = base / "unrelated"
            unrelated.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(unrelated)
                with patch.object(artifacts, "PARITY_BUNDLE_ROOT", root):
                    bound = pipeline.bindings(
                        client, PARITY_CAPABILITY_FROM, PARITY_CAPABILITY_FROM,
                        PARITY_CAPABILITY_FROM, PARITY_CAPABILITY_FROM,
                        fixture_manifest(PARITY_CAPABILITY_FROM)["hub_commit"])
            finally:
                os.chdir(previous)
            self.assertEqual(bound["versions"]["engine"]["parity"], identity)
            self.assertEqual(bound["inventories"]["parity"], inventory)

    def test_missing_capability_refuses_beside_positive_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
            self.assert_control(manifest, built, identity, inventory)
            del built["versions"]["engine"]["parity"]
            self.assert_candidate_refuses(manifest, built, "parity capability missing for release")
            manifest["versions"]["engine"]["parity"] = None
            with self.assertRaisesRegex(contract.Invalid, "^parity capability missing for release$"):
                contract.validate(manifest)

    def test_candidate_inventory_mismatch_refuses_beside_positive_control(self):
        for fault in ("payload", "missing", "null_inventories", "extra"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
                self.assert_control(manifest, built, identity, inventory)
                if fault == "payload":
                    built["inventories"]["parity"]["run_strategy.py"] = digest(b"changed script\n")
                elif fault == "missing":
                    del built["inventories"]["parity"]
                elif fault == "null_inventories":
                    built["inventories"] = None
                else:
                    built["inventories"]["parity"]["unapproved.py"] = digest(b"extra\n")
                self.assert_candidate_refuses(manifest, built, "parity inventory differs from candidate")

    def test_source_commit_mismatch_refuses_even_with_consistent_descriptor_and_sums(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
            self.assert_control(manifest, built, identity, inventory)
            built["versions"]["engine"]["commit"] = hashlib.sha1(b"another engine commit").hexdigest()
            self.assert_candidate_refuses(manifest, built, "parity identity differs from engine commit")
            bad = copy.deepcopy(manifest)
            bad["versions"]["engine"]["commit"] = built["versions"]["engine"]["commit"]
            with self.assertRaisesRegex(contract.Invalid, "^parity identity differs from engine commit$"):
                contract.validate(bad)

    def test_descriptor_hash_mismatch_refuses_beside_positive_control(self):
        for location in ("identity", "inventory"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
                self.assert_control(manifest, built, identity, inventory)
                if location == "identity":
                    built["versions"]["engine"]["parity"]["descriptor_sha256"] = digest(b"wrong descriptor\n")
                else:
                    built["inventories"]["parity"]["PARITY-MANIFEST.json"] = digest(b"wrong descriptor\n")
                self.assert_candidate_refuses(manifest, built, "parity descriptor hash differs")

    def test_sums_hash_mismatch_refuses_beside_positive_control(self):
        for location in ("identity", "inventory"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
                self.assert_control(manifest, built, identity, inventory)
                if location == "identity":
                    built["versions"]["engine"]["parity"]["sums_sha256"] = digest(b"wrong SUMS\n")
                else:
                    built["inventories"]["parity"]["PARITY-SHA256SUMS"] = digest(b"wrong SUMS\n")
                self.assert_candidate_refuses(manifest, built, "parity SUMS hash differs")

    def test_manifest_identity_mismatch_refuses_before_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
            self.assert_control(manifest, built, identity, inventory)
            altered = copy.deepcopy(manifest)
            altered["versions"]["engine"]["parity"] = alternate_identity(identity)
            self.assertEqual(contract.validate(altered), altered)
            self.assertEqual(contract.validate_candidate(built), built)
            with self.assertRaisesRegex(
                    contract.Invalid, "^parity identity differs from manifest$",
                    msg="pair_payload parity identity"):
                contract.pair_payload(canonical(altered), canonical(built))
            client, store, registry, row = finalization_case(manifest, built, published=altered)
            with self.assertRaisesRegex(
                    contract.Invalid, "^parity identity differs from manifest$",
                    msg="finalize parity identity"):
                finish(client, store, registry, row, manifest)
            self.assertEqual(registry.operations, [])
            self.assertTrue(row["draft"])
            self.assertEqual(store.read(row, contract.ASSET), canonical(altered))

    def test_exact_parity_object_fields_are_required(self):
        for field in ("schema", "source_manifest_sha256", "engine_commit", "payload_sha256",
                      "descriptor_sha256", "sums_sha256", "extra"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
                self.assert_control(manifest, built, identity, inventory)
                obj = built["versions"]["engine"]["parity"]
                if field == "extra":
                    obj[field] = "unreviewed"
                else:
                    del obj[field]
                with self.assertRaises(contract.Invalid):
                    contract.validate_candidate(built)

    def test_old_shape_at_capability_refuses_but_historical_bytes_replay_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, built, identity, inventory = wire_case(Path(temporary) / "parity")
            self.assert_control(manifest, built, identity, inventory)
            del manifest["versions"]["engine"]["parity"]
            with self.assertRaisesRegex(contract.Invalid, "^legacy manifest predates parity capability$"):
                contract.payload(canonical(manifest))
        for release in ("1.8.0", "1.9.0", "1.9.1", "1.10.0", "1.10.1",
                        PARITY_CAPABILITY_FROM + "-rc.1"):
            with self.subTest(release=release):
                self.assertLess(pair.parse(release), pair.parse(PARITY_CAPABILITY_FROM))
                historical = fixture_manifest(release)
                raw = canonical(historical)
                built = candidate(historical)
                candidate_raw = canonical(built)
                self.assertEqual(contract.validate(historical), json.loads(raw))
                self.assertEqual(contract.validate_candidate(built), json.loads(candidate_raw))
                self.assertNotIn("parity", historical["versions"]["engine"])
                self.assertNotIn("parity", built["inventories"])
                self.assertEqual(contract.pair_payload(raw, candidate_raw),
                                 expected_pair_payload(raw, candidate_raw))
                client, store, registry, row = finalization_case(historical, built, published=historical)
                finish(client, store, registry, row, historical)
                finish(client, store, registry, row, historical)
                self.assertEqual(store.read(row, contract.ASSET), raw)
                self.assertEqual(store.read(row, contract.CANDIDATE), candidate_raw)

    def test_stored_historical_manifest_assets_replay_their_pinned_bytes(self):
        stored = {
            "1.8.0": "764ba4341b4790be9288f62d497f90f330f86a2e6dbc7afcde102195eed203e3",
            "1.9.0": "fc430e3b9f0f7334bba3c00283b4a1fb4f8f01ad75ca7ed1410efc51519a448a",
            "1.9.1": "dd6f32d05dbe05b48a083a1a5f076423167d4cbaf8697d9b8e8c2a5dfe19ccba",
        }
        for release, expected_sha256 in stored.items():
            with self.subTest(release=release):
                path = ROOT / "tests/fixtures/stored-manifests" / f"v{release}" / "release-manifest.json"
                self.assertTrue(path.is_file(), "released fixture required: " + str(path.relative_to(ROOT)))
                raw = path.read_bytes()
                self.assertEqual(len(raw), 1391)
                self.assertEqual(digest(raw), expected_sha256)
                event = contract.payload(raw)
                self.assertEqual(event["manifest"]["sha256"], expected_sha256)
                built = candidate(json.loads(raw))
                candidate_raw = canonical(built)
                pair_event = contract.pair_payload(raw, candidate_raw)
                self.assertEqual(pair_event["receipt_sha256"], expected_sha256)
                self.assertEqual(pair_event, expected_pair_payload(raw, candidate_raw))


if __name__ == "__main__":
    unittest.main()
