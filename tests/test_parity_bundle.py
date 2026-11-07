#!/usr/bin/env python3
"""Regressions for the tagged-source parity bundle fetch."""
from __future__ import annotations

import base64
import copy
import errno
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from parity_bundle import PARITY_CAPABILITY_FROM, sync_parity_bundle  # noqa: E402
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import parity_bundle as bundle  # noqa: E402
from test_release_binding import ProducerFixture  # noqa: E402


FIXTURE_MANIFEST = ROOT / "tests/fixtures/parity-bundle.json"
MANIFEST_RAW = FIXTURE_MANIFEST.read_bytes()
MANIFEST = json.loads(MANIFEST_RAW)
ROWS = tuple(MANIFEST["files"])
PAYLOAD_BYTES = {
    row["source"]: ("parity fixture bytes for " + row["source"] + "\n").encode()
    for row in ROWS
}
ENGINE_REPO = artifacts.PRODUCERS["engine"]
ENGINE_TAG = "v" + PARITY_CAPABILITY_FROM


def canonical(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def content_record(path, raw=None, kind="file"):
    record = {"type": kind, "path": path}
    if kind == "file":
        record.update({
            "encoding": "base64",
            "sha": artifacts.git_blob(raw),
            "content": base64.b64encode(raw).decode("ascii"),
        })
    elif kind == "symlink":
        record["target"] = "scripts/run_strategy.py"
    return record


class TaggedEngineFixture(ProducerFixture):
    """Reuse ProducerFixture's exact endpoint fake for tagged content reads."""

    def __init__(self, version=PARITY_CAPABILITY_FROM):
        super().__init__(version)
        self.source_commit = self.commits["engine"]
        self.content_calls = 0
        self.fail_after = None
        self.source_tree = {"truncated": False, "tree": []}
        self.api_data[f"repos/{ENGINE_REPO}/git/trees/{self.source_commit}?recursive=1"] = self.source_tree
        self.install_manifest(MANIFEST_RAW)
        for row in ROWS:
            self.install_source(row["source"], PAYLOAD_BYTES[row["source"]])

    def route(self, path):
        return f"repos/{ENGINE_REPO}/contents/{path}?ref={self.source_commit}"

    def install_manifest(self, raw):
        self.install_source("scripts/parity-bundle.json", raw)

    def install_source(self, path, raw=None, kind="file"):
        self.api_data[self.route(path)] = content_record(path, raw, kind)
        self.source_tree["tree"] = [row for row in self.source_tree["tree"] if row["path"] != path]
        if kind != "missing":
            self.source_tree["tree"].append({
                "path": path,
                "type": "blob" if kind in ("file", "symlink") else "tree",
                "mode": {"file": "100644", "symlink": "120000"}.get(kind, "040000"),
                "sha": artifacts.git_blob(raw if raw is not None else b"scripts/run_strategy.py"),
            })

    def mutate_manifest(self, value):
        self.install_manifest(canonical(value))

    def mutate_source_bytes(self, path, raw):
        self.install_source(path, raw)

    def mutate_source_kind(self, path, kind):
        self.install_source(path, kind=kind)

    def mutate_blob_sha(self, path, sha):
        self.api_data[self.route(path)]["sha"] = sha

    def api(self, route, **kwargs):
        if "/contents/" in route:
            self.content_calls += 1
            if self.fail_after is not None and self.content_calls > self.fail_after:
                raise OSError("fixture partial fetch")
            if route not in self.api_data:
                return {"type": "missing", "path": route}
        return super().api(route, **kwargs)


def sync(client, destination, **kwargs):
    return sync_parity_bundle(client, ENGINE_TAG, destination, **kwargs)


def snapshot(root):
    return {path.name: path.read_bytes() for path in root.iterdir()}


def wrong_commit(client):
    client.commits["engine"] = "b" * 40


def manifest_subset(client):
    value = copy.deepcopy(MANIFEST)
    value["files"].pop()
    client.mutate_manifest(value)


def manifest_superset(client):
    value = copy.deepcopy(MANIFEST)
    value["files"].append({"source": "scripts/unapproved.py", "destination": "unapproved.py"})
    client.mutate_manifest(value)


def noncanonical_manifest(client):
    client.install_manifest((json.dumps(MANIFEST, indent=2) + "\n").encode())


def empty_source(client):
    client.mutate_source_bytes("scripts/run_strategy.py", b"")


def bad_blob_hash(client):
    client.mutate_blob_sha("scripts/run_strategy.py", "0" * 40)


def bad_tree_hash(client):
    for row in client.source_tree["tree"]:
        if row["path"] == "scripts/run_strategy.py":
            row["sha"] = "0" * 40
            return
    raise AssertionError("fixture source tree has no run_strategy.py row")


def truncated_tree(client):
    client.source_tree["truncated"] = True


def missing_tree_entry(client):
    client.source_tree["tree"] = [
        row for row in client.source_tree["tree"] if row["path"] != "scripts/run_strategy.py"]


def duplicate_destination(client):
    value = copy.deepcopy(MANIFEST)
    value["files"].append(copy.deepcopy(value["files"][0]))
    client.mutate_manifest(value)


def traversal_destination(client):
    value = copy.deepcopy(MANIFEST)
    value["files"][0]["destination"] = "../escape.py"
    client.mutate_manifest(value)


def symlink_source(client):
    client.mutate_source_kind("scripts/run_strategy.py", "symlink")


def followed_symlink_source(client):
    # Contents may report the target as type=file; the pinned tree owns mode.
    for row in client.source_tree["tree"]:
        if row["path"] == "scripts/run_strategy.py":
            row["mode"] = "120000"


NEGATIVE_ROWS = (
    ("wrong_commit", wrong_commit, "parity source file missing"),
    ("manifest_subset", manifest_subset, "parity manifest payload differs from approved allowlist"),
    ("manifest_superset", manifest_superset, "parity manifest payload differs from approved allowlist"),
    ("noncanonical_manifest", noncanonical_manifest, "parity manifest is not canonical"),
    ("empty_file", empty_source, "parity payload is empty"),
    ("bad_hash", bad_blob_hash, "parity payload blob digest differs"),
    ("bad_tree_hash", bad_tree_hash, "parity payload blob digest differs"),
    ("truncated_tree", truncated_tree, "parity source inventory truncated"),
    ("missing_tree_entry", missing_tree_entry, "parity source file missing"),
    ("duplicate_destination", duplicate_destination, "parity manifest has duplicate destination"),
    ("traversal_in_name", traversal_destination, "parity path is unsafe"),
    ("symlink", symlink_source, "parity source is not a regular file"),
    ("followed_symlink", followed_symlink_source, "parity source is not a regular file"),
)


class ParityBundleTests(unittest.TestCase):
    def assert_positive_fetch(self, client, destination):
        sync(client, destination)
        self.assertTrue(destination.is_dir())

    def test_complete_fetch_writes_exact_payload_descriptor_and_sums(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            sync(client, destination)
            expected_names = {"PARITY-MANIFEST.json", "PARITY-SHA256SUMS"}
            expected_names.update(row["destination"] for row in ROWS)
            self.assertEqual({path.name for path in destination.iterdir()}, expected_names)
            self.assertTrue(all(path.is_file() and not path.is_symlink()
                                for path in destination.iterdir()))
            for row in ROWS:
                self.assertEqual((destination / row["destination"]).read_bytes(),
                                 PAYLOAD_BYTES[row["source"]])
            descriptor_raw = (destination / "PARITY-MANIFEST.json").read_bytes()
            descriptor = json.loads(descriptor_raw)
            expected_payload_hashes = {
                row["destination"]: digest(PAYLOAD_BYTES[row["source"]]) for row in ROWS
            }
            self.assertEqual(set(descriptor), {
                "schema", "source_manifest_sha256", "engine_commit", "payload_sha256"})
            self.assertEqual(descriptor_raw, canonical(descriptor))
            self.assertEqual(descriptor["schema"], "pineforge/parity-descriptor/v1")
            self.assertEqual(descriptor["source_manifest_sha256"], digest(MANIFEST_RAW))
            self.assertEqual(descriptor["engine_commit"], client.source_commit)
            self.assertEqual(descriptor["payload_sha256"], expected_payload_hashes)
            sums = (destination / "PARITY-SHA256SUMS").read_text(encoding="ascii").splitlines()
            pairs = [line.split("  ", 1) for line in sums]
            expected_order = ["PARITY-MANIFEST.json"] + sorted(expected_payload_hashes)
            expected_sums = {"PARITY-MANIFEST.json": digest(descriptor_raw), **expected_payload_hashes}
            self.assertEqual([name for _, name in pairs], expected_order)
            self.assertEqual({name: value for value, name in pairs}, expected_sums)
            self.assertNotIn("PARITY-SHA256SUMS", {name for _, name in pairs})

            # Replacement must install newly fetched bytes, not retain the old directory.
            row = ROWS[0]
            replacement = PAYLOAD_BYTES[row["source"]] + b"updated source bytes\n"
            client.mutate_source_bytes(row["source"], replacement)
            identity = sync(client, destination)
            self.assertEqual((destination / row["destination"]).read_bytes(), replacement)
            expected_payload_hashes[row["destination"]] = digest(replacement)
            self.assertEqual(identity["payload_sha256"], expected_payload_hashes)
            descriptor_raw = (destination / "PARITY-MANIFEST.json").read_bytes()
            self.assertEqual(json.loads(descriptor_raw)["payload_sha256"], expected_payload_hashes)
            expected_sums = {"PARITY-MANIFEST.json": digest(descriptor_raw), **expected_payload_hashes}
            self.assertEqual(
                (destination / "PARITY-SHA256SUMS").read_bytes(),
                "".join(f"{expected_sums[name]}  {name}\n" for name in expected_order).encode("ascii"))

    def test_source_bundle_reads_are_bound_to_resolved_commit(self):
        client = TaggedEngineFixture()
        # Another commit can contain the same blobs. Serve its real content
        # records so an unpinned-read mutant reaches the routing assertion.
        # An import, malformed commit or missing fixture is never its kill.
        for row in ROWS:
            client.api_data[f"repos/{ENGINE_REPO}/contents/{row['source']}?ref={'b' * 40}"] = (
                copy.deepcopy(client.api_data[client.route(row["source"])]))
        seen = []
        original_api = client.api

        def observe(route, **kwargs):
            if "/contents/" in route:
                seen.append(route)
            return original_api(route, **kwargs)

        client.api = observe
        with tempfile.TemporaryDirectory() as temporary:
            sync(client, Path(temporary) / "parity")
        self.assertEqual(len(seen), len(ROWS) + 1)
        self.assertTrue(all(route.endswith("?ref=" + client.source_commit) for route in seen),
                        "commit-pinned source read")

    def test_negative_rows_have_a_green_control_and_fail_closed(self):
        for label, mutate, message in NEGATIVE_ROWS:
            with self.subTest(row=label), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "parity"
                client = TaggedEngineFixture()
                self.assert_positive_fetch(client, destination)
                before = snapshot(destination)
                mutate(client)
                with self.assertRaisesRegex(contract.Invalid, "^" + message + "$", msg=label):
                    sync(client, destination)
                self.assertEqual(snapshot(destination), before)

    def test_partial_fetch_keeps_previous_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            self.assert_positive_fetch(client, destination)
            before = snapshot(destination)
            client.content_calls = 0
            client.fail_after = 2
            with self.assertRaisesRegex(
                    contract.Invalid, "^parity bundle fetch failed; previous bundle preserved$"):
                sync(client, destination)
            self.assertEqual(snapshot(destination), before)

    def test_unsupported_atomic_exchange_has_a_distinct_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            self.assert_positive_fetch(client, destination)
            before = snapshot(destination)
            with patch.object(bundle, "_install_bundle",
                              side_effect=OSError(errno.ENOTSUP, "renameat2 unsupported")):
                with self.assertRaisesRegex(
                        contract.Invalid,
                        "^parity atomic directory exchange unsupported; previous bundle preserved$"):
                    sync(client, destination)
            self.assertEqual(snapshot(destination), before)

    def test_release_mode_rejects_proof_commit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "parity"
            client = TaggedEngineFixture()
            self.assert_positive_fetch(client, destination)
            before = snapshot(destination)
            with self.assertRaisesRegex(
                    contract.Invalid, "^proof commit override refused in release mode$"):
                sync(client, destination, release_mode=True, proof_commit=client.source_commit)
            self.assertEqual(snapshot(destination), before)
            with patch.object(client, "tag_commit", side_effect=contract.Pending("tag not published")):
                identity = sync(client, destination, release_mode=False, proof_commit=client.source_commit)
            self.assertEqual(identity["engine_commit"], client.source_commit)
            self.assertEqual(snapshot(destination), before)

    def test_mutation_rows_name_a_matching_negative_control(self):
        from run_parity_mutations import MUTANTS
        selected = [spec for spec in MUTANTS if spec.test_file == "tests/test_parity_bundle.py"]
        self.assertTrue(selected)
        for spec in selected:
            with self.subTest(mutant=spec.name):
                self.assertTrue(callable(getattr(type(self), spec.test_name.split(".")[-1])))
                source = (ROOT / spec.relative).read_text(encoding="utf-8")
                self.assertEqual(source.count(spec.old), 1)
                if spec.subtest_row is not None:
                    self.assertIn(spec.subtest_row, {label for label, _, _ in NEGATIVE_ROWS})


if __name__ == "__main__":
    unittest.main()
