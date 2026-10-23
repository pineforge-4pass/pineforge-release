#!/usr/bin/env python3
"""Sweep isolation and replay regressions; execution belongs to CI/cloud only."""
from __future__ import annotations

import copy
import io
import json
import os
import shlex
import sys
import unittest
import urllib.error
import zipfile
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

import test_release_binding as binding
import test_release_coordinator_receipts as coordinator
import test_release_hosted_postmerge_receipts as postmerge
import test_release_receipts as history

contract = history.contract
receipts = history.receipts
REPO = history.REPO
POLICY = postmerge.HOSTED_POLICY
INVENTORY_ERROR = "required check inventory differs from owner policy"
IDENTITY_ERROR = "receipt identity reused with different evidence"


class SweepFixture(history.HistoryClient):
    """Use the existing archive/discovery fake and real hosted evidence fixture."""

    def __init__(self, run_ids=(90,), release_version="1.6.0"):
        hosted = postmerge.HostedPostmergeFixture()
        manifest = binding.fixture_manifest(release_version)
        self.facts_raw = contract.canonical(binding.facts_for(manifest))
        manifest["facts"]["sha256"] = contract.sha256(self.facts_raw)
        self.event = contract.payload(contract.canonical(manifest))
        hosted.receipt["event_id"] = self.event["event_id"]
        hosted.receipt["release_version"] = self.event["release_version"]
        for name in ("preview", "deployed"):
            hosted.receipt[name]["facts_sha256"] = manifest["facts"]["sha256"]
        self.routes, self.files, self.archives = [], [], {}
        self.responses = copy.deepcopy(hosted.responses)
        commit = manifest["facts"]["commit"]
        self.responses[f"repos/{contract.HUB}/compare/{commit}...{commit}"] = {"status": "identical"}
        self.documents, self.runs, self.artifact_by_run = {}, {}, {}
        for offset, run_id in enumerate(run_ids):
            self.documents[run_id] = {**copy.deepcopy(hosted.receipt), "sequence": 7 + offset}
            self.runs[run_id] = {**hosted.reporting_run, "id": run_id, "status": "completed"}
            self.responses[f"repos/{REPO}/actions/runs/{run_id}"] = self.runs[run_id]
            self.artifact_by_run[run_id] = {
                "id": run_id + 1000, "name": POLICY["artifact"], "expired": False,
                "workflow_run": {"id": run_id},
            }
            self.seal(run_id)
        self.artifacts = list(reversed(self.artifact_by_run.values()))
        self.old_shape = copy.deepcopy(self.documents[run_ids[0]])
        legacy = coordinator.ReceiptFixture().receipt
        for name in ("ci", "e2e", "deploy"):
            self.old_shape[name] = copy.deepcopy(legacy[name])
        self.old_shape["preview"]["source"] = legacy["pr"]["head"]
        self.store = binding.MemoryStore(manifest)
        self.row = self.store.get("v" + manifest["release_version"])
        self.row.update(draft=False, prerelease=manifest["prerelease"])
        self.store.put(self.row, contract.ASSET, contract.canonical(manifest))
        self.store.operations.clear()

    def file(self, repo, commit, path):
        self.files.append((repo, commit, path))
        expected = (contract.HUB, self.event["facts"]["commit"], self.event["facts"]["path"])
        if (repo, commit, path) != expected:
            raise AssertionError("unexpected fixture facts request")
        return self.facts_raw

    def replace_archive(self, raw):
        artifact = self.artifact_by_run[90]
        artifact["digest"] = "sha256:" + contract.sha256(raw)
        self.archives[self.archive_url(90)] = raw

    def require_delivery(self, role="hosted"):
        self.store.put(self.row, f"delivery-{role}.json", contract.canonical({
            "schema": "pineforge/release-delivery/v1", "event_id": self.event["event_id"],
            "consumer": role, "state": "dispatch_accepted", "attempt": "1",
            "run_id": 500, "timestamp": "2020-01-01T00:00:00Z",
        }))
        self.store.operations.clear()

    def break_zip_entry(self, failure):
        raw = bytearray(self.archives[self.archive_url(90)])
        central = raw.index(b"PK\x01\x02")
        if failure == "encrypted":
            for offset in (6, central + 8):
                flags = int.from_bytes(raw[offset:offset + 2], "little") | 1
                raw[offset:offset + 2] = flags.to_bytes(2, "little")
        else:
            compression = 99 if failure == "unsupported" else zipfile.ZIP_DEFLATED
            for offset in (8, central + 10):
                raw[offset:offset + 2] = compression.to_bytes(2, "little")
            if failure == "deflate":
                name_size = int.from_bytes(raw[26:28], "little")
                extra_size = int.from_bytes(raw[28:30], "little")
                # BFINAL=1, BTYPE=3 is a reserved (invalid) deflate block.
                raw[30 + name_size + extra_size] = 0x07
        self.replace_archive(bytes(raw))

    def archive_url(self, run_id):
        return f"https://api.github.com/repos/{REPO}/actions/artifacts/{run_id + 1000}/zip"

    def discovery_routes(self):
        return [
            f"repos/{REPO}",
            f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=1",
            *[f"repos/{REPO}/actions/runs/{run_id}" for run_id in sorted(self.runs)],
            *[self.archive_url(run_id) for run_id in sorted(self.runs)],
        ]

    def retain(self, *, attempt=90, sequence=7, run_id=90):
        """Seed the public contract projection of a valid, previously stored receipt."""
        raw = {**copy.deepcopy(self.documents[run_id]), "attempt": attempt, "sequence": sequence}
        public = {
            "schema": "pineforge/consumer-receipt/v1", "consumer": "hosted",
            "event_id": self.event["event_id"], "release_version": self.event["release_version"],
            "attempt": attempt, "sequence": sequence, "state": "verified",
            "timestamp": raw["timestamp"], "reporting_run_id": run_id,
            "evidence_sha256": contract.sha256(contract.canonical(raw)),
            "pr": raw["pr"], "preview": raw["preview"],
            "deployed": raw["deployed"], "previous": raw["previous"],
        }
        for stage in ("ci", "e2e", "deploy"):
            public[stage] = {
                "run_id": raw[stage]["run_id"], "run_attempt": raw[stage]["run_attempt"],
                "inventory_sha256": contract.sha256(contract.canonical(raw[stage]["results"])),
            }
        self.store.put(self.row, f"consumer-hosted-{attempt}-{sequence}.json", contract.canonical(public))
        self.store.operations.clear()

    def written_names(self):
        return [name for operation, name in self.store.operations if operation == "asset"]


class ReceiptCollectSweepTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": ""}))
        self.output = io.StringIO()
        self.enterContext(redirect_stderr(self.output))
        self.report = self.enterContext(patch.object(receipts, "report_issue"))

    def collect(self, client, store=None, role="hosted", run_id=0):
        policy = POLICY if role == "hosted" else history.POLICY
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture",
                                     "RELEASE_RECEIPT_POLICY": json.dumps({role: policy})}):
            return receipts.collect(client, client, client.store if store is None else store, role, run_id)

    def collect_from_workflow(self, client, workflow=None):
        """Run the checked-in collect command with its configured-role env wiring.

        Only GitHub clients/storage are faked. Missing step configuration stays
        missing; the real CLI, collection and required-set check all execute.
        """
        import test_release_workflows as workflows
        if workflow is None:
            workflow = workflows._workflow("release-receipts.yml")
        collectors = workflows.ReceiptCollectorWorkflowTest.collectors(workflow)
        self.assertEqual(len(collectors), 1)
        job_id, _, step = collectors[0]
        declared = {**workflow.get("env", {}), **workflow["jobs"][job_id].get("env", {}),
                    **step.get("env", {})}
        env = {
            "TARGET_REPOSITORY": "fixture", "RELEASE_RECEIPT_POLICY": json.dumps({"hosted": POLICY}),
            "CONSUMER": "hosted", "RUN_ID": "0", "RELEASE_READY_CONSUMERS": "",
        }
        if "RELEASE_READY_CONSUMERS" in declared:
            value = declared["RELEASE_READY_CONSUMERS"]
            env["RELEASE_READY_CONSUMERS"] = (
                "hosted" if value == "${{ vars.RELEASE_READY_CONSUMERS }}" else value)
        with patch.dict(os.environ, env, clear=True):
            command = [os.path.expandvars(word) for word in shlex.split(step["run"])]
            self.assertEqual(command[:2], ["python3", "scripts/release_receipts.py"])
            with patch.object(sys, "argv", command[1:]), \
                    patch.object(receipts, "Client", return_value=client), \
                    patch.object(receipts, "Store", return_value=client.store):
                return receipts.main()

    def assert_skipped(self, run_id, reason, client, role="hosted"):
        self.assertEqual(
            self.output.getvalue(),
            f"::warning::receipt artifact skipped: role={role} run={run_id}: {reason}\n",
        )
        self.report.assert_called_once_with(
            client, "receipt-skipped-" + role,
            f"Release chain: receipt artifacts skipped ({role})", f"run={run_id}: {reason}",
        )

    def assert_validated(self, client):
        for route in (f"repos/{REPO}/pulls/9",
                      f"repos/{REPO}/actions/runs/41/attempts/2",
                      f"repos/{REPO}/actions/runs/52/attempts/3",
                      f"repos/{REPO}/actions/runs/63/attempts/4",
                      f"repos/{REPO}/commits/{'2' * 40}"):
            self.assertIn(route, client.routes)
        self.assertTrue(client.files)

    def assert_bad_first_continues(self, fixture, reason):
        self.assertIsNone(self.collect(fixture))
        self.assertEqual(fixture.written_names(), ["consumer-hosted-90-8.json", "consumer-hosted-90-9.json"])
        self.assertNotIn((fixture.row["tag_name"], "consumer-hosted-90-7.json"), fixture.store.assets)
        self.assert_skipped(90, reason, fixture)

    def test_sweep_skips_one_invalid_artifact_among_valid_ones_and_names_it(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90] = f.old_shape
        f.seal(90)
        self.assert_bad_first_continues(f, INVENTORY_ERROR)

    def test_cli_fails_after_reporting_an_invalid_required_hosted_receipt(self):
        f = SweepFixture((90,))
        f.require_delivery()
        f.documents[90]["ci"]["expected"] = ["old-check"]
        f.seal(90)
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture",
                                     "RELEASE_RECEIPT_POLICY": json.dumps({"hosted": POLICY})}), \
                patch.object(sys, "argv", ["release_receipts.py", "collect", "--consumer", "hosted"]), \
                patch.object(receipts, "Client", return_value=f), \
                patch.object(receipts, "Store", return_value=f.store):
            self.assertEqual(receipts.main(), 1)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), (
            f"::warning::receipt artifact skipped: role=hosted run=90: {INVENTORY_ERROR}\n"
            "::error::consumer evidence invalid or unavailable; no verified state recorded: "
            "required consumer receipt missing: role=hosted releases=v1.6.0\n"
        ))
        self.report.assert_called_once_with(
            f, "receipt-skipped-hosted", "Release chain: receipt artifacts skipped (hosted)",
            f"run=90: {INVENTORY_ERROR}",
        )
        status = binding.pipeline.aggregate(f.store, f.row, f.event, enabled=("hosted",))
        self.assertEqual(status["consumers"], {"hosted": "pending"})
        self.assertFalse(status["complete"])

    def test_required_hosted_target_without_any_retained_artifact_fails(self):
        f = SweepFixture()
        f.require_delivery()
        f.artifacts = []
        with self.assertRaisesRegex(
                contract.Invalid, "^required consumer receipt missing: role=hosted releases=v1[.]6[.]0$"):
            self.collect(f)
        self.assertEqual(f.routes, [
            f"repos/{REPO}",
            f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=1",
        ])
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_configured_product_target_requires_a_receipt_before_delivery_acceptance(self):
        f = SweepFixture()
        f.artifacts = []
        with patch.dict(os.environ, {"RELEASE_READY_CONSUMERS": "hosted"}), \
                self.assertRaisesRegex(
                    contract.Invalid, "^required consumer receipt missing: role=hosted releases=v1[.]6[.]0$"):
            self.collect(f)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_workflow_requires_configured_receipts_through_delivery_repair(self):
        for delivery in ("not_started", "attempted", "failed", "redelivered"):
            for corrupt in (False, True):
                with self.subTest(delivery=delivery, corrupt=corrupt):
                    f = SweepFixture()
                    valid_document = copy.deepcopy(f.documents[90])
                    if delivery != "not_started":
                        attempts = 1 if delivery == "attempted" else 3
                        for attempt in range(1, attempts + 1):
                            f.store.put(f.row, f"delivery-attempt-hosted-{attempt}.json", contract.canonical({
                                "event_id": f.event["event_id"], "consumer": "hosted", "attempt": attempt,
                                "run_id": 500, "timestamp": "2020-01-01T00:00:00Z",
                            }))
                        if delivery in ("failed", "redelivered"):
                            f.store.put(f.row, "delivery-failed-hosted.json", contract.canonical({
                                "event_id": f.event["event_id"], "consumer": "hosted", "state": "failed",
                                "attempts": 3, "timestamp": "2020-01-01T00:00:00Z",
                            }))
                        if delivery == "redelivered":
                            f.store.put(f.row, "delivery-redelivered-hosted-1.json", contract.canonical({
                                "schema": "pineforge/release-redelivery/v1", "event_id": f.event["event_id"],
                                "consumer": "hosted", "attempt": 1, "state": "dispatch_accepted",
                                "run_id": 501, "timestamp": "2020-01-01T00:00:00Z", "actor": "operator",
                            }))
                    if corrupt:
                        f.documents[90]["ci"]["expected"] = ["old-check"]
                        f.seal(90)
                    else:
                        f.artifacts = []
                    f.store.operations.clear()
                    self.output.seek(0)
                    self.output.truncate()
                    self.report.reset_mock()
                    self.assertEqual(self.collect_from_workflow(f), 1)
                    self.assertEqual(f.written_names(), [])
                    warning = (f"::warning::receipt artifact skipped: role=hosted run=90: {INVENTORY_ERROR}\n"
                               if corrupt else "")
                    self.assertEqual(self.output.getvalue(), warning + (
                        "::error::consumer evidence invalid or unavailable; no verified state recorded: "
                        "required consumer receipt missing: role=hosted releases=v1.6.0\n"))
                    if corrupt:
                        self.report.assert_called_once_with(
                            f, "receipt-skipped-hosted", "Release chain: receipt artifacts skipped (hosted)",
                            f"run=90: {INVENTORY_ERROR}",
                        )
                    else:
                        self.report.assert_not_called()
                    # A stored, event-bound receipt satisfies the same configured target.
                    f.documents[90] = valid_document
                    f.retain()
                    f.artifacts = []
                    self.output.seek(0)
                    self.output.truncate()
                    self.report.reset_mock()
                    self.assertEqual(self.collect_from_workflow(f), 0)
                    self.assertEqual(f.written_names(), [])
                    self.assertEqual(self.output.getvalue(), "")
                    self.report.assert_not_called()

    def test_removing_workflow_roles_reopens_the_missing_receipt_gate(self):
        import test_release_workflows as workflows
        f = SweepFixture()
        f.artifacts = []
        workflow = workflows._workflow("release-receipts.yml")
        self.assertEqual(self.collect_from_workflow(f, workflow), 1)
        mutant = copy.deepcopy(workflow)
        _, _, step = workflows.ReceiptCollectorWorkflowTest.collectors(mutant)[0]
        step["env"].pop("RELEASE_READY_CONSUMERS")
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(self.collect_from_workflow(f, mutant), 0)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_missing_required_target_fails_after_collecting_another_release(self):
        missing = SweepFixture()
        missing.require_delivery()
        f = SweepFixture((91,), release_version="1.6.0-rc.1")
        f.require_delivery()
        f.store.rows.update(missing.store.rows)
        f.store.assets.update(missing.store.assets)
        with self.assertRaisesRegex(
                contract.Invalid, "^required consumer receipt missing: role=hosted releases=v1[.]6[.]0$"):
            self.collect(f)
        self.assertEqual(f.written_names(), ["consumer-hosted-90-7.json"])
        self.assert_validated(f)
        self.assertNotIn((missing.row["tag_name"], "consumer-hosted-90-7.json"), f.store.assets)
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_required_target_with_a_stored_receipt_still_reports_invalid_new_evidence(self):
        f = SweepFixture()
        f.require_delivery()
        f.retain()
        assets = dict(f.store.assets)
        f.documents[90]["ci"]["expected"] = ["old-check"]
        f.seal(90)
        self.assertIsNone(self.collect(f))
        self.assertEqual(f.store.assets, assets)
        self.assertEqual(f.written_names(), [])
        self.assert_skipped(90, INVENTORY_ERROR, f)

    def test_a_valid_pending_receipt_satisfies_collection_but_not_completion(self):
        f = SweepFixture()
        f.require_delivery()
        f.documents[90].update(state="pending", e2e=None)
        f.seal(90)
        self.assertIsNone(self.collect(f))
        self.assertEqual(f.written_names(), ["consumer-hosted-90-7.json"])
        stored = contract.decode(f.store.assets[(f.row["tag_name"], "consumer-hosted-90-7.json")])
        self.assertEqual((stored["state"], stored["reporting_run_id"]), ("pending", 90))
        status = binding.pipeline.aggregate(f.store, f.row, f.event, enabled=("hosted",))
        self.assertEqual(status["consumers"], {"hosted": "pending"})
        self.assertFalse(status["complete"])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_required_set_excludes_drafts_rejections_and_superseded_releases(self):
        for exclusion in ("draft", "rejected", "superseded"):
            with self.subTest(exclusion=exclusion):
                f = SweepFixture()
                f.require_delivery()
                f.artifacts = []
                if exclusion == "draft":
                    f.row["draft"] = True
                elif exclusion == "rejected":
                    f.store.put(f.row, "release-rejected.json", contract.canonical({"state": "rejected"}))
                else:
                    newer = SweepFixture(release_version="1.6.1")
                    f.store.rows.update(newer.store.rows)
                    f.store.assets.update(newer.store.assets)
                f.store.operations.clear()
                self.assertIsNone(self.collect(f))
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_prerelease_policy_skips_do_not_require_product_receipts(self):
        for role in ("application", "web", "data", "license"):
            with self.subTest(role=role):
                f = SweepFixture(release_version="1.6.0-rc.1")
                f.require_delivery(role)
                f.artifacts = []
                self.assertIsNone(self.collect(f, role=role))
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_newer_prerelease_supersedes_an_unverified_prerelease(self):
        stable = SweepFixture((80,))
        stable.retain(attempt=80, sequence=1, run_id=80)
        older = SweepFixture(release_version="1.7.0-rc.1")
        older.require_delivery()
        f = SweepFixture((91,), release_version="1.7.0-rc.2")
        f.require_delivery()
        f.documents[91]["attempt"] = 91
        f.seal(91)
        for previous in (stable, older):
            f.store.rows.update(previous.store.rows)
            f.store.assets.update(previous.store.assets)
        self.assertIsNone(self.collect(f))
        self.assertEqual(f.written_names(), ["consumer-hosted-91-7.json"])
        self.assertIn(("v1.6.0", "consumer-hosted-80-1.json"), f.store.assets)
        self.assertFalse(any(tag == "v1.7.0-rc.1" and name.startswith("consumer-")
                             for tag, name in f.store.assets))
        self.assert_validated(f)
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_draft_rejected_or_unbacked_prerelease_does_not_supersede(self):
        for exclusion in ("draft", "rejected", "unbacked"):
            with self.subTest(exclusion=exclusion):
                f = SweepFixture(release_version="1.7.0-rc.1")
                f.require_delivery()
                f.artifacts = []
                newer = SweepFixture(release_version="1.7.0-rc.2")
                if exclusion == "draft":
                    newer.row["draft"] = True
                elif exclusion == "rejected":
                    newer.store.put(newer.row, "release-rejected.json", contract.canonical({"state": "rejected"}))
                else:
                    newer.store = binding.MemoryStore(binding.fixture_manifest("1.7.0-rc.2"))
                    newer.row = newer.store.get("v1.7.0-rc.2")
                    newer.row["draft"] = False
                f.store.rows.update(newer.store.rows)
                f.store.assets.update(newer.store.assets)
                with self.assertRaisesRegex(
                        contract.Invalid, "^required consumer receipt missing: role=hosted releases=v1[.]7[.]0-rc[.]1$"):
                    self.collect(f)
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_callback_run_still_fails_closed_on_the_same_invalid_artifact(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90] = f.old_shape
        f.seal(90)
        with self.assertRaisesRegex(contract.Invalid, INVENTORY_ERROR):
            self.collect(f, run_id=90)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_sweep_skips_a_malformed_artifact(self):
        f = SweepFixture((90, 91, 92))
        f.replace_archive(b"not a zip archive")
        self.assert_bad_first_continues(f, "File is not a zip file")

    def test_sweep_skips_encrypted_unsupported_and_corrupt_deflate_entries(self):
        for failure in ("encrypted", "unsupported", "deflate"):
            with self.subTest(failure=failure):
                f = SweepFixture((90, 91, 92))
                f.break_zip_entry(failure)
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                self.assert_bad_first_continues(f, "receipt archive cannot be decoded")

    def test_callback_rejects_encrypted_unsupported_and_corrupt_deflate_entries(self):
        for failure in ("encrypted", "unsupported", "deflate"):
            with self.subTest(failure=failure):
                f = SweepFixture()
                f.break_zip_entry(failure)
                with self.assertRaisesRegex(contract.Invalid, "^receipt archive cannot be decoded$"):
                    self.collect(f, run_id=90)
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_sweep_skips_a_truncated_entry_with_spoofed_compressed_size(self):
        f = SweepFixture((90, 91, 92))
        raw = bytearray(f.archives[f.archive_url(90)])
        central = raw.index(b"PK\x01\x02")
        for offset in (18, central + 20):
            raw[offset:offset + 4] = (100000).to_bytes(4, "little")
        f.replace_archive(bytes(raw))
        self.assert_bad_first_continues(f, "receipt archive cannot be decoded")

    def test_callback_normalizes_a_truncated_entry_with_spoofed_compressed_size(self):
        f = SweepFixture()
        raw = bytearray(f.archives[f.archive_url(90)])
        central = raw.index(b"PK\x01\x02")
        for offset in (18, central + 20):
            raw[offset:offset + 4] = (100000).to_bytes(4, "little")
        f.replace_archive(bytes(raw))
        with self.assertRaisesRegex(contract.Invalid, "^receipt archive cannot be decoded$"):
            self.collect(f, run_id=90)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_entry_eof_is_normalized_before_the_sweep_continues(self):
        """Fault control for decoder EOF, independent of ZIP overlap defenses."""
        f = SweepFixture((90, 91, 92))
        entry_read = zipfile.ZipExtFile.read

        # Match the first entry by its CRC, not a product-code test literal.
        first_crc = zipfile.crc32(contract.canonical(f.documents[90]))

        def read_with_eof(entry, size=-1):
            if entry._expected_crc == first_crc:
                raise EOFError("truncated entry stream")
            return entry_read(entry, size)

        with patch.object(zipfile.ZipExtFile, "read", new=read_with_eof):
            self.assert_bad_first_continues(f, "receipt archive cannot be decoded")

    def test_spoofed_file_size_cannot_trigger_an_unbounded_entry_read(self):
        f = SweepFixture()
        limit = 2 * 1024 * 1024
        document = contract.canonical(f.documents[90])
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("release-receipt.json", document + b" " * (10 * 1024 * 1024 - len(document)))
        raw = bytearray(packed.getvalue())
        central = raw.index(b"PK\x01\x02")
        for offset in (22, central + 24):
            raw[offset:offset + 4] = (100).to_bytes(4, "little")
        f.replace_archive(bytes(raw))
        entry_read = zipfile.ZipExtFile.read
        read_sizes = []

        def observe_read(entry, size=-1):
            read_sizes.append(size)
            return entry_read(entry, size)

        with patch.object(zipfile.ZipExtFile, "read", new=observe_read):
            with self.assertRaisesRegex(
                    contract.Invalid, "^receipt (archive cannot be decoded|exceeds size limit)$"):
                self.collect(f, run_id=90)
        self.assertEqual(read_sizes, [limit + 1])
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_sweep_skips_a_malformed_artifact_with_wrong_inventory(self):
        f = SweepFixture((90, 91, 92))
        f.seal(90, names=("receipt.json",))
        self.assert_bad_first_continues(f, "unexpected receipt archive inventory")

    def test_sweep_skips_a_malformed_artifact_with_invalid_json(self):
        f = SweepFixture((90, 91, 92))
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as zipped:
            zipped.writestr("release-receipt.json", b"{")
        f.replace_archive(packed.getvalue())
        self.assert_bad_first_continues(f, "invalid JSON")

    def test_sweep_reports_a_null_receipt_document(self):
        f = SweepFixture((90, 91))
        f.documents[90] = None
        f.seal(90)
        self.collect(f)
        self.assertEqual(f.written_names(), ["consumer-hosted-90-8.json"])
        self.assertNotIn((f.row["tag_name"], "consumer-hosted-90-7.json"), f.store.assets)
        self.assert_validated(f)
        self.assert_skipped(90, "receipt document must be an object", f)

    def test_callback_rejects_a_null_document_as_invalid_evidence(self):
        f = SweepFixture((90, 91))
        f.documents[90] = None
        f.seal(90)
        with self.assertRaisesRegex(contract.Invalid, "^receipt document must be an object$"):
            self.collect(f, run_id=90)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_sweep_skips_a_malformed_artifact_missing_release_version(self):
        f = SweepFixture((90, 91, 92))
        del f.documents[90]["release_version"]
        f.seal(90)
        self.assert_bad_first_continues(f, "'release_version'")

    def test_sweep_skips_a_value_error_in_receipt_validation(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90]["timestamp"] = "2020-99-01T00:07:00Z"
        f.seal(90)
        self.assert_bad_first_continues(f, "month must be in 1..12")

    def test_skipped_reason_carries_no_marker_mention_markup_or_line_break_and_is_capped(self):
        safe_suffix = " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _.,;()'=- "
        for hostile, expected in (
            ("<!-- pineforge-release-chain:x -->", "??-- pineforge-release-chain?x --?"),
            ("\r\n@owner [a](b) `c`", "???owner ?a?(b) ?c?"),
            ("https://example.com", "https???example.com"),
            ("#123", "?123"),
            ("owner/repo#1", "owner?repo?1"),
            ("&#64;owner", "??64;owner"),
            ("~~~", "???"),
            ("*x*", "?x?"),
            ("|", "?"),
            ("é\t\\", "???"),
        ):
            with self.subTest(hostile=hostile):
                f = SweepFixture()
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                error = contract.Invalid(hostile + safe_suffix + "x" * 300)
                with patch.object(receipts, "validate_receipt", side_effect=error):
                    self.assertIsNone(self.collect(f))
                self.assertEqual(f.written_names(), [])
                self.assert_skipped(90, (expected + safe_suffix + "x" * 300)[:200], f)

    def test_sweep_skips_a_non_object_receipt(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90] = []
        f.seal(90)
        self.assert_bad_first_continues(f, "receipt document must be an object")

    def test_sweep_guards_malformed_attempt_and_sequence_before_comparison(self):
        for key in ("attempt", "sequence"):
            for value in (None, True, "7", [], 0):
                with self.subTest(key=key, value=value):
                    f = SweepFixture()
                    f.retain()
                    f.documents[90][key] = value
                    f.seal(90)
                    self.report.reset_mock()
                    self.output.seek(0)
                    self.output.truncate()
                    self.collect(f)
                    self.assertEqual(f.written_names(), [])
                    self.assertEqual(f.routes, f.discovery_routes())
                    self.assert_skipped(90, "expected positive integer", f)

    def test_oserror_during_a_sweep_stays_fatal(self):
        f = SweepFixture((90, 91, 92))
        error = OSError("transport unavailable")
        with patch.object(f, "request", side_effect=error), self.assertRaises(OSError) as caught:
            self.collect(f)
        self.assertIs(caught.exception, error)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_real_client_http_and_transport_errors_abort_the_sweep(self):
        url = f"https://api.github.com/repos/{REPO}/actions/artifacts/1090/zip"
        failures = [
            (urllib.error.HTTPError(url, status, "fixture", {}, None),
             f"artifact request failed (HTTP {status})")
            for status in (401, 403, 429, 500, 502, 503)
        ] + [
            (error, "artifact transport failed")
            for error in (TimeoutError("timed out"), OSError("transport unavailable"),
                          urllib.error.URLError("transport unavailable"))
        ]
        for error, message in failures:
            with self.subTest(error=repr(error), message=message):
                f = SweepFixture((90, 91, 92))
                opener = Mock()
                opener.open.side_effect = error
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                # Keep the actual Client.request translation and fail only urllib.
                with patch.object(f, "request", binding.artifacts.Client(token="dummy").request), \
                        patch("urllib.request.build_opener", return_value=opener), \
                        self.assertRaises(contract.Invalid) as caught:
                    self.collect(f)
                self.assertEqual(str(caught.exception), message)
                opener.open.assert_called_once()
                self.assertEqual(opener.open.call_args.args[0].full_url, f.archive_url(90))
                self.assertEqual(opener.open.call_args.kwargs, {"timeout": 120})
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_unavailable_earlier_receipt_cannot_bypass_terminal_or_regression_checks(self):
        for source in ("pending", "http404"):
            for earlier, later, reason in (
                ("failed", "verified", "terminal consumer state requires a new attempt"),
                ("deploying", "ready", "consumer state regressed"),
            ):
                with self.subTest(source=source, earlier=earlier, later=later):
                    f = SweepFixture((90, 91))
                    f.require_delivery()
                    f.documents[90].update(state=earlier, e2e=None)
                    if later != "verified":
                        f.documents[91].update(state=later, e2e=None)
                    f.seal(90)
                    f.seal(91)
                    request = f.request
                    unavailable = contract.Pending("published artifact unavailable")
                    real_client = binding.artifacts.Client(token="dummy")
                    opener = Mock()
                    opener.open.side_effect = urllib.error.HTTPError(
                        f.archive_url(90), 404, "fixture", {}, None)
                    self.report.reset_mock()
                    self.output.seek(0)
                    self.output.truncate()

                    def request_with_unavailable_artifact(url, **kwargs):
                        if url == f.archive_url(90):
                            if source == "http404":
                                return real_client.request(url, **kwargs)
                            raise unavailable
                        return request(url, **kwargs)

                    with patch("urllib.request.build_opener", return_value=opener), \
                            patch.object(f, "request", side_effect=request_with_unavailable_artifact) as requests, \
                            self.assertRaisesRegex(
                                contract.Pending, "^published artifact unavailable$") as caught:
                        self.collect(f)
                    requests.assert_called_once_with(f.archive_url(90), github=True)
                    if source == "pending":
                        self.assertIs(caught.exception, unavailable)
                        opener.open.assert_not_called()
                    else:
                        opener.open.assert_called_once()
                        self.assertEqual(opener.open.call_args.args[0].full_url, f.archive_url(90))
                        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 120})
                    self.assertEqual(f.written_names(), [])
                    self.assertEqual(f.files, [])
                    self.assertEqual(self.output.getvalue(), "")
                    self.report.assert_not_called()
                    status = binding.pipeline.aggregate(f.store, f.row, f.event, enabled=("hosted",))
                    self.assertEqual(status["consumers"], {"hosted": "pending"})
                    self.assertFalse(status["complete"])

                    # Recovery must apply (90, 7) before judging (90, 8).
                    self.collect(f)
                    self.assertEqual(f.written_names(), ["consumer-hosted-90-7.json"])
                    stored = contract.decode(
                        f.store.assets[(f.row["tag_name"], "consumer-hosted-90-7.json")])
                    self.assertEqual((stored["state"], stored["reporting_run_id"]), (earlier, 90))
                    self.assertNotIn((f.row["tag_name"], "consumer-hosted-90-8.json"), f.store.assets)
                    self.assertEqual(self.report.call_count, 2 if earlier == "failed" else 1)
                    if earlier == "failed":
                        self.assertEqual(self.report.call_args_list[0].args[:3], (
                            f, "v1.6.0", "Release 1.6.0: consumer failed",
                        ))
                    self.assertEqual(self.report.call_args_list[-1].args, (
                        f, "receipt-skipped-hosted", "Release chain: receipt artifacts skipped (hosted)",
                        f"run=91: {reason}",
                    ))
                    self.assertEqual(self.output.getvalue(), (
                        f"::warning::receipt artifact skipped: role=hosted run=91: {reason}\n"
                    ))
                    status = binding.pipeline.aggregate(f.store, f.row, f.event, enabled=("hosted",))
                    self.assertEqual(status["consumers"], {"hosted": earlier})
                    self.assertFalse(status["complete"])

    def test_pending_during_a_sweep_stays_fatal(self):
        f = SweepFixture((90, 91, 92))
        request = f.request
        error = contract.Pending("artifact unavailable")

        def request_with_unavailable_artifact(url, **kwargs):
            if url == f.archive_url(90):
                raise error
            return request(url, **kwargs)

        with patch.object(f, "request", side_effect=request_with_unavailable_artifact) as requests, \
                self.assertRaises(contract.Pending) as caught:
            self.collect(f)
        self.assertIs(caught.exception, error)
        requests.assert_called_once_with(f.archive_url(90), github=True)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(f.files, [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_sweep_stops_on_a_receipt_naming_an_unavailable_release(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90]["release_version"] = "99.99.99"
        f.seal(90)
        with self.assertRaisesRegex(contract.Pending, "^hub release unavailable$"):
            self.collect(f)
        self.assertIn(f.archive_url(90), f.routes)
        for run_id in (91, 92):
            self.assertNotIn(f.archive_url(run_id), f.routes)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(f.files, [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_already_stored_older_receipt_skips_validation_without_api_calls(self):
        for attempt, sequence in ((91, 1), (90, 8)):
            with self.subTest(attempt=attempt, sequence=sequence):
                f = SweepFixture()
                f.retain()
                f.retain(attempt=attempt, sequence=sequence)
                assets = dict(f.store.assets)
                self.collect(f)
                self.assertEqual(f.routes, f.discovery_routes())
                self.assertEqual(f.files, [])
                self.assertEqual(f.written_names(), [])
                self.assertEqual(f.store.assets, assets)
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_unretained_older_invalid_receipt_is_validated_and_reported(self):
        for attempt, sequence in ((91, 1), (90, 8)):
            with self.subTest(attempt=attempt, sequence=sequence):
                f = SweepFixture()
                f.retain(attempt=attempt, sequence=sequence)
                assets = dict(f.store.assets)
                f.documents[90] = f.old_shape
                f.seal(90)
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                self.collect(f)
                self.assertEqual(f.routes, [*f.discovery_routes(), f"repos/{REPO}/pulls/9"])
                self.assertEqual(f.written_names(), [])
                self.assertEqual(f.store.assets, assets)
                self.assert_skipped(90, INVENTORY_ERROR, f)

    def test_unretained_older_valid_receipt_is_reported_without_rewriting_state(self):
        for state in ("verified", "failed"):
            with self.subTest(state=state):
                f = SweepFixture()
                f.retain(sequence=8)
                assets = dict(f.store.assets)
                if state == "failed":
                    f.documents[90].update(state=state, e2e=None)
                    f.seal(90)
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                self.collect(f)
                if state == "verified":
                    self.assert_validated(f)
                self.assertEqual(f.written_names(), [])
                self.assertEqual(f.store.assets, assets)
                self.assert_skipped(90, "older receipt has no matching retained evidence", f)

    def test_older_stored_identity_replayed_from_another_run_is_reported(self):
        f = SweepFixture((90, 91, 92))
        f.documents[91] = copy.deepcopy(f.documents[90])
        f.seal(91)
        f.retain()
        self.collect(f, run_id=92)
        f.store.operations.clear()
        f.routes.clear()
        f.files.clear()
        assets = dict(f.store.assets)
        self.collect(f)
        self.assert_validated(f)
        self.assertEqual(f.routes.count(f"repos/{REPO}/pulls/9"), 1)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(f.store.assets, assets)
        self.assert_skipped(91, IDENTITY_ERROR, f)

    def test_same_key_same_evidence_is_skipped_without_validation(self):
        f = SweepFixture()
        f.retain()
        assets = dict(f.store.assets)
        self.collect(f)
        self.assertEqual(f.routes, f.discovery_routes())
        self.assertEqual(f.files, [])
        self.assertEqual(f.written_names(), [])
        self.assertEqual(f.store.assets, assets)
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_equal_evidence_from_another_reporting_run_is_validated_and_reported(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                f = SweepFixture((90, 91))
                f.documents[91] = copy.deepcopy(f.documents[90])
                f.seal(91)
                if retained:
                    f.retain()
                self.report.reset_mock()
                self.output.seek(0)
                self.output.truncate()
                self.assertIsNone(self.collect(f))
                self.assertEqual(f.written_names(), [] if retained else ["consumer-hosted-90-7.json"])
                stored = contract.decode(f.store.assets[(f.row["tag_name"], "consumer-hosted-90-7.json")])
                self.assertEqual(stored["reporting_run_id"], 90)
                self.assertEqual(stored["evidence_sha256"], contract.sha256(contract.canonical(f.documents[91])))
                self.assert_validated(f)
                self.assertEqual(f.routes.count(f"repos/{REPO}/pulls/9"), 1 if retained else 2)
                self.assert_skipped(91, IDENTITY_ERROR, f)

    def test_same_key_different_evidence_is_validated_and_reported_not_stored(self):
        f = SweepFixture()
        f.retain()
        assets = dict(f.store.assets)
        f.documents[90]["timestamp"] = "2020-01-01T00:08:00Z"
        f.seal(90)
        self.assertIsNone(self.collect(f))
        self.assert_validated(f)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(f.store.assets, assets)
        self.assert_skipped(90, IDENTITY_ERROR, f)

    def test_stored_older_receipt_still_advances(self):
        for attempt, sequence in ((89, 100), (90, 6)):
            with self.subTest(attempt=attempt, sequence=sequence):
                f = SweepFixture()
                f.retain(attempt=attempt, sequence=sequence)
                self.collect(f)
                self.assert_validated(f)
                self.assertEqual(f.written_names(), ["consumer-hosted-90-7.json"])
                stored = contract.decode(f.store.assets[(f.row["tag_name"], "consumer-hosted-90-7.json")])
                self.assertEqual((stored["attempt"], stored["sequence"], stored["state"]), (90, 7, "verified"))
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_unchanged_skipped_set_does_not_change_the_issue_body(self):
        f = SweepFixture((90, 91, 92))
        for run_id in f.documents:
            f.documents[run_id] = copy.deepcopy(f.old_shape)
            f.seal(run_id)
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "500", "GITHUB_REPOSITORY": contract.HUB}):
            self.collect(f)
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "600", "GITHUB_REPOSITORY": contract.HUB}):
            self.collect(f)
        self.assertEqual(self.report.call_count, 2)
        first, second = self.report.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(first.args[1], "receipt-skipped-hosted")
        self.assertEqual(first.args[3], "\n".join(f"run={run_id}: {INVENTORY_ERROR}" for run_id in (90, 91, 92)))
        expected = "".join(
            f"::warning::receipt artifact skipped: role=hosted run={run_id}: {INVENTORY_ERROR}\n"
            for run_id in (90, 91, 92)
        )
        self.assertEqual(self.output.getvalue(), expected * 2)
        self.assertEqual(f.written_names(), [])

    def test_skipped_issue_lists_at_most_50_runs_and_replays_identically(self):
        f = SweepFixture(tuple(reversed(range(90, 150))))
        for run_id in f.documents:
            f.documents[run_id] = copy.deepcopy(f.old_shape)
            f.seal(run_id)
        self.collect(f)
        f.artifacts.reverse()
        self.collect(f)
        self.assertEqual(self.report.call_count, 2)
        first, second = self.report.call_args_list
        expected_body = "\n".join([
            *(f"run={run_id}: {INVENTORY_ERROR}" for run_id in range(90, 140)),
            "and 10 more skipped",
        ])
        self.assertEqual(first.args[1:], (
            "receipt-skipped-hosted", "Release chain: receipt artifacts skipped (hosted)", expected_body,
        ))
        self.assertEqual(first.args[3].encode("utf-8"), second.args[3].encode("utf-8"))
        expected_warnings = "".join(
            f"::warning::receipt artifact skipped: role=hosted run={run_id}: {INVENTORY_ERROR}\n"
            for run_id in range(90, 150)
        )
        self.assertEqual(self.output.getvalue(), expected_warnings * 2)
        self.assertEqual(f.written_names(), [])

    def test_clean_sweep_makes_no_skip_report(self):
        f = SweepFixture((90, 91, 92))
        self.collect(f)
        self.assertEqual(f.written_names(), [
            "consumer-hosted-90-7.json", "consumer-hosted-90-8.json", "consumer-hosted-90-9.json",
        ])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_facts_only_artifact_invalid_is_skipped_and_reported(self):
        client, store = history.HistoryClient(), history.MemoryStore()
        for name, raw in history.EXPECTED_STORED.items():
            store.put(store.row, name, raw)
        store.writes.clear()
        assets = dict(store.assets)
        client.documents[1007]["ci"]["expected"] = ["old-check"]
        client.seal(1007)
        self.collect(client, store, role="license")
        self.assertIn(f"repos/{REPO}/actions/runs/203/attempts/1", client.routes)
        self.assertTrue(client.files)
        self.assertEqual(store.writes, [])
        self.assertEqual(store.assets, assets)
        self.assert_skipped(1007, INVENTORY_ERROR, client, role="license")

    def test_callback_revalidates_a_stored_receipt(self):
        f = SweepFixture()
        f.retain()
        self.collect(f, run_id=90)
        self.assert_validated(f)
        self.assertIn(f"repos/{REPO}/actions/runs/90/artifacts?per_page=100&page=1", f.routes)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.output.getvalue(), "")
        self.report.assert_not_called()

    def test_callback_fetch_errors_still_propagate(self):
        for error_type in (contract.Invalid, ValueError, KeyError, TypeError, zipfile.BadZipFile,
                           OSError, contract.Pending):
            with self.subTest(error_type=error_type.__name__):
                f = SweepFixture()
                error = error_type("artifact rejected")
                with patch.object(receipts, "fetch_artifact", side_effect=error), \
                        self.assertRaises(error_type) as caught:
                    self.collect(f, run_id=90)
                self.assertIs(caught.exception, error)
                self.assertEqual(f.written_names(), [])
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

    def test_sweep_skips_a_store_rejection(self):
        f = SweepFixture()
        with patch.object(f.store, "put", side_effect=contract.Invalid("hub write rejected")):
            self.collect(f)
        self.assertEqual(f.written_names(), [])
        self.assert_skipped(90, "hub write rejected", f)

    def test_failed_state_report_error_cannot_leave_a_stored_skipped_receipt(self):
        f = SweepFixture()
        f.documents[90].update(state="failed", e2e=None)
        f.seal(90)
        self.report.side_effect = [ValueError("failure report rejected"), None]
        self.collect(f)
        self.assertEqual(f.written_names(), [])
        self.assertEqual(self.report.call_count, 2)
        self.assertEqual(self.report.call_args_list[0].args[2], "Release 1.6.0: consumer failed")
        self.assertEqual(self.report.call_args_list[1].args[1:], (
            "receipt-skipped-hosted", "Release chain: receipt artifacts skipped (hosted)",
            "run=90: failure report rejected",
        ))
        self.assertEqual(
            self.output.getvalue(),
            "::warning::receipt artifact skipped: role=hosted run=90: failure report rejected\n",
        )


if __name__ == "__main__":
    unittest.main()
