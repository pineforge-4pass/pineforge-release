#!/usr/bin/env python3
"""Manual redelivery regressions; execute in CI, never on the operator's Mac.

The existing store fake keys every asset by release and exact name. Only HTTP
transport is replaced: Client.api/request still serialize the real wire bytes.
Each test names a one-line production mutation that must make it fail.
"""
from __future__ import annotations

import copy
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
from test_release_binding import MemoryStore, fixture_manifest  # noqa: E402

TAG = "v1.6.0"
CONSUMER = "hosted"
TARGET = "fixture-hosted"
CLOCK = "2026-10-23T00:00:00Z"


class PublicHub:
    """Named public GETs only; an authenticated read or any write is a failure."""

    def __init__(self, store):
        self.store = store
        self.reads = []

    def api(self, route, *, method="GET", data=None):
        if method != "GET" or data is not None:
            raise AssertionError("public hub client must never mutate")
        self.reads.append(route)
        prefix = f"repos/{contract.HUB}/releases/tags/"
        if route.startswith(prefix):
            # Also expose a draft fixture to exercise the explicit draft gate.
            return self.store.rows[route.removeprefix(prefix)]
        return self.store.api(route, method=method, data=data)

    def pages(self, route):
        self.reads.append(route)
        return self.store.pages(route)

    def request(self, url, **kwargs):
        self.reads.append(url)
        return self.store.request(url, **kwargs)

    def download(self, url, origins):
        if origins != {"github.com"}:
            raise AssertionError("unexpected manifest download origins")
        for tag, row in self.store.rows.items():
            if url == f"https://github.com/{contract.HUB}/releases/download/{tag}/{contract.ASSET}":
                self.reads.append(url)
                raw = self.store.read(row, contract.ASSET)
                if raw is None:
                    raise contract.Pending("public manifest unavailable")
                return raw
        raise AssertionError("unmodeled public manifest URL " + url)


class RedeliverTest(unittest.TestCase):
    def setUp(self):
        self.manifest = fixture_manifest()
        self.raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        self.event = contract.decode((ROOT / "tests/fixtures/release-ready.json").read_bytes())
        self.store = MemoryStore(self.manifest)
        self.row = self.store.rows[TAG]
        self.row.update(draft=False, prerelease=False)
        self.seed = self.store.put
        self.seed(self.row, contract.ASSET, self.raw)
        self.store.operations.clear()
        self.public = PublicHub(self.store)
        self.target_client = artifacts.Client(token="target-app-token")
        self.hub_client = object()
        self.real_store = pipeline.Store
        self.tokens, self.requests, self.accepted, self.puts, self.effects = [], [], [], [], []
        self.fail_send = self.fail_put = False
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "redelivery.json"
        self.start_patch(patch.dict(os.environ, {
            "GH_TOKEN": "target-app-token", "HUB_TOKEN": "hub-write-token",
            "TARGET_REPOSITORY": TARGET, "RELEASE_READY_CONSUMERS": CONSUMER,
            "GITHUB_RUN_ID": "900", "GITHUB_ACTOR": "release-operator",
        }, clear=True))
        self.start_patch(patch.object(pipeline, "now", return_value=CLOCK))
        self.start_patch(patch.object(pipeline, "Client", side_effect=self.client))
        self.start_patch(patch.object(pipeline, "Store", side_effect=self.make_store))
        self.start_patch(patch.object(self.store, "put", side_effect=self.put))
        self.start_patch(patch.object(artifacts.urllib.request, "build_opener", return_value=self))

    def start_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def client(self, token=None):
        self.tokens.append(token)
        if token is None:
            return self.target_client
        if token == "":
            return self.public
        if token == "hub-write-token":
            return self.hub_client
        raise AssertionError("unexpected credential used")

    def make_store(self, client):
        return self.store if client is self.hub_client else self.real_store(client)

    def open(self, request, timeout):
        self.assertEqual(request.full_url,
                         f"https://api.github.com/repos/pineforge-4pass/{TARGET}/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer target-app-token")
        self.requests.append(request.data)
        if self.fail_send:
            raise urllib.error.URLError("dispatch transport unavailable")
        self.accepted.append(request.data)
        self.effects.append("send")
        return io.BytesIO(b"")

    def put(self, row, name, raw):
        self.puts.append((row["tag_name"], name, raw))
        self.effects.append("put")
        if self.fail_put:
            raise OSError("hub audit storage unavailable")
        self.seed(row, name, raw)

    def audit(self, attempt, *, consumer=CONSUMER):
        return {"schema": "pineforge/release-redelivery/v1", "event_id": self.event["event_id"],
                "consumer": consumer, "attempt": attempt, "state": "dispatch_accepted",
                "run_id": 900, "timestamp": CLOCK, "actor": "release-operator"}

    def seed_audit(self, number, *, consumer=CONSUMER, changes=None, name=None):
        obj = {**self.audit(number, consumer=consumer), **(changes or {})}
        name = name or f"delivery-redelivered-{consumer}-{number}.json"
        self.seed(self.row, name, contract.canonical(obj))

    def seed_acceptance(self):
        self.seed(self.row, f"delivery-{CONSUMER}.json", contract.canonical({
            "schema": "pineforge/release-delivery/v1", "event_id": self.event["event_id"],
            "consumer": CONSUMER, "attempt": 3, "state": "dispatch_accepted",
            "run_id": 800, "timestamp": CLOCK}))

    def seed_receipt(self, state, *, consumer=CONSUMER, attempt=1, sequence=1):
        # These are trusted hub summaries, after the existing receipt adapter
        # verified consumer evidence. Redelivery only selects their latest state.
        self.seed(self.row, f"consumer-{consumer}-{attempt}-{sequence}.json", contract.canonical({
            "schema": "pineforge/consumer-receipt/v1", "event_id": self.event["event_id"],
            "release_version": "1.6.0", "consumer": consumer, "state": state,
            "attempt": attempt, "sequence": sequence, "timestamp": CLOCK,
            "evidence_sha256": "a" * 64, "reporting_run_id": 700}))

    def seed_planner(self):
        for number in (1, 2, 3):
            self.seed(self.row, f"delivery-attempt-{CONSUMER}-{number}.json", contract.canonical({
                "event_id": self.event["event_id"], "consumer": CONSUMER, "attempt": number,
                "run_id": 800 + number, "timestamp": CLOCK}))
        self.seed(self.row, f"delivery-failed-{CONSUMER}.json", contract.canonical({
            "event_id": self.event["event_id"], "consumer": CONSUMER,
            "state": "failed", "attempts": 3, "timestamp": CLOCK}))

    def use_prerelease_manifest(self):
        # Derive the variant with the same helper as the superseded-release test.
        self.manifest = fixture_manifest("1.6.0-rc.1")
        self.raw = contract.canonical(self.manifest)
        self.event = contract.payload(self.raw)
        self.assertIs(self.event["prerelease"], True)
        fixture = MemoryStore(self.manifest)
        # Retain the store object and its patched put so writes remain observable.
        self.store.rows, self.store.assets = fixture.rows, fixture.assets
        self.row = self.store.rows["v1.6.0-rc.1"]
        self.row.update(draft=False, prerelease=True)
        self.seed(self.row, contract.ASSET, self.raw)
        self.store.operations.clear()

    def invoke(self, *, tag=TAG, consumer=CONSUMER):
        return pipeline.redeliver(self.target_client, self.store, tag, consumer, self.output)

    def cli(self, command="redeliver", *, output=None):
        argv = ["release_pipeline.py", command, "--tag", TAG, "--consumer", CONSUMER,
                "--output", str(output or self.output)]
        with patch.object(sys, "argv", argv):
            return pipeline.main()

    def assert_refused(self, *, tag=TAG, consumer=CONSUMER, message=None):
        before = copy.deepcopy(self.store.assets)
        with self.assertRaises(contract.Invalid) as refused:
            self.invoke(tag=tag, consumer=consumer)
        if message is not None:
            self.assertEqual(str(refused.exception), message)
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def test_identical_wire_payload_and_exactly_one_append_after_acceptance(self):
        """Mutation: replace client_payload=event with {**event, 'run_id': run_id}."""
        self.assertEqual(contract.payload(self.raw), self.event)
        self.assertEqual(self.cli("dispatch", output=self.output.with_name("automatic.json")), 0)
        automatic_body = self.requests[0]
        self.requests.clear()
        self.accepted.clear()
        self.effects.clear()
        self.tokens.clear()
        self.public.reads.clear()
        self.seed_planner()
        self.seed_receipt("ready")
        self.seed(self.row, "release-ready.json", contract.canonical({
            "event_id": self.event["event_id"], "timestamp": CLOCK}))
        before = copy.deepcopy(self.store.assets)
        self.assertEqual(self.cli(), 0)
        expected_body = contract.canonical({"event_type": "pineforge-release", "client_payload": self.event})
        self.assertEqual(self.requests, [automatic_body])
        self.assertEqual(self.requests[0], expected_body)
        self.assertEqual(contract.canonical(contract.decode(self.requests[0])["client_payload"]),
                         contract.canonical(contract.payload(self.raw)))
        name = f"delivery-redelivered-{CONSUMER}-1.json"
        audit_raw = contract.canonical(self.audit(1))
        self.assertEqual(self.puts, [(TAG, name, audit_raw)])
        self.assertEqual(self.effects, ["send", "put"])
        self.assertEqual(self.store.assets, {**before, (TAG, name): audit_raw})
        self.assertEqual(self.output.read_bytes(), audit_raw)
        self.assertEqual(self.tokens, [None, "hub-write-token", ""])
        self.assertIn(f"repos/{contract.HUB}/releases/tags/{TAG}", self.public.reads)
        self.assertIn(f"https://github.com/{contract.HUB}/releases/download/{TAG}/{contract.ASSET}",
                      self.public.reads)

    def test_numbers_are_contiguous_independent_of_planner_and_other_roles(self):
        """Mutation: cap redeliveries at len(DELIVERY_BACKOFF_MINUTES)."""
        self.seed_planner()
        self.seed_audit(1)
        self.seed_audit(7, consumer="offline")
        before = copy.deepcopy(self.store.assets)
        for number in (2, 3, 4):
            self.invoke()
            self.assertEqual(self.output.read_bytes(), contract.canonical(self.audit(number)))
        self.assertEqual([name for _, name, _ in self.puts],
                         [f"delivery-redelivered-{CONSUMER}-{n}.json" for n in (2, 3, 4)])
        for key, raw in before.items():
            self.assertEqual(self.store.assets[key], raw)

    def test_refuses_a_gap_in_redelivery_history(self):
        """Mutation: replace the contiguous-history require with require(True, ...)."""
        self.seed_audit(2)
        self.assert_refused()

    def test_refuses_history_with_wrong_identity_shape_or_asset_number(self):
        """Mutation: omit history event_id validation (the event subcase then sends)."""
        cases = [
            ("event", {"event_id": "sha256:" + "0" * 64}, None),
            ("consumer", {"consumer": "offline"}, None),
            ("zero", {"attempt": 0}, None),
            ("bool", {"attempt": True}, None),
            ("name", {}, f"delivery-redelivered-{CONSUMER}-2.json"),
            ("schema", {"schema": "pineforge/release-delivery/v1"}, None),
            ("state", {"state": "failed"}, None),
            ("extra", {"target_repository": "must-not-be-recorded"}, None),
            ("run", {"run_id": 0}, None),
            ("actor", {"actor": "not/a/login"}, None),
            ("clock", {"timestamp": "not-a-timestamp"}, None),
        ]
        for label, changes, name in cases:
            with self.subTest(case=label):
                before = copy.deepcopy(self.store.assets)
                assets = copy.deepcopy(self.row["assets"])
                try:
                    self.seed_audit(1, changes=changes, name=name)
                    self.assert_refused()
                finally:
                    self.store.assets = before
                    self.row["assets"] = assets

    def test_refuses_draft(self):
        """Mutation: remove the draft require."""
        self.row["draft"] = True
        self.assert_refused()

    def test_refuses_rejected_release(self):
        """Mutation: remove the release-rejected.json check."""
        self.seed(self.row, "release-rejected.json", contract.canonical({"reason": "withdrawn"}))
        self.assert_refused()

    def test_refuses_prerelease_consumers_skipped_by_policy(self):
        """Mutation: replace the prerelease-policy require condition with True."""
        self.use_prerelease_manifest()
        for consumer in ("application", "web", "data", "license"):
            with self.subTest(consumer=consumer), \
                    patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": consumer}):
                self.assert_refused(
                    tag=self.row["tag_name"], consumer=consumer,
                    message="redelivery consumer is skipped by policy for a prerelease")

    def test_prerelease_offline_and_hosted_are_delivered_once(self):
        """Mutation: drop consumer membership from the prerelease-policy require."""
        self.use_prerelease_manifest()
        expected_body = contract.canonical({"event_type": "pineforge-release", "client_payload": self.event})
        for consumer in ("offline", "hosted"):
            with self.subTest(consumer=consumer), \
                    patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": consumer}):
                before = copy.deepcopy(self.store.assets)
                sent_before = len(self.accepted)
                tag = self.row["tag_name"]
                self.invoke(tag=tag, consumer=consumer)
                self.assertEqual(self.requests[sent_before:], [expected_body])
                self.assertEqual(self.accepted[sent_before:], [expected_body])
                name = f"delivery-redelivered-{consumer}-1.json"
                audit_raw = contract.canonical(self.audit(1, consumer=consumer))
                self.assertEqual(self.puts[sent_before:], [(tag, name, audit_raw)])
                self.assertEqual(self.store.assets, {**before, (tag, name): audit_raw})
                self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_stable_application_is_delivered_once(self):
        """Mutation: drop event['prerelease'] from the policy require."""
        self.assertIs(self.event["prerelease"], False)
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "application"}):
            self.invoke(consumer="application")
        expected_body = contract.canonical({"event_type": "pineforge-release", "client_payload": self.event})
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        audit_raw = contract.canonical(self.audit(1, consumer="application"))
        self.assertEqual(self.puts, [(TAG, "delivery-redelivered-application-1.json", audit_raw)])
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_refuses_superseded_release(self):
        """Mutation: replace the newer-release require condition with True."""
        newer = fixture_manifest("1.6.1")
        other = MemoryStore(newer)
        row = other.rows["v1.6.1"]
        row.update(id=2, draft=False, prerelease=False)
        self.store.rows[row["tag_name"]] = row
        self.seed(row, contract.ASSET, contract.canonical(newer))
        self.assertFalse(any(asset["name"].startswith("consumer-") for asset in self.row["assets"]))
        self.assert_refused(message="release superseded before redelivery")

    def test_newer_draft_rejected_or_prerelease_does_not_supersede_stable(self):
        """Mutation: drop the dispatch rule's stable/prerelease filter."""
        for tag, draft, prerelease, rejected in (
                ("v1.6.1", True, False, False),
                ("v1.6.2", False, False, True),
                ("v1.7.0-rc.1", False, True, False)):
            row = {"id": len(self.store.rows) + 1, "tag_name": tag, "draft": draft,
                   "prerelease": prerelease, "assets": []}
            self.store.rows[tag] = row
            self.seed(row, contract.ASSET, b"not downloaded by the supersession check\n")
            if rejected:
                self.seed(row, "release-rejected.json", b"{}\n")
        self.invoke()
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.output.read_bytes(), contract.canonical(self.audit(1)))

    def test_refuses_tag_different_from_manifest(self):
        """Mutation: remove the tag == 'v' + event['release_version'] require."""
        wrong = "v1.5.9"
        self.row["tag_name"] = wrong
        self.store.rows = {wrong: self.row}
        self.store.assets = {(wrong, name): raw for (_, name), raw in self.store.assets.items()}
        self.assert_refused(tag=wrong)

    def test_refuses_consumer_outside_product_configuration_even_with_facts_flags(self):
        """Mutation: use receipt_roles() instead of configured_roles(RELEASE_READY_CONSUMERS)."""
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "offline",
                                    "FACTS_WEB_DISPATCH_ENABLED": "true",
                                    "FACTS_LICENSE_DISPATCH_ENABLED": "true"}):
            for consumer in ("hosted", "web", "license"):
                with self.subTest(consumer=consumer):
                    self.assert_refused(consumer=consumer)

    def test_refuses_accepted_consumer_with_latest_verified_receipt(self):
        """Mutation: remove the accepted-and-verified refusal."""
        self.seed_acceptance()
        # Insert the later attempt first: asset ordering must not select the
        # older failed receipt just because it was uploaded last.
        self.seed_receipt("verified", attempt=2, sequence=1)
        self.seed_receipt("failed", attempt=1, sequence=9)
        self.assert_refused()

    def test_accepted_but_unverified_consumer_can_be_repaired(self):
        """Mutation: refuse whenever delivery-<consumer>.json exists."""
        self.seed_acceptance()
        self.seed_receipt("ready")
        self.seed_receipt("verified", consumer="offline")
        before = copy.deepcopy(self.store.assets)
        self.invoke()
        name = f"delivery-redelivered-{CONSUMER}-1.json"
        self.assertEqual(self.store.assets, {**before, (TAG, name): contract.canonical(self.audit(1))})
        self.assertEqual([entry[1] for entry in self.puts], [name])
        self.assertEqual(len(self.accepted), 1)

    def test_refuses_noncanonical_public_manifest_bytes(self):
        """Mutation: build the event from decode(raw) instead of payload(raw)."""
        with patch.object(self.public, "download", return_value=self.raw + b" "):
            self.assert_refused()

    def test_refuses_malformed_target_repository(self):
        """Mutation: remove the repository-name match before sending."""
        for target in ("", "another-owner/repo", "repo\ninjected"):
            with self.subTest(target=target), patch.dict(os.environ, {"TARGET_REPOSITORY": target}):
                self.assert_refused()

    def test_send_failure_is_nonzero_writes_nothing_and_reuses_number(self):
        """Mutation: reserve the audit with store.put before client.api."""
        self.seed_planner()
        before = copy.deepcopy(self.store.assets)
        self.fail_send = True
        self.assertNotEqual(self.cli(), 0)
        self.assertEqual(self.puts, [])
        self.assertEqual(self.store.assets, before)
        self.assertFalse(self.output.exists())
        self.fail_send = False
        self.assertEqual(self.cli(), 0)
        self.assertEqual(self.output.read_bytes(), contract.canonical(self.audit(1)))
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual([name for _, name, _ in self.puts],
                         [f"delivery-redelivered-{CONSUMER}-1.json"])

    def test_audit_failure_is_nonzero_and_retry_sends_duplicate_with_same_number(self):
        """Mutation: swallow OSError from store.put and return success."""
        self.seed_planner()
        before = copy.deepcopy(self.store.assets)
        self.fail_put = True
        self.assertNotEqual(self.cli(), 0)
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.store.assets, before)
        self.assertFalse(self.output.exists())
        self.fail_put = False
        self.assertEqual(self.cli(), 0)
        name = f"delivery-redelivered-{CONSUMER}-1.json"
        self.assertEqual([item[1] for item in self.puts], [name, name])
        self.assertEqual(self.accepted, [self.accepted[0], self.accepted[0]])
        self.assertEqual(self.effects, ["send", "put", "send", "put"])
        self.assertEqual(self.store.assets, {**before, (TAG, name): contract.canonical(self.audit(1))})
        self.assertEqual(self.output.read_bytes(), contract.canonical(self.audit(1)))

    def test_real_store_refuses_a_different_asset_at_the_same_number_after_send(self):
        """Mutation: let Store.put accept different bytes for an existing name."""
        rival = contract.canonical({**self.audit(1), "actor": "other-operator"})

        def collide(row, name, raw):
            self.seed(row, name, rival)
            self.real_store(self.public).put(row, name, raw)

        with patch.object(self.store, "put", side_effect=collide), self.assertRaises(contract.Invalid):
            self.invoke()
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.store.read(self.row, f"delivery-redelivered-{CONSUMER}-1.json"), rival)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
