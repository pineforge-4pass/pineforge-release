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
import sys
import tarfile
import unittest
import urllib.error
import zipfile
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_artifacts as artifacts
import release_contract as contract
import release_pair as pair
import release_pipeline as pipeline
import release_receipts as receipts
import release_rollback as rollback


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
        return self.rows[tag]

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
            row["assets"].append({"name": name, "id": len(self.assets)})
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
        status = pipeline.aggregate(self.store, self.row, old, newest=pair.parse("1.6.1"))
        self.assertEqual(set(status["consumers"].values()), {"superseded"})

    def test_dispatch_and_missing_receipt_never_mean_verified(self):
        event = self.finish()
        self.store.put(self.row, "delivery-offline-42-1.json", contract.canonical({"state": "dispatch_accepted"}))
        status = pipeline.aggregate(self.store, self.row, event)
        self.assertEqual(set(status["consumers"].values()), {"pending"})
        self.assertFalse(status["complete"])

    def test_prerelease_never_moves_latest_and_has_explicit_policy_inventory(self):
        m = fixture_manifest("1.6.0-rc.1")
        self.client, self.store = FinalizeClient(m), MemoryStore(m)
        event = pipeline.finalize(self.client, self.store, self.registry, self.store.get("v1.6.0-rc.1"))
        self.assertEqual(self.registry.tags["latest"], "sha256:" + "0" * 64)
        status = pipeline.aggregate(self.store, self.store.get("v1.6.0-rc.1"), event)
        self.assertEqual(status["consumers"]["application"], "skipped_by_policy")
        self.assertEqual(status["consumers"]["offline"], "pending")

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


class ReconciliationTests(unittest.TestCase):
    def client(self):
        class Client(ProducerFixture):
            def __init__(self):
                super().__init__()
                self.dispatched = []

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
            for name in ("preview", "deployed"):
                self.receipt[name].update(facts_commit="f" * 40, facts_sha256=contract.sha256(newer))
            receipts.collect(self, Hub(), store, "web")
        self.assertTrue(any(asset["name"].startswith("facts-consumer-web-") for asset in row["assets"]))
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
