#!/usr/bin/env python3
"""Release state-machine and byte-identity regressions, run in CI/cloud only.

The fakes model external storage and registry effects, not readiness decisions.
The actual producer/package validators, finalizer, payload and receipt verifier
run against nonempty fixtures. A missing gate must make a negative control fail.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# These imports use the repository scripts path established above.
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pair as pair  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
import release_receipts as receipts  # noqa: E402
import release_rollback as rollback  # noqa: E402
import release_sources as sources  # noqa: E402
import consumer_receipt_example as receipt_example  # noqa: E402


def fixture_manifest(v="1.6.0"):
    obj = contract.decode((ROOT / "tests/fixtures/release-manifest.json").read_bytes())
    obj["release_version"] = v
    if v != "1.6.0":
        obj["hub_commit"] = hashlib.sha1(v.encode()).hexdigest()
    obj["prerelease"] = pair.parse(v).prerelease
    for role, pin in obj["versions"].items():
        pin["version"] = v
        if "tag" in pin:
            pin["tag"] = "v" + (pair.parse(v).pep440 if role == "hpo" else v)
        for key in ("pypi_version", "package_version"):
            if key in pin:
                pin[key] = pair.parse(v).pep440
        if "npm_version" in pin:
            pin["npm_version"] = v
    if not pair.hpo_pairing(pair.parse(v)):
        obj["versions"]["hpo"] = None
    obj["facts"]["release_key"] = v
    return obj


def facts_for(manifest):
    facts = contract.decode((ROOT / "tests/fixtures/repo-descriptions-facts.json").read_bytes())
    entry = copy.deepcopy(facts["releases"][next(iter(facts["releases"]))])
    key = manifest["release_version"]
    entry["source"] = f"https://github.com/pineforge-4pass/pineforge-engine/blob/v{key}/README.md#validation-scoreboard"
    for role in ("engine", "codegen"):
        entry["scoreboard"][role + "Commit"] = manifest["versions"][role]["commit"]
        entry["scoreboard"]["provenance"][role + "Commit"] = manifest["versions"][role]["commit"]
    facts["releases"][key] = entry
    return facts


def bootstrap_previous():
    """Independent literal transcription of the reviewed pre-manifest form."""
    return {
        "provenance": "pre-manifest-bootstrap",
        "package": "pineforge-data",
        "version": "0.3.0",
        "source": "fb5cec39b6c6d70af103df0df182d47bb5812191",
        "image_digest": "sha256:b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86",
        "deployment_id_sha256": "12ffb9b3d3cd270604b06f558ae73a9eb0ca1e2f82a84d45728232091d3e3307",
        "distributions": {
            "pineforge_data-0.3.0-py3-none-any.whl": "609b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc",
            "pineforge_data-0.3.0.tar.gz": "f29a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc",
        },
    }


def candidate(manifest):
    pins = copy.deepcopy(manifest["versions"])
    for name in ("npm_version", "npm_integrity"):
        pins["codegen"].pop(name)
    return {"schema": "pineforge/release-candidate/v1", "release_version": manifest["release_version"],
            "hub_commit": manifest["hub_commit"], "versions": pins,
            "inventories": {"codegen": {"pineforge_codegen/__init__.py": "0" * 64}},
            "image": manifest["image"], "run_id": manifest["run_id"],
            "verification": {arch: {"backtest_sha256": "8" * 64, "trades": 3,
                                   **({"hpo_trials": 9, "hpo_sha256": "9" * 64} if pins["hpo"] else {})}
                             for arch in ("amd64", "arm64")},
            "sources": {"release_tag": "v1.4.0", "list_asset": "set-02.SOURCES.list",
                        "list_sha256": "a" * 64, "amd64_sha256": "b" * 64, "arm64_sha256": "c" * 64}}


def expected_pair_payload(manifest_raw, candidate_raw):
    """Independent P04/P05 mapping; no production payload/hash helpers."""
    manifest, built = json.loads(manifest_raw), json.loads(candidate_raw)
    base = ("https://github.com/pineforge-4pass/pineforge-release/releases/download/v"
            + manifest["release_version"] + "/")
    return {
        "pair_id": manifest["release_version"],
        "engine_tag": manifest["versions"]["engine"]["tag"],
        "codegen_tag": "v" + manifest["versions"]["codegen"]["version"],
        "hpo_tag": manifest["versions"]["hpo"]["tag"] if manifest["versions"]["hpo"] else None,
        "receipt_url": base + "release-manifest.json",
        "receipt_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "source_sets": [built["sources"]],
        "build": {
            "prerelease": manifest["prerelease"], "hub_commit": manifest["hub_commit"],
            "run_id": manifest["run_id"],
            "image": {key: manifest["image"][key]
                      for key in ("repository", "digest", "amd64_digest", "arm64_digest")},
            "candidate_url": base + "release-candidate.json",
            "candidate_sha256": hashlib.sha256(candidate_raw).hexdigest(),
        },
    }


class MemoryStore:
    def __init__(self, *manifests):
        self.rows, self.assets, self.operations = {}, {}, []
        for obj in manifests:
            tag = "v" + obj["release_version"]
            self.rows[tag] = {"id": len(self.rows) + 1, "tag_name": tag, "draft": True,
                              "created_at": "2026-10-16T00:00:00Z", "assets": []}
            self.put(self.rows[tag], pipeline.CANDIDATE, contract.canonical(candidate(obj)))
        self.operations.clear()

    def get(self, tag):
        return pipeline.Store(self).get(tag)

    def pages(self, route):
        if route != f"repos/{contract.HUB}/releases":
            raise AssertionError("unmodeled paginated hub route " + route)
        return self.releases()

    def api(self, route, *, method="GET", data=None):
        prefix = f"repos/{contract.HUB}/releases"
        if route == prefix and method == "POST":
            row = {**data, "id": len(self.rows) + 1, "assets": [], "created_at": "2026-10-16T00:00:00Z"}
            self.rows[row["tag_name"]] = row
            self.operations.append(("create", row["tag_name"]))
            return row
        if route.startswith(prefix + "/tags/"):
            row = self.rows.get(route.removeprefix(prefix + "/tags/"))
            # Match GitHub: a draft exists in releases(), but not releases/tags.
            if row is None or row["draft"]:
                raise contract.Pending("published release unavailable")
            return row
        if route.startswith(prefix + "/"):
            return next(row for row in self.rows.values() if str(row["id"]) == route.removeprefix(prefix + "/"))
        raise AssertionError("unmodeled hub route " + route)

    def request(self, url, **kwargs):
        prefix = f"https://api.github.com/repos/{contract.HUB}/releases/assets/"
        if url.startswith(prefix):
            asset_id = int(url.removeprefix(prefix))
            for row in self.rows.values():
                for asset in row["assets"]:
                    if asset["id"] == asset_id:
                        return self.read(row, asset["name"])
        raise AssertionError("unmodeled hub asset request " + url)

    def facts(self):
        return pipeline.Store(self).facts()

    def releases(self):
        return list(self.rows.values())

    def read(self, row, name):
        return self.assets.get((row["tag_name"], name))

    def put(self, row, name, raw):
        key = row["tag_name"], name
        if key in self.assets:
            contract.require(self.assets[key] == raw, "immutable asset changed")
        else:
            self.assets[key] = raw
            row["assets"].append({"name": name, "id": len(self.assets), "state": "uploaded",
                                  "digest": "sha256:" + contract.sha256(raw)})
            self.operations.append(("asset", name))

    def publish(self, row, latest):
        row["draft"] = False
        self.operations.append(("publish", row["tag_name"], latest))


class MemoryRegistry:
    def __init__(self):
        self.tags = {"latest": "sha256:" + "0" * 64, "1.6": "sha256:" + "0" * 64}
        self.operations = []

    def digest(self, tag):
        return self.tags.get(tag)

    def point(self, tag, digest, fixed=False):
        if fixed:
            contract.require(self.tags.get(tag, digest) == digest, "fixed image changed")
        self.tags[tag] = digest
        self.operations.append((tag, digest, fixed))


class FinalizeClient:
    def __init__(self, *manifests):
        self.manifests = {"v" + m["release_version"]: m for m in manifests}
        self.facts = facts_for(manifests[0])
        for m in manifests[1:]:
            self.facts["releases"].update(facts_for(m)["releases"])

    def tag_commit(self, repo, tag):
        contract.require(repo == contract.HUB, "unexpected repository")
        return self.manifests[tag]["hub_commit"]

    def api(self, route, **kwargs):
        if route == f"repos/{contract.HUB}/git/ref/heads/main":
            return {"object": {"sha": "e" * 40}}
        if route == f"repos/{contract.HUB}/commits?path=facts/facts.json&sha=main&per_page=1":
            return [{"sha": "e" * 40, "commit": {"committer": {"date": "2026-10-16T00:00:00Z"}}}]
        raise AssertionError("unmodeled API request " + route)

    def file(self, repo, commit, path):
        if (repo, commit, path) == (contract.HUB, "e" * 40, "facts/facts.json"):
            return contract.canonical(self.facts)
        raise AssertionError("unmodeled immutable source request")


class ManifestTests(unittest.TestCase):
    def test_section_32_exact_fields_types_and_non_circular_identity(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        event = contract.payload(raw)
        self.assertEqual(len(event), 10)
        self.assertEqual(event["event_id"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(event["manifest"], {"asset": "release-manifest.json", "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertNotIn("manifest", contract.decode(raw))
        self.assertNotIn("event_id", contract.decode(raw))
        self.assertIs(type(event["prerelease"]), bool)
        self.assertIs(type(event["run_id"]), int)
        contract.validate_payload(event, raw)

    def test_image_repository_accepts_exact_old_and_new_organization_paths(self):
        for organization, repository in (
                ("old", "ghcr.io/pineforge-4pass/pineforge-release"),
                ("new", "ghcr.io/tunaxis/pineforge-release")):
            with self.subTest(organization=organization):
                obj = fixture_manifest()
                obj["image"]["repository"] = repository
                raw = contract.canonical(obj)
                event = contract.payload(raw)
                self.assertEqual(event["image"]["repository"], repository)
                contract.validate_payload(event, raw)

    def test_image_repository_rejects_other_paths_and_wrong_types(self):
        for case, repository in (
                ("third_organization", "ghcr.io/other/pineforge-release"),
                ("old_organization_other_repository", "ghcr.io/pineforge-4pass/other"),
                ("new_organization_other_repository", "ghcr.io/tunaxis/other"),
                ("empty", ""),
                ("different_case", "GHCR.IO/tunaxis/pineforge-release"),
                ("trailing_slash", "ghcr.io/tunaxis/pineforge-release/"),
                ("null", None),
                ("list", ["ghcr.io/tunaxis/pineforge-release"]),
                ("object", {"repository": "ghcr.io/tunaxis/pineforge-release"}),
                ("integer", 1)):
            with self.subTest(case=case, repository=repository):
                obj = fixture_manifest()
                obj["image"]["repository"] = repository
                with self.assertRaisesRegex(contract.Invalid, "^unexpected image repository$"):
                    contract.payload(contract.canonical(obj))

    def test_producer_image_stays_on_old_organization_path(self):
        self.assertEqual(contract.IMAGE, "ghcr.io/pineforge-4pass/pineforge-release")

    def test_red_old_version_only_payload_cannot_authorize_a_consumer(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        with self.assertRaises(contract.Invalid):
            contract.validate_payload({"release_version": "1.6.0", "prerelease": False, "run_id": 123456}, raw)

    def test_reject_corrupt_identity_contradictory_channel_and_wrong_types(self):
        for path, value in ((["prerelease"], "false"), (["prerelease"], True), (["run_id"], True),
                            (["hub_commit"], "main"), (["image", "repository"], "ghcr.io/other/release"),
                            (["facts", "path"], "other.json"), (["versions", "hpo", "package_version"], "1.6.0rc1")):
            with self.subTest(path=path):
                obj = fixture_manifest()
                target = obj
                for name in path[:-1]:
                    target = target[name]
                target[path[-1]] = value
                with self.assertRaises((ValueError, contract.Invalid)):
                    contract.payload(contract.canonical(obj))

    def test_hpo_cutoff_and_pep440_are_shared_with_pairing(self):
        for v in ("1.5.9", "1.6.0-beta.1", "1.6.0-rc.1", "1.6.0"):
            event = contract.payload(contract.canonical(fixture_manifest(v)))
            if pair.hpo_pairing(pair.parse(v)):
                self.assertEqual(event["versions"]["hpo"]["package_version"], pair.parse(v).pep440)
            else:
                self.assertIsNone(event["versions"]["hpo"])

    def test_bytes_are_not_reserialized_on_replay(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        with self.assertRaises(contract.Invalid):
            contract.payload(json.dumps(contract.decode(raw), indent=2).encode())
        with self.assertRaises(contract.Invalid):
            contract.decode(b'{"run_id":1,"run_id":2}')


class PairPayloadTests(unittest.TestCase):
    def test_event_types_cover_all_roles_and_preserve_legacy_events(self):
        legacy = {"offline", "hosted", "web", "data", "license"}
        self.assertEqual(set(contract.ROLES), legacy | {"application", "compute"})
        self.assertEqual(set(contract.EVENT_TYPES), set(contract.ROLES))
        self.assertEqual(contract.EVENT_TYPES,
                         {**dict.fromkeys(legacy, "pineforge-release"),
                          "application": "release-pair", "compute": "release-pair"})
        self.assertEqual(contract.DELIVERY_ONLY, ("compute",))
        self.assertEqual(set(contract.RECEIPT_ROLES), set(contract.ROLES) - {"compute"})

    def test_payload_matches_fixture_bytes_and_independent_p04_mapping(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        ready = json.loads((ROOT / "tests/fixtures/release-ready.json").read_bytes())
        candidate_raw = contract.canonical(candidate(json.loads(raw)))
        expected = expected_pair_payload(raw, candidate_raw)
        actual = contract.pair_payload(raw, candidate_raw)
        self.assertEqual(set(actual), {"pair_id", "engine_tag", "codegen_tag", "hpo_tag",
                                      "receipt_url", "receipt_sha256", "source_sets", "build"})
        self.assertEqual(actual, expected)
        self.assertEqual(actual["receipt_sha256"], ready["manifest"]["sha256"])
        self.assertEqual(actual["pair_id"], ready["release_version"])
        self.assertEqual(actual["build"]["image"], ready["image"])
        self.assertNotIn("npm_version", json.loads(candidate_raw)["versions"]["codegen"])
        self.assertEqual(set(actual["build"]), {"prerelease", "hub_commit", "run_id", "image",
                                              "candidate_url", "candidate_sha256"})

    def test_noncanonical_npm_integrity_is_rejected_before_dispatch(self):
        manifest = fixture_manifest()
        manifest["versions"]["codegen"]["npm_integrity"] = (
            "sha512-o1hmSb6FwjkF8lIwLhENJV+YPbYqSwG1KPvHyViPj5Yan3aHoNRC"
            "pM5uINaYbjZKjGS95VDClmASOUSD6ZktVx==")
        raw = contract.canonical(manifest)
        candidate_raw = contract.canonical(candidate(manifest))
        for entry_point, args in ((contract.payload, (raw,)),
                                  (contract.pair_payload, (raw, candidate_raw))):
            with self.subTest(entry_point=entry_point.__name__):
                with self.assertRaisesRegex(contract.Invalid, "^invalid npm integrity$"):
                    entry_point(*args)

    def test_canonical_npm_integrity_is_preserved_by_dispatch_payloads(self):
        manifest = fixture_manifest()
        integrity = (
            "sha512-o1hmSb6FwjkF8lIwLhENJV+YPbYqSwG1KPvHyViPj5Yan3aHoNRC"
            "pM5uINaYbjZKjGS95VDClmASOUSD6ZktVw==")
        manifest["versions"]["codegen"]["npm_integrity"] = integrity
        raw = contract.canonical(manifest)
        candidate_raw = contract.canonical(candidate(manifest))
        event = contract.payload(raw)
        self.assertEqual(event["versions"]["codegen"]["npm_integrity"], integrity)
        self.assertEqual(event["event_id"], "sha256:" + hashlib.sha256(raw).hexdigest())
        contract.validate_payload(event, raw)
        self.assertEqual(contract.pair_payload(raw, candidate_raw),
                         expected_pair_payload(raw, candidate_raw))

    def test_pair_payload_preserves_cutoff_and_prerelease_tag_spellings(self):
        for release in ("1.5.9", "1.6.0-beta.1", "1.6.0-rc.1", "1.6.0"):
            with self.subTest(release=release):
                manifest = fixture_manifest(release)
                raw, candidate_raw = contract.canonical(manifest), contract.canonical(candidate(manifest))
                actual = contract.pair_payload(raw, candidate_raw)
                self.assertEqual(actual, expected_pair_payload(raw, candidate_raw))
                self.assertEqual(actual["build"]["prerelease"], manifest["prerelease"])
                self.assertEqual(actual["hpo_tag"],
                                 manifest["versions"]["hpo"]["tag"] if manifest["versions"]["hpo"] else None)

    def test_pair_payload_hashes_untouched_candidate_bytes(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        built = candidate(json.loads(raw))
        candidate_raw = (json.dumps(built, indent=2) + "\n").encode()
        actual = contract.pair_payload(raw, candidate_raw)
        self.assertEqual(actual, expected_pair_payload(raw, candidate_raw))
        self.assertNotEqual(actual["build"]["candidate_sha256"],
                            hashlib.sha256(contract.canonical(built)).hexdigest())
        self.assertEqual(actual["receipt_sha256"], hashlib.sha256(raw).hexdigest())

    def test_pair_payload_uses_the_producer_source_tag_rule(self):
        manifest = fixture_manifest()
        raw = contract.canonical(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "SOURCE-SETS.tsv"
            for tag in ("v1.4.0-preview.1", "v1.4.0-nightly-2026.10", "v01.04.00", "v1.4.0-preview/1"):
                built = candidate(manifest)
                built["sources"]["release_tag"] = tag
                registry.write_text(
                    sources.REGISTRY_HEADER + "\n"
                    + f"{'a' * 64}\t{tag}\t84\t{'b' * 64}\tset-02.SOURCES.list\n",
                    encoding="utf-8")
                with self.subTest(tag=tag):
                    if tag == "v1.4.0-preview/1":
                        with self.assertRaisesRegex(sources.SourcesError, "release tag must be a v-prefixed version"):
                            sources.registry_set(registry, "a" * 64)
                        with self.assertRaisesRegex(contract.Invalid, "^malformed identity$"):
                            contract.pair_payload(raw, contract.canonical(built))
                    else:
                        produced = sources.registry_set(registry, "a" * 64)
                        self.assertEqual(produced.release_tag, tag)
                        candidate_raw = contract.canonical(built)
                        actual = contract.pair_payload(raw, candidate_raw)
                        self.assertEqual(actual, expected_pair_payload(raw, candidate_raw))
                        self.assertEqual(actual["source_sets"][0]["release_tag"], produced.release_tag)

    def test_pair_payload_refuses_image_hub_commit_and_producer_mismatches(self):
        manifest = fixture_manifest()
        raw = contract.canonical(manifest)
        changes = (
            (("image", "digest"), "sha256:" + "f" * 64, "manifest does not bind this candidate"),
            (("hub_commit",), "f" * 40, "manifest does not bind this candidate"),
            *((("versions", role, "commit"), "f" * 40,
               "manifest producer identity differs from candidate") for role in ("engine", "codegen", "hpo")),
        )
        for path, value, message in changes:
            built = copy.deepcopy(candidate(manifest))
            target = built
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
                contract.pair_payload(raw, contract.canonical(built))

    def test_pair_payload_refuses_a_different_release_or_build_run(self):
        manifest = fixture_manifest()
        for built in (candidate(fixture_manifest("1.6.1")), {**candidate(manifest), "run_id": manifest["run_id"] + 1}):
            with self.subTest(release=built["release_version"], run=built["run_id"]), \
                    self.assertRaisesRegex(contract.Invalid, "^manifest does not bind this candidate$"):
                contract.pair_payload(contract.canonical(manifest), contract.canonical(built))

    def test_pair_payload_refuses_private_identifiers_and_extra_fields(self):
        changes = (
            ("manifest", ("image", "repository"), "ghcr.io/private-project/private-image", "unexpected image repository"),
            ("manifest", ("hub_commit",), "projects/private-project", "malformed identity"),
            ("candidate", ("sources", "release_tag"), "private-workload", "malformed identity"),
            ("candidate", ("sources", "list_asset"), "gs://private-project/SOURCES.list", "malformed identity"),
            ("candidate", ("sources", "project"), "private-project", "unexpected object fields"),
        )
        for source, path, value, message in changes:
            manifest = fixture_manifest()
            built = copy.deepcopy(candidate(manifest))
            target = manifest if source == "manifest" else built
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(source=source, path=path), self.assertRaisesRegex(contract.Invalid, "^" + message + "$"):
                contract.pair_payload(contract.canonical(manifest), contract.canonical(built))

    def test_pair_payload_keeps_candidate_smoke_and_source_digest_gates(self):
        manifest = fixture_manifest()
        for path, value in ((("verification", "arm64", "trades"), 0),
                            (("verification", "amd64", "hpo_trials"), 8),
                            (("sources", "amd64_sha256"), "private-project")):
            built = copy.deepcopy(candidate(manifest))
            target = built
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(contract.Invalid):
                contract.pair_payload(contract.canonical(manifest), contract.canonical(built))

    def test_pair_payload_refuses_noncanonical_manifest_bytes(self):
        manifest = fixture_manifest()
        raw = (json.dumps(manifest, indent=2) + "\n").encode()
        with self.assertRaisesRegex(contract.Invalid, "^noncanonical manifest bytes$"):
            contract.pair_payload(raw, contract.canonical(candidate(manifest)))


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.m = fixture_manifest()
        self.client = FinalizeClient(self.m)
        self.store = MemoryStore(self.m)
        self.registry = MemoryRegistry()
        self.row = self.store.get("v1.6.0")
        # Only npm transport is substituted; manifest/facts/candidate rules run.
        self.npm = patch.object(pipeline, "npm", side_effect=lambda c, pin, inventory: {
            **pin, "npm_version": pin["version"], "npm_integrity": self.m["versions"]["codegen"]["npm_integrity"]})
        self.npm.start()
        self.addCleanup(self.npm.stop)

    def finish(self):
        return pipeline.finalize(self.client, self.store, self.registry, self.row)

    def plan(self, release="1.6.0", *, enabled="compute", first="1.6.0"):
        manifest = fixture_manifest(release)
        client, store, registry = FinalizeClient(manifest), MemoryStore(manifest), MemoryRegistry()
        row = store.get("v" + release)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": enabled, "RELEASE_COMPUTE_FIRST_PAIR": first,
                "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "age_minutes", return_value=0), patch.object(pipeline, "report_issue"):
            output = Path(tmp) / "deliveries.json"
            pipeline.finalize_all(client, store, registry, row["tag_name"], output)
            deliveries = json.loads(output.read_bytes())
        status = json.loads(store.read(row, "release-status-100-1.json"))
        return deliveries, status, store

    def test_draft_is_invisible_by_tag_but_found_by_real_store_and_candidate_probe(self):
        with self.assertRaises(contract.Pending):
            self.store.api(f"repos/{contract.HUB}/releases/tags/v1.6.0")
        store = pipeline.Store(self.store)
        self.assertTrue(store.get("v1.6.0")["draft"])
        self.assertTrue(pipeline.candidate_exists(store, "v1.6.0"))
        self.assertFalse(pipeline.candidate_exists(store, "v1.6.1"))
        event = pipeline.finalize(self.client, self.store, self.registry, store.get("v1.6.0"))
        self.assertEqual(event["release_version"], "1.6.0")

    def test_existing_draft_never_creates_a_second_candidate_or_requests_a_rebuild(self):
        store = pipeline.Store(self.store)
        for _ in range(2):
            row = store.ensure_candidate_release("v1.6.0", "replay")
            self.assertEqual(row["id"], self.row["id"])
            decision, _ = pair.decide("hpo", "1.6.0", "false", "1.6.0", "1.6.0", "1.6.0",
                                      True, False, False, prev_hpo="1.6.0", candidate_exists=pipeline.candidate_exists(store, "v1.6.0"))
            self.assertEqual(decision["mode"], "noop")
        self.assertEqual(len(store.releases()), 1)
        self.assertNotIn("create", [op[0] for op in self.store.operations])

    def test_missing_npm_and_missing_facts_leave_all_aliases_and_release_unpublished(self):
        for missing in ("npm", "facts"):
            with self.subTest(missing=missing):
                if missing == "npm":
                    context = patch.object(pipeline, "npm", side_effect=contract.Pending("npm missing"))
                else:
                    self.client.facts["releases"].pop("1.6.0")
                    context = patch.object(self.client, "file", return_value=contract.canonical(self.client.facts))
                with context, self.assertRaises(contract.Pending):
                    self.finish()
                self.assertEqual(self.registry.operations, [])
                self.assertIsNone(self.store.read(self.row, contract.ASSET))
                self.assertTrue(self.row["draft"])

    def test_facts_bind_release_evidence_not_active_scoreboard(self):
        event = self.finish()
        self.assertEqual(event["facts"]["sha256"], contract.sha256(contract.canonical(self.client.facts)))
        self.assertNotEqual(self.client.facts["scoreboard"]["engineCommit"], self.m["versions"]["engine"]["commit"])
        self.assertFalse(self.row["draft"])
        self.assertEqual(self.registry.tags["latest"], self.m["image"]["digest"])

    def test_wrong_facts_source_and_failed_architecture_verification_are_red(self):
        self.client.facts["releases"]["1.6.0"]["scoreboard"]["engineCommit"] = "f" * 40
        with self.assertRaises(ValueError):
            self.finish()
        self.assertEqual(self.registry.operations, [])
        self.client = FinalizeClient(self.m)
        obj = candidate(self.m)
        obj["verification"]["arm64"]["trades"] = 0
        self.store.assets[("v1.6.0", pipeline.CANDIDATE)] = contract.canonical(obj)
        with self.assertRaises(contract.Invalid):
            self.finish()
        self.assertEqual(self.registry.operations, [])

    def test_duplicate_replay_keeps_manifest_bytes_event_and_original_run_id(self):
        first = self.finish()
        original = self.store.read(self.row, contract.ASSET)
        with patch.object(pipeline, "npm", side_effect=AssertionError("must not re-resolve packages")), \
                patch.object(self.client, "file", side_effect=AssertionError("must not bind newer facts")):
            second = self.finish()
        self.assertEqual(original, self.store.read(self.row, contract.ASSET))
        self.assertEqual(first, second)

    def test_older_completion_and_its_receipt_cannot_move_newer_alias_or_consumer(self):
        newer = fixture_manifest("1.6.1")
        newer["image"]["digest"] = "sha256:" + "a" * 64
        self.client = FinalizeClient(self.m, newer)
        self.store = MemoryStore(self.m, newer)
        pipeline.finalize(self.client, self.store, self.registry, self.store.get("v1.6.1"))
        old = self.finish()
        self.assertEqual(self.registry.tags["latest"], newer["image"]["digest"])
        status = pipeline.aggregate(self.store, self.row, old, enabled=contract.ROLES, newest=pair.parse("1.6.1"))
        self.assertEqual(status["consumers"],
                         {**dict.fromkeys(contract.RECEIPT_ROLES, "superseded"), "compute": "pending"})
        self.assertFalse(status["complete"])

    def test_dispatch_and_missing_receipt_never_mean_verified(self):
        event = self.finish()
        self.store.put(self.row, "delivery-offline-42-1.json", contract.canonical({"state": "dispatch_accepted"}))
        status = pipeline.aggregate(self.store, self.row, event, enabled=contract.ROLES)
        self.assertEqual(set(status["consumers"].values()), {"pending"})
        self.assertEqual(set(status["consumers"]), set(contract.ROLES))
        self.assertEqual(status["consumers"]["compute"], "pending")
        self.assertFalse(status["complete"])

    def test_prerelease_never_moves_latest_and_has_explicit_policy_inventory(self):
        m = fixture_manifest("1.6.0-rc.1")
        self.client, self.store = FinalizeClient(m), MemoryStore(m)
        self.client.facts["releases"].pop("1.6.0-rc.1")
        event = pipeline.finalize(self.client, self.store, self.registry, self.store.get("v1.6.0-rc.1"))
        self.assertEqual(self.registry.tags["latest"], "sha256:" + "0" * 64)
        status = pipeline.aggregate(self.store, self.store.get("v1.6.0-rc.1"), event, enabled=contract.ROLES)
        self.assertEqual(status["consumers"]["application"], "skipped_by_policy")
        self.assertEqual(status["consumers"]["offline"], "pending")
        self.assertEqual(status["consumers"]["compute"], "pending")
        facts = contract.canonical(self.client.facts)
        self.assertEqual(contract.retain_facts(event, facts, facts), event["facts"]["sha256"])
        with self.assertRaises(contract.Invalid):
            contract.retain_facts(event, facts, facts + b"\n")

    def test_aggregate_ignores_disabled_roles_for_completion_and_overdue_inventory(self):
        event = self.finish()
        empty = pipeline.aggregate(self.store, self.row, event)
        self.assertEqual(empty["consumers"], {})
        self.assertTrue(empty["complete"])
        status = pipeline.aggregate(self.store, self.row, event, enabled=("offline",))
        self.assertEqual(status["consumers"], {"offline": "pending"})

    def test_compute_delivery_completes_a_release_with_only_compute_pending(self):
        event = self.finish()
        for role in contract.RECEIPT_ROLES:
            self.store.put(self.row, f"consumer-{role}-1-1.json", contract.canonical({
                "event_id": event["event_id"], "consumer": role, "attempt": 1, "sequence": 1, "state": "verified"}))
        pending = pipeline.aggregate(self.store, self.row, event, enabled=contract.ROLES)
        self.assertEqual(pending["consumers"],
                         {**dict.fromkeys(contract.RECEIPT_ROLES, "verified"), "compute": "pending"})
        self.assertFalse(pending["complete"])
        self.store.put(self.row, "delivery-compute.json", contract.canonical({
            "event_id": event["event_id"], "consumer": "compute", "state": "dispatch_accepted"}))
        delivered = pipeline.aggregate(self.store, self.row, event, enabled=contract.ROLES)
        self.assertEqual(delivered["consumers"],
                         {**dict.fromkeys(contract.RECEIPT_ROLES, "verified"), "compute": "delivered"})
        self.assertTrue(delivered["complete"])

    def test_compute_is_never_superseded_before_or_after_delivery(self):
        event = self.finish()
        for accepted in (False, True):
            if accepted:
                self.store.put(self.row, "delivery-compute.json", contract.canonical({
                    "event_id": event["event_id"], "consumer": "compute", "state": "dispatch_accepted"}))
            with self.subTest(accepted=accepted):
                status = pipeline.aggregate(self.store, self.row, event, enabled=contract.ROLES,
                                            newest=pair.parse("1.6.1"))
                self.assertEqual(status["consumers"],
                                 {**dict.fromkeys(contract.RECEIPT_ROLES, "superseded"),
                                  "compute": "delivered" if accepted else "pending"})
                self.assertEqual(status["complete"], accepted)

    def test_compute_prerelease_delivery_completes_while_application_keeps_policy_skip(self):
        manifest = fixture_manifest("1.6.0-rc.1")
        store = MemoryStore(manifest)
        row = store.get("v1.6.0-rc.1")
        event = contract.payload(contract.canonical(manifest))
        pending = pipeline.aggregate(store, row, event, enabled=("compute", "application"))
        self.assertEqual(pending["consumers"], {"compute": "pending", "application": "skipped_by_policy"})
        self.assertFalse(pending["complete"])
        store.put(row, "delivery-compute.json", contract.canonical({
            "event_id": event["event_id"], "consumer": "compute", "state": "dispatch_accepted"}))
        delivered = pipeline.aggregate(store, row, event, enabled=("compute", "application"))
        self.assertEqual(delivered["consumers"], {"compute": "delivered", "application": "skipped_by_policy"})
        self.assertTrue(delivered["complete"])

    def test_compute_delivery_closes_finalizer_incident_without_a_consumer_receipt(self):
        event = self.finish()
        self.store.put(self.row, "delivery-compute.json", contract.canonical({
            "event_id": event["event_id"], "consumer": "compute", "state": "dispatch_accepted"}))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "compute", "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0",
                "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "age_minutes", return_value=0), patch.object(pipeline, "report_issue") as report:
            output = Path(tmp) / "deliveries.json"
            pipeline.finalize_all(self.client, self.store, self.registry, "v1.6.0", output)
            self.assertEqual(json.loads(output.read_bytes()), [])
            status = json.loads(self.store.read(self.row, "release-status-100-1.json"))
            self.assertEqual(status["consumers"], {"compute": "delivered"})
            self.assertTrue(status["complete"])
            self.assertEqual(status["stale"], [])
            self.assertTrue(report.call_args.kwargs["complete"])
            self.assertIn("Delivery-only roles complete at delivered (transport acceptance).",
                          report.call_args.args[3])
        self.assertFalse(any(a["name"].startswith("delivery-attempt-") for a in self.row["assets"]))

    def test_compute_off_incident_body_is_byte_identical_to_base_literal(self):
        legacy = "offline,hosted,application,web,data,license"
        for enabled, first in ((legacy, "1.6.0"), (legacy + ",compute", ""),
                               (legacy + ",compute", "1.6.1")):
            store = MemoryStore(self.m)
            row = store.get("v1.6.0")
            store.put(row, contract.ASSET, (ROOT / "tests/fixtures/release-manifest.json").read_bytes())
            with self.subTest(enabled=enabled, first=first), tempfile.TemporaryDirectory() as tmp, \
                    patch.dict(os.environ, {
                        "RELEASE_READY_CONSUMERS": enabled, "RELEASE_COMPUTE_FIRST_PAIR": first,
                        "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                        "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                    patch.object(pipeline, "age_minutes", return_value=0), \
                    patch.object(pipeline, "report_issue") as report:
                pipeline.finalize_all(self.client, store, MemoryRegistry(), "v1.6.0",
                                      Path(tmp) / "deliveries.json")
                incidents = [call for call in report.call_args_list if call.args[1] == "v1.6.0"]
                self.assertEqual(len(incidents), 1)
                self.assertEqual(incidents[0].args[3].encode("utf-8"), (
                    b"Event: `sha256:12a79d3776c447b1caf9513c96db3407b1d3cd2d71a510081e6763e994c2288e`\n\n"
                    b"- offline: pending\n- hosted: pending\n- application: pending\n"
                    b"- web: pending\n- data: pending\n- license: pending\n\n"
                    b"Dispatch acceptance is transport only. "
                    b"Verified means checked consumer receipt and served-state evidence."))

    def test_compute_plan_is_off_when_first_pair_is_empty(self):
        deliveries, status, store = self.plan(first="")
        self.assertEqual(deliveries, [])
        self.assertEqual(status["consumers"], {})
        self.assertTrue(status["complete"])
        self.assertFalse(any(name.startswith("delivery-attempt-") for _, name in store.assets))

    def test_compute_plan_requires_ready_consumer_membership(self):
        deliveries, status, store = self.plan(enabled="")
        self.assertEqual(deliveries, [])
        self.assertEqual(status["consumers"], {})
        self.assertTrue(status["complete"])
        self.assertFalse(any(name.startswith("delivery-attempt-") for _, name in store.assets))

    def test_compute_plan_excludes_pairs_below_the_semver_floor(self):
        for release, first in (("1.6.0-rc.2", "1.6.0-rc.10"),
                               ("1.6.0-rc.10", "1.6.0"), ("1.9.9", "1.10.0")):
            with self.subTest(release=release, first=first):
                deliveries, status, store = self.plan(release, first=first)
                self.assertEqual(deliveries, [])
                self.assertEqual(status["consumers"], {})
                self.assertTrue(status["complete"])
                self.assertFalse(any(name.startswith("delivery-attempt-compute-") for _, name in store.assets))

    def test_compute_plan_reserves_one_delivery_at_or_above_the_semver_floor(self):
        for release, first in (("1.6.0", "1.6.0"), ("1.6.0-rc.10", "1.6.0-rc.2"),
                               ("1.6.0", "1.6.0-rc.10"), ("1.10.0", "1.9.9")):
            with self.subTest(release=release, first=first):
                deliveries, status, store = self.plan(release, first=first)
                self.assertEqual(deliveries, [{"consumer": "compute", "tag": "v" + release, "attempt": 1}])
                self.assertEqual(status["consumers"], {"compute": "pending"})
                self.assertFalse(status["complete"])
                attempts = [name for _, name in store.assets if name.startswith("delivery-attempt-compute-")]
                self.assertEqual(attempts, ["delivery-attempt-compute-1.json"])

    def test_application_plan_keeps_its_stable_and_prerelease_policy_with_compute_off(self):
        for release, expected in (("1.6.0", "pending"), ("1.6.0-rc.1", "skipped_by_policy")):
            with self.subTest(release=release):
                deliveries, status, store = self.plan(release, enabled="application,compute", first="")
                self.assertEqual(deliveries, [{"consumer": "application", "tag": "v" + release, "attempt": 1}]
                                 if expected == "pending" else [])
                self.assertEqual(status["consumers"], {"application": expected})
                self.assertFalse(any("compute" in name for _, name in store.assets))

    def test_compute_plan_includes_an_older_pair_despite_a_newer_ready_release(self):
        manifests = [fixture_manifest(v) for v in ("1.6.0", "1.6.1")]
        client, store, registry = FinalizeClient(*manifests), MemoryStore(*manifests), MemoryRegistry()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "compute", "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0",
                "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "age_minutes", return_value=0), patch.object(pipeline, "report_issue"):
            output = Path(tmp) / "deliveries.json"
            pipeline.finalize_all(client, store, registry, "", output)
            self.assertEqual(json.loads(output.read_bytes()),
                             [{"consumer": "compute", "tag": "v1.6.1", "attempt": 1},
                              {"consumer": "compute", "tag": "v1.6.0", "attempt": 1}])

    def test_compute_plan_refuses_a_noncanonical_first_pair(self):
        legacy = "offline,hosted,application,web,data,license"
        manifests = [fixture_manifest(v) for v in ("1.6.0", "1.6.1", "1.6.2-rc.1")]

        def run(first, enabled, *, github_output_enabled=True):
            client, store, registry = FinalizeClient(*manifests), MemoryStore(*manifests), MemoryRegistry()
            store_type = pipeline.Store
            stderr, stdout = io.StringIO(), io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                    "RELEASE_READY_CONSUMERS": enabled, "RELEASE_COMPUTE_FIRST_PAIR": first,
                    "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_OUTPUT": str(Path(tmp) / "github-output") if github_output_enabled else "",
                    "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                    patch.object(pipeline, "Client", return_value=client), \
                    patch.object(pipeline, "Store", side_effect=lambda bound_client:
                                 store if bound_client is client else store_type(bound_client)), \
                    patch.object(pipeline, "Registry", return_value=registry), \
                    patch.object(pipeline, "age_minutes", return_value=0), \
                    patch.object(pipeline, "now", return_value="2026-10-16T00:00:00Z"), \
                    patch.object(pipeline, "version", wraps=pipeline.version) as parse_version, \
                    patch.object(pipeline, "report_issue") as report, \
                    patch.object(sys, "stderr", stderr), patch.object(sys, "stdout", stdout):
                output = Path(tmp) / "deliveries.json"
                github_output = Path(tmp) / "github-output"
                github_output.write_text("existing_output=kept\n", encoding="utf-8")
                with patch.object(sys, "argv", ["release_pipeline.py", "finalize", "--output", str(output)]):
                    result = pipeline.main()
                deliveries = output.read_bytes()
                configuration_output = github_output.read_text(encoding="utf-8")
                reports = [(call.args[1:], call.kwargs) for call in report.call_args_list]
                validations = sum(call.args == (first,) for call in parse_version.call_args_list)
            return (result, stderr.getvalue(), deliveries, store.assets, reports, registry.operations,
                    validations, configuration_output, stdout.getvalue())

        baseline = run("", legacy)
        self.assertEqual(baseline[:2], (0, ""))
        self.assertEqual(baseline[7:], ("existing_output=kept\n", ""))
        self.assertEqual(json.loads(baseline[2]), [
            {"consumer": "offline", "tag": "v1.6.2-rc.1", "attempt": 1},
            {"consumer": "hosted", "tag": "v1.6.2-rc.1", "attempt": 1},
            *({"consumer": role, "tag": "v1.6.1", "attempt": 1} for role in legacy.split(",")),
        ])
        configuration_line = ("compute_configuration_error=RELEASE_COMPUTE_FIRST_PAIR value was not accepted; "
                              "compute is off for this run\n")
        for first in ("v1.6.0", "1.6", "v1.7.0"):
            with self.subTest(first=first):
                actual = run(first, legacy + ",compute")
                self.assertEqual(actual[2:6], baseline[2:6])
                self.assertEqual(actual[:2], (0, ""))
                self.assertEqual(actual[6], 1)
                self.assertEqual(actual[7:], ("existing_output=kept\n" + configuration_line, ""))
                unselected = run(first, legacy)
                self.assertEqual(unselected[:6], baseline[:6])
                self.assertEqual(unselected[6], 0)
                self.assertEqual(unselected[7:], baseline[7:])

        standalone = run("v1.7.0", legacy + ",compute", github_output_enabled=False)
        self.assertEqual(standalone[:6], baseline[:6])
        self.assertEqual(standalone[6:], (1, "existing_output=kept\n", configuration_line))

    def test_compute_configuration_output_precedes_row_and_facts_failures(self):
        configuration_line = ("compute_configuration_error=RELEASE_COMPUTE_FIRST_PAIR value was not accepted; "
                              "compute is off for this run\n")

        def check_configuration_output(*args, **kwargs):
            self.assertEqual(github_output.read_text(encoding="utf-8"), configuration_line)

        class MissingFactsClockClient(FinalizeClient):
            def api(self, route, **kwargs):
                if route == f"repos/{contract.HUB}/commits?path=facts/facts.json&sha=main&per_page=1":
                    check_configuration_output()
                    return []
                return super().api(route, **kwargs)

        for failure in ("row", "facts"):
            with self.subTest(failure=failure):
                manifest = fixture_manifest()
                if failure == "row":
                    manifest["image"]["digest"] = "malformed"
                client = MissingFactsClockClient(manifest) if failure == "facts" else FinalizeClient(manifest)
                store, registry = MemoryStore(manifest), MemoryRegistry()
                store_type = pipeline.Store
                stderr, stdout = io.StringIO(), io.StringIO()
                with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                        "RELEASE_READY_CONSUMERS": "offline,web,compute", "RELEASE_COMPUTE_FIRST_PAIR": "v1.7.0",
                        "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                        "GITHUB_OUTPUT": str(Path(tmp) / "github-output"),
                        "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                        patch.object(pipeline, "Client", return_value=client), \
                        patch.object(pipeline, "Store", side_effect=lambda bound_client:
                                     store if bound_client is client else store_type(bound_client)), \
                        patch.object(pipeline, "Registry", return_value=registry), \
                        patch.object(pipeline, "age_minutes", return_value=0), \
                        patch.object(pipeline, "report_issue", side_effect=check_configuration_output) as report, \
                        patch.object(sys, "stderr", stderr), patch.object(sys, "stdout", stdout):
                    output = Path(tmp) / "deliveries.json"
                    github_output = Path(tmp) / "github-output"
                    github_output.write_text("", encoding="utf-8")
                    with patch.object(sys, "argv", ["release_pipeline.py", "finalize", "--output", str(output)]):
                        self.assertEqual(pipeline.main(), 1)
                    check_configuration_output()
                    self.assertEqual(stderr.getvalue().splitlines(), [
                        "::error::release chain operation failed; inspect the neutral incident and workflow run"])
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(json.loads(output.read_bytes()), [] if failure == "row" else [
                        {"consumer": "offline", "tag": "v1.6.0", "attempt": 1},
                        {"consumer": "web", "tag": "v1.6.0", "attempt": 1},
                    ])
                    if failure == "row":
                        self.assertTrue(any(call.args[2] == "Release v1.6.0: readiness failed"
                                            for call in report.call_args_list))
                    self.assertFalse(any("compute" in name for _, name in store.assets))

    def test_finalize_all_never_redispatches_after_delivery_acceptance(self):
        event = self.finish()
        self.store.put(self.row, "delivery-offline.json", contract.canonical({"event_id": event["event_id"], "state": "dispatch_accepted"}))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "offline", "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "report_issue"):
            output = Path(tmp) / "deliveries.json"
            for _ in range(3):
                pipeline.finalize_all(self.client, self.store, self.registry, "v1.6.0", output)
                self.assertEqual(contract.decode(output.read_bytes()), [])
        self.assertFalse(any(a["name"].startswith("delivery-attempt-") for a in self.row["assets"]))

    def test_finalize_all_delivery_retries_back_off_then_fail_once_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "offline", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "report_issue") as report:
            output = Path(tmp) / "deliveries.json"

            def tick(run_id, elapsed):
                os.environ["GITHUB_RUN_ID"] = str(run_id)
                with patch.object(pipeline, "age_minutes", return_value=elapsed):
                    pipeline.finalize_all(self.client, self.store, self.registry, "v1.6.0", output)
                return contract.decode(output.read_bytes())

            self.assertEqual(tick(100, 0), [{"consumer": "offline", "tag": "v1.6.0", "attempt": 1}])
            self.assertEqual(tick(101, 14), [])
            self.assertEqual(tick(102, 15)[0]["attempt"], 2)
            self.assertEqual(tick(103, 59), [])
            self.assertEqual(tick(104, 60)[0]["attempt"], 3)
            self.assertEqual(tick(105, 14), [])
            self.assertEqual(tick(106, 15), [])
            failed = self.store.read(self.row, "delivery-failed-offline.json")
            self.assertEqual(contract.decode(failed)["attempts"], 3)
            report.reset_mock()
            before = len(self.store.assets)
            self.assertEqual(tick(107, 1000), [])
            self.assertEqual(len(self.store.assets), before)
            self.assertEqual(self.store.read(self.row, "delivery-failed-offline.json"), failed)
            report.assert_not_called()

    def test_queued_delivery_rechecks_acceptance_and_exhaustion_before_sending(self):
        self.finish()
        for asset, state in (("delivery-offline.json", "dispatch_accepted"), ("delivery-failed-hosted.json", "failed")):
            role = "offline" if state == "dispatch_accepted" else "hosted"
            self.store.put(self.row, asset, contract.canonical({"consumer": role, "state": state}))
            with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "Client", return_value=self.store):
                output = Path(tmp) / "delivery.json"
                with patch.object(sys, "argv", ["release_pipeline.py", "dispatch", "--tag", "v1.6.0", "--consumer", role,
                                               "--attempt", "3", "--output", str(output)]):
                    self.assertEqual(pipeline.main(), 0)
                self.assertEqual(contract.decode(output.read_bytes())["state"],
                                 "dispatch_accepted" if state == "dispatch_accepted" else "dispatch_stopped")
        # The fake has no consumer dispatch endpoint; any send would fail above.

    def test_rollback_preserves_fixed_identity_and_stops_finalizer(self):
        event = self.finish()
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}), patch.object(rollback, "report_issue"):
            rollback.rollback(self.client, self.store, self.registry, "v1.6.0")
        self.assertEqual(self.registry.tags["latest"], "sha256:" + "0" * 64)
        self.assertEqual(self.registry.tags["1.6.0"], event["image"]["digest"])
        self.assertEqual(contract.payload(self.store.read(self.row, contract.ASSET)), event)
        self.assertIsNone(self.finish())


class DispatchTests(unittest.TestCase):
    """Use the existing MemoryStore/CLI harness with separate target/public clients."""
    def ready_store(self, *manifests):
        store = MemoryStore(*manifests)
        for manifest in manifests:
            row = store.get("v" + manifest["release_version"])
            row.update(draft=False, prerelease=manifest["prerelease"])
            store.put(row, contract.ASSET, contract.canonical(manifest))
        return store

    def send(self, store, consumer, tag="v1.6.0"):
        sender = unittest.mock.Mock(spec=["api"])

        def download(url, origins):
            prefix = "https://github.com/pineforge-4pass/pineforge-release/releases/download/"
            self.assertEqual(origins, {"github.com"})
            self.assertTrue(url.startswith(prefix))
            release_tag, asset = url.removeprefix(prefix).split("/")
            self.assertIn(asset, ("release-manifest.json", "release-candidate.json"))
            raw = store.assets.get((release_tag, asset))
            if raw is None:
                raise contract.Pending("published asset unavailable")
            return raw

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "TARGET_REPOSITORY": "fixture", "GITHUB_RUN_ID": "100"}), \
                patch.object(pipeline, "Client", side_effect=[sender, store]) as clients, \
                patch.object(store, "download", side_effect=download, create=True) as fetch, \
                patch.object(store, "pages", wraps=store.pages) as scan:
            output = Path(tmp) / "delivery.json"
            with patch.object(sys, "argv", ["release_pipeline.py", "dispatch", "--tag", tag,
                                           "--consumer", consumer, "--output", str(output)]):
                result = pipeline.main()
            written = output.read_bytes() if output.exists() else None
            self.assertEqual(clients.call_args_list, [unittest.mock.call(), unittest.mock.call(token="")])
        return result, sender, written, fetch.call_args_list, scan.call_args_list

    def test_pair_dispatch_posts_release_pair_with_exact_eight_key_payload(self):
        for consumer, release in (("compute", "1.6.0"), ("application", "1.6.0"), ("compute", "1.6.0-rc.1")):
            with self.subTest(consumer=consumer, release=release):
                manifest = fixture_manifest(release)
                store = self.ready_store(manifest)
                tag = "v" + release
                raw, candidate_raw = store.assets[(tag, contract.ASSET)], store.assets[(tag, pipeline.CANDIDATE)]
                result, sender, written, fetches, _ = self.send(store, consumer, tag)
                self.assertEqual(result, 0)
                sender.api.assert_called_once_with("repos/pineforge-4pass/fixture/dispatches", method="POST",
                    data={"event_type": "release-pair", "client_payload": expected_pair_payload(raw, candidate_raw)})
                self.assertEqual(len(sender.api.call_args.kwargs["data"]["client_payload"]), 8)
                self.assertEqual({call.args[0].rsplit("/", 1)[-1] for call in fetches},
                                 {"release-manifest.json", "release-candidate.json"})
                self.assertEqual(json.loads(written)["consumer"], consumer)
                self.assertNotIn(b"fixture", written)

    def test_legacy_dispatch_keeps_full_manifest_payload_bytes_for_every_other_role(self):
        ready_raw = (ROOT / "tests/fixtures/release-ready.json").read_bytes()
        for consumer in ("hosted", "offline", "web", "data", "license"):
            with self.subTest(consumer=consumer):
                store = self.ready_store(fixture_manifest())
                result, sender, written, fetches, _ = self.send(store, consumer)
                self.assertEqual(result, 0)
                sent = sender.api.call_args.kwargs["data"]
                sender.api.assert_called_once_with("repos/pineforge-4pass/fixture/dispatches", method="POST",
                    data={"event_type": "pineforge-release", "client_payload": json.loads(ready_raw)})
                self.assertEqual(contract.canonical(sent["client_payload"]), ready_raw)
                self.assertEqual(len(fetches), 1)
                self.assertTrue(fetches[0].args[0].endswith("/release-manifest.json"))
                self.assertEqual(json.loads(written)["state"], "dispatch_accepted")

    def test_compute_dispatch_accepts_older_pairs_while_receipt_roles_refuse(self):
        for consumer in ("compute", *contract.RECEIPT_ROLES):
            with self.subTest(consumer=consumer):
                store = self.ready_store(fixture_manifest(), fixture_manifest("1.6.1"))
                result, sender, written, _, scans = self.send(store, consumer)
                if consumer == "compute":
                    self.assertEqual(result, 0)
                    sender.api.assert_called_once()
                    self.assertEqual(json.loads(written)["consumer"], "compute")
                    self.assertEqual(scans, [])
                else:
                    self.assertEqual(result, 1)
                    sender.api.assert_not_called()
                    self.assertIsNone(written)
                    self.assertTrue(scans)

    def test_pair_dispatch_refuses_candidate_mismatch_before_posting(self):
        for consumer in ("compute", "application"):
            with self.subTest(consumer=consumer):
                store = self.ready_store(fixture_manifest())
                built = json.loads(store.assets[("v1.6.0", pipeline.CANDIDATE)])
                built["hub_commit"] = "f" * 40
                store.assets[("v1.6.0", pipeline.CANDIDATE)] = contract.canonical(built)
                result, sender, written, _, _ = self.send(store, consumer)
                self.assertEqual(result, 1)
                sender.api.assert_not_called()
                self.assertIsNone(written)

    def test_compute_dispatch_still_refuses_rejected_pairs(self):
        store = self.ready_store(fixture_manifest())
        store.put(store.get("v1.6.0"), "release-rejected.json", contract.canonical({"state": "rejected"}))
        result, sender, written, fetches, _ = self.send(store, "compute")
        self.assertEqual(result, 1)
        sender.api.assert_not_called()
        self.assertIsNone(written)
        self.assertEqual(fetches, [])


def archive(files):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, raw in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            tar.addfile(entry, io.BytesIO(raw))
    return out.getvalue()


class ProducerFixture:
    """Exact endpoint fixtures; an unknown call is an error, never a synthetic 404."""
    def __init__(self, v="1.6.0"):
        self.api_data, self.downloads = {}, {}
        self.v = pair.parse(v)
        self.commits = {role: char * 40 for role, char in (("engine", "a"), ("codegen", "b"), ("hpo", "c"))}
        self.releases = {}
        for role, repo in artifacts.PRODUCERS.items():
            tag = "v" + (self.v.pep440 if role == "hpo" else str(self.v))
            obj = {"tag_name": tag, "draft": False, "prerelease": self.v.prerelease, "assets": []}
            self.api_data[f"repos/{repo}/releases/tags/{tag}"] = obj
            self.releases[role] = obj
        for triple in ("linux-x86_64", "linux-aarch64", "macos-universal"):
            name = f"pineforge-v{self.v}-{triple}.tar.gz"
            data = b"engine-" + triple.encode()
            self.add_asset("engine", name, data)
            self.add_asset("engine", name + ".sha256", (contract.sha256(data) + "  " + name + "\n").encode())
        code = {"pineforge_codegen/__init__.py": b"VERSION='fixture'\n"}
        code_sdist = {"pineforge_codegen-fixture/" + name: data for name, data in code.items()}
        code_sdist["pineforge_codegen-fixture/PKG-INFO"] = f"Name: pineforge-codegen\nVersion: {self.v.pep440}\n\n".encode()
        sdist = archive(code_sdist)
        url = "https://files.pythonhosted.org/fixture.tar.gz"
        self.downloads[url] = sdist
        self.downloads[f"https://pypi.org/pypi/pineforge-codegen/{self.v.pep440}/json"] = contract.canonical({
            "info": {"name": "pineforge-codegen", "version": self.v.pep440}, "urls": [{
                "packagetype": "sdist", "yanked": False, "url": url, "digests": {"sha256": contract.sha256(sdist)}}]})
        self.tree("codegen", code)
        files = {"package/" + name: data for name, data in code.items()}
        files["package/package.json"] = contract.canonical({"name": "@pineforge/codegen-pyodide", "version": str(self.v)})
        browser_archive = archive(code)
        files[f"package/pineforge_codegen-{self.v}.tar.gz"] = browser_archive
        files["package/release.json"] = contract.canonical({"codegen": str(self.v), "sha256": contract.sha256(browser_archive)})
        self.npm_url = "https://registry.npmjs.org/@pineforge/codegen-pyodide/-/fixture.tgz"
        packed = archive(files)
        self.downloads[self.npm_url] = packed
        self.downloads["https://registry.npmjs.org/@pineforge%2fcodegen-pyodide/" + str(self.v)] = contract.canonical({
            "name": "@pineforge/codegen-pyodide", "version": str(self.v), "gitHead": self.commits["codegen"],
            "dist": {"tarball": self.npm_url, "integrity": "sha512-" + base64.b64encode(hashlib.sha512(packed).digest()).decode()}})
        hpo_sources = {"pineforge_hpo/__init__.py": b"VERSION='fixture'\n"}
        self.tree("hpo", hpo_sources)
        native = {"CMakeLists.txt": b"project(fixture)\n", "src/main.cpp": b"int main() {return 0;}\n"}
        tree_key = f"repos/{artifacts.PRODUCERS['hpo']}/git/trees/{self.commits['hpo']}?recursive=1"
        self.api_data[tree_key]["tree"].extend({"type": "blob", "path": name, "sha": artifacts.git_blob(raw)} for name, raw in native.items())
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as zipf:
            zipf.writestr(f"pineforge_hpo-{self.v.pep440}.dist-info/METADATA", f"Name: pineforge-hpo\nVersion: {self.v.pep440}\n\n")
            for name, raw in hpo_sources.items():
                zipf.writestr(name, raw)
            for name in ("hpo_failure_codes.json", "hpo_failure_codes_diff.json"):
                zipf.writestr("pineforge_hpo/" + name, b'{"fixture":true}\n')
                self.add_asset("hpo", name, b'{"fixture":true}\n')
        self.add_asset("hpo", f"pineforge_hpo-{self.v.pep440}-py3-none-any.whl", wheel.getvalue())
        hpo_sdist = {"fixture/" + name: raw for name, raw in hpo_sources.items()}
        hpo_sdist.update({"fixture/" + name: raw for name, raw in native.items()})
        hpo_sdist["fixture/PKG-INFO"] = f"Name: pineforge-hpo\nVersion: {self.v.pep440}\n\n".encode()
        self.add_asset("hpo", f"pineforge_hpo-{self.v.pep440}.tar.gz", archive(hpo_sdist))

    def tree(self, role, sources):
        prefix = "python/" if role == "hpo" else ""
        self.api_data[f"repos/{artifacts.PRODUCERS[role]}/git/trees/{self.commits[role]}?recursive=1"] = {
            "truncated": False, "tree": [{"type": "blob", "path": prefix + name, "sha": artifacts.git_blob(raw)} for name, raw in sources.items()]}

    def add_asset(self, role, name, raw):
        obj, repo = self.releases[role], artifacts.PRODUCERS[role]
        url = f"https://github.com/{repo}/releases/download/{obj['tag_name']}/{name}"
        obj["assets"].append({"name": name, "state": "uploaded", "browser_download_url": url, "digest": "sha256:" + contract.sha256(raw)})
        self.downloads[url] = raw

    def api(self, route, **kwargs):
        return copy.deepcopy(self.api_data[route])

    def tag_commit(self, repo, tag):
        return self.commits[next(role for role, name in artifacts.PRODUCERS.items() if name == repo)]

    def download(self, url, origins):
        if url not in self.downloads:
            raise AssertionError("unmodeled download " + url)
        data = self.downloads[url]
        if isinstance(data, Exception):
            raise data
        return data


class ProducerTests(unittest.TestCase):
    def test_three_producers_and_both_codegen_delivery_paths_are_bound(self):
        client = ProducerFixture()
        bound = pipeline.bindings(client, "1.6.0", "1.6.0", "1.6.0", "1.6.0", "d" * 40)
        codegen = artifacts.npm(client, bound["versions"]["codegen"], bound["inventories"]["codegen"])
        self.assertEqual(codegen["commit"], "b" * 40)
        self.assertEqual(bound["versions"]["hpo"]["commit"], "c" * 40)
        self.assertTrue(bound["inventories"]["hpo"])

    def test_missing_sdist_hpo_upload_and_bad_auth_cannot_form_ready_bindings(self):
        for problem in ("sdist", "hpo", "auth"):
            client = ProducerFixture()
            if problem == "sdist":
                url = f"https://pypi.org/pypi/pineforge-codegen/{client.v.pep440}/json"
                obj = contract.decode(client.downloads[url])
                obj["urls"] = []
                client.downloads[url] = contract.canonical(obj)
            elif problem == "hpo":
                client.releases["hpo"]["assets"] = [a for a in client.releases["hpo"]["assets"] if not a["name"].endswith(".tar.gz")]
            else:
                client.downloads["https://files.pythonhosted.org/fixture.tar.gz"] = contract.Invalid("HTTP 403")
            with self.subTest(problem=problem), self.assertRaises((contract.Pending, contract.Invalid)):
                pipeline.bindings(client, "1.6.0", "1.6.0", "1.6.0", "1.6.0", "d" * 40)

    def test_corrupt_npm_integrity_and_source_mismatch_are_red(self):
        client = ProducerFixture()
        pin, inventory = artifacts.producer(client, "codegen", "1.6.0")
        client.downloads[client.npm_url] += b"corruption"
        with self.assertRaises(contract.Invalid):
            artifacts.npm(client, pin, inventory)
        client = ProducerFixture()
        route = f"repos/{artifacts.PRODUCERS['codegen']}/git/trees/{client.commits['codegen']}?recursive=1"
        client.api_data[route]["tree"][0]["sha"] = "f" * 40
        with self.assertRaises(contract.Invalid):
            artifacts.producer(client, "codegen", "1.6.0")

    def test_hpo_pep440_requests_and_all_six_arrival_orders(self):
        for spelling in ("1.6.0rc1", "1.6.0-rc.1"):
            pin, files = artifacts.producer(ProducerFixture("1.6.0-rc.1"), "hpo", spelling)
            self.assertEqual(pin["tag"], "v1.6.0rc1")
            self.assertEqual(pin["version"], "1.6.0-rc.1")
        for v in ("1.6.0", "1.6.0-rc.1"):
            for order in permutations(("engine", "codegen", "hpo")):
                published = set()
                for index, role in enumerate(order):
                    published.add(role)
                    out, note = pair.decide(role, v, str(pair.parse(v).prerelease).lower(), "1.5.9", "1.5.9", "1.5.9",
                        False, False, False, partners_published={kind: kind in published for kind in order})
                    self.assertEqual(out["mode"], "bump" if index == 2 else "wait", (v, order, note))

    def test_404_is_pending_but_auth_rate_limit_and_transport_are_errors(self):
        for status, kind in ((404, contract.Pending), (401, contract.Invalid), (403, contract.Invalid), (429, contract.Invalid), (503, contract.Invalid)):
            opener = unittest.mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/example", status, "fixture", {}, None)
            with self.subTest(status=status), patch("urllib.request.build_opener", return_value=opener), self.assertRaises(kind):
                artifacts.Client(token="dummy").api("repos/fixture/releases")

    def test_corrupt_hpo_native_sources_and_pyodide_archive_are_red(self):
        client = ProducerFixture()
        route = f"repos/{artifacts.PRODUCERS['hpo']}/git/trees/{client.commits['hpo']}?recursive=1"
        client.api_data[route]["tree"][-1]["sha"] = "f" * 40
        with self.assertRaises(contract.Invalid):
            artifacts.producer(client, "hpo", "1.6.0")
        client = ProducerFixture()
        pin, inventory = artifacts.producer(client, "codegen", "1.6.0")
        obj = contract.decode(client.downloads["https://registry.npmjs.org/@pineforge%2fcodegen-pyodide/1.6.0"])
        files = artifacts.tar_files(client.downloads[client.npm_url])
        files["package/pineforge_codegen-1.6.0.tar.gz"] += b"corruption"
        packed = archive(files)
        obj["dist"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(packed).digest()).decode()
        client.downloads[client.npm_url] = packed
        client.downloads["https://registry.npmjs.org/@pineforge%2fcodegen-pyodide/1.6.0"] = contract.canonical(obj)
        with self.assertRaises(contract.Invalid):
            artifacts.npm(client, pin, inventory)

    def test_package_metadata_normalizes_pep440_prerelease_spellings_and_rejects_mismatches(self):
        for ver, accepted in (("1.6.1-rc.1", True), ("1.6.1rc1", True), ("1.6.1.rc1", True),
                              ("1.6.1rc2", False), ("1.6.1-rc.2", False),
                              ("1.6.2rc1", False), ("1.6.1", False)):
            with self.subTest(kind="codegen-sdist", version=ver):
                client = ProducerFixture("1.6.1-rc.1")
                sdist_url = "https://files.pythonhosted.org/fixture.tar.gz"
                pypi_url = "https://pypi.org/pypi/pineforge-codegen/1.6.1rc1/json"
                sdist_files = artifacts.tar_files(client.downloads[sdist_url])
                sdist_files["pineforge_codegen-fixture/PKG-INFO"] = (
                    f"Name: pineforge-codegen\nVersion: {ver}\n\n".encode()
                )
                sdist = archive(sdist_files)
                client.downloads[sdist_url] = sdist
                meta = contract.decode(client.downloads[pypi_url])
                meta["urls"][0]["digests"]["sha256"] = contract.sha256(sdist)
                client.downloads[pypi_url] = contract.canonical(meta)
                if accepted:
                    artifacts.package_metadata(sdist_files, "pineforge-codegen", "1.6.1rc1")
                    pin, _ = artifacts.producer(client, "codegen", "1.6.1-rc.1")
                    self.assertEqual(pin["pypi_version"], "1.6.1rc1")
                else:
                    with self.assertRaisesRegex(contract.Invalid, "^package metadata identity differs$"):
                        artifacts.package_metadata(sdist_files, "pineforge-codegen", "1.6.1rc1")
                    with self.assertRaisesRegex(contract.Invalid, "^package metadata identity differs$"):
                        artifacts.producer(client, "codegen", "1.6.1-rc.1")
            with self.subTest(kind="hpo-wheel", version=ver):
                client = ProducerFixture("1.6.1-rc.1")
                wheel_name = "pineforge_hpo-1.6.1rc1-py3-none-any.whl"
                wheel_url = f"https://github.com/{artifacts.PRODUCERS['hpo']}/releases/download/v1.6.1rc1/{wheel_name}"
                with zipfile.ZipFile(io.BytesIO(client.downloads[wheel_url])) as zf:
                    wheel_files = {name: zf.read(name) for name in zf.namelist()}
                wheel_files["pineforge_hpo-1.6.1rc1.dist-info/METADATA"] = (
                    f"Name: pineforge-hpo\nVersion: {ver}\n\n".encode()
                )
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    for name, raw in wheel_files.items():
                        zf.writestr(name, raw)
                wheel_raw = buf.getvalue()
                client.downloads[wheel_url] = wheel_raw
                wheel_asset = next(a for a in client.releases["hpo"]["assets"] if a["name"] == wheel_name)
                wheel_asset["digest"] = "sha256:" + contract.sha256(wheel_raw)
                if accepted:
                    artifacts.package_metadata(wheel_files, "pineforge-hpo", "1.6.1rc1", wheel=True)
                    pin, _ = artifacts.producer(client, "hpo", "1.6.1-rc.1")
                    self.assertEqual(pin["package_version"], "1.6.1rc1")
                else:
                    with self.assertRaisesRegex(contract.Invalid, "^package metadata identity differs$"):
                        artifacts.package_metadata(wheel_files, "pineforge-hpo", "1.6.1rc1", wheel=True)
                    with self.assertRaisesRegex(contract.Invalid, "^package metadata identity differs$"):
                        artifacts.producer(client, "hpo", "1.6.1-rc.1")


class ReconciliationTests(unittest.TestCase):
    def client(self):
        class Client(ProducerFixture):
            def __init__(self):
                super().__init__()
                self.dispatched = []
                self.hub_tag = False

            def tag_commit(self, repo, tag):
                if repo == contract.HUB:
                    if not self.hub_tag:
                        raise contract.Pending("hub tag unavailable")
                    return "d" * 40
                return super().tag_commit(repo, tag)

            def pages(self, route):
                role = next(role for role, repo in artifacts.PRODUCERS.items() if route == f"repos/{repo}/releases")
                return [self.releases[role], {"tag_name": "v1.5.9", "draft": False, "prerelease": False},
                        {"tag_name": "v9.9.9", "draft": True, "prerelease": False},
                        {"tag_name": "v9.9.9rc1" if role == "hpo" else "v9.9.9-rc.1", "draft": False, "prerelease": True}]

            def api(self, route, **kwargs):
                if route == f"repos/{contract.HUB}/dispatches":
                    assert kwargs["method"] == "POST"
                    self.dispatched.append(kwargs["data"])
                    return None
                return super().api(route, **kwargs)
        return Client()

    def test_dropped_notifications_are_replayed_through_the_original_three_events(self):
        client = self.client()
        with patch.object(pipeline.Path, "read_text", return_value="1.5.9\n"), patch.dict(os.environ, {"GITHUB_RUN_ID": "42"}):
            pipeline.reconcile(client, None)
        self.assertEqual({row["event_type"] for row in client.dispatched}, {"engine-release", "codegen-release", "hpo-release"})
        for row in client.dispatched:
            self.assertEqual(row["client_payload"], {"version": "1.6.0", "prerelease": False, "run_id": 42})

    def test_missing_upload_does_not_replay_that_producer_and_auth_errors_are_not_pending(self):
        for failure in ("upload", "auth"):
            client = self.client()
            if failure == "upload":
                client.releases["hpo"]["assets"] = []
            else:
                client.downloads["https://files.pythonhosted.org/fixture.tar.gz"] = contract.Invalid("HTTP 403")
            with patch.object(pipeline.Path, "read_text", return_value="1.5.9\n"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "42"}), patch.object(pipeline, "report_issue") as alert:
                if failure == "auth":
                    with self.assertRaises(contract.Invalid):
                        pipeline.reconcile(client, None)
                else:
                    pipeline.reconcile(client, None)
                self.assertTrue(alert.called)
            blocked = "hpo-release" if failure == "upload" else "codegen-release"
            self.assertNotIn(blocked, {row["event_type"] for row in client.dispatched})

    def test_existing_hub_tag_skips_all_producer_downloads_and_redispatches(self):
        client = self.client()
        client.hub_tag = True
        with patch.object(pipeline.Path, "read_text", return_value="1.5.9\n"), \
                patch.object(client, "download", side_effect=AssertionError("handled version must not download artifacts")):
            for _ in range(3):
                pipeline.reconcile(client, None)
        self.assertEqual(client.dispatched, [])

    def test_unknown_hub_tag_probe_errors_do_not_authorize_replay(self):
        client = self.client()
        with patch.object(pipeline.Path, "read_text", return_value="1.5.9\n"), \
                patch.object(client, "tag_commit", side_effect=contract.Invalid("HTTP 403")), self.assertRaises(contract.Invalid):
            pipeline.reconcile(client, None)
        self.assertEqual(client.dispatched, [])


class FactsFreshnessTests(unittest.TestCase):
    def test_clock_comes_from_the_facts_path_and_missing_served_receipt_is_overdue(self):
        obj = fixture_manifest()
        store = MemoryStore(obj)
        facts = contract.canonical(facts_for(obj))
        class Client:
            def api(self, route):
                if route != f"repos/{contract.HUB}/commits?path=facts/facts.json&sha=main&per_page=1":
                    raise AssertionError("freshness must not use main's unrelated commit date")
                return [{"sha": "e" * 40, "commit": {"committer": {"date": "2026-10-16T00:00:00Z"}}}]

            def file(self, repo, commit, path):
                return facts
        with patch.object(pipeline, "age_minutes", return_value=31), patch.object(pipeline, "report_issue"):
            report = pipeline.facts_freshness(Client(), store, ["web", "license"])
        self.assertEqual(report["consumers"], {"web": "overdue", "license": "overdue"})
        row = store.get("v1.6.0")
        store.put(row, "facts-consumer-web-fixture.json", contract.canonical({"consumer": "web", "state": "verified",
            "timestamp": "2026-10-16T00:01:00Z", "deployed": {"facts_sha256": contract.sha256(facts)}}))
        with patch.object(pipeline, "age_minutes", return_value=31), patch.object(pipeline, "report_issue"):
            report = pipeline.facts_freshness(Client(), store, ["web", "license"])
        self.assertEqual(report["consumers"], {"web": "verified", "license": "overdue"})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.manifest = fixture_manifest()
        self.event = contract.payload(contract.canonical(self.manifest))
        self.head, self.merge = "1" * 40, "2" * 40
        self.policy = {"workflow": "release.yml", "artifact": "release-receipt", "base": "main"}
        self.api_data = {}
        self.repo = "pineforge-4pass/fixture"
        self.receipt = {"schema": "pineforge/consumer-receipt/v1", "event_id": self.event["event_id"],
            "consumer": "offline", "release_version": "1.6.0", "attempt": 1, "sequence": 7, "state": "verified",
            "timestamp": "2020-01-01T00:07:00Z", "pr": {"number": 9, "base": "main", "base_sha": "3" * 40,
                "head": self.head, "merge": self.merge}}
        self.api_data[f"repos/{self.repo}/pulls/9"] = {"base": {"ref": "main", "sha": "3" * 40}, "head": {"sha": self.head, "repo": {"full_name": self.repo}},
            "merged": True, "merge_commit_sha": self.merge, "merged_at": "2020-01-01T00:05:00Z"}
        self.api_data[f"repos/{self.repo}/commits/{self.merge}"] = {"parents": [{"sha": "3" * 40}, {"sha": self.head}]}
        for index, name in enumerate(("ci", "e2e", "deploy"), 1):
            expected = [name + "/one", name + "/two"]
            self.policy[name] = {"workflow": name + ".yml", "expected": expected, "approved_skips": []}
            sha = self.merge if name == "deploy" else self.head
            self.receipt[name] = {"run_id": index, "run_attempt": 1, "sha": sha,
                "expected": expected, "approved_skips": [], "results": {item: "success" for item in expected}}
            self.api_data[f"repos/{self.repo}/actions/runs/{index}/attempts/1"] = {
                "path": ".github/workflows/" + name + ".yml", "head_sha": sha, "status": "completed", "conclusion": "success",
                "updated_at": "2020-01-01T00:04:00Z"}
            self.api_data[f"repos/{self.repo}/actions/runs/{index}/attempts/1/jobs?per_page=100&page=1"] = {
                "jobs": [{"name": item, "status": "completed", "conclusion": "success", "completed_at": "2020-01-01T00:04:00Z"} for item in expected]}
        # Ordinary five-key identities stay applicable to every consumer.
        # The data bootstrap is a separate literal fixture, never synthesized.
        identity = {"source": self.head, "image_digest": self.event["image"]["digest"],
                    "facts_commit": self.event["facts"]["commit"], "facts_sha256": self.event["facts"]["sha256"],
                    "deployment_id_sha256": "d" * 64}
        self.receipt.update(preview=identity, deployed={**identity, "source": self.merge}, previous={**identity, "source": "4" * 40})
        self.run = {"id": 90, "path": ".github/workflows/release.yml", "head_sha": self.merge, "event": "push"}

    def api(self, route, **kwargs):
        return copy.deepcopy(self.api_data[route])

    def verify(self):
        return receipts.validate_receipt(self, self.repo, self.receipt, self.event, self.policy, self.run)

    def data_bootstrap_previous(self):
        self.receipt.update(consumer="data", previous=bootstrap_previous())
        return self.receipt["previous"]

    def test_complete_evidence_is_verified_and_public_receipt_is_neutral(self):
        public = self.verify()
        self.assertEqual(public["state"], "verified")
        raw = contract.canonical(public)
        self.assertNotIn(b"fixture", raw)
        self.assertNotIn(b"e2e/one", raw)
        self.assertEqual(public["deployed"]["image_digest"], self.event["image"]["digest"])

    def test_compute_consumer_receipts_are_refused_before_evidence_checks(self):
        self.receipt["consumer"] = "compute"
        for state in receipts.STATES:
            self.receipt["state"] = state
            with self.subTest(state=state), patch.object(receipts, "check_run") as check:
                with self.assertRaisesRegex(contract.Invalid, "^invalid consumer receipt schema$"):
                    self.verify()
                check.assert_not_called()

    def test_receipt_selection_keeps_all_six_policies_without_a_compute_policy(self):
        expected = ("offline", "hosted", "application", "web", "data", "license")
        policies = {role: self.policy for role in expected}
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": ",".join(contract.ROLES),
                "RELEASE_RECEIPT_POLICY": json.dumps(policies),
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}):
            self.assertEqual(pipeline.receipt_roles(), list(expected))
            for role in expected:
                with self.subTest(role=role):
                    self.assertEqual(receipts.profile(role), self.policy)
            with self.assertRaisesRegex(contract.Invalid, "^unexpected receipt role$"):
                receipts.profile("compute")

    def test_compute_collection_refuses_before_configuration_policy_or_network(self):
        client = unittest.mock.Mock(spec=["api", "request"])
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "", "RELEASE_RECEIPT_POLICY": "{}"}), \
                patch.object(receipts, "profile") as profile:
            with self.assertRaisesRegex(contract.Invalid, "^unexpected receipt role$"):
                receipts.collect(client, client, MemoryStore(), "compute")
            profile.assert_not_called()
        client.api.assert_not_called()
        client.request.assert_not_called()

    def test_compute_signal_is_refused_even_if_the_caller_enables_all_roles(self):
        signal = {"schema": "pineforge/consumer-signal/v1", "consumer": "compute",
                  "release_version": "1.6.0", "run_id": 90}
        with self.assertRaisesRegex(contract.Invalid, "^invalid consumer signal$"):
            receipts.signal_targets(signal, contract.ROLES)

    def test_scheduled_receipt_plan_omits_compute_without_loading_any_policy(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": ",".join(contract.ROLES), "RELEASE_RECEIPT_POLICY": "{}",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false",
                "GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_PATH": str(Path(tmp) / "event.json")}), \
                patch.object(receipts, "Store", return_value=store), patch.object(receipts, "Client"), \
                patch.object(receipts, "profile") as profile:
            Path(os.environ["GITHUB_EVENT_PATH"]).write_bytes(b"{}")
            output = Path(tmp) / "consumers.json"
            with patch.object(sys, "argv", ["release_receipts.py", "signal", "--output", str(output)]):
                self.assertEqual(receipts.main(), 0)
            self.assertEqual(json.loads(output.read_bytes()),
                             [{"consumer": role, "run_id": "0"} for role in contract.RECEIPT_ROLES])
            self.assertNotIn(b"compute", output.read_bytes())
            profile.assert_not_called()

    def test_example_builder_refuses_the_delivery_only_role(self):
        with self.assertRaisesRegex(contract.Invalid, "^unexpected consumer$"):
            receipt_example.build_receipt(self.event, consumer="compute")
        with self.assertRaisesRegex(contract.Invalid, "^unexpected consumer$"):
            receipt_example.example_policy("compute")

    def test_stored_compute_consumer_receipt_cannot_claim_verified(self):
        store = MemoryStore(self.manifest)
        row = store.get("v1.6.0")
        store.put(row, "consumer-compute-1-1.json", contract.canonical({
            "event_id": self.event["event_id"], "consumer": "compute",
            "attempt": 1, "sequence": 1, "state": "verified"}))
        with self.assertRaisesRegex(contract.Invalid, "^consumer receipt identity differs$"):
            pipeline.aggregate(store, row, self.event, enabled=("compute",))

    def test_exact_data_bootstrap_previous_is_verified(self):
        previous = self.data_bootstrap_previous()
        public = self.verify()
        self.assertEqual(public["state"], "verified")
        self.assertEqual(public["previous"], previous)
        self.assertNotIn("facts_commit", public["previous"])
        self.assertNotIn("facts_sha256", public["previous"])

    def test_documented_data_bootstrap_previous_uses_the_hub_validator(self):
        doc = (ROOT / "docs/release-chain.md").read_text().split("## Consumer receipt contract v1\n", 1)[1].split("## Supervisor configuration", 1)[0]
        examples = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", doc, re.S)]
        previous = [obj for obj in examples if obj.get("provenance") == "pre-manifest-bootstrap"]
        self.assertEqual(previous, [bootstrap_previous()])
        self.receipt.update(consumer="data", previous=previous[0])
        self.assertEqual(self.verify()["previous"], previous[0])

    def test_data_bootstrap_previous_refuses_web(self):
        self.data_bootstrap_previous()
        self.receipt["consumer"] = "web"
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_license(self):
        self.data_bootstrap_previous()
        self.receipt["consumer"] = "license"
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_offline(self):
        self.data_bootstrap_previous()
        self.receipt["consumer"] = "offline"
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_hosted(self):
        self.data_bootstrap_previous()
        self.receipt["consumer"] = "hosted"
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_application(self):
        self.data_bootstrap_previous()
        self.receipt["consumer"] = "application"
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_hub_subject(self):
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            receipts.identity(bootstrap_previous(), consumer="hub", slot="previous")

    def test_data_bootstrap_previous_refuses_another_package(self):
        previous = self.data_bootstrap_previous()
        # Alternate hashes are literals for the changed deployment identity:
        # a self-consistent hash cannot excuse an unpinned package/version/map.
        previous.update(package="pineforge-other",
                        deployment_id_sha256="8f2f306592fa9fef24528932c434169ef65749ceb08a6808cadd588c16d16779")
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_version_0_3_1(self):
        previous = self.data_bootstrap_previous()
        previous.update(version="0.3.1",
                        deployment_id_sha256="08023494e81a761dd2973d3a876f82fe6830ebdc3c6d8a2cfa3d24a7796c5325")
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_version_0_2_0(self):
        previous = self.data_bootstrap_previous()
        previous.update(version="0.2.0",
                        deployment_id_sha256="b3dea0a3aa8b32a16fc483c279e1c832d8159b7b407f42a131460f411e9736b4")
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_missing_distributions(self):
        self.data_bootstrap_previous().pop("distributions")
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_incomplete_distribution_inventory(self):
        original = self.data_bootstrap_previous()
        for missing in ("pineforge_data-0.3.0-py3-none-any.whl", "pineforge_data-0.3.0.tar.gz"):
            self.receipt["previous"] = copy.deepcopy(original)
            self.receipt["previous"]["distributions"].pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                self.verify()

    def test_data_bootstrap_previous_refuses_empty_or_null_distributions(self):
        previous = self.data_bootstrap_previous()
        for distributions in ({}, None, []):
            previous["distributions"] = distributions
            with self.subTest(distributions=distributions), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                self.verify()

    def test_data_bootstrap_previous_refuses_extra_distribution(self):
        previous = self.data_bootstrap_previous()
        previous["distributions"]["extra.whl"] = "0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_wrong_wheel_hash(self):
        previous = self.data_bootstrap_previous()
        previous["distributions"]["pineforge_data-0.3.0-py3-none-any.whl"] = "009b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc"
        previous["deployment_id_sha256"] = "83e8a707daccc5c75526e1419bd2b0f44e5477036b395d3500156bf0f4d2eeb5"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_wrong_sdist_hash(self):
        previous = self.data_bootstrap_previous()
        previous["distributions"]["pineforge_data-0.3.0.tar.gz"] = "029a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc"
        previous["deployment_id_sha256"] = "cee2a7fef1cd296b6de3473b531938d7653d2efa511ab5d188f8a53072994a01"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_swapped_distribution_names(self):
        previous = self.data_bootstrap_previous()
        previous["distributions"] = {
            "pineforge_data-0.3.0-py3-none-any.whl": "f29a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc",
            "pineforge_data-0.3.0.tar.gz": "609b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc",
        }
        previous["deployment_id_sha256"] = "7c24d8060b89adf87c9885ea2e8541aefff834dd252aaefd67890b6882278e21"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_wrong_source(self):
        self.data_bootstrap_previous()["source"] = "0000000000000000000000000000000000000000"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_wrong_image_digest(self):
        self.data_bootstrap_previous()["image_digest"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_wrong_deployment_hash(self):
        self.data_bootstrap_previous()["deployment_id_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap deployment identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_hash_without_canonical_final_lf(self):
        self.data_bootstrap_previous()["deployment_id_sha256"] = "5b92247b57110c8942f0883ca2a0f401dedc80a381a03b4dcb62170c37779eeb"
        with self.assertRaisesRegex(contract.Invalid, "bootstrap deployment identity differs"):
            self.verify()

    def test_data_bootstrap_previous_refuses_other_provenance(self):
        previous = self.data_bootstrap_previous()
        for provenance in ("release-manifest", "", None, True):
            previous["provenance"] = provenance
            with self.subTest(provenance=provenance), self.assertRaisesRegex(contract.Invalid, "bootstrap previous identity differs"):
                self.verify()

    def test_data_bootstrap_previous_refuses_extra_key(self):
        self.data_bootstrap_previous()["extra"] = True
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_each_missing_key(self):
        original = self.data_bootstrap_previous()
        for missing in ("provenance", "package", "version", "source", "image_digest", "deployment_id_sha256", "distributions"):
            self.receipt["previous"] = copy.deepcopy(original)
            self.receipt["previous"].pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                self.verify()

    def test_data_bootstrap_previous_refuses_facts_commit_value(self):
        self.data_bootstrap_previous()["facts_commit"] = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_facts_sha256_value(self):
        self.data_bootstrap_previous()["facts_sha256"] = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_null_facts_commit(self):
        self.data_bootstrap_previous()["facts_commit"] = None
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_null_facts_sha256(self):
        self.data_bootstrap_previous()["facts_sha256"] = None
        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            self.verify()

    def test_data_bootstrap_previous_refuses_preview_slot(self):
        previous = self.data_bootstrap_previous()
        # Exercise identity() directly too, so a later source/image check
        # cannot mask removal of the slot restriction.
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            receipts.identity(previous, consumer="data", slot="preview")
        self.receipt["preview"] = previous
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_refuses_deployed_slot(self):
        previous = self.data_bootstrap_previous()
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            receipts.identity(previous, consumer="data", slot="deployed")
        self.receipt["deployed"] = previous
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            self.verify()

    def test_data_bootstrap_previous_needs_explicit_consumer_and_slot(self):
        for context in ({}, {"consumer": "data"}, {"slot": "previous"}):
            with self.subTest(context=context), self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
                receipts.identity(bootstrap_previous(), **context)

    def test_data_bootstrap_previous_refuses_facts_only_receipts(self):
        previous = self.data_bootstrap_previous()
        self.receipt.update(schema="pineforge/facts-consumer-receipt/v1",
                            facts={"commit": self.event["facts"]["commit"], "sha256": self.event["facts"]["sha256"]})
        self.receipt.pop("event_id")
        self.receipt.pop("release_version")
        self.event = None
        for consumer in ("web", "license"):
            self.receipt["consumer"] = consumer
            with self.subTest(consumer=consumer), self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
                self.verify()
        # Isolate facts_only from the independent consumer-role restriction.
        with self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
            receipts.identity(previous, consumer="data", slot="previous", facts_only=True)

    def test_bootstrap_scope_is_checked_before_identity_stage(self):
        original = copy.deepcopy(self.receipt)
        for state in ("pending", "pr_open", "testing", "ready", "merged", "deploying", "failed", "superseded", "skipped_by_policy"):
            for slot in ("preview", "deployed", "previous"):
                self.receipt = copy.deepcopy(original)
                self.receipt.update(state=state, consumer="web" if slot == "previous" else "data")
                self.receipt[slot] = bootstrap_previous()
                with self.subTest(state=state, slot=slot), self.assertRaisesRegex(contract.Invalid, "only valid for product data.previous"):
                    self.verify()

    def test_real_five_key_previous_still_passes_for_every_consumer(self):
        for consumer in ("offline", "hosted", "application", "web", "data", "license"):
            self.receipt["consumer"] = consumer
            with self.subTest(consumer=consumer):
                public = self.verify()
                self.assertEqual(public["state"], "verified")
                self.assertEqual(set(public["previous"]),
                                 {"source", "image_digest", "facts_commit", "facts_sha256", "deployment_id_sha256"})

    def test_real_previous_with_provenance_is_refused(self):
        self.receipt["consumer"] = "data"
        for provenance in ("pre-manifest-bootstrap", "release-manifest", None):
            self.receipt["previous"]["provenance"] = provenance
            with self.subTest(provenance=provenance), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                self.verify()

    def test_example_builder_and_documented_examples_use_the_hub_validator(self):
        self.receipt = receipt_example.build_receipt(self.event)
        self.assertEqual(self.verify()["state"], "verified")
        facts = {"commit": "e" * 40, "sha256": "f" * 64}
        for role in ("web", "license"):
            obj = receipt_example.build_receipt(consumer=role, facts=facts)
            public = receipts.validate_receipt(self, self.repo, obj, None, receipt_example.example_policy(role), self.run)
            self.assertEqual(public["facts"], facts)
            self.assertNotIn("event_id", obj)
            self.assertNotIn("release_version", obj)
            self.assertIsNone(public["deployed"]["image_digest"])
        doc = (ROOT / "docs/release-chain.md").read_text().split("## Consumer receipt contract v1\n", 1)[1].split("## Supervisor configuration", 1)[0]
        examples = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", doc, re.S)]
        reports = [obj for obj in examples if obj.get("schema") in (receipts.PRODUCT_SCHEMA, receipts.FACTS_SCHEMA)]
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0], receipt_example.build_receipt(self.event))
        self.assertEqual(reports[1], receipt_example.build_receipt(consumer="license", facts=facts))
        for obj in reports:
            event = self.event if obj["schema"] == receipts.PRODUCT_SCHEMA else None
            self.assertEqual(receipts.validate_receipt(self, self.repo, obj, event, self.policy, self.run)["state"], "verified")

    def test_facts_only_signal_has_exact_four_keys_and_needs_no_product_role(self):
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "", "FACTS_LICENSE_DISPATCH_ENABLED": "true",
                                    "FACTS_WEB_DISPATCH_ENABLED": "false"}):
            enabled = pipeline.receipt_roles()
        self.assertEqual(enabled, ["license"])
        signal = {"schema": "pineforge/consumer-signal/v1", "consumer": "license", "release_version": None, "run_id": 90}
        self.assertEqual(receipts.signal_targets(signal, enabled), [{"consumer": "license", "run_id": "90"}])
        for bad in ({**signal, "state": "verified"}, {**signal, "consumer": "offline"}, {**signal, "run_id": True}):
            with self.assertRaises(contract.Invalid):
                receipts.signal_targets(bad, enabled)

    def test_policy_named_artifact_requires_the_single_contract_file_and_matching_digest(self):
        route = f"repos/{self.repo}/actions/runs/90/artifacts?per_page=100&page=1"
        policy = receipt_example.example_policy()
        for names in (("release-receipt.json",), ("receipt.json",), ("release-receipt.json", "extra.json")):
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, "w") as zipped:
                for name in names:
                    zipped.writestr(name, contract.canonical(self.receipt))
            raw = packed.getvalue()
            metadata = {"id": 9, "name": policy["artifact"], "expired": False, "digest": "sha256:" + contract.sha256(raw)}
            self.api_data[route] = {"artifacts": [metadata]}
            with patch.object(self, "request", return_value=raw, create=True):
                if names == ("release-receipt.json",):
                    self.assertEqual(receipts.fetch_artifact(self, self.repo, self.run, policy), self.receipt)
                else:
                    with self.assertRaises(contract.Invalid):
                        receipts.fetch_artifact(self, self.repo, self.run, policy)
                metadata["digest"] = "sha256:" + "0" * 64
                with self.assertRaises(contract.Invalid):
                    receipts.fetch_artifact(self, self.repo, self.run, policy)
            metadata["name"] += "-90"
            self.assertIsNone(receipts.fetch_artifact(self, self.repo, self.run, policy))

    def test_facts_only_collector_needs_no_product_release_and_keeps_full_evidence_gates(self):
        facts_raw = contract.canonical(facts_for(self.manifest))
        facts = {"commit": "e" * 40, "sha256": contract.sha256(facts_raw)}
        store = MemoryStore()
        self.api_data[f"repos/{self.repo}"] = {"full_name": self.repo}
        self.api_data[f"repos/{self.repo}/actions/runs/90"] = self.run
        self.api_data[f"repos/{contract.HUB}/git/ref/heads/main"] = {"object": {"sha": "f" * 40}}
        self.api_data[f"repos/{contract.HUB}/compare/{facts['commit']}...{'f' * 40}"] = {"status": "ahead"}
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture"}), \
                patch.object(receipts, "profile", return_value=self.policy), \
                patch.object(self, "file", return_value=facts_raw, create=True), \
                patch.object(receipts, "fetch_artifact") as fetch:
            for role in ("license", "web"):
                obj = receipt_example.build_receipt(consumer=role, facts=facts)
                fetch.return_value = obj
                receipts.collect(self, self, store, role, 90)
                receipts.collect(self, self, store, role, 90)
            self.assertEqual(set(store.rows), {pipeline.FACTS_RECEIPTS_TAG})
            self.assertEqual(len(store.assets), 2)
            self.assertTrue(store.rows[pipeline.FACTS_RECEIPTS_TAG]["draft"])
            for raw in store.assets.values():
                self.assertNotIn("event_id", contract.decode(raw))
            obj = receipt_example.build_receipt(consumer="license", facts=facts)
            obj["deployed"]["facts_sha256"] = "0" * 64
            fetch.return_value = obj
            with self.assertRaises(contract.Invalid):
                receipts.collect(self, self, store, "license", 90)
            obj = receipt_example.build_receipt(consumer="license", facts={**facts, "sha256": "0" * 64})
            fetch.return_value = obj
            with self.assertRaises(contract.Invalid):
                receipts.collect(self, self, store, "license", 90)
            fetch.return_value = receipt_example.build_receipt(consumer="license", facts=facts)
            comparison = f"repos/{contract.HUB}/compare/{facts['commit']}...{'f' * 40}"
            self.api_data[comparison] = {"status": "diverged"}
            with self.assertRaises(contract.Invalid):
                receipts.collect(self, self, store, "license", 90)
            self.api_data[comparison] = {"status": "ahead"}
            self.api_data[f"repos/{self.repo}/actions/runs/2/attempts/1/jobs?per_page=100&page=1"]["jobs"].pop()
            with self.assertRaises(contract.Invalid):
                receipts.collect(self, self, store, "license", 90)

    def test_red_missing_failed_cancelled_and_skipped_e2e_job_cannot_report_verified(self):
        route = f"repos/{self.repo}/actions/runs/2/attempts/1/jobs?per_page=100&page=1"
        original = copy.deepcopy(self.api_data[route])
        for result in ("missing", "failure", "cancelled", "skipped"):
            self.api_data[route] = copy.deepcopy(original)
            if result == "missing":
                self.api_data[route]["jobs"].pop()
            else:
                self.api_data[route]["jobs"][1]["conclusion"] = result
            with self.subTest(result=result), self.assertRaises(contract.Invalid):
                self.verify()

    def test_green_after_merge_stale_source_wrong_digest_and_invented_inventory_are_red(self):
        original = copy.deepcopy(self.receipt)
        mutations = (
            lambda: self.receipt["deployed"].update(image_digest="sha256:" + "f" * 64),
            lambda: self.receipt["preview"].update(source="f" * 40),
            lambda: self.receipt["e2e"].update(expected=["e2e/one"]),
            lambda: self.api_data[f"repos/{self.repo}/actions/runs/2/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1].update(completed_at="2020-01-01T00:06:00Z"))
        for mutation in mutations:
            self.receipt = copy.deepcopy(original)
            mutation()
            with self.assertRaises(contract.Invalid):
                self.verify()

    def test_duplicate_old_receipt_is_idempotent_and_cannot_regress_verified(self):
        verified = self.verify()
        self.assertFalse(receipts.transition(verified, copy.deepcopy(verified)))
        self.assertFalse(receipts.transition(verified, {**verified, "sequence": 6, "state": "pending"}))
        with self.assertRaises(contract.Invalid):
            receipts.transition(verified, {**verified, "attempt": 2, "sequence": 1, "state": "pending"})

    def test_collector_repairs_dropped_callback_and_separates_facts_only_completion(self):
        facts = facts_for(self.manifest)
        original = contract.canonical(facts)
        self.manifest["facts"]["sha256"] = contract.sha256(original)
        self.event = contract.payload(contract.canonical(self.manifest))
        self.receipt["event_id"] = self.event["event_id"]
        for name in ("preview", "deployed"):
            self.receipt[name]["facts_sha256"] = contract.sha256(original)
        store = MemoryStore(self.manifest)
        row = store.get("v1.6.0")
        row["draft"] = False
        store.put(row, contract.ASSET, contract.canonical(self.manifest))
        self.api_data[f"repos/{self.repo}"] = {"full_name": self.repo}
        self.run["status"] = "completed"
        self.api_data[f"repos/{self.repo}/actions/runs/{self.run['id']}"] = self.run
        self.api_data[f"repos/{self.repo}/actions/artifacts?name={self.policy['artifact']}&per_page=100&page=1"] = {
            "artifacts": [{"id": 1, "name": self.policy["artifact"], "expired": False,
                           "workflow_run": {"id": self.run["id"]}}]}
        owner = self
        newer = (json.dumps(facts, indent=2) + "\n").encode()
        class Hub:
            def api(self, route, **kwargs):
                if route == f"repos/{contract.HUB}/git/ref/heads/main":
                    return {"object": {"sha": "f" * 40}}
                prefix = f"repos/{contract.HUB}/compare/"
                if not route.startswith(prefix):
                    raise AssertionError("unexpected hub request")
                return {"status": "ahead" if route.endswith("f" * 40) else "identical"}

            def file(self, repo, commit, path):
                return newer if commit == "f" * 40 else original
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture"}), patch.object(receipts, "profile", return_value=self.policy), \
                patch.object(receipts, "fetch_artifact", side_effect=lambda *args: copy.deepcopy(owner.receipt)):
            receipts.collect(self, Hub(), store, "offline")
            first = pipeline.receipts(store, row, self.event["event_id"])["offline"]
            receipts.collect(self, Hub(), store, "offline")
            self.assertEqual(pipeline.receipts(store, row, self.event["event_id"])["offline"], first)
            self.receipt.update(schema="pineforge/facts-consumer-receipt/v1", consumer="web",
                                facts={"commit": "f" * 40, "sha256": contract.sha256(newer)})
            self.receipt.pop("event_id")
            self.receipt.pop("release_version")
            for name in ("preview", "deployed"):
                self.receipt[name].update(facts_commit="f" * 40, facts_sha256=contract.sha256(newer))
            receipts.collect(self, Hub(), store, "web")
        self.assertTrue(any(asset["name"].startswith("facts-consumer-web-") for asset in store.facts()["assets"]))
        self.assertNotIn("web", pipeline.receipts(store, row, self.event["event_id"]))


class ImmutableAssetTests(unittest.TestCase):
    def test_real_store_put_refuses_replacement_and_verifies_upload_readback(self):
        class Store(pipeline.Store):
            def __init__(self):
                self.raw = None
                self.client = self

            def read(self, row, name):
                return self.raw

            def request(self, url, **kwargs):
                self.raw = kwargs["data"]
        store = Store()
        store.put({"id": 1}, "release-manifest.json", b"original\n")
        store.put({"id": 1}, "release-manifest.json", b"original\n")
        with self.assertRaises(contract.Invalid):
            store.put({"id": 1}, "release-manifest.json", b"changed\n")
        self.assertEqual(store.raw, b"original\n")
        store.raw = None
        with patch.object(store, "request", side_effect=lambda *args, **kwargs: None), self.assertRaises(contract.Invalid):
            store.put({"id": 1}, "release-manifest.json", b"original\n")


if __name__ == "__main__":
    unittest.main()
