#!/usr/bin/env python3
"""Dual product names share real manifest bytes; run on CI/lab only.

Only registry transport/storage and GitHub boundaries are substituted. Digest,
copy planning, fixed-tag guards, finalization and rollback are production code.
"""
from __future__ import annotations

import copy
import base64
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import release_contract as contract
import release_artifacts as artifacts
import release_pipeline as pipeline
import release_rollback as rollback
import release_smoke as smoke
from test_release_binding import FinalizeClient, MemoryStore, fixture_manifest


class RegistryTransport(pipeline.Registry):
    """The external registry stores bytes and implements imagetools copy effects."""

    def __init__(self):
        self.tags, self.blobs, self.calls = {}, {}, []
        self.corrupt = None
        super().__init__(runner=self.run)

    def seed(self, repository, tag, raw):
        digest = "sha256:" + contract.sha256(raw)
        self.blobs[repository, digest] = raw
        self.tags[repository, tag] = digest
        return digest

    def raw(self, ref, *, repository=contract.IMAGE):
        digest = ref if ref.startswith("sha256:") else self.tags.get((repository, ref))
        if (repository, digest) not in self.blobs:
            raise contract.Pending("registry manifest unavailable")
        return self.blobs[repository, digest]

    def run(self, *args):
        if args[:5] != ("docker", "buildx", "imagetools", "create", "--prefer-index=false"):
            raise AssertionError("unmodeled registry command")
        self.calls.append(args)
        source, digest = args[-1].split("@", 1)
        raw = self.blobs[source, digest]
        targets = args[5:-1]
        if len(targets) % 2:
            raise AssertionError("malformed tag options")
        for flag, reference in zip(targets[::2], targets[1::2]):
            if flag != "--tag":
                raise AssertionError("unexpected imagetools option")
            repository, tag = reference.rsplit(":", 1)
            self.seed(repository, tag, raw + b" " if repository == self.corrupt else raw)
        return b""


class RegistryHTTPTransport:
    """HTTP and copy boundaries only; Client.request/Registry.raw remain real."""

    def __init__(self):
        self.storage = RegistryTransport()
        self.before, self.after = {}, {}
        self.tokenless = set()
        self.reads = []

    def open(self, request, *, timeout):
        url = request.full_url
        for repository in contract.IMAGES:
            path = repository.removeprefix("ghcr.io/")
            token_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:" + path + ":pull"
            manifest_url = "https://ghcr.io/v2/" + path + "/manifests/"
            if url == token_url:
                return self.reply(repository, "token", "", request)
            if url.startswith(manifest_url):
                return self.reply(repository, "manifest", url.removeprefix(manifest_url), request)
        raise AssertionError("unmodeled HTTP registry request")

    def reply(self, repository, endpoint, ref, request):
        copied = bool(self.storage.calls)
        self.reads.append((repository, endpoint, copied, dict(request.header_items())))
        fault = (self.after if copied else self.before).get((repository, endpoint))
        if fault is not None:
            status, body = fault
            if status == 200:
                return io.BytesIO(body)
            raise urllib.error.HTTPError(request.full_url, status, "registry response", {}, io.BytesIO(body))
        if endpoint == "token":
            return io.BytesIO(contract.canonical({} if repository in self.tokenless else {"token": "test-pull"}))
        try:
            raw = self.storage.raw(ref, repository=repository)
        except contract.Pending:
            raise urllib.error.HTTPError(request.full_url, 404, "missing manifest", {}, io.BytesIO(b"{}")) from None
        return io.BytesIO(raw)


class FirstRegistryCopyTests(unittest.TestCase):
    def setUp(self):
        self.transport = RegistryHTTPTransport()
        self.registry = pipeline.Registry(runner=self.transport.storage.run)
        self.tag = "candidate-first-copy"
        self.raw = contract.canonical({"schemaVersion": 2, "manifests": []})
        self.digest = self.transport.storage.seed(contract.IMAGE, self.tag, self.raw)
        self.new = contract.IMAGES[1]
        self.opener = patch.object(artifacts.urllib.request, "build_opener", return_value=self.transport)
        self.opener.start()
        self.addCleanup(self.opener.stop)

    def denied(self, status=403, code="DENIED"):
        return status, contract.canonical({"errors": [{"code": code}]})

    def test_new_name_denied_before_copy_is_absent_only_after_old_tag_read_succeeds(self):
        for endpoint, status in (("token", 401), ("token", 403), ("manifest", 403)):
            with self.subTest(endpoint=endpoint, status=status):
                transport = RegistryHTTPTransport()
                transport.storage.seed(contract.IMAGE, self.tag, self.raw)
                transport.before[self.new, endpoint] = self.denied(status)
                with patch.object(artifacts.urllib.request, "build_opener", return_value=transport):
                    registry = pipeline.Registry(runner=transport.storage.run)
                    registry.mirror_candidate(self.tag, self.digest)
                    registry.assert_equal(self.tag, self.digest)
                self.assertEqual(len(transport.storage.calls), 1)
                old_read = next(i for i, read in enumerate(transport.reads)
                                if read[:3] == (contract.IMAGE, "manifest", False))
                denied_read = next(i for i, read in enumerate(transport.reads)
                                   if read[:3] == (self.new, endpoint, False))
                self.assertLess(old_read, denied_read)

    def test_tokenless_new_name_reaches_actual_manifest_403_then_copies(self):
        self.transport.tokenless.add(self.new)
        self.transport.before[self.new, "manifest"] = self.denied()
        self.registry.mirror_candidate(self.tag, self.digest)
        self.registry.assert_equal(self.tag, self.digest)
        reads = [read for read in self.transport.reads if read[:2] == (self.new, "manifest")]
        self.assertTrue(reads)
        for read in reads:
            self.assertNotIn("Authorization", read[3])

    def test_named_registry_error_replies_are_classified_before_copy(self):
        for code in ("DENIED", "UNAUTHORIZED", "NAME_UNKNOWN"):
            for body in ({"errors": [{"code": code}]}, {"error": code.lower()}):
                with self.subTest(body=body):
                    transport = RegistryHTTPTransport()
                    transport.storage.seed(contract.IMAGE, self.tag, self.raw)
                    transport.before[self.new, "token"] = 200, contract.canonical(body)
                    with patch.object(artifacts.urllib.request, "build_opener", return_value=transport):
                        registry = pipeline.Registry(runner=transport.storage.run)
                        registry.mirror_candidate(self.tag, self.digest)
                        registry.assert_equal(self.tag, self.digest)
                    self.assertEqual(len(transport.storage.calls), 1)

    def test_same_new_name_403_after_copy_is_a_failure(self):
        for endpoint in ("token", "manifest"):
            with self.subTest(endpoint=endpoint):
                transport = RegistryHTTPTransport()
                transport.storage.seed(contract.IMAGE, self.tag, self.raw)
                transport.before[self.new, endpoint] = self.denied()
                transport.after[self.new, endpoint] = self.denied()
                with patch.object(artifacts.urllib.request, "build_opener", return_value=transport):
                    registry = pipeline.Registry(runner=transport.storage.run)
                    with self.assertRaises(artifacts.RegistryReadError):
                        registry.mirror_candidate(self.tag, self.digest)
                self.assertEqual(len(transport.storage.calls), 1)

    def test_old_name_403_never_authorizes_copy(self):
        for endpoint in ("token", "manifest"):
            with self.subTest(endpoint=endpoint):
                self.transport.before = {(contract.IMAGE, endpoint): self.denied()}
                with self.assertRaises(artifacts.RegistryReadError):
                    self.registry.point(self.tag, self.digest, fixed=True)
                self.assertEqual(self.transport.storage.calls, [])

    def test_old_tag_absent_direct_denial_and_other_http_errors_remain_strict(self):
        self.transport.before[self.new, "token"] = self.denied()
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.point("missing-old-tag", self.digest, fixed=True)
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.digest(self.tag, repository=self.new)
        self.assertEqual(self.transport.storage.calls, [])
        for status in (429, 500):
            self.transport.before[self.new, "token"] = status, b"{}"
            with self.subTest(status=status), self.assertRaises(contract.Invalid):
                self.registry.point(self.tag, self.digest, fixed=True)
            self.assertEqual(self.transport.storage.calls, [])

    def test_corrupted_destination_digest_still_fails_after_denied_preflight(self):
        self.transport.before[self.new, "token"] = self.denied()
        self.transport.storage.corrupt = self.new
        with self.assertRaisesRegex(contract.Invalid, "digests differ"):
            self.registry.mirror_candidate(self.tag, self.digest)
        self.assertNotEqual(self.registry.digest(self.tag), self.registry.digest(self.tag, repository=self.new))
        self.assertEqual(len(self.transport.storage.calls), 1)


class DualImageTests(unittest.TestCase):
    def setUp(self):
        self.registry = RegistryTransport()
        self.manifest = fixture_manifest()
        self.index = contract.canonical({"schemaVersion": 2, "manifests": [
            {"digest": self.manifest["image"][arch + "_digest"],
             "platform": {"architecture": arch, "os": "linux"}}
            for arch in ("amd64", "arm64")]})
        self.tag = "candidate-verified-42-1"
        self.digest = self.registry.seed(contract.IMAGE, self.tag, self.index)
        self.manifest["image"].update(digest=self.digest, repositories=list(contract.IMAGES))

    def assert_both(self, tag, digest):
        observed = self.registry.digests(tag)
        self.assertEqual(observed, {name: digest for name in contract.IMAGES})
        self.registry.assert_equal(tag, digest)

    def test_candidate_copy_requires_original_tag_and_compares_real_bytes(self):
        with self.assertRaises(contract.Invalid):
            self.registry.mirror_candidate(self.tag, "sha256:" + contract.sha256(self.index + b"\n"))
        self.assertEqual(self.registry.calls, [])
        self.registry.mirror_candidate(self.tag, self.digest)
        self.assert_both(self.tag, self.digest)
        self.assertEqual(len(self.registry.calls), 1)
        self.assertEqual(self.registry.calls[0][-1], contract.IMAGE + "@" + self.digest)
        self.assertEqual(self.registry.calls[0][6:-1:2],
                         tuple(name + ":" + self.tag for name in contract.IMAGES))
        self.registry.mirror_candidate(self.tag, self.digest)
        self.assertEqual(len(self.registry.calls), 1)

    def test_different_destination_digest_fails_actual_post_copy_assertion(self):
        self.registry.corrupt = contract.IMAGES[1]
        with self.assertRaisesRegex(contract.Invalid, "digests differ"):
            self.registry.mirror_candidate(self.tag, self.digest)
        observed = self.registry.digests(self.tag)
        self.assertNotEqual(observed[contract.IMAGES[0]], observed[contract.IMAGES[1]])
        self.assertEqual(observed[contract.IMAGES[0]], "sha256:" + contract.sha256(self.index))
        self.assertEqual(observed[contract.IMAGES[1]], "sha256:" + contract.sha256(self.index + b" "))

    def test_fixed_tag_conflict_on_second_name_prevents_every_write(self):
        self.registry.seed(contract.IMAGES[1], "1.6.0", self.index + b"\n")
        with self.assertRaisesRegex(contract.Invalid, "fixed image identity"):
            self.registry.point("1.6.0", self.digest, fixed=True)
        self.assertEqual(self.registry.calls, [])
        self.assertIsNone(self.registry.digest("1.6.0"))

    def finish(self):
        client, store = FinalizeClient(self.manifest), MemoryStore(self.manifest)
        with patch.object(pipeline, "npm", side_effect=lambda _, pin, inventory: {
                **pin, "npm_version": pin["version"],
                "npm_integrity": self.manifest["versions"]["codegen"]["npm_integrity"]}):
            event = pipeline.finalize(client, store, self.registry, store.get("v1.6.0"))
        return client, store, event

    def test_finalizer_moves_all_fixed_and_moving_tags_on_both_names_then_rollback(self):
        previous_raw = contract.canonical({"schemaVersion": 2, "release": "previous"})
        previous = self.registry.seed(contract.IMAGE, "latest", previous_raw)
        self.registry.seed(contract.IMAGE, "1.6", previous_raw)
        self.registry.mirror_candidate(self.tag, self.digest)
        client, store, event = self.finish()
        for tag in ("1.6.0", "engine1.6.0-codegen1.6.0", "sha-" + event["hub_commit"][:7], "latest", "1.6"):
            self.assert_both(tag, self.digest)
        for call in self.registry.calls:
            targets = call[6:-1:2]
            self.assertEqual(len(targets), len(contract.IMAGES))
            self.assertEqual({ref.rsplit(":", 1)[0] for ref in targets}, set(contract.IMAGES))
        before = store.read(store.get("v1.6.0"), "release-aliases-before.json")
        self.assertEqual(contract.decode(before), {"latest": previous, "1.6": previous})
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}), patch.object(rollback, "report_issue"):
            rollback.rollback(client, store, self.registry, "v1.6.0")
        for tag in ("latest", "1.6"):
            self.assert_both(tag, previous)
        self.assert_both("1.6.0", self.digest)
        self.assertEqual(store.read(store.get("v1.6.0"), "release-aliases-before.json"), before)

    def test_rollback_repairs_a_partial_prior_restore_and_does_not_move_newer_aliases(self):
        good_raw = contract.canonical({"release": "good"})
        good = self.registry.seed(contract.IMAGE, "latest", good_raw)
        self.registry.seed(contract.IMAGES[1], "latest", self.index)
        self.assertTrue(self.registry.restore("latest", self.digest, good))
        self.assert_both("latest", good)
        newer_raw = contract.canonical({"release": "newer"})
        newer = self.registry.seed(contract.IMAGE, "latest", newer_raw)
        self.registry.seed(contract.IMAGES[1], "latest", newer_raw)
        calls = len(self.registry.calls)
        self.assertTrue(self.registry.restore("latest", self.digest, good))
        self.assert_both("latest", newer)
        self.assertEqual(len(self.registry.calls), calls)

    def test_conflicting_prior_aliases_and_partial_supersession_fail(self):
        good = self.registry.seed(contract.IMAGE, "latest", self.index + b"\n")
        self.registry.seed(contract.IMAGES[1], "latest", self.index)
        with self.assertRaisesRegex(contract.Invalid, "prior image alias"):
            self.registry.alias_digest("latest")
        with self.assertRaisesRegex(contract.Invalid, "partly superseded"):
            self.registry.restore("latest", self.digest, "sha256:" + contract.sha256(b"another"))
        self.assertNotEqual(good, self.digest)
        self.assertEqual(self.registry.calls, [])

    def test_smoke_uses_either_recorded_repository_and_new_index_records_both(self):
        with patch("release_pipeline.Registry.raw", side_effect=self.registry.raw):
            image = smoke.inspect_index(self.digest)
        self.assertEqual(image["repositories"], list(contract.IMAGES))
        self.assertEqual(image["digest"], self.digest)
        for repository in contract.IMAGES:
            obj = copy.deepcopy(self.manifest)
            obj["image"]["repository"] = repository
            self.assertEqual(contract.payload(contract.canonical(obj))["image"]["digest"], self.digest)

    def test_image_identity_uses_digests_and_preserves_legacy_records(self):
        old = fixture_manifest()["image"]
        raw = contract.canonical(old)
        renamed = {**old, "repository": contract.IMAGES[1], "repositories": list(contract.IMAGES)}
        self.assertTrue(contract.same_image(old, renamed))
        renamed["digest"] = "sha256:" + contract.sha256(self.index)
        self.assertFalse(contract.same_image(old, renamed))
        self.assertEqual(contract.canonical(old), raw)


class ReleaseTextTests(unittest.TestCase):
    def test_registry_reads_both_names_and_authenticates_first_private_package_readback(self):
        raw = contract.canonical({"schemaVersion": 2, "manifests": []})
        calls = []

        def request(url, **options):
            calls.append((url, options))
            for repository in contract.IMAGES:
                path = repository.removeprefix("ghcr.io/")
                if url == "https://ghcr.io/token?service=ghcr.io&scope=repository:" + path + ":pull":
                    return contract.canonical({"token": "test-registry-pull"})
                if url == "https://ghcr.io/v2/" + path + "/manifests/candidate-test":
                    return raw
            raise AssertionError("unmodeled registry request")

        with patch.dict(os.environ, {"GH_TOKEN": "test-workflow-token", "GITHUB_ACTOR": "test-actor"}), \
                patch.object(pipeline.Client, "request", side_effect=request):
            observed = pipeline.Registry().digests("candidate-test")
        self.assertEqual(observed, {repository: "sha256:" + contract.sha256(raw) for repository in contract.IMAGES})
        basic = "Basic " + base64.b64encode(b"test-actor:test-workflow-token").decode()
        for url, options in calls:
            self.assertEqual(options["extra_headers"]["Authorization"],
                             basic if "/token?" in url else "Bearer test-registry-pull")

    def test_real_store_creates_and_publishes_tunaxis_titles_in_the_existing_hub(self):
        calls = []

        class GitHubTransport:
            def pages(self, route):
                calls.append(("GET", route, None))
                return []

            def tag_commit(self, repository, tag):
                calls.append(("TAG", repository, tag))
                return "a" * 40

            def api(self, route, *, method="GET", data=None):
                calls.append((method, route, data))
                return {"id": 1, **data}

        store = pipeline.Store(GitHubTransport())
        notes = "Tunaxis (formerly PineForge): verified image.\n"
        row = store.ensure_candidate_release("v1.6.0", notes)
        self.assertEqual(row["name"], "Tunaxis v1.6.0")
        self.assertEqual(row["body"], notes)
        self.assertTrue(row["draft"])
        published = store.publish(row, True)
        self.assertEqual(published["name"], "Tunaxis v1.6.0")
        self.assertFalse(published["draft"])
        self.assertEqual(calls[-1][1], "repos/" + contract.HUB + "/releases/1")


if __name__ == "__main__":
    unittest.main()
