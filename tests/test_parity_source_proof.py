#!/usr/bin/env python3
"""Regressions for publish bindings' independent engine parity source proof."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from release_artifacts import parity_source_proof  # noqa: E402
from parity_bundle import PARITY_CAPABILITY_FROM, sync_parity_bundle  # noqa: E402
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
from test_parity_bundle import (  # noqa: E402
    ENGINE_REPO, ENGINE_TAG, MANIFEST, MANIFEST_RAW, PAYLOAD_BYTES, ROWS,
    TaggedEngineFixture, canonical, digest,
)


def expected_payload_hashes():
    return {row["destination"]: digest(PAYLOAD_BYTES[row["source"]]) for row in ROWS}


def write_bundle(root, *, descriptor_commit=None, manifest_raw=MANIFEST_RAW,
                 payload_hashes=None, payload_files=None):
    root.mkdir(exist_ok=True)
    payload_hashes = expected_payload_hashes() if payload_hashes is None else payload_hashes
    payload_files = payload_hashes if payload_files is None else payload_files
    descriptor = {
        "schema": "pineforge/parity-descriptor/v1",
        "source_manifest_sha256": digest(manifest_raw),
        "engine_commit": descriptor_commit or "a" * 40,
        "payload_sha256": payload_hashes,
    }
    descriptor_raw = canonical(descriptor)
    (root / "PARITY-MANIFEST.json").write_bytes(descriptor_raw)
    for row in ROWS:
        if row["destination"] in payload_files:
            (root / row["destination"]).write_bytes(PAYLOAD_BYTES[row["source"]])
    sums = {
        "PARITY-MANIFEST.json": digest(descriptor_raw),
        **{name: digest(PAYLOAD_BYTES[next(row["source"] for row in ROWS
                                        if row["destination"] == name)])
           for name in payload_files},
    }
    (root / "PARITY-SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in
                [("PARITY-MANIFEST.json", sums["PARITY-MANIFEST.json"])] +
                [(name, sums[name]) for name in sorted(payload_hashes)]),
        encoding="ascii",
    )


def expected_inventory(root):
    return {path.name: digest(path.read_bytes())
            for path in sorted(root.iterdir(), key=lambda item: item.name)}


def bind(client, root):
    version = str(client.v)
    with patch.object(artifacts, "PARITY_BUNDLE_ROOT", root):
        return pipeline.bindings(client, version, version, version, version, "d" * 40)


def tag_source_mismatch(client, root):
    client.releases["engine"]["tag_name"] = "v1.8.1"


def missing_script(client, root):
    client.install_source("scripts/run_strategy.py", kind="missing")


def altered_script(client, root):
    client.mutate_source_bytes("scripts/run_strategy.py", b"altered parity script\n")


def altered_manifest(client, root):
    value = copy.deepcopy(MANIFEST)
    value["schema"] = "pineforge/engine-parity-source/v2"
    client.mutate_manifest(value)


def empty_inventory(client, root):
    write_bundle(root, payload_hashes={}, payload_files=expected_payload_hashes())


def extra_unapproved_file(client, root):
    value = copy.deepcopy(MANIFEST)
    value["files"].append({"source": "scripts/unapproved.py", "destination": "unapproved.py"})
    raw = canonical(value)
    client.mutate_manifest(value)
    write_bundle(root, manifest_raw=raw)


def wrong_source_commit(client, root):
    # Recompute descriptor and SUMS together: self-consistency is insufficient.
    write_bundle(root, descriptor_commit="b" * 40)


def local_payload_mismatch(client, root):
    original = (root / "run_strategy.py").read_bytes()
    changed = original + b"changed tagged bytes\n"
    client.mutate_source_bytes("scripts/run_strategy.py", changed)
    descriptor = json.loads((root / "PARITY-MANIFEST.json").read_bytes())
    descriptor["payload_sha256"]["run_strategy.py"] = digest(changed)
    descriptor_raw = canonical(descriptor)
    (root / "PARITY-MANIFEST.json").write_bytes(descriptor_raw)
    sums = {"PARITY-MANIFEST.json": digest(descriptor_raw), **descriptor["payload_sha256"]}
    (root / "PARITY-SHA256SUMS").write_bytes(
        "".join(f"{value}  {name}\n" for name, value in
                [("PARITY-MANIFEST.json", sums["PARITY-MANIFEST.json"])] +
                [(name, sums[name]) for name in sorted(descriptor["payload_sha256"])])
        .encode("ascii"))


NEGATIVE_ROWS = (
    ("tag_source_mismatch", tag_source_mismatch, "producer release channel differs"),
    ("missing_script", missing_script, "parity source file missing"),
    ("altered_script", altered_script, "parity source bytes differ"),
    ("altered_manifest", altered_manifest, "parity source manifest differs from tagged source"),
    ("empty_inventory", empty_inventory, "empty parity source inventory"),
    ("extra_unapproved_file", extra_unapproved_file, "unapproved parity source file"),
    ("wrong_source_commit", wrong_source_commit, "parity descriptor commit differs from engine tag"),
    ("local_payload_mismatch", local_payload_mismatch, "parity source bytes differ"),
)


class ParitySourceProofTests(unittest.TestCase):
    def assert_positive_bindings(self, client, root):
        write_bundle(root)
        bound = bind(client, root)
        inventory = bound["inventories"]["parity"]
        identity = {
            **json.loads((root / "PARITY-MANIFEST.json").read_bytes()),
            "descriptor_sha256": digest((root / "PARITY-MANIFEST.json").read_bytes()),
            "sums_sha256": digest((root / "PARITY-SHA256SUMS").read_bytes()),
        }
        self.assertEqual(bound["versions"]["engine"], {
            "version": str(client.v), "commit": "a" * 40, "tag": "v" + str(client.v),
            "parity": identity,
        })
        self.assertEqual(inventory, expected_inventory(root))
        self.assertEqual(len(inventory), len(ROWS) + 2)
        self.assertNotIn("engine", bound["inventories"])
        self.assertEqual(client.content_calls, len(ROWS) + 1)
        return bound

    def test_proof_returns_independent_tagged_source_identity_and_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            sync_parity_bundle(client, ENGINE_TAG, root)
            client.content_calls = 0
            with patch.object(client, "tag_commit", wraps=client.tag_commit) as resolve:
                identity = parity_source_proof(client, PARITY_CAPABILITY_FROM, root)
            resolve.assert_called_once_with(ENGINE_REPO, ENGINE_TAG)
            self.assertEqual(client.content_calls, len(ROWS) + 1)
            self.assertEqual(identity, {
                "engine_commit": client.source_commit,
                "source_manifest_sha256": digest(MANIFEST_RAW),
                "payload_sha256": expected_payload_hashes(),
                "descriptor_sha256": digest((root / "PARITY-MANIFEST.json").read_bytes()),
                "sums_sha256": digest((root / "PARITY-SHA256SUMS").read_bytes()),
            })

            # A moved tag with identical source blobs must still fail the
            # independent proof's commit comparison against the synced bundle.
            moved = "b" * 40
            for path in ("scripts/parity-bundle.json", *(row["source"] for row in ROWS)):
                route = f"repos/{ENGINE_REPO}/contents/{path}?ref={moved}"
                client.api_data[route] = copy.deepcopy(client.api_data[client.route(path)])
            client.api_data[f"repos/{ENGINE_REPO}/git/trees/{moved}?recursive=1"] = (
                copy.deepcopy(client.source_tree))
            client.commits["engine"] = moved
            # Keep the direct proof's commit assertion observable independently
            # of bindings' additional descriptor and inventory validation.
            with self.subTest(row="wrong_source_commit"), self.assertRaisesRegex(
                    contract.Invalid, "^parity descriptor commit differs from engine tag$",
                    msg="wrong_source_commit"):
                parity_source_proof(client, PARITY_CAPABILITY_FROM, root)

    def test_existing_engine_archives_and_checksums_stay_green_with_nonempty_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = TaggedEngineFixture("2.0.0")
            names = {f"pineforge-v{client.v}-{triple}.tar.gz"
                     for triple in ("linux-x86_64", "linux-aarch64")}
            self.assertEqual({a["name"] for a in client.releases["engine"]["assets"]},
                             names | {name + ".sha256" for name in names})
            self.assertFalse(any("macos" in url for url in client.downloads))
            bound = self.assert_positive_bindings(client, Path(temporary) / "parity")
            self.assertEqual(len(bound["inventories"]["parity"]), 8)

    def test_bindings_refuse_a_proof_from_a_different_engine_tag_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self.assert_positive_bindings(TaggedEngineFixture(), base / "positive")
            client = TaggedEngineFixture()
            moved = "b" * 40
            root = base / "negative"
            write_bundle(root, descriptor_commit=moved)
            for path in ("scripts/parity-bundle.json", *(row["source"] for row in ROWS)):
                route = f"repos/{ENGINE_REPO}/contents/{path}?ref={moved}"
                client.api_data[route] = copy.deepcopy(client.api_data[client.route(path)])
            client.api_data[f"repos/{ENGINE_REPO}/git/trees/{moved}?recursive=1"] = (
                copy.deepcopy(client.source_tree))
            # The bundle agrees with the second resolution, so the independent
            # proof succeeds; it must still agree with the producer's first pin.
            with patch.object(client, "tag_commit", side_effect=[client.source_commit, moved]) as resolve:
                with self.assertRaisesRegex(
                        contract.Invalid, "^parity descriptor commit differs from engine tag$"):
                    bind(client, root)
            self.assertEqual(resolve.call_count, 2)
            self.assertTrue(all(call.args == (ENGINE_REPO, ENGINE_TAG)
                                for call in resolve.call_args_list))
            self.assertEqual(client.content_calls, len(ROWS) + 1)

    def test_negative_rows_have_a_green_control_and_refuse(self):
        for label, mutate, message in NEGATIVE_ROWS:
            with self.subTest(row=label), tempfile.TemporaryDirectory() as temporary:
                self.assert_positive_bindings(TaggedEngineFixture(), Path(temporary) / "positive")
                negative_root = Path(temporary) / "negative"
                client = TaggedEngineFixture()
                write_bundle(negative_root)
                mutate(client, negative_root)
                if label == "wrong_source_commit":
                    descriptor_raw = (negative_root / "PARITY-MANIFEST.json").read_bytes()
                    listed = {
                        name: value for value, name in (
                            line.split("  ", 1)
                            for line in (negative_root / "PARITY-SHA256SUMS")
                            .read_text(encoding="ascii").splitlines())
                    }
                    self.assertEqual(listed, {
                        "PARITY-MANIFEST.json": digest(descriptor_raw), **expected_payload_hashes(),
                    })
                with self.assertRaisesRegex(contract.Invalid, "^" + message + "$", msg=label):
                    bind(client, negative_root)

    def test_mutation_rows_name_a_matching_negative_control(self):
        from run_parity_mutations import MUTANTS
        selected = [spec for spec in MUTANTS if spec.test_file == "tests/test_parity_source_proof.py"]
        self.assertTrue(selected)
        for spec in selected:
            with self.subTest(mutant=spec.name):
                self.assertTrue(callable(getattr(type(self), spec.test_name.split(".")[-1])))
                self.assertIn(spec.subtest_row, {label for label, _, _ in NEGATIVE_ROWS})
                self.assertEqual((ROOT / spec.relative).read_text(encoding="utf-8").count(spec.old), 1)


if __name__ == "__main__":
    unittest.main()
