#!/usr/bin/env python3
"""Dual product names share real manifest bytes; run on CI/lab only.

Only registry transport/storage and GitHub boundaries are substituted. Digest,
copy planning, fixed-tag guards, finalization and rollback are production code.
"""
from __future__ import annotations

import copy
import base64
import http.client
import io
import json
import os
import sys
import tempfile
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


class UnreadableBody(io.BytesIO):
    def __init__(self, error):
        super().__init__()
        self.error = error
        self.read_calls = 0

    def read(self, *args):
        self.read_calls += 1
        raise self.error


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
            stream = body if hasattr(body, "read") else io.BytesIO(body)
            if status == 200:
                return stream
            raise urllib.error.HTTPError(request.full_url, status, "registry response", {}, stream)
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

    def test_error_body_read_failures_preserve_http_status_classification(self):
        for status in (401, 403, 500):
            for error in (TimeoutError("error body timeout"), ConnectionResetError("error body reset"),
                          http.client.IncompleteRead(b"partial error body")):
                with self.subTest(status=status, error=type(error).__name__):
                    body = UnreadableBody(error)
                    self.transport.before[contract.IMAGE, "manifest"] = status, body
                    expected = artifacts.RegistryReadError if status in (401, 403) else contract.Invalid
                    with self.assertRaises(expected) as caught:
                        self.registry.digest(self.tag)
                    self.assertEqual(body.read_calls, 1)
                    if status in (401, 403):
                        self.assertEqual(caught.exception.status, status)
                    else:
                        self.assertIs(type(caught.exception), contract.Invalid)
                        self.assertIn("HTTP 500", str(caught.exception))
                    self.assertEqual(self.transport.storage.calls, [])

    def test_incomplete_denial_body_before_copy_does_not_weaken_post_copy_reads(self):
        before = UnreadableBody(http.client.IncompleteRead(b"before"))
        after = UnreadableBody(http.client.IncompleteRead(b"after"))
        self.transport.before[self.new, "manifest"] = 403, before
        self.transport.after[self.new, "manifest"] = 403, after
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.mirror_candidate(self.tag, self.digest)
        self.assertEqual(before.read_calls, 1)
        self.assertEqual(after.read_calls, 1)
        self.assertEqual(len(self.transport.storage.calls), 1)

    def test_rollback_files_incident_and_exits_nonzero_when_body_reads_raise_incomplete_read(self):
        for status in (500, 200):
            with self.subTest(status=status):
                manifest = fixture_manifest()
                manifest["image"]["digest"] = self.digest
                client, store = FinalizeClient(manifest), MemoryStore(manifest)
                row = store.get("v1.6.0")
                store.put(row, contract.ASSET, contract.canonical(manifest))
                previous_raw = contract.canonical({"release": "previous body-read test target"})
                previous = self.transport.storage.seed(contract.IMAGE, "previous", previous_raw)
                for alias in ("latest", "1.6"):
                    self.transport.storage.seed(contract.IMAGE, alias, self.raw)
                store.put(row, "release-aliases-before.json",
                          contract.canonical({"latest": previous, "1.6": previous}))
                body = UnreadableBody(http.client.IncompleteRead(b"partial response"))
                self.transport.before[contract.IMAGE, "manifest"] = status, body
                reports = []

                def report(client, key, title, text):
                    reports.append(text)

                # A 500 exercises the guarded error-body read; a broken 200
                # exercises the alias loop's catch for an unexpected Exception.
                with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}), \
                        patch.object(rollback, "Client", return_value=client), \
                        patch.object(rollback, "Store", return_value=store), \
                        patch.object(rollback, "Registry", return_value=self.registry), \
                        patch.object(rollback, "report_issue", side_effect=report), \
                        patch.object(sys, "argv", ["release_rollback.py", "--tag", "v1.6.0"]):
                    self.assertEqual(rollback.main(), 1)
                self.assertEqual(body.read_calls, 2)
                self.assertEqual(len(reports), 1)
                for alias in ("latest", "1.6"):
                    self.assertIn("- " + alias + ":", reports[0])
                self.assertEqual(self.transport.storage.calls, [])

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

    def test_later_point_cannot_treat_new_name_denial_as_absent_after_a_successful_copy(self):
        self.transport.before[self.new, "token"] = self.denied()
        self.registry.point(self.tag, self.digest, fixed=True)
        next_tag = "candidate-next-copy"
        self.transport.storage.seed(contract.IMAGE, next_tag, self.raw)
        self.transport.after[self.new, "token"] = self.denied()
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.point(next_tag, self.digest, fixed=True)
        self.assertEqual(len(self.transport.storage.calls), 1)
        self.assertNotIn((self.new, next_tag), self.transport.storage.tags)

    def test_successful_new_name_read_disables_first_copy_exception_without_a_write(self):
        for reader in ("raw", "digest"):
            with self.subTest(reader=reader):
                transport = RegistryHTTPTransport()
                for repository in contract.IMAGES:
                    transport.storage.seed(repository, self.tag, self.raw)
                with patch.object(artifacts.urllib.request, "build_opener", return_value=transport):
                    registry = pipeline.Registry(runner=transport.storage.run)
                    ref = self.digest if reader == "raw" else self.tag
                    getattr(registry, reader)(ref, repository=self.new)
                    transport.before[self.new, "manifest"] = self.denied()
                    with self.assertRaises(artifacts.RegistryReadError):
                        registry.point(self.tag, self.digest, fixed=True)
                self.assertEqual(transport.storage.calls, [])

    def test_successful_write_disables_exception_even_if_its_readback_fails(self):
        self.transport.before[self.new, "token"] = self.denied()
        self.transport.after[self.new, "token"] = self.denied()
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.point(self.tag, self.digest, fixed=True)
        next_tag = "candidate-after-failed-readback"
        self.transport.storage.seed(contract.IMAGE, next_tag, self.raw)
        with self.assertRaises(artifacts.RegistryReadError):
            self.registry.point(next_tag, self.digest, fixed=True)
        self.assertEqual(len(self.transport.storage.calls), 1)
        self.assertNotIn((self.new, next_tag), self.transport.storage.tags)

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
        self.assertEqual(contract.payload(store.read(store.get("v1.6.0"), contract.ASSET)), event)
        self.assertIsNone(pipeline.finalize(client, store, self.registry, store.get("v1.6.0")))

    def test_rollback_reports_partial_supersession_continues_other_aliases_and_exits_nonzero(self):
        for previous_minor in (True, False):
            with self.subTest(previous_minor=previous_minor):
                self.registry = RegistryTransport()
                self.registry.seed(contract.IMAGE, self.tag, self.index)
                previous_raw = contract.canonical({"release": "previous good rollback target"})
                previous = self.registry.seed(contract.IMAGE, "latest", previous_raw)
                if previous_minor:
                    self.registry.seed(contract.IMAGE, "1.6", previous_raw)
                client, store, event = self.finish()
                # Historical alias evidence may list latest first. Preserve
                # its actual values while modeling that valid JSON ordering,
                # so the failing alias precedes the still-restorable one.
                before = contract.decode(store.read(store.get("v1.6.0"), "release-aliases-before.json"))
                store.assets["v1.6.0", "release-aliases-before.json"] = (
                    json.dumps({alias: before[alias] for alias in ("latest", "1.6")}) + "\n").encode()
                newer_raw = contract.canonical({"release": "newer owner"})
                newer = self.registry.seed(contract.IMAGES[1], "latest", newer_raw)
                reports = []

                def report(client, key, title, body):
                    reports.append((key, title, body))

                # Only external storage, registry transport and incident writes
                # are substituted. main(), rollback() and Registry.restore()
                # all execute their production failure/continuation logic.
                with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}), \
                        patch.object(rollback, "Client", return_value=client), \
                        patch.object(rollback, "Store", return_value=store), \
                        patch.object(rollback, "Registry", return_value=self.registry), \
                        patch.object(rollback, "report_issue", side_effect=report), \
                        patch.object(sys, "argv", ["release_rollback.py", "--tag", "v1.6.0"]):
                    self.assertEqual(rollback.main(), 1)
                self.assertEqual(len(reports), 1)
                body = reports[0][2]
                self.assertIn("latest: rollback alias partly superseded", body)
                self.assertEqual(self.registry.digests("latest"),
                                 {contract.IMAGE: self.digest, contract.IMAGES[1]: newer})
                if previous_minor:
                    self.assert_both("1.6", previous)
                    self.assertNotIn("1.6:", body)
                else:
                    self.assert_both("1.6", self.digest)
                    self.assertIn("1.6: no prior good identity", body)
                self.assert_both("1.6.0", self.digest)
                self.assertEqual(contract.payload(store.read(store.get("v1.6.0"), contract.ASSET)), event)
                self.assertIsNone(pipeline.finalize(client, store, self.registry, store.get("v1.6.0")))

    def test_finalizer_and_rollback_build_the_same_incident_title_prefix(self):
        previous_raw = contract.canonical({"release": "previous title test target"})
        for alias in ("latest", "1.6"):
            self.registry.seed(contract.IMAGE, alias, previous_raw)
        client, store, event = self.finish()
        reports = []

        def report(client, key, title, body, **options):
            reports.append((key, title))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1", "RELEASE_READY_CONSUMERS": "",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "report_issue", side_effect=report), \
                patch.object(rollback, "report_issue", side_effect=report):
            pipeline.finalize_all(client, store, self.registry, "v1.6.0", Path(tmp) / "deliveries.json")
            rollback.rollback(client, store, self.registry, "v1.6.0")
        self.assertEqual(reports, [("v1.6.0", "Tunaxis 1.6.0: complete"),
                                   ("v1.6.0", "Tunaxis 1.6.0: rejected / rollback")])
        for state in ("readiness pending", "readiness failed", "consumer failed"):
            self.assertEqual(pipeline.release_incident_title("v1.6.0", state),
                             pipeline.release_incident_title(event["release_version"], state))

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
    def test_issue_title_only_drift_is_repaired_once_under_the_existing_marker(self):
        issue = {"number": 7, "title": "Release v1.6.0: readiness pending", "state": "open",
                 "body": "<!-- pineforge-release-chain:v1.6.0 -->\n\nWaiting for artifacts."}
        updates = []

        class GitHubTransport:
            def pages(self, route):
                return [issue]

            def api(self, route, *, method="GET", data=None):
                updates.append((route, method, data))
                issue.update(data)
                return issue

        client = GitHubTransport()
        title = pipeline.release_incident_title("v1.6.0", "readiness pending")
        with patch.dict(os.environ, {"RELEASE_REPORT_TOKEN": "", "RELEASE_REPORT_OWNER": ""}):
            for _ in range(2):
                pipeline.report_issue(client, "v1.6.0", title, "Waiting for artifacts.")
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0:2], ("repos/" + contract.HUB + "/issues/7", "PATCH"))
        self.assertEqual(updates[0][2]["title"], "Tunaxis 1.6.0: readiness pending")
        self.assertEqual(issue["body"], "<!-- pineforge-release-chain:v1.6.0 -->\n\nWaiting for artifacts.")

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
