#!/usr/bin/env python3
"""Verified attachment regressions for hub CI; all HTTP is an in-memory fixture.

Authorities are captured before mutations. Removing a digest gate must let its
isolated negative input through, or cause a forbidden upload before refusal.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
from test_release_binding import (MemoryRegistry, ProducerFixture, archive,  # noqa: E402
                                  candidate, facts_for, fixture_manifest)


class AttachmentHTTP:
    """Serve the real Client, including origin, auth, API and readback handling."""
    def __init__(self):
        self.producers = ProducerFixture((ROOT / "VERSION").read_text().strip())
        self.manifest = fixture_manifest(str(self.producers.v))
        self.pypi_url = f"https://pypi.org/pypi/pineforge-codegen/{self.producers.v.pep440}/json"
        self.npm_meta_url = "https://registry.npmjs.org/@pineforge%2fcodegen-pyodide/" + str(self.producers.v)
        pypi = json.loads(self.producers.downloads[self.pypi_url])
        self.sdist_url = pypi["urls"][0]["url"]
        self.manifest["versions"]["codegen"]["sdist_sha256"] = pypi["urls"][0]["digests"]["sha256"]
        self.manifest["versions"]["codegen"]["npm_integrity"] = json.loads(
            self.producers.downloads[self.npm_meta_url])["dist"]["integrity"]
        self.built = candidate(self.manifest)
        self.built["inventories"]["codegen"] = {
            "pineforge_codegen/__init__.py": hashlib.sha256(b"VERSION='fixture'\n").hexdigest()}
        self.tag = "v" + self.manifest["release_version"]
        self.facts_raw = contract.canonical(facts_for(self.manifest))
        self.row = {"id": 1, "tag_name": self.tag, "draft": True,
                    "prerelease": self.manifest["prerelease"],
                    "created_at": "2020-01-01T00:00:00Z", "assets": []}
        self.data, self.requests, self.uploads, self.publications = {}, [], [], []
        self.readback_problem = None
        self.unavailable_reads = set()
        self.fail_upload_number = None
        self.forbid_producers = False
        self.add_hub_asset(contract.CANDIDATE, contract.canonical(self.built))
        urls = [self.producers.npm_url, self.sdist_url]
        urls += [a["browser_download_url"] for a in self.producers.releases["engine"]["assets"]
                 if a["name"].endswith(("-linux-x86_64.tar.gz", "-linux-aarch64.tar.gz"))]
        # Freeze independent producer responses before any negative mutation.
        self.originals = {url: self.producers.downloads[url] for url in urls}
        self.descriptors = sorted([
            {"name": url.rsplit("/", 1)[1], "size": len(raw),
             "sha256": hashlib.sha256(raw).hexdigest(), "source": url}
            for url, raw in self.originals.items()], key=lambda item: item["name"])
        self.extended = {**copy.deepcopy(self.manifest), "assets": copy.deepcopy(self.descriptors)}
        for role, repo in artifacts.PRODUCERS.items():
            obj = self.producers.releases[role]
            self.producers.api_data[f"repos/{repo}/git/ref/tags/{obj['tag_name']}"] = {
                "object": {"type": "commit", "sha": self.producers.commits[role]}}

    def add_hub_asset(self, name, raw):
        asset_id = max(self.data, default=0) + 1
        self.data[asset_id] = raw
        # Deliberately no optional GitHub digest: descriptor verification must work.
        self.row["assets"].append({"id": asset_id, "name": name, "state": "uploaded"})

    def hub_bytes(self, name):
        found = [a for a in self.row["assets"] if a["name"] == name]
        return self.data[found[0]["id"]] if found else None

    def engine_asset(self, suffix="-linux-x86_64.tar.gz"):
        return next(a for a in self.producers.releases["engine"]["assets"] if a["name"].endswith(suffix))

    def replace_sdist(self, *, update_api):
        files = artifacts.tar_files(self.producers.downloads[self.sdist_url])
        files["pineforge_codegen-fixture/NOTICE"] = b"changed outer distribution\n"
        raw = archive(files)
        self.producers.downloads[self.sdist_url] = raw
        if update_api:
            meta = json.loads(self.producers.downloads[self.pypi_url])
            meta["urls"][0]["digests"]["sha256"] = hashlib.sha256(raw).hexdigest()
            self.producers.downloads[self.pypi_url] = contract.canonical(meta)

    def corrupt(self, problem):
        if problem == "npm-integrity":
            meta = json.loads(self.producers.downloads[self.npm_meta_url])
            meta["dist"]["integrity"] = "sha512-" + base64.b64encode(bytes(64)).decode()
            self.producers.downloads[self.npm_meta_url] = contract.canonical(meta)
        elif problem in ("pypi-digest", "candidate-sdist"):
            self.replace_sdist(update_api=problem == "candidate-sdist")
        elif problem == "engine-checksum":
            for item in self.producers.releases["engine"]["assets"]:
                item.pop("digest", None)
            item = self.engine_asset()
            raw = self.producers.downloads[item["browser_download_url"]]
            self.producers.downloads[item["browser_download_url"]] = bytes([raw[0] ^ 1]) + raw[1:]
        elif problem == "github-digest":
            self.engine_asset()["digest"] = "sha256:" + "f" * 64
        elif problem == "missing-checksum":
            self.producers.releases["engine"]["assets"].remove(self.engine_asset("-linux-x86_64.tar.gz.sha256"))
        elif problem in ("engine-commit", "codegen-commit"):
            role = problem.removesuffix("-commit")
            repo, obj = artifacts.PRODUCERS[role], self.producers.releases[role]
            self.producers.api_data[f"repos/{repo}/git/ref/tags/{obj['tag_name']}"]["object"]["sha"] = "f" * 40
            if role == "codegen":
                old = f"repos/{repo}/git/trees/{self.producers.commits[role]}?recursive=1"
                self.producers.api_data[f"repos/{repo}/git/trees/{'f' * 40}?recursive=1"] = self.producers.api_data[old]
        elif problem in ("checksum-filename", "checksum-marker", "malformed-checksum", "nonascii-checksum"):
            item = self.engine_asset("-linux-x86_64.tar.gz.sha256")
            original = self.producers.downloads[item["browser_download_url"]]
            if problem == "checksum-filename":
                raw = original.split()[0] + b"  another.tar.gz\n"
            elif problem == "checksum-marker":
                raw = original.split()[0] + b"  **" + original.split()[1] + b"\n"
            elif problem == "malformed-checksum":
                raw = original.split()[0] + b"\n"
            else:
                raw = b"\xff\n"
            self.producers.downloads[item["browser_download_url"]] = raw
            item.pop("digest", None)
        else:
            raise AssertionError("unknown negative input")

    def open(self, request, timeout):
        method, url = request.get_method(), request.full_url
        self.requests.append((method, url, request.data, dict(request.header_items())))
        api = f"https://api.github.com/repos/{contract.HUB}"
        upload = f"https://uploads.github.com/repos/{contract.HUB}/releases/1/assets?"
        if url.startswith(upload):
            if method != "POST":
                raise AssertionError("binary upload must be POST")
            name = parse_qs(urlsplit(url).query)["name"][0]
            self.uploads.append((name, request.data))
            if self.fail_upload_number == len(self.uploads):
                raise urllib.error.HTTPError(url, 503, "fixture", {}, None)
            raw = request.data
            if self.readback_problem == "missing":
                return io.BytesIO(b"{}")
            if self.readback_problem == "short":
                raw = raw[:-1]
            elif self.readback_problem == "same-size":
                raw = bytes([raw[0] ^ 1]) + raw[1:]
            self.add_hub_asset(name, raw)
            if self.readback_problem == "404":
                self.unavailable_reads.add(self.row["assets"][-1]["id"])
            return io.BytesIO(b"{}")
        if url == api + "/releases/1":
            if method == "PATCH":
                data = json.loads(request.data)
                self.publications.append(data)
                self.row.update(draft=data["draft"], prerelease=data["prerelease"])
            elif method != "GET":
                raise AssertionError("unexpected hub mutation")
            return io.BytesIO(contract.canonical(self.row))
        if url == api + "/releases?per_page=100&page=1":
            return io.BytesIO(contract.canonical([self.row]))
        if url.startswith(api + "/releases/assets/"):
            asset_id = int(url.rsplit("/", 1)[1])
            if asset_id in self.unavailable_reads:
                raise urllib.error.HTTPError(url, 404, "fixture", {}, None)
            return io.BytesIO(self.data[asset_id])
        if url == api + "/git/ref/tags/" + self.tag:
            return io.BytesIO(contract.canonical({"object": {"type": "commit", "sha": self.manifest["hub_commit"]}}))
        if not self.forbid_producers:
            if url == api + "/git/ref/heads/main":
                return io.BytesIO(contract.canonical({"object": {"sha": "e" * 40}}))
            if url == api + "/contents/facts/facts.json?ref=" + "e" * 40:
                return io.BytesIO(contract.canonical({
                    "encoding": "base64", "content": base64.b64encode(self.facts_raw).decode(),
                    "sha": artifacts.git_blob(self.facts_raw)}))
            if url.startswith("https://api.github.com/"):
                route = url.removeprefix("https://api.github.com/")
                if route in self.producers.api_data:
                    return io.BytesIO(contract.canonical(self.producers.api_data[route]))
            if url in self.producers.downloads:
                raw = self.producers.downloads[url]
                if isinstance(raw, Exception):
                    raise raw
                return io.BytesIO(raw)
        raise AssertionError("unmodeled or forbidden HTTP request: " + url)


class AttachmentTestCase(unittest.TestCase):
    def setUp(self):
        self.http = AttachmentHTTP()
        self.opener = patch("urllib.request.build_opener", return_value=self.http)
        self.opener.start()
        self.addCleanup(self.opener.stop)
        self.client = artifacts.Client(token="fixture-token")
        self.store, self.registry = pipeline.Store(self.client), MemoryRegistry()

    def finish(self):
        return pipeline.finalize(self.client, self.store, self.registry, self.http.row)

    def plan(self, output):
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "offline",
                "RELEASE_READY_EXCLUDE": ",".join(r for r in contract.ROLES if r != "offline"),
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false",
                "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1"}), \
                patch.object(pipeline, "report_issue"), patch.object(pipeline, "age_minutes", return_value=0):
            pipeline.finalize_all(self.client, self.store, self.registry, self.http.tag, output)

    def assert_blocked(self):
        self.assertIsNone(self.http.hub_bytes(contract.ASSET))
        self.assertTrue(self.http.row["draft"])
        self.assertEqual(self.registry.operations, [])
        self.assertEqual(self.http.publications, [])
        self.assertFalse(any(a["name"].startswith("delivery-") for a in self.http.row["assets"]))


class ExtendedManifestTests(AttachmentTestCase):
    def test_exact_assets_and_ten_field_projection_hash_all_raw_bytes(self):
        raw = contract.canonical(self.http.extended)
        event = contract.payload(raw)
        self.assertEqual(set(event), {"schema", "release_version", "prerelease", "hub_commit",
                                     "versions", "image", "facts", "run_id", "event_id", "manifest"})
        self.assertEqual(len(event), 10)
        self.assertNotIn("assets", event)
        self.assertEqual(contract.decode(raw)["assets"], self.http.descriptors)
        for descriptor in contract.decode(raw)["assets"]:
            self.assertEqual(set(descriptor), {"name", "size", "sha256", "source"})
            original = self.http.originals[descriptor["source"]]
            self.assertEqual(descriptor["size"], len(original))
            self.assertEqual(descriptor["sha256"], hashlib.sha256(original).hexdigest())
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(event["event_id"], "sha256:" + digest)
        self.assertEqual(event["manifest"], {"asset": contract.ASSET, "sha256": digest})
        contract.validate_payload(event, raw)
        actual = contract.pair_payload(raw, contract.canonical(self.http.built))
        self.assertEqual(actual["receipt_sha256"], digest)
        self.assertEqual(actual["build"]["candidate_sha256"],
                         hashlib.sha256(contract.canonical(self.http.built)).hexdigest())

    def test_descriptor_tampering_cannot_keep_either_old_hash(self):
        raw = contract.canonical(self.http.extended)
        event = contract.payload(raw)
        changed = copy.deepcopy(self.http.extended)
        changed["assets"][0]["size"] += 1
        changed_raw = contract.canonical(changed)
        for keep in ("both", "event", "manifest"):
            proposed = contract.payload(changed_raw)
            if keep in ("both", "event"):
                proposed["event_id"] = event["event_id"]
            if keep in ("both", "manifest"):
                proposed["manifest"] = event["manifest"]
            with self.subTest(keep=keep), self.assertRaises(contract.Invalid):
                contract.validate_payload(proposed, changed_raw)
        # Valid hashes cannot mask an invalid extension.
        changed["assets"][0]["size"] = True
        with self.assertRaises(contract.Invalid):
            contract.validate_payload(event, contract.canonical(changed))

    def test_reject_duplicate_missing_extra_unsorted_and_nonarray_assets(self):
        assets = self.http.descriptors
        cases = [None, {}, [], assets[::-1], assets + [copy.deepcopy(assets[0])]]
        cases += [assets[:i] + assets[i + 1:] for i in range(len(assets))]
        duplicate = copy.deepcopy(assets)
        duplicate[-1] = copy.deepcopy(duplicate[0])
        cases.append(sorted(duplicate, key=lambda item: item["name"]))
        extra = copy.deepcopy(assets)
        extra.append({**extra[0], "name": "unexpected.tgz",
                      "source": "https://registry.npmjs.org/@pineforge/codegen-pyodide/-/unexpected.tgz"})
        cases.append(sorted(extra, key=lambda item: item["name"]))
        # Four unique names can still omit a required type/architecture.
        for missing in range(len(assets)):
            wrong_kind = copy.deepcopy(assets)
            if urlsplit(assets[missing]["source"]).netloc == "registry.npmjs.org":
                wrong_kind[missing] = {**assets[0], "name": "another.tar.gz",
                                      "source": "https://files.pythonhosted.org/another.tar.gz"}
            else:
                wrong_kind[missing] = {**assets[0], "name": "another.tgz",
                                      "source": "https://registry.npmjs.org/@pineforge/codegen-pyodide/-/another.tgz"}
            cases.append(sorted(wrong_kind, key=lambda item: item["name"]))
        for descriptors in cases:
            with self.subTest(assets=descriptors), self.assertRaises(contract.Invalid):
                contract.payload(contract.canonical({**self.http.manifest, "assets": descriptors}))
        with self.assertRaises(contract.Invalid):
            contract.payload(contract.canonical({**self.http.extended, "extra": []}))

    def test_reject_malformed_descriptor_fields_names_sizes_hashes_and_sources(self):
        for field in ("name", "size", "sha256", "source"):
            obj = copy.deepcopy(self.http.extended)
            del obj["assets"][0][field]
            with self.subTest(missing=field), self.assertRaises(contract.Invalid):
                contract.payload(contract.canonical(obj))
        changes = [
            ("extra", True), ("size", True), ("size", False), ("size", 0),
            ("size", -1), ("size", 1.5), ("size", "1"), ("size", 2**53),
            ("sha256", "F" * 64), ("sha256", "0" * 63), ("sha256", None),
            ("name", "../outside.tgz"), ("name", "/outside.tgz"), ("name", "a\\b.tgz"),
            ("name", ".hidden.tgz"), ("name", "release-manifest.json"),
            ("name", "consumer-offline.tgz"), ("name", "release-audit.tgz"),
            ("name", "delivery-audit.tgz"), ("name", "facts-audit.tgz"),
            ("source", "http://registry.npmjs.org/fixture.tgz"),
            ("source", "https://registry.npmjs.org.evil/fixture.tgz"),
            ("source", "https://user@registry.npmjs.org/fixture.tgz"),
            ("source", "https://registry.npmjs.org:443/fixture.tgz"),
            ("source", "https://github.com/other/releases/download/tag/fixture.tgz"),
            ("source", "https://[invalid/fixture.tgz"), ("source", None),
            ("source", ["https://registry.npmjs.org/fixture.tgz"]),
        ]
        for field, value in changes:
            obj = copy.deepcopy(self.http.extended)
            obj["assets"][0][field] = value
            obj["assets"].sort(key=lambda item: item["name"])
            with self.subTest(field=field, value=value), self.assertRaises(contract.Invalid):
                contract.payload(contract.canonical(obj))
        for descriptor in self.http.descriptors:
            for source in (descriptor["source"] + "?download=1", descriptor["source"] + "#fragment",
                           descriptor["source"].replace("https://", "https://other@"),
                           descriptor["source"].replace("/" + descriptor["name"], "/../" + descriptor["name"]),
                           descriptor["source"].replace("/" + descriptor["name"], "/%2e%2e/" + descriptor["name"]),
                           descriptor["source"].replace(descriptor["name"], "different.tar.gz")):
                obj = copy.deepcopy(self.http.extended)
                next(a for a in obj["assets"] if a["name"] == descriptor["name"])["source"] = source
                with self.subTest(source=source), self.assertRaises(contract.Invalid):
                    contract.payload(contract.canonical(obj))

    def test_legacy_bytes_and_ready_fixture_are_unchanged(self):
        raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        expected = (ROOT / "tests/fixtures/release-ready.json").read_bytes()
        self.assertEqual(contract.canonical(contract.payload(raw)), expected)
        self.assertEqual(contract.canonical(contract.validate(contract.decode(raw))), raw)


class ProducerAttachmentTests(AttachmentTestCase):
    def test_collect_original_outer_bytes_with_existing_producer_authorities(self):
        pins, staged = artifacts.verified_assets(
            self.client, self.http.built["versions"], self.http.built["inventories"]["codegen"])
        self.assertEqual(pins, self.http.manifest["versions"])
        self.assertEqual([descriptor for descriptor, _ in staged], self.http.descriptors)
        self.assertEqual({descriptor["source"]: raw for descriptor, raw in staged}, self.http.originals)
        npm_raw = next(raw for descriptor, raw in staged if descriptor["source"] == self.http.producers.npm_url)
        self.assertIn("package/package.json", artifacts.tar_files(npm_raw))
        self.assertNotEqual(npm_raw, artifacts.tar_files(npm_raw)[
            f"package/pineforge_codegen-{self.http.producers.v}.tar.gz"])
        for _, url, _, headers in self.http.requests:
            if urlsplit(url).netloc in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "github.com"):
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers["Accept"], "*/*")

    def test_changed_sdist_against_unchanged_api_digest_is_rejected_by_real_producer(self):
        # Mutation control: removing the PyPI digest comparison alone makes this succeed.
        self.http.corrupt("pypi-digest")
        with self.assertRaisesRegex(contract.Invalid, "^codegen sdist checksum differs$"):
            artifacts.producer(self.client, "codegen", str(self.http.producers.v), attachments=[])

    def test_changed_engine_against_unchanged_companion_without_api_digest_is_rejected(self):
        # Mutation control: only the companion checksum can reject these bytes.
        self.http.corrupt("engine-checksum")
        with self.assertRaisesRegex(contract.Invalid, "^engine tarball checksum differs$"):
            artifacts.producer(self.client, "engine", str(self.http.producers.v), attachments=[])

    def test_wrong_npm_integrity_with_valid_tar_structure_is_rejected(self):
        self.http.corrupt("npm-integrity")
        self.assertIn("package/package.json", artifacts.tar_files(self.http.producers.downloads[self.http.producers.npm_url]))
        with self.assertRaisesRegex(contract.Invalid, "^npm integrity differs$"):
            artifacts.npm(self.client, self.http.built["versions"]["codegen"],
                          self.http.built["inventories"]["codegen"], attachments=[])

    def test_updated_pypi_digest_cannot_replace_the_candidate_sdist(self):
        # API verification and tagged source still pass; only candidate binding fails.
        self.http.corrupt("candidate-sdist")
        artifacts.producer(self.client, "codegen", str(self.http.producers.v))
        with self.assertRaisesRegex(contract.Invalid, "^codegen producer identity differs from candidate$"):
            artifacts.verified_assets(self.client, self.http.built["versions"],
                                      self.http.built["inventories"]["codegen"])

    def test_conflicting_optional_github_digest_refuses_archive_and_checksum(self):
        for suffix in ("-linux-x86_64.tar.gz", "-linux-x86_64.tar.gz.sha256"):
            self.http = AttachmentHTTP()
            self.http.engine_asset(suffix)["digest"] = "sha256:" + "f" * 64
            with self.subTest(suffix=suffix), patch("urllib.request.build_opener", return_value=self.http), \
                    self.assertRaisesRegex(contract.Invalid, "^release asset digest differs$"):
                artifacts.producer(self.client, "engine", str(self.http.producers.v), attachments=[])

    def test_producer_commit_must_still_equal_candidate_for_each_refetched_producer(self):
        for role in ("engine", "codegen"):
            http = AttachmentHTTP()
            repo = artifacts.PRODUCERS[role]
            tag = http.producers.releases[role]["tag_name"]
            http.producers.api_data[f"repos/{repo}/git/ref/tags/{tag}"]["object"]["sha"] = "f" * 40
            if role == "codegen":
                old = f"repos/{repo}/git/trees/{http.producers.commits[role]}?recursive=1"
                http.producers.api_data[f"repos/{repo}/git/trees/{'f' * 40}?recursive=1"] = http.producers.api_data[old]
            with self.subTest(role=role), patch("urllib.request.build_opener", return_value=http), \
                    self.assertRaisesRegex(contract.Invalid, "^" + role + " producer identity differs from candidate$"):
                artifacts.verified_assets(self.client, http.built["versions"], http.built["inventories"]["codegen"])

    def test_bad_checksum_format_and_filename_are_invalid_and_missing_is_pending(self):
        for problem in ("checksum-filename", "checksum-marker", "malformed-checksum", "nonascii-checksum", "missing-checksum"):
            http = AttachmentHTTP()
            http.corrupt(problem)
            kind = contract.Pending if problem == "missing-checksum" else contract.Invalid
            with self.subTest(problem=problem), patch("urllib.request.build_opener", return_value=http), \
                    self.assertRaises(kind):
                artifacts.verified_assets(self.client, http.built["versions"], http.built["inventories"]["codegen"])

    def test_engine_checksum_accepts_the_single_standard_binary_marker(self):
        item = self.http.engine_asset("-linux-x86_64.tar.gz.sha256")
        raw = self.http.producers.downloads[item["browser_download_url"]]
        self.http.producers.downloads[item["browser_download_url"]] = (
            raw.split()[0] + b" *" + raw.split()[1] + b"\n")
        item.pop("digest", None)
        pins, staged = artifacts.verified_assets(
            self.client, self.http.built["versions"], self.http.built["inventories"]["codegen"])
        self.assertEqual(pins, self.http.manifest["versions"])
        self.assertEqual([d for d, _ in staged], self.http.descriptors)

    def test_ambiguous_engine_asset_is_invalid(self):
        self.http.producers.releases["engine"]["assets"].append(copy.deepcopy(self.http.engine_asset()))
        with self.assertRaisesRegex(contract.Invalid, "^ambiguous or incomplete asset$"):
            artifacts.verified_assets(self.client, self.http.built["versions"],
                                      self.http.built["inventories"]["codegen"])

    def test_real_download_origin_checks_refuse_foreign_producer_urls(self):
        for role in ("npm", "sdist", "engine"):
            http = AttachmentHTTP()
            if role == "npm":
                meta = json.loads(http.producers.downloads[http.npm_meta_url])
                meta["dist"]["tarball"] = "https://files.pythonhosted.org/fixture.tgz"
                http.producers.downloads[http.npm_meta_url] = contract.canonical(meta)
            elif role == "sdist":
                meta = json.loads(http.producers.downloads[http.pypi_url])
                meta["urls"][0]["url"] = "https://registry.npmjs.org/fixture.tar.gz"
                http.producers.downloads[http.pypi_url] = contract.canonical(meta)
            else:
                http.engine_asset()["browser_download_url"] = "https://github.com/other/fixture.tar.gz"
            with self.subTest(role=role), patch("urllib.request.build_opener", return_value=http), \
                    self.assertRaises(contract.Invalid):
                artifacts.verified_assets(self.client, http.built["versions"], http.built["inventories"]["codegen"])
            self.assertEqual(http.uploads, [])

    def test_unsafe_selected_filename_blocks_finalization_even_with_valid_integrity(self):
        original_url = self.http.producers.npm_url
        changed_url = original_url.rsplit("/", 1)[0] + "/release-audit.tgz"
        self.http.producers.downloads[changed_url] = self.http.producers.downloads[original_url]
        meta = json.loads(self.http.producers.downloads[self.http.npm_meta_url])
        meta["dist"]["tarball"] = changed_url
        self.http.producers.downloads[self.http.npm_meta_url] = contract.canonical(meta)
        with self.assertRaisesRegex(contract.Invalid, "^reserved attachment name$"):
            self.finish()
        self.assertEqual(self.http.uploads, [])
        self.assert_blocked()


class BinaryAttachmentTests(AttachmentTestCase):
    def test_real_binary_writer_exact_byte_replay_and_json_writer_scope(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        for _ in range(2):
            self.store.put_binary(self.http.row, descriptor, raw, self.http.manifest["versions"])
        self.assertEqual(self.http.uploads, [(descriptor["name"], raw)])
        self.assertEqual(self.http.hub_bytes(descriptor["name"]), raw)
        self.assertEqual(self.store.verify_binary(self.http.row, descriptor, self.http.manifest["versions"]), raw)
        with self.assertRaises(contract.Invalid):
            self.store.put(self.http.row, descriptor["name"], raw)

    def test_different_existing_bytes_are_never_overwritten(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        changed = bytes([raw[0] ^ 1]) + raw[1:]
        self.http.add_hub_asset(descriptor["name"], changed)
        with self.assertRaises(contract.Invalid):
            self.store.put_binary(self.http.row, descriptor, raw, self.http.manifest["versions"])
        self.assertEqual(self.http.uploads, [])
        self.assertEqual(self.http.hub_bytes(descriptor["name"]), changed)

    def test_existing_attachment_404_is_invalid_and_never_uploaded_again(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        self.http.add_hub_asset(descriptor["name"], raw)
        self.http.unavailable_reads.add(self.http.row["assets"][-1]["id"])
        with self.assertRaisesRegex(contract.Invalid, "^hub attachment unavailable$"):
            self.store.put_binary(self.http.row, descriptor, raw, self.http.manifest["versions"])
        self.assertEqual(self.http.uploads, [])
        self.assertEqual(self.http.hub_bytes(descriptor["name"]), raw)

    def test_input_size_and_digest_are_checked_before_upload(self):
        descriptor = self.http.descriptors[0]
        original = self.http.originals[descriptor["source"]]
        for raw in (original[:-1], bytes([original[0] ^ 1]) + original[1:]):
            with self.subTest(size=len(raw)), self.assertRaises(contract.Invalid):
                self.store.put_binary(self.http.row, descriptor, raw, self.http.manifest["versions"])
            # Removing the input guard cannot hide behind a later readback failure.
            self.assertEqual(self.http.uploads, [])
        with self.assertRaisesRegex(contract.Invalid, "^hub attachment size differs$"):
            self.store.put_binary(self.http.row, {**descriptor, "size": len(original) + 1},
                                  original, self.http.manifest["versions"])
        self.assertEqual(self.http.uploads, [])

    def test_missing_wrong_size_and_same_size_corrupt_readback_fail_without_api_digest(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        for problem in ("missing", "404", "short", "same-size"):
            http = AttachmentHTTP()
            http.readback_problem = problem
            with self.subTest(problem=problem), patch("urllib.request.build_opener", return_value=http), \
                    self.assertRaises(contract.Invalid):
                self.store.put_binary(http.row, descriptor, raw, http.manifest["versions"])
            self.assertEqual(http.uploads, [(descriptor["name"], raw)])

    def test_binary_writer_refuses_unsafe_descriptor_before_any_request(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        for changes in ({"name": "release-manifest.json"}, {"name": "consumer-offline.tgz"},
                        {"source": "https://example.org/fixture.tgz"}, {"size": True}):
            before = len(self.http.requests)
            with self.subTest(changes=changes), self.assertRaises(contract.Invalid):
                self.store.put_binary(self.http.row, {**descriptor, **changes}, raw, self.http.manifest["versions"])
            self.assertEqual(len(self.http.requests), before)

    def test_optional_hub_api_digest_cannot_disagree_with_valid_descriptor_bytes(self):
        descriptor = self.http.descriptors[0]
        raw = self.http.originals[descriptor["source"]]
        self.http.add_hub_asset(descriptor["name"], raw)
        self.http.row["assets"][-1]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(contract.Invalid, "^hub asset digest differs$"):
            self.store.verify_binary(self.http.row, descriptor, self.http.manifest["versions"])


class AttachmentFinalizerTests(AttachmentTestCase):
    def test_finalizer_attaches_all_verified_bytes_before_manifest_and_publication(self):
        event = self.finish()
        stored = json.loads(self.http.hub_bytes(contract.ASSET))
        self.assertEqual(stored["assets"], self.http.descriptors)
        self.assertEqual(set(stored), contract.MANIFEST_KEYS | {"assets"})
        self.assertEqual(self.http.uploads[:4], [
            (d["name"], self.http.originals[d["source"]]) for d in self.http.descriptors])
        self.assertEqual(self.http.uploads[4][0], contract.ASSET)
        self.assertFalse(self.http.row["draft"])
        self.assertEqual(len(event), 10)
        self.assertEqual(event["run_id"], self.http.built["run_id"])
        self.assertEqual(set(json.loads(self.http.hub_bytes("release-ready.json"))), {"event_id", "timestamp"})
        # Every producer read precedes the first upload, not just the manifest.
        first_upload = next(i for i, (_, u, _, _) in enumerate(self.http.requests)
                            if u.startswith("https://uploads.github.com/"))
        producer_reads = [i for i, (_, u, _, _) in enumerate(self.http.requests)
                          if u in self.http.producers.downloads]
        self.assertTrue(producer_reads)
        self.assertLess(max(producer_reads), first_upload)

    def test_every_wrong_authority_blocks_all_uploads_manifest_promotion_and_delivery(self):
        problems = ("npm-integrity", "pypi-digest", "candidate-sdist", "engine-checksum",
                    "github-digest", "checksum-filename", "checksum-marker", "malformed-checksum", "nonascii-checksum",
                    "engine-commit", "codegen-commit")
        for problem in problems:
            self.http, self.registry = AttachmentHTTP(), MemoryRegistry()
            self.http.corrupt(problem)
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as tmp, \
                    patch("urllib.request.build_opener", return_value=self.http):
                output = Path(tmp) / "deliveries.json"
                with self.assertRaisesRegex(contract.Invalid, "release finalization failed"):
                    self.plan(output)
                self.assertEqual(json.loads(output.read_bytes()), [])
                self.assertEqual(self.http.uploads, [])
                self.assert_blocked()

    def test_missing_checksum_stays_pending_and_readiness_cannot_plan_a_delivery(self):
        self.http.corrupt("missing-checksum")
        with self.assertRaises(contract.Pending):
            self.finish()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deliveries.json"
            self.plan(output)
            self.assertEqual(json.loads(output.read_bytes()), [])
        self.assertEqual(self.http.uploads, [])
        self.assert_blocked()

    def test_facts_failure_precedes_every_binary_upload(self):
        facts = json.loads(self.http.facts_raw)
        facts["releases"].pop(self.http.manifest["release_version"])
        self.http.facts_raw = contract.canonical(facts)
        with self.assertRaises(contract.Pending):
            self.finish()
        self.assertEqual(self.http.uploads, [])
        self.assert_blocked()

    def test_partial_upload_keeps_draft_and_retry_reuses_bytes(self):
        self.http.fail_upload_number = 2
        with self.assertRaises(contract.Invalid):
            self.finish()
        first_name, first_raw = self.http.uploads[0]
        self.assertEqual(self.http.hub_bytes(first_name), first_raw)
        self.assert_blocked()
        self.http.fail_upload_number = None
        event = self.finish()
        self.assertEqual(sum(name == first_name for name, _ in self.http.uploads), 1)
        self.assertEqual(event, contract.payload(self.http.hub_bytes(contract.ASSET)))
        self.assertEqual(json.loads(self.http.hub_bytes(contract.ASSET))["assets"], self.http.descriptors)

    def test_corrupt_upload_readback_blocks_the_manifest_and_all_later_operations(self):
        for problem in ("missing", "404", "short", "same-size"):
            self.http, self.registry = AttachmentHTTP(), MemoryRegistry()
            self.http.readback_problem = problem
            with self.subTest(problem=problem), patch("urllib.request.build_opener", return_value=self.http), \
                    self.assertRaises(contract.Invalid):
                self.finish()
            self.assert_blocked()

    def test_extended_replay_verifies_hub_bytes_without_producer_resolution(self):
        first = self.finish()
        raw = self.http.hub_bytes(contract.ASSET)
        uploads = list(self.http.uploads)
        self.http.forbid_producers = True
        self.http.requests.clear()
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "999"}):
            again = self.finish()
        self.assertEqual(again, first)
        self.assertEqual(self.http.hub_bytes(contract.ASSET), raw)
        self.assertEqual(again["run_id"], self.http.built["run_id"])
        self.assertEqual(self.http.uploads, uploads)
        read_ids = {int(url.rsplit("/", 1)[1]) for _, url, _, _ in self.http.requests
                    if "/releases/assets/" in url}
        for descriptor in self.http.descriptors:
            item = next(a for a in self.http.row["assets"] if a["name"] == descriptor["name"])
            self.assertIn(item["id"], read_ids)

    def test_interruption_after_manifest_commit_replays_without_resolving_producers(self):
        # Fail the external registry operation at the first promotion.
        with patch.object(self.registry, "point", side_effect=contract.Invalid("registry unavailable")), \
                self.assertRaisesRegex(contract.Invalid, "^registry unavailable$"):
            self.finish()
        raw = self.http.hub_bytes(contract.ASSET)
        self.assertIsNotNone(raw)
        self.assertTrue(self.http.row["draft"])
        self.http.forbid_producers = True
        uploads = list(self.http.uploads)
        event = self.finish()
        self.assertEqual(self.http.hub_bytes(contract.ASSET), raw)
        self.assertEqual(event, contract.payload(raw))
        self.assertEqual(self.http.uploads[:len(uploads)], uploads)
        self.assertFalse(self.http.row["draft"])

    def test_committed_manifest_with_missing_or_corrupt_attachment_cannot_promote_or_deliver(self):
        for problem in ("missing", "404", "short", "same-size"):
            self.http, self.registry = AttachmentHTTP(), MemoryRegistry()
            with self.subTest(problem=problem), patch("urllib.request.build_opener", return_value=self.http):
                self.finish()
                raw = self.http.hub_bytes(contract.ASSET)
                first = self.http.descriptors[0]
                item = next(a for a in self.http.row["assets"] if a["name"] == first["name"])
                if problem == "missing":
                    self.http.row["assets"].remove(item)
                elif problem == "404":
                    self.http.unavailable_reads.add(item["id"])
                else:
                    original = self.http.data[item["id"]]
                    self.http.data[item["id"]] = (original[:-1] if problem == "short"
                                                 else bytes([original[0] ^ 1]) + original[1:])
                self.registry.operations.clear()
                self.http.uploads.clear()
                self.http.publications.clear()
                self.http.forbid_producers = True
                with tempfile.TemporaryDirectory() as tmp:
                    output = Path(tmp) / "deliveries.json"
                    with self.assertRaisesRegex(contract.Invalid, "release finalization failed"):
                        self.plan(output)
                    self.assertEqual(json.loads(output.read_bytes()), [])
                self.assertEqual(self.http.hub_bytes(contract.ASSET), raw)
                self.assertEqual(self.registry.operations, [])
                self.assertEqual(self.http.publications, [])
                self.assertEqual(self.http.uploads, [])

    def test_legacy_replay_never_resolves_producers_or_extends_stored_bytes(self):
        legacy_raw = contract.canonical(self.http.manifest)
        self.http.add_hub_asset(contract.ASSET, legacy_raw)
        self.http.forbid_producers = True
        first = self.finish()
        self.assertEqual(first, self.finish())
        self.assertEqual(self.http.hub_bytes(contract.ASSET), legacy_raw)
        self.assertNotIn("assets", json.loads(legacy_raw))
        self.assertEqual(first["event_id"], "sha256:" + hashlib.sha256(legacy_raw).hexdigest())
        self.assertEqual(first["run_id"], self.http.built["run_id"])
        self.assertFalse(any(name.endswith((".tgz", ".tar.gz")) for name, _ in self.http.uploads))


if __name__ == "__main__":
    unittest.main()
