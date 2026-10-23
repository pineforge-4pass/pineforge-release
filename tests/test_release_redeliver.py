#!/usr/bin/env python3
"""Manual redelivery regressions; execute in CI, never on the operator's Mac.

The existing store fake keys every asset by release and exact name. Only HTTP
transport is replaced: production code still serializes the real wire bytes.
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
from test_release_binding import (FinalizeClient, MemoryRegistry, MemoryStore,  # noqa: E402
                                  expected_pair_payload, fixture_manifest)

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
            raise AssertionError("unexpected release asset download origins")
        for tag, row in self.store.rows.items():
            for name in (contract.ASSET, contract.CANDIDATE):
                if url == f"https://github.com/{contract.HUB}/releases/download/{tag}/{name}":
                    self.reads.append(url)
                    raw = self.store.read(row, name)
                    if raw is None:
                        raise contract.Pending("public release asset unavailable")
                    return raw
        raise AssertionError("unmodeled public release asset URL " + url)


class RedeliverTest(unittest.TestCase):
    def setUp(self):
        self.manifest = fixture_manifest()
        self.raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        self.event = contract.decode((ROOT / "tests/fixtures/release-ready.json").read_bytes())
        self.store = MemoryStore(self.manifest)
        self.row = self.store.rows[TAG]
        self.row.update(draft=False, prerelease=False)
        self.store.workflow_runs[900] = {
            "id": 900, "path": ".github/workflows/release-redeliver.yml",
            "head_branch": "main", "status": "completed", "conclusion": "success"}
        self.seed = self.store.put
        self.seed(self.row, contract.ASSET, self.raw)
        self.store.operations.clear()
        self.public = PublicHub(self.store)
        self.target_client = artifacts.Client(token="target-app-token")
        self.hub_client = object()
        self.real_store = pipeline.Store
        self.tokens, self.requests, self.accepted, self.puts, self.effects = [], [], [], [], []
        self.fail_send = self.fail_put = False
        self.dispatch_status = 204
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
        response = io.BytesIO(b"")
        response.status = self.dispatch_status
        return response

    def put(self, row, name, raw):
        self.puts.append((row["tag_name"], name, raw))
        self.effects.append("put")
        if self.fail_put:
            raise OSError("hub audit storage unavailable")
        self.seed(row, name, raw)

    def audit(self, attempt, *, consumer=CONSUMER):
        return {"schema": "pineforge/release-redelivery/v2", "event_id": self.event["event_id"],
                "consumer": consumer, "attempt": attempt, "state": "dispatch_accepted",
                "run_id": 900, "dispatch_status": 204, "timestamp": CLOCK, "actor": "release-operator"}

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

    def seed_planner(self, *, consumer=CONSUMER):
        for number in (1, 2, 3):
            self.seed(self.row, f"delivery-attempt-{consumer}-{number}.json", contract.canonical({
                "event_id": self.event["event_id"], "consumer": consumer, "attempt": number,
                "run_id": 800 + number, "timestamp": CLOCK}))
        self.seed(self.row, f"delivery-failed-{consumer}.json", contract.canonical({
            "event_id": self.event["event_id"], "consumer": consumer,
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

    def cli(self, command="redeliver", *, output=None, tag=TAG, consumer=CONSUMER):
        argv = ["release_pipeline.py", command, "--tag", tag, "--consumer", consumer,
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

    def test_audit_records_environment_run_id_and_actual_dispatch_status(self):
        """Mutation: omit the HTTP response status or substitute a constant run id."""
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "9123"}):
            self.invoke()
        audit = contract.decode(self.output.read_bytes())
        self.assertEqual(audit["schema"], "pineforge/release-redelivery/v2")
        self.assertEqual(audit["run_id"], 9123)
        self.assertEqual(audit["dispatch_status"], self.dispatch_status)
        self.assertEqual(audit["dispatch_status"], 204)
        self.assertEqual(self.puts, [(TAG, f"delivery-redelivered-{CONSUMER}-1.json",
                                     self.output.read_bytes())])
        self.assertEqual(self.effects, ["send", "put"])

    def test_non_204_dispatch_response_never_writes_an_acceptance_audit(self):
        """Mutation: record 204 after any successful client.api call."""
        before = copy.deepcopy(self.store.assets)
        for status in (200, 201, 202):
            with self.subTest(status=status):
                self.dispatch_status = status
                with self.assertRaisesRegex(contract.Invalid, "^redelivery dispatch status differs$"):
                    self.invoke()
                self.assertEqual(self.puts, [])
                self.assertEqual(self.store.assets, before)
                self.assertFalse(self.output.exists())
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.effects, ["send", "send", "send"])

    def test_unconfirmed_history_refuses_before_another_dispatch(self):
        """Mutation: omit confirm_redelivery_run from redeliver's history check."""
        success = self.store.workflow_runs[900]
        cases = (
            ({**success, "path": ".github/workflows/release-finalize.yml"}, "redelivery workflow run differs"),
            ({**success, "head_branch": "repair"}, "redelivery workflow run differs"),
            ({**success, "conclusion": "failure"}, "redelivery workflow run differs"),
            ({**success, "status": "in_progress", "conclusion": None}, "redelivery workflow run differs"),
            (None, "redelivery workflow run unavailable"),
            (contract.Invalid("artifact request failed (HTTP 500)"), "artifact request failed (HTTP 500)"),
        )
        for schema in ("pineforge/release-redelivery/v1", "pineforge/release-redelivery/v2"):
            obj = {**self.audit(1), "schema": schema}
            if schema.endswith("/v1"):
                obj = {key: value for key, value in obj.items() if key != "dispatch_status"}
            for run, message in cases:
                with self.subTest(schema=schema, run=run):
                    before = copy.deepcopy(self.store.assets)
                    assets = copy.deepcopy(self.row["assets"])
                    self.store.workflow_runs = {} if run is None else {900: run}
                    self.store.run_reads.clear()
                    try:
                        self.seed(self.row, f"delivery-redelivered-{CONSUMER}-1.json", contract.canonical(obj))
                        self.assert_refused(message=message)
                        self.assertEqual(self.store.run_reads, [f"repos/{contract.HUB}/actions/runs/900"])
                    finally:
                        self.store.assets = before
                        self.row["assets"] = assets

    def test_confirmed_v1_history_can_append_v2_without_rewriting_history(self):
        """Mutation: reject all legacy history instead of confirming its workflow run."""
        legacy = {**self.audit(1), "schema": "pineforge/release-redelivery/v1"}
        legacy = {key: value for key, value in legacy.items() if key != "dispatch_status"}
        self.seed(self.row, f"delivery-redelivered-{CONSUMER}-1.json", contract.canonical(legacy))
        before = copy.deepcopy(self.store.assets)
        self.invoke()
        audit = contract.canonical(self.audit(2))
        name = f"delivery-redelivered-{CONSUMER}-2.json"
        self.assertEqual(self.store.run_reads, [f"repos/{contract.HUB}/actions/runs/900"])
        self.assertEqual(self.puts, [(TAG, name, audit)])
        self.assertEqual(self.store.assets, {**before, (TAG, name): audit})
        self.assertEqual(self.effects, ["send", "put"])

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
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "application",
                                    "RELEASE_COMPUTE_FIRST_PAIR": "not-semver"}):
            self.invoke(consumer="application")
        expected_body = contract.canonical({
            "event_type": "pineforge-release", "client_payload": self.event,
        })
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        audit_raw = contract.canonical(self.audit(1, consumer="application"))
        self.assertEqual(self.puts, [(TAG, "delivery-redelivered-application-1.json", audit_raw)])
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_application_redelivery_preserves_release_ready_v1_without_an_opt_in(self):
        """Mutation: map application to release-pair in manual stable replay."""
        ready_raw = (ROOT / "tests/fixtures/release-ready.json").read_bytes()
        before = copy.deepcopy(self.store.assets)
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "application"}):
            self.assertEqual(self.cli(consumer="application"), 0)
        expected_body = contract.canonical({
            "event_type": "pineforge-release", "client_payload": contract.decode(ready_raw)})
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        sent = contract.decode(self.requests[0])["client_payload"]
        self.assertEqual(contract.canonical(sent), ready_raw)
        self.assertEqual(sent["schema"], "pineforge/release-ready/v1")
        self.assertEqual(set(sent), {"schema", "event_id", "release_version", "prerelease", "hub_commit",
                                    "versions", "image", "facts", "manifest", "run_id"})
        self.assertNotIn(f"https://github.com/{contract.HUB}/releases/download/{TAG}/{contract.CANDIDATE}",
                         self.public.reads)
        audit_raw = contract.canonical(self.audit(1, consumer="application"))
        name = "delivery-redelivered-application-1.json"
        self.assertEqual(self.puts, [(TAG, name, audit_raw)])
        self.assertEqual(self.store.assets, {**before, (TAG, name): audit_raw})
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_redelivery_matches_each_targets_automatic_dispatch_contract(self):
        """Mutation: switch either target to the other target's event/payload."""
        expected_bodies = {
            "application": contract.canonical({
                "event_type": "pineforge-release", "client_payload": self.event}),
            "compute": contract.canonical({
                "event_type": "release-pair",
                "client_payload": expected_pair_payload(self.raw, self.store.read(self.row, contract.CANDIDATE))}),
        }
        for consumer in ("application", "compute"):
            with self.subTest(consumer=consumer), patch.dict(os.environ, {
                    "RELEASE_READY_CONSUMERS": consumer, "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}):
                expected_body = expected_bodies[consumer]
                self.assertEqual(self.cli("dispatch", consumer=consumer,
                                          output=self.output.with_name("automatic.json")), 0)
                self.assertEqual(self.cli(consumer=consumer), 0)
                self.assertEqual(self.requests[-2:], [expected_body, expected_body])
                audit_raw = contract.canonical(self.audit(1, consumer=consumer))
                self.assertEqual(self.puts[-1], (TAG, f"delivery-redelivered-{consumer}-1.json", audit_raw))
                self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_compute_redelivery_keeps_an_eligible_older_prerelease_pair(self):
        """Mutation: apply the newer-release refusal to delivery-only compute."""
        self.use_prerelease_manifest()
        newer = fixture_manifest("1.6.1")
        row = MemoryStore(newer).rows["v1.6.1"]
        row.update(id=2, draft=False, prerelease=False)
        self.store.rows[row["tag_name"]] = row
        self.seed(row, contract.ASSET, contract.canonical(newer))
        self.seed(self.row, "delivery-compute.json", contract.canonical({
            "schema": "pineforge/release-delivery/v1", "event_id": self.event["event_id"],
            "consumer": "compute", "attempt": 1, "state": "dispatch_accepted",
            "run_id": 800, "timestamp": CLOCK}))
        before = copy.deepcopy(self.store.assets)
        tag = self.row["tag_name"]
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "compute",
                                    "RELEASE_COMPUTE_FIRST_PAIR": self.event["release_version"]}):
            self.invoke(tag=tag, consumer="compute")
        expected_body = contract.canonical({
            "event_type": "release-pair",
            "client_payload": expected_pair_payload(self.raw, self.store.read(self.row, contract.CANDIDATE)),
        })
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        name = "delivery-redelivered-compute-1.json"
        audit_raw = contract.canonical(self.audit(1, consumer="compute"))
        self.assertEqual(self.puts, [(tag, name, audit_raw)])
        self.assertEqual(self.store.assets, {**before, (tag, name): audit_raw})
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_compute_redelivery_requires_the_existing_first_pair_floor(self):
        """Mutation: omit the compute floor check from manual repairs."""
        for floor in ("", "1.6.1"):
            with self.subTest(floor=floor), patch.dict(os.environ, {
                    "RELEASE_READY_CONSUMERS": "compute", "RELEASE_COMPUTE_FIRST_PAIR": floor}):
                self.assert_refused(consumer="compute",
                                    message="redelivery consumer is not enabled for this pair")

    def test_accepted_compute_repair_completes_finalization_and_preserves_exhaustion(self):
        """Mutation: aggregate reads only delivery-compute.json after repair."""
        self.seed_planner(consumer="compute")
        client, registry = FinalizeClient(self.manifest), MemoryRegistry()
        deliveries = self.output.with_name("deliveries.json")
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "compute", "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0",
                "GITHUB_RUN_ID": "899", "GITHUB_RUN_ATTEMPT": "1",
                "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false"}), \
                patch.object(pipeline, "age_minutes", return_value=1000), \
                patch.object(pipeline, "report_issue") as report:
            pipeline.finalize_all(client, self.store, registry, TAG, deliveries)
            failed = contract.decode(self.store.read(self.row, "release-status-899-1.json"))
            self.assertEqual(failed["consumers"], {"compute": "failed"})
            self.assertFalse(failed["complete"])
            self.assertEqual(contract.decode(deliveries.read_bytes()), [])
            self.assertFalse(report.call_args.kwargs["complete"])
            before = copy.deepcopy(self.store.assets)
            self.puts.clear()
            os.environ["GITHUB_RUN_ID"] = "900"
            self.store.workflow_runs[900].update(status="in_progress", conclusion=None)
            self.assertEqual(self.cli(consumer="compute"), 0)
            expected_body = contract.canonical({
                "event_type": "release-pair",
                "client_payload": expected_pair_payload(self.raw, self.store.read(self.row, contract.CANDIDATE)),
            })
            self.assertEqual(self.requests, [expected_body])
            self.assertEqual(self.accepted, [expected_body])
            audit_raw = contract.canonical(self.audit(1, consumer="compute"))
            name = "delivery-redelivered-compute-1.json"
            self.assertEqual(self.puts, [(TAG, name, audit_raw)])
            self.assertEqual(self.output.read_bytes(), audit_raw)
            self.assertEqual(self.store.assets, {**before, (TAG, name): audit_raw})
            self.assertIsNone(self.store.read(self.row, "delivery-compute.json"))
            os.environ["GITHUB_RUN_ID"] = "901"
            report.reset_mock()
            with self.assertRaisesRegex(contract.Invalid,
                                        "^release finalization failed; inspect release incidents$"):
                pipeline.finalize_all(client, self.store, registry, TAG, deliveries)
            self.assertEqual(self.store.assets, {**before, (TAG, name): audit_raw})
            self.assertEqual(contract.decode(deliveries.read_bytes()), [])
            self.assertFalse(report.call_args.kwargs.get("complete", False))
            self.assertEqual(self.store.run_reads, [f"repos/{contract.HUB}/actions/runs/900"])
            self.store.workflow_runs[900].update(status="completed", conclusion="success")
            self.store.run_reads.clear()
            report.reset_mock()
            pipeline.finalize_all(client, self.store, registry, TAG, deliveries)
            latest = max((asset for asset in self.row["assets"] if asset["name"].startswith("release-status-")),
                         key=lambda asset: asset["id"])
            status = contract.decode(self.store.read(self.row, latest["name"]))
            self.assertEqual(status["consumers"], {"compute": "delivered"})
            self.assertTrue(status["complete"])
            self.assertEqual(latest["name"], "release-status-901-1.json")
            self.assertEqual(status["stale"], [])
            self.assertEqual(contract.decode(deliveries.read_bytes()), [])
            self.assertTrue(report.call_args.kwargs["complete"])
            self.assertEqual(self.store.run_reads, [f"repos/{contract.HUB}/actions/runs/900"])
            for key, raw in before.items():
                self.assertEqual(self.store.assets[key], raw)
            self.assertFalse(any(name.startswith("consumer-") for _, name in self.store.assets))

    def test_malformed_compute_floor_fails_without_a_send_or_audit(self):
        """Mutation: treat a malformed compute floor as enabled."""
        before = copy.deepcopy(self.store.assets)
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "compute",
                                    "RELEASE_COMPUTE_FIRST_PAIR": "not-semver"}):
            self.assertNotEqual(self.cli(consumer="compute"), 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertEqual(self.store.assets, before)
        self.assertFalse(self.output.exists())

    def test_pair_redelivery_refuses_a_candidate_that_differs_from_the_manifest(self):
        """Mutation: send the pair without checking the candidate binding."""
        changed = contract.decode(self.store.read(self.row, contract.CANDIDATE))
        changed["hub_commit"] = "9" * 40
        candidate_url = f"https://github.com/{contract.HUB}/releases/download/{TAG}/{contract.CANDIDATE}"
        download = self.public.download
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "application,compute", "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}), \
                patch.object(self.public, "download", side_effect=lambda url, origins:
                             contract.canonical(changed) if url == candidate_url else download(url, origins)):
            self.assert_refused(consumer="compute", message="manifest does not bind this candidate")
            self.public.reads.clear()
            self.invoke(consumer="application")
        expected_body = contract.canonical({"event_type": "pineforge-release", "client_payload": self.event})
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        self.assertNotIn(candidate_url, self.public.reads)
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
