#!/usr/bin/env python3
"""Sweep isolation and replay regressions; execution belongs to CI/cloud only."""
from __future__ import annotations

import copy
import io
import json
import os
import unittest
import zipfile
from contextlib import redirect_stderr
from unittest.mock import patch

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

    def __init__(self, run_ids=(90,)):
        hosted = postmerge.HostedPostmergeFixture()
        manifest = binding.fixture_manifest()
        self.facts_raw = contract.canonical(binding.facts_for(manifest))
        manifest["facts"]["sha256"] = contract.sha256(self.facts_raw)
        self.event = contract.payload(contract.canonical(manifest))
        hosted.receipt["event_id"] = self.event["event_id"]
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
        self.row["draft"] = False
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

    def archive_url(self, run_id):
        return f"https://api.github.com/repos/{REPO}/actions/artifacts/{run_id + 1000}/zip"

    def discovery_routes(self):
        return [
            f"repos/{REPO}",
            f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=1",
            *[f"repos/{REPO}/actions/runs/{run_id}" for run_id in sorted(self.runs)],
            *[self.archive_url(run_id) for run_id in sorted(self.runs)],
        ]

    def retain(self, *, attempt=90, sequence=7):
        """Seed the public contract projection of a valid, previously stored receipt."""
        raw = {**copy.deepcopy(self.documents[90]), "attempt": attempt, "sequence": sequence}
        public = {
            "schema": "pineforge/consumer-receipt/v1", "consumer": "hosted",
            "event_id": self.event["event_id"], "release_version": self.event["release_version"],
            "attempt": attempt, "sequence": sequence, "state": "verified",
            "timestamp": raw["timestamp"], "reporting_run_id": 90,
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
        self.output = io.StringIO()
        self.enterContext(redirect_stderr(self.output))
        self.report = self.enterContext(patch.object(receipts, "report_issue"))

    def collect(self, client, store=None, role="hosted", run_id=0):
        policy = POLICY if role == "hosted" else history.POLICY
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture",
                                     "RELEASE_RECEIPT_POLICY": json.dumps({role: policy})}):
            return receipts.collect(client, client, client.store if store is None else store, role, run_id)

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

    def test_sweep_skips_a_type_error_in_receipt(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90] = []
        f.seal(90)
        self.assert_bad_first_continues(f, "list indices must be integers or slices, not str")

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

    def test_pending_during_a_sweep_is_reported_and_skipped(self):
        f = SweepFixture((90, 91, 92))
        request = f.request

        def request_with_unavailable_artifact(url, **kwargs):
            if url == f.archive_url(90):
                raise contract.Pending("artifact unavailable")
            return request(url, **kwargs)

        with patch.object(f, "request", side_effect=request_with_unavailable_artifact):
            self.assert_bad_first_continues(f, "artifact unavailable")

    def test_sweep_skips_a_receipt_naming_an_unknown_release(self):
        f = SweepFixture((90, 91, 92))
        f.documents[90]["release_version"] = "99.99.99"
        f.seal(90)
        self.assert_bad_first_continues(f, "hub release unavailable")

    def test_already_stored_newer_receipt_skips_validation_without_api_calls(self):
        for attempt, sequence in ((91, 1), (90, 8)):
            with self.subTest(attempt=attempt, sequence=sequence):
                f = SweepFixture()
                f.retain(attempt=attempt, sequence=sequence)
                assets = dict(f.store.assets)
                self.collect(f)
                self.assertEqual(f.routes, f.discovery_routes())
                self.assertEqual(f.files, [])
                self.assertEqual(f.written_names(), [])
                self.assertEqual(f.store.assets, assets)
                self.assertEqual(self.output.getvalue(), "")
                self.report.assert_not_called()

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
