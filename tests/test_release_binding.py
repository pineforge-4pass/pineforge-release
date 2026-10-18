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
            "sources": {"release_tag": "source-set-02", "list_asset": "SOURCES.list",
                        "list_sha256": "a" * 64, "amd64_sha256": "b" * 64, "arm64_sha256": "c" * 64}}


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

    def alias_digest(self, tag):
        return self.digest(tag)

    def restore(self, tag, rejected, previous):
        if self.digest(tag) != rejected:
            return True
        if previous is None:
            return False
        self.point(tag, previous)
        return True

    def point(self, tag, digest, fixed=False, source=contract.IMAGE):
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
        raise AssertionError("unmodeled API request " + route)

    def file(self, repo, commit, path):
        if (repo, commit, path) == (contract.HUB, "e" * 40, "facts/facts.json"):
            return contract.canonical(self.facts)
        raise AssertionError("unmodeled immutable source request")


class ManifestTests(unittest.TestCase):
    def test_existing_fixture_bytes_and_ids_validate_without_rewriting(self):
        from facts_validate import validate_facts
        hashes = {
            "release-manifest.json": "12a79d3776c447b1caf9513c96db3407b1d3cd2d71a510081e6763e994c2288e",
            "release-ready.json": "b2709e7545387bb43a978adabce983ae128d7bee5791fdc0eb912e0fdda05e06",
            "repo-descriptions-facts.json": "acc0639894780a571be10383eb22b1fbd150f704e1f0ee5d8906ab8d101d4f7b",
        }
        before = {path.name: path.read_bytes() for path in (ROOT / "tests/fixtures").glob("*.json")}
        self.assertEqual(set(before), set(hashes))
        for name, raw in before.items():
            self.assertEqual(contract.sha256(raw), hashes[name], name)
        raw = before["release-manifest.json"]
        contract.validate_payload(contract.decode(before["release-ready.json"]), raw)
        validate_facts(contract.decode(before["repo-descriptions-facts.json"]))
        self.assertEqual(contract.canonical(contract.validate(contract.decode(raw))), raw)
        for name, raw in before.items():
            self.assertEqual((ROOT / "tests/fixtures" / name).read_bytes(), raw)
        schema = contract.decode((ROOT / "facts/facts.schema.json").read_bytes())
        self.assertEqual(schema["$id"], "https://raw.githubusercontent.com/pineforge-4pass/pineforge-release/main/facts/facts.schema.json")
        self.assertEqual(schema["properties"]["schema"]["const"], "pineforge/facts/v1")
        self.assertEqual(set(schema["required"]), {"schema", "scoreboard", "releases", "inventory"})

    def test_both_repository_names_optional_names_and_invalid_name_lists(self):
        for repository in contract.IMAGES:
            for names in (None, [repository], list(contract.IMAGES)):
                obj = fixture_manifest()
                obj["image"]["repository"] = repository
                if names is not None:
                    obj["image"]["repositories"] = names
                raw = contract.canonical(obj)
                self.assertEqual(contract.validate_payload(contract.payload(raw), raw)["image"], obj["image"])
        for names in ([], [contract.IMAGE, contract.IMAGE], ["ghcr.io/unrelated/image"],
                      [contract.IMAGES[1]], "not-an-array"):
            obj = fixture_manifest()
            obj["image"]["repositories"] = names
            with self.subTest(names=names), self.assertRaises(contract.Invalid):
                contract.payload(contract.canonical(obj))

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
        self.assertEqual(set(status["consumers"].values()), {"superseded"})

    def test_dispatch_and_missing_receipt_never_mean_verified(self):
        event = self.finish()
        self.store.put(self.row, "delivery-offline-42-1.json", contract.canonical({"state": "dispatch_accepted"}))
        status = pipeline.aggregate(self.store, self.row, event, enabled=contract.ROLES)
        self.assertEqual(set(status["consumers"].values()), {"pending"})
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
        identity = {"source": self.head, "image_digest": self.event["image"]["digest"],
                    "facts_commit": self.event["facts"]["commit"], "facts_sha256": self.event["facts"]["sha256"],
                    "deployment_id_sha256": "d" * 64}
        self.receipt.update(preview=identity, deployed={**identity, "source": self.merge}, previous={**identity, "source": "4" * 40})
        self.run = {"id": 90, "path": ".github/workflows/release.yml", "head_sha": self.merge, "event": "push"}

    def api(self, route, **kwargs):
        return copy.deepcopy(self.api_data[route])

    def verify(self):
        return receipts.validate_receipt(self, self.repo, self.receipt, self.event, self.policy, self.run)

    def test_complete_evidence_is_verified_and_public_receipt_is_neutral(self):
        public = self.verify()
        self.assertEqual(public["state"], "verified")
        raw = contract.canonical(public)
        self.assertNotIn(b"fixture", raw)
        self.assertNotIn(b"e2e/one", raw)
        self.assertEqual(public["deployed"]["image_digest"], self.event["image"]["digest"])
        self.assertEqual(public["image"]["repositories"], list(contract.IMAGES))

    def test_receipts_accept_either_image_reference_with_one_matching_digest(self):
        for repository in contract.IMAGES:
            manifest = fixture_manifest()
            manifest["image"].update(repository=repository, repositories=list(contract.IMAGES))
            self.event = contract.payload(contract.canonical(manifest))
            self.receipt["event_id"] = self.event["event_id"]
            for name in ("preview", "deployed", "previous"):
                self.receipt[name].update(image_digest=repository + "@" + self.event["image"]["digest"],
                                          image_reference=repository + "@" + self.event["image"]["digest"],
                                          image_repositories=list(contract.IMAGES))
            public = self.verify()
            self.assertEqual(public["image"]["digest"], manifest["image"]["digest"])
            self.assertEqual(public["image"]["repositories"], list(contract.IMAGES))
        different = "sha256:" + contract.sha256(contract.canonical({"another": "image"}))
        self.receipt["deployed"].update(image_digest=contract.IMAGES[1] + "@" + different,
                                         image_reference=contract.IMAGES[1] + "@" + different)
        with self.assertRaisesRegex(contract.Invalid, "actual served source/image differs"):
            self.verify()

    def test_receipt_names_cannot_hide_a_different_reference_or_unrelated_repository(self):
        for reference in (contract.IMAGES[1] + "@sha256:" + "f" * 64,
                          "ghcr.io/unrelated/image@" + self.event["image"]["digest"]):
            self.receipt["preview"]["image_reference"] = reference
            with self.subTest(reference=reference), self.assertRaises(contract.Invalid):
                self.verify()

    def test_replaying_an_old_neutral_receipt_keeps_its_original_bytes(self):
        current = self.verify()
        previous = {key: value for key, value in current.items() if key != "image"}
        original = contract.canonical(previous)
        self.assertFalse(receipts.transition(previous, current))
        self.assertEqual(contract.canonical(previous), original)
        changed = copy.deepcopy(current)
        changed["evidence_sha256"] = contract.sha256(original + b"changed evidence")
        with self.assertRaises(contract.Invalid):
            receipts.transition(previous, changed)

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
        self.api_data[f"repos/{self.repo}/actions/workflows/release.yml/runs?per_page=100&page=1"] = {"workflow_runs": [self.run]}
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
