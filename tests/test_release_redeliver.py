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
import shlex
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_artifacts as artifacts  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
import release_receipts as adapter  # noqa: E402
from test_release_binding import MemoryStore, expected_pair_payload, fixture_manifest  # noqa: E402

TAG = "v1.6.0"
CONSUMER = "hosted"
TARGET = "fixture-hosted"
CLOCK = "2026-10-23T00:00:00Z"
# The keys release_receipts.validate_receipt publishes for a product receipt in every state
# (a test below compares this list with the adapter's real output).
RECEIPT_FIELDS = ("schema", "consumer", "attempt", "sequence", "state", "timestamp",
                  "event_id", "release_version", "evidence_sha256", "reporting_run_id")


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

    def receipt_body(self, state, *, consumer=CONSUMER, attempt=1, sequence=1):
        # A trusted hub summary, after the existing receipt adapter verified the
        # consumer's evidence. Redelivery eligibility reads its shape, identity and state.
        return {"schema": "pineforge/consumer-receipt/v1", "event_id": self.event["event_id"],
                "release_version": "1.6.0", "consumer": consumer, "state": state,
                "attempt": attempt, "sequence": sequence, "timestamp": CLOCK,
                "evidence_sha256": "a" * 64, "reporting_run_id": 700}

    def seed_receipt(self, state, *, consumer=CONSUMER, attempt=1, sequence=1):
        self.seed_receipt_asset(f"consumer-{consumer}-{attempt}-{sequence}.json", self.receipt_body(
            state, consumer=consumer, attempt=attempt, sequence=sequence))

    def seed_receipt_asset(self, name, body):
        self.seed(self.row, name, contract.canonical(body))

    @contextmanager
    def restored_store(self):
        """Undo a subtest's seeded assets and any effects so every case starts alike."""
        assets, listed = copy.deepcopy(self.store.assets), copy.deepcopy(self.row["assets"])
        try:
            yield
        finally:
            self.store.assets = assets
            self.row["assets"] = listed
            for effects in (self.tokens, self.requests, self.accepted, self.puts, self.effects):
                effects.clear()

    def assert_public_refusal(self, message):
        """Refused with this message after public reads only: no other credential, send or write."""
        self.assert_refused(message=message)
        self.assertEqual(self.tokens, [""])

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

    def workflow_before_target_mint(self, *, tag=TAG, consumer=CONSUMER, configured=CONSUMER):
        """Run the workflow's Python checks, stopping on failure or the token action.

        Checkout, the apt guard and repository-name setup are covered by the
        workflow wiring tests; this fixture supplies a valid target repository.
        No App action or shell command is executed by this walk.
        """
        from test_release_workflows import _workflow
        bindings = {
            "${{ inputs.tag }}": tag,
            "${{ inputs.consumer }}": consumer,
            "${{ vars.RELEASE_READY_CONSUMERS }}": configured,
            "${{ vars.RELEASE_COMPUTE_FIRST_PAIR }}": os.environ.get("RELEASE_COMPUTE_FIRST_PAIR", ""),
        }
        result, diagnostic = None, ""
        for step in _workflow("release-redeliver.yml")["jobs"]["redeliver"]["steps"]:
            if "create-github-app-token@" in step.get("uses", ""):
                return "mint", result, diagnostic
            if "release_pipeline.py" not in step.get("run", ""):
                continue
            words = shlex.split(step["run"])
            self.assertEqual(words[:2], ["python3", "scripts/release_pipeline.py"])
            env = {name: bindings[value] for name, value in step.get("env", {}).items()}
            stderr = io.StringIO()
            with patch.dict(os.environ, env, clear=True), redirect_stderr(stderr):
                argv = [os.path.expandvars(word) for word in words[1:]]
                with patch.object(sys, "argv", argv):
                    result = pipeline.main()
            diagnostic = stderr.getvalue()
            if result != 0:
                return "refused", result, diagnostic
        self.fail("redelivery workflow never reached a target token mint")

    def test_workflow_refuses_prerelease_application_before_target_token_mint(self):
        """Mutation: move the public eligibility check after the App token mint."""
        self.use_prerelease_manifest()
        before = copy.deepcopy(self.store.assets)
        outcome, result, stderr = self.workflow_before_target_mint(
            tag="v1.6.0-rc.1", consumer="application", configured="application")
        self.assertEqual((outcome, result), ("refused", 1))
        self.assertIn("::error::", stderr)
        self.assertEqual(self.tokens, [""])
        # The same prerelease is eligible for hosted; the check must allow it
        # through to the mint without sending or appending anything itself.
        self.assertEqual(self.workflow_before_target_mint(
            tag="v1.6.0-rc.1", consumer="hosted", configured="hosted"), ("mint", 0, ""))
        self.assertEqual(self.tokens, ["", ""])
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def test_workflow_refuses_unconfigured_consumer_before_target_token_mint(self):
        """Mutation: omit the public check before minting, or only its configured-consumer require."""
        before = copy.deepcopy(self.store.assets)
        # The valid published tag: the membership require is the only possible refusal.
        outcome, result, stderr = self.workflow_before_target_mint(
            tag=TAG, consumer="license", configured="offline")
        self.assertEqual((outcome, result), ("refused", 1))
        self.assertIn("::error::", stderr)
        self.assertEqual(self.tokens, [])
        self.assertEqual(self.public.reads, [])
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def test_workflow_refuses_noncanonical_tag_before_target_token_mint(self):
        """Mutation: bypass the canonical-tag check in the shared eligibility check."""
        before = copy.deepcopy(self.store.assets)
        outcome, result, stderr = self.workflow_before_target_mint(
            tag="not-a-semver-tag", consumer="license", configured="license")
        self.assertEqual((outcome, result), ("refused", 1))
        self.assertIn("::error::", stderr)
        self.assertEqual(self.tokens, [])
        self.assertEqual(self.public.reads, [])
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def test_workflow_refuses_verified_consumer_before_target_token_mint(self):
        """Mutation: omit receipt verification from the public eligibility check."""
        self.seed_planner()
        self.seed_audit(1)
        self.seed_receipt("verified", attempt=2, sequence=1)
        before = copy.deepcopy(self.store.assets)
        outcome, result, stderr = self.workflow_before_target_mint()
        self.assertEqual((outcome, result), ("refused", 1))
        self.assertIn("::error::", stderr)
        self.assertEqual(self.tokens, [""])
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def assert_workflow_refuses_before_mint(self):
        """Only the public client exists; nothing reaches the mint, a target or the hub store."""
        before = copy.deepcopy(self.store.assets)
        outcome, result, stderr = self.workflow_before_target_mint()
        self.assertEqual((outcome, result), ("refused", 1))
        self.assertIn("::error::", stderr)
        self.assertEqual(self.tokens, [""])
        self.assertEqual(self.store.assets, before)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.puts, [])
        self.assertFalse(self.output.exists())

    def test_workflow_refuses_stateless_receipt_before_target_token_mint(self):
        """Mutation: decide from receipts()'s selected receipt, whose missing state is not 'verified'."""
        # Reader input: a valid verified receipt, then its copy at attempt 2 without a state.
        # The rules are isolated below (malformed shape, any verified, duplicate identity).
        self.seed_receipt("verified")
        stateless = self.receipt_body("verified", attempt=2)
        del stateless["state"]
        self.seed_receipt_asset(f"consumer-{CONSUMER}-2-1.json", stateless)
        self.assertIsNone(self.store.read(self.row, f"delivery-{CONSUMER}.json"))
        self.assert_workflow_refuses_before_mint()

    def test_workflow_refuses_conflicting_duplicate_receipt_before_target_token_mint(self):
        """Mutation: decide from receipts()'s selected receipt, the lower asset id of equal identities."""
        # Reader input: a failed copy uploaded first (lower asset id) hides the canonical
        # verified receipt of the same event, consumer, attempt and sequence.
        self.seed_receipt_asset(f"consumer-{CONSUMER}-copy.json", self.receipt_body("failed"))
        self.seed_receipt("verified")
        self.assertIsNone(self.store.read(self.row, f"delivery-{CONSUMER}.json"))
        self.assert_workflow_refuses_before_mint()

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
            "event_type": "release-pair",
            "client_payload": expected_pair_payload(self.raw, self.store.read(self.row, contract.CANDIDATE)),
        })
        self.assertEqual(self.requests, [expected_body])
        self.assertEqual(self.accepted, [expected_body])
        audit_raw = contract.canonical(self.audit(1, consumer="application"))
        self.assertEqual(self.puts, [(TAG, "delivery-redelivered-application-1.json", audit_raw)])
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_pair_redelivery_matches_automatic_dispatch_for_both_targets(self):
        """Mutation: keep redeliver's former pineforge-release event/payload."""
        expected_body = contract.canonical({
            "event_type": "release-pair",
            "client_payload": expected_pair_payload(self.raw, self.store.read(self.row, contract.CANDIDATE)),
        })
        for consumer in ("application", "compute"):
            with self.subTest(consumer=consumer), patch.dict(os.environ, {
                    "RELEASE_READY_CONSUMERS": consumer, "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}):
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
        for consumer in ("application", "compute"):
            with self.subTest(consumer=consumer), patch.dict(os.environ, {
                    "RELEASE_READY_CONSUMERS": consumer, "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}), \
                    patch.object(self.public, "download", side_effect=lambda url, origins:
                                 contract.canonical(changed) if url == candidate_url else download(url, origins)):
                self.assert_refused(consumer=consumer, message="manifest does not bind this candidate")

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

    def test_refuses_verified_consumer_without_delivery_acceptance(self):
        """Mutation: put receipt verification back inside the acceptance branch."""
        self.seed_planner()
        self.seed_audit(1)
        self.seed_receipt("verified", attempt=2, sequence=1)
        self.assertIsNone(self.store.read(self.row, f"delivery-{CONSUMER}.json"))
        self.assert_refused(message="consumer already verified")

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

    def test_repairs_a_consumer_with_only_pending_or_failed_receipts_beside_verified_roles(self):
        """Mutation: refuse when the consumer has any receipt, or when any other role is verified."""
        self.seed_planner()
        self.seed_audit(1)
        for attempt in (1, 2):
            self.seed_receipt("pending", attempt=attempt, sequence=1)
            self.seed_receipt("failed", attempt=attempt, sequence=2)
        for other in ("offline", "application"):
            self.seed_receipt("verified", consumer=other, attempt=3)
        self.assertIsNone(self.store.read(self.row, f"delivery-{CONSUMER}.json"))
        before = copy.deepcopy(self.store.assets)
        # The public check lets the workflow reach the mint, then the repair sends and appends.
        self.assertEqual(self.workflow_before_target_mint(), ("mint", 0, ""))
        self.assertEqual(self.tokens, [""])
        self.assertEqual((self.requests, self.puts, self.store.assets), ([], [], before))
        self.invoke()
        name = f"delivery-redelivered-{CONSUMER}-2.json"
        audit_raw = contract.canonical(self.audit(2))
        self.assertEqual(self.puts, [(TAG, name, audit_raw)])
        self.assertEqual(self.effects, ["send", "put"])
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.store.assets, {**before, (TAG, name): audit_raw})
        self.assertEqual(self.output.read_bytes(), audit_raw)

    def test_refuses_a_malformed_receipt_of_the_consumer(self):
        """Mutation: drop the receipt shape validation, or pattern-match its timestamp without parsing it."""
        def drop(name):
            return lambda body: body.pop(name)

        def change(**values):
            return lambda body: body.update(values)

        malformed, positive = "malformed consumer receipt", "expected positive integer"
        unparsable = "malformed consumer receipt timestamp"
        cases = [(f"missing {name}", drop(name), malformed)
                 for name in RECEIPT_FIELDS if name not in ("event_id", "consumer")]
        cases += [
            ("unknown state", change(state="VERIFIED"), malformed),
            ("another schema", change(schema="pineforge/facts-consumer-receipt/v1"), malformed),
            ("another release version", change(release_version="1.6.1"), malformed),
            ("zero attempt", change(attempt=0), positive),
            ("boolean attempt", change(attempt=True), positive),
            ("zero sequence", change(sequence=0), positive),
            ("zero reporting run", change(reporting_run_id=0), positive),
            ("bad timestamp", change(timestamp="yesterday"), "malformed identity"),
            # Both match the pattern; only parsing, as the publisher does, refuses them.
            ("impossible date and time", change(timestamp="2026-99-99T99:99:99Z"), unparsable),
            ("impossible day of month", change(timestamp="2026-02-30T00:00:00Z"), unparsable),
            ("bad evidence digest", change(evidence_sha256="xyz"), "malformed identity"),
        ]
        for label, edit, message in cases:
            # One non-verified receipt: its shape is the only possible refusal.
            with self.subTest(case=label), self.restored_store():
                body = self.receipt_body("ready")
                edit(body)
                self.seed_receipt_asset(f"consumer-{CONSUMER}-1-1.json", body)
                self.assert_public_refusal(message)

    def test_refuses_a_verified_receipt_that_is_not_the_selected_one(self):
        """Mutation: decide from the highest (attempt, sequence) receipt instead of every receipt."""
        orders = {
            "verified first": (("verified", 1, 1), ("failed", 2, 1)),
            "verified last": (("failed", 2, 1), ("verified", 1, 1)),
        }
        for label, seeds in orders.items():
            with self.subTest(order=label), self.restored_store():
                for state, attempt, sequence in seeds:
                    self.seed_receipt(state, attempt=attempt, sequence=sequence)
                self.assert_public_refusal("consumer already verified")

    def test_refuses_two_receipts_with_the_same_identity_whichever_asset_is_older(self):
        """Mutation: drop the duplicate-identity require (the lower asset id is then selected)."""
        copy_name, canonical_name = f"consumer-{CONSUMER}-copy.json", f"consumer-{CONSUMER}-1-1.json"
        orders = {
            "copy first": ((copy_name, "failed"), (canonical_name, "ready")),
            "canonical first": ((canonical_name, "ready"), (copy_name, "failed")),
        }
        for label, seeds in orders.items():
            # Neither receipt is verified: the duplicate identity is the only possible refusal.
            with self.subTest(order=label), self.restored_store():
                for name, state in seeds:
                    self.seed_receipt_asset(name, self.receipt_body(state))
                self.assert_public_refusal("duplicate consumer receipt identity")

    def test_receipt_constants_match_the_receipt_adapter(self):
        """Mutation: change a receipt state, the schema or a field in only one of the two modules."""
        # release_receipts imports release_pipeline, so the pipeline repeats these values.
        self.assertEqual(pipeline.RECEIPT_STATES, adapter.STATES)
        self.assertEqual(pipeline.RECEIPT_SCHEMA, adapter.PRODUCT_SCHEMA)
        self.assertEqual(set(pipeline.RECEIPT_FIELDS), set(RECEIPT_FIELDS))

    def test_every_state_the_receipt_adapter_publishes_is_understood(self):
        """Mutation: leave a state of release_receipts.STATES out of the known receipt states."""
        for index, state in enumerate(adapter.STATES, start=1):
            with self.subTest(state=state), self.restored_store():
                self.seed_receipt(state, attempt=index)
                if state == "verified":
                    self.assert_public_refusal("consumer already verified")
                else:
                    pipeline.redelivery_source(TAG, CONSUMER)

    def test_eligibility_accepts_receipts_exactly_as_the_adapter_publishes_them(self):
        """Mutation: require a receipt field or value form that release_receipts never publishes."""
        self.assertEqual(set(self.receipt_body("ready")), set(RECEIPT_FIELDS))
        policy = {"workflow": "release.yml", "e2e": {"workflow": "e2e.yml"}}
        reporting_run = {"id": 90, "path": ".github/workflows/release.yml", "event": "push"}
        for attempt, state in enumerate(("pending", "failed", "superseded"), start=1):
            private = {
                "schema": adapter.PRODUCT_SCHEMA, "event_id": self.event["event_id"],
                "release_version": self.event["release_version"], "consumer": CONSUMER,
                "attempt": attempt, "sequence": 1, "state": state, "timestamp": "2020-01-01T00:00:00Z",
                "pr": None, "ci": None, "e2e": None, "deploy": None,
                "preview": None, "deployed": None, "previous": None}
            public = adapter.validate_receipt(None, "pineforge-4pass/fixture", private, self.event,
                                              policy, reporting_run)
            self.assertEqual(set(public), set(RECEIPT_FIELDS))
            self.seed_receipt_asset(f"consumer-{CONSUMER}-{attempt}-1.json", public)
        pipeline.redelivery_source(TAG, CONSUMER)

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
