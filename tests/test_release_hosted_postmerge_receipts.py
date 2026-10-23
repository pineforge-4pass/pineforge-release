#!/usr/bin/env python3
"""Hosted postmerge receipts for box/CI execution; no network or consumer code."""
from __future__ import annotations

import copy
import json
import os
import re
import unittest
from unittest.mock import patch

import test_release_coordinator_receipts as coordinator

contract = coordinator.contract
receipts = coordinator.receipts
example = coordinator.example
ROOT = coordinator.ROOT

# Transcribed from hosted receipt.mjs RECEIPT_POLICY at 0a1c82c8.
HOSTED_POLICY = {
    "workflow": "release-report.yml", "artifact": "release-receipt-hosted", "base": "main",
    "ci": {"workflow": "release-pr.yml", "expected": ["authz", "validate", "checks", "container"], "approved_skips": []},
    "e2e": {"workflow": "release-postmerge.yml", "expected": ["authz", "build", "verified"], "approved_skips": []},
    "deploy": {"workflow": "release.yml", "expected": ["decide", "deploy"], "approved_skips": []},
}


class HostedPostmergeFixture(coordinator.ReceiptFixture):
    """Reuse the coordinator fake API, with the hosted buildReceipt projection."""

    def __init__(self):
        super().__init__("hosted", HOSTED_POLICY)
        event, pr = self.event, self.pr
        identity = {
            "source": pr["merge"], "image_digest": event["image"]["digest"],
            "facts_commit": event["facts"]["commit"], "facts_sha256": event["facts"]["sha256"],
            "deployment_id_sha256": "d" * 64,
        }
        self.receipt = {
            "schema": "pineforge/consumer-receipt/v1", "consumer": "hosted",
            "event_id": event["event_id"], "release_version": event["release_version"],
            "attempt": 90, "sequence": 7, "state": "verified", "timestamp": "2020-01-01T00:07:00Z",
            "pr": pr, "ci": None, "e2e": None, "deploy": None,
            "preview": dict(identity), "deployed": dict(identity),
            "previous": {**identity, "source": "4" * 40},
        }
        for stage, source in (("ci", pr["head"]), ("e2e", pr["merge"]), ("deploy", pr["merge"])):
            run = self.runs[stage]
            expected = HOSTED_POLICY[stage]["expected"]
            self.receipt[stage] = {
                "run_id": run["id"], "run_attempt": run["run_attempt"], "sha": source,
                "expected": list(expected), "approved_skips": [],
                "results": {name: "success" for name in expected},
            }
        self.runs["e2e"].update(head_sha=pr["merge"], event="workflow_dispatch")
        for job in self.jobs["e2e"]:
            job["completed_at"] = "2020-01-01T00:05:30Z"
        self.artifact = {"id": 801, "name": "postmerge-evidence-52-3", "expired": False}
        self.artifacts = [
            {"id": 802, "name": "postmerge-build-52-3", "expired": False},
            self.artifact,
        ]
        self.artifacts_route = f"repos/{self.repo}/actions/runs/52/artifacts?per_page=100&page=1"
        self.responses[self.artifacts_route] = {"artifacts": self.artifacts}


class HostedPostmergeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f = HostedPostmergeFixture()

    def assert_refused(self, message):
        with self.assertRaisesRegex(contract.Invalid, message):
            self.f.verify()

    def test_verified_hosted_postmerge_receipt_is_accepted(self):
        public = self.f.verify()
        self.assertEqual(public["state"], "verified")
        self.assertEqual(public["preview"], self.f.receipt["preview"])
        self.assertEqual(public["deployed"], self.f.receipt["deployed"])
        self.assertEqual(public["previous"], self.f.receipt["previous"])
        for stage in ("ci", "e2e", "deploy"):
            evidence = self.f.receipt[stage]
            self.assertEqual(public[stage], {
                "run_id": evidence["run_id"], "run_attempt": evidence["run_attempt"],
                "inventory_sha256": contract.sha256(contract.canonical(evidence["results"])),
            })
        self.assertIn(self.f.artifacts_route, self.f.queries)
        self.assertEqual(self.f.downloads, [])
        # Both inclusive time boundaries belong to this same accepted shape.
        self.f.actual_pr["merged_at"] = self.f.jobs["e2e"][-1]["completed_at"]
        self.assertEqual(self.f.verify()["state"], "verified")
        self.f.jobs["e2e"][-1]["completed_at"] = self.f.receipt["timestamp"]
        self.assertEqual(self.f.verify()["state"], "verified")

    def test_e2e_run_at_pr_head_is_refused(self):
        self.f.runs["e2e"]["head_sha"] = self.f.pr["head"]
        self.assert_refused("unexpected checks workflow or head")

    def test_e2e_receipt_at_pr_head_is_refused(self):
        self.f.receipt["e2e"]["sha"] = self.f.pr["head"]
        self.assert_refused("checks ran against a different source")

    def test_e2e_push_event_is_refused(self):
        self.f.runs["e2e"]["event"] = "push"
        self.assert_refused("untrusted postmerge source")

    def test_e2e_pull_request_event_is_refused(self):
        self.f.runs["e2e"]["event"] = "pull_request"
        self.assert_refused("untrusted postmerge source")

    def test_e2e_schedule_event_is_refused(self):
        self.f.runs["e2e"]["event"] = "schedule"
        self.assert_refused("untrusted postmerge source")

    def test_e2e_branch_other_than_main_is_refused(self):
        self.f.runs["e2e"]["head_branch"] = "feature"
        self.assert_refused("untrusted postmerge source")

    def test_repository_default_branch_other_than_main_is_refused(self):
        self.f.metadata["default_branch"] = "dev"
        self.assert_refused("untrusted postmerge source")

    def test_e2e_finished_before_merge_is_refused(self):
        self.f.actual_pr["merged_at"] = "2020-01-01T00:05:31Z"
        self.assert_refused("postmerge E2E completed before merge")

    def test_e2e_finished_after_receipt_is_refused(self):
        self.f.jobs["e2e"][-1]["completed_at"] = "2020-01-01T00:07:01Z"
        self.assert_refused("receipt predates postmerge verification")

    def test_e2e_workflow_path_differs_is_refused(self):
        self.f.runs["e2e"]["path"] = ".github/workflows/other.yml"
        self.assert_refused("unexpected checks workflow or head")

    def test_e2e_missing_verified_job_is_refused(self):
        self.f.jobs["e2e"].pop()
        self.assert_refused("required check missing or incomplete")

    def test_e2e_extra_declared_job_is_refused(self):
        self.f.receipt["e2e"]["expected"].append("extra")
        self.assert_refused("required check inventory differs from owner policy")

    def test_e2e_approved_skip_is_refused(self):
        self.f.receipt["e2e"]["approved_skips"] = ["build"]
        self.assert_refused("required check inventory differs from owner policy")

    def test_e2e_skipped_job_is_refused(self):
        self.f.jobs["e2e"][-1]["conclusion"] = "skipped"
        self.assert_refused("required check failed/cancelled/skipped")

    def test_e2e_incomplete_run_is_refused(self):
        self.f.runs["e2e"]["status"] = "in_progress"
        self.assert_refused("required check run not successful")

    def test_e2e_failed_run_is_refused(self):
        self.f.runs["e2e"]["conclusion"] = "failure"
        self.assert_refused("required check run not successful")

    def test_e2e_duplicate_verified_job_is_refused(self):
        self.f.jobs["e2e"].append(copy.deepcopy(self.f.jobs["e2e"][-1]))
        self.assert_refused("required check missing or incomplete")

    def test_e2e_reported_results_differ_is_refused(self):
        self.f.receipt["e2e"]["results"]["verified"] = "skipped"
        self.assert_refused("reported checks differ from GitHub")

    def test_evidence_artifact_missing_is_refused(self):
        self.f.artifacts.remove(self.f.artifact)
        self.assert_refused("missing/ambiguous postmerge evidence")

    def test_evidence_artifact_expired_is_refused(self):
        self.f.artifact["expired"] = True
        self.assert_refused("missing/ambiguous postmerge evidence")

    def test_evidence_artifact_for_another_run_is_refused(self):
        self.f.artifact["name"] = "postmerge-evidence-53-3"
        self.assert_refused("missing/ambiguous postmerge evidence")

    def test_evidence_artifact_for_another_attempt_is_refused(self):
        self.f.artifact["name"] = "postmerge-evidence-52-4"
        self.assert_refused("missing/ambiguous postmerge evidence")

    def test_evidence_artifact_duplicated_is_refused(self):
        self.f.artifacts.append({**self.f.artifact, "id": 803})
        self.assert_refused("missing/ambiguous postmerge evidence")

    def test_unmerged_pr_is_refused(self):
        self.f.actual_pr["merged"] = False
        self.assert_refused("PR is not the recorded merge")

    def test_actual_pr_merge_differs_is_refused(self):
        self.f.actual_pr["merge_commit_sha"] = "f" * 40
        self.assert_refused("PR is not the recorded merge")

    def test_merge_first_parent_differs_is_refused(self):
        self.f.commit["parents"][0]["sha"] = "f" * 40
        self.assert_refused("merge inputs differ from the tested base")

    def test_preview_at_pr_head_is_refused(self):
        self.f.receipt["preview"]["source"] = self.f.pr["head"]
        self.assert_refused("preview did not exercise the candidate source/image")

    def test_repository_metadata_differs_is_refused(self):
        self.f.metadata["full_name"] = "pineforge-4pass/other"
        self.assert_refused("postmerge repository differs")

    def test_run_repository_differs_is_refused(self):
        self.f.runs["e2e"]["repository"]["full_name"] = "pineforge-4pass/other"
        self.assert_refused("postmerge repository differs")

    def test_run_head_repository_differs_is_refused(self):
        self.f.runs["e2e"]["head_repository"]["full_name"] = "pineforge-4pass/other"
        self.assert_refused("postmerge repository differs")

    def test_run_id_differs_is_refused(self):
        self.f.runs["e2e"]["id"] += 1
        self.assert_refused("postmerge run identity differs")

    def test_run_attempt_differs_is_refused(self):
        self.f.runs["e2e"]["run_attempt"] += 1
        self.assert_refused("postmerge run identity differs")

    def test_policy_e2e_inventory_loosened_is_refused(self):
        self.f.policy["e2e"]["expected"] = ["build"]
        self.assert_refused("postmerge inventory differs")

    def test_policy_e2e_inventory_reordered_is_refused(self):
        self.f.policy["e2e"]["expected"].reverse()
        self.assert_refused("postmerge inventory differs")

    def test_policy_e2e_approved_skip_is_refused(self):
        self.f.policy["e2e"]["approved_skips"] = ["build"]
        self.assert_refused("postmerge inventory differs")

    def test_policy_ci_workflow_differs_is_refused(self):
        self.f.policy["ci"]["workflow"] = "other.yml"
        self.assert_refused("unexpected postmerge consumer")

    def test_policy_deploy_workflow_differs_is_refused(self):
        self.f.policy["deploy"]["workflow"] = "other.yml"
        self.assert_refused("unexpected postmerge consumer")

    def test_application_role_is_refused(self):
        self.f.receipt["consumer"] = "application"
        self.assert_refused("unexpected postmerge consumer")

    def test_web_role_is_refused(self):
        self.f.receipt["consumer"] = "web"
        self.assert_refused("unexpected postmerge consumer")

    def test_data_role_is_refused(self):
        self.f.receipt["consumer"] = "data"
        self.assert_refused("unexpected postmerge consumer")

    def test_license_role_is_refused(self):
        self.f.receipt["consumer"] = "license"
        self.assert_refused("unexpected postmerge consumer")

    def test_offline_role_is_refused(self):
        self.f.receipt["consumer"] = "offline"
        self.assert_refused("unexpected postmerge consumer")

    def test_pending_with_non_null_e2e_is_refused(self):
        self.f.receipt["state"] = "pending"
        self.assert_refused("postmerge E2E is required exactly in verified state")

    def test_other_non_verified_states_with_e2e_are_refused(self):
        for state in ("pr_open", "testing", "ready", "merged", "deploying", "failed", "superseded", "skipped_by_policy"):
            self.f = HostedPostmergeFixture()
            self.f.receipt["state"] = state
            with self.subTest(state=state):
                self.assert_refused("postmerge E2E is required exactly in verified state")

    def test_verified_without_e2e_is_refused(self):
        self.f.receipt["e2e"] = None
        self.assert_refused("postmerge E2E is required exactly in verified state")

    def test_ci_finished_after_merge_is_refused(self):
        self.f.jobs["ci"][-1]["completed_at"] = "2020-01-01T00:05:01Z"
        self.assert_refused("CI completed after merge")

    def test_preview_image_differs_is_refused(self):
        self.f.receipt["preview"]["image_digest"] = "sha256:" + "f" * 64
        self.assert_refused("preview did not exercise the candidate source/image")

    def test_documentation_policy_and_example_match_the_hosted_contract(self):
        doc = (ROOT / "docs/release-chain.md").read_text().split("## Supervisor configuration\n", 1)[1]
        policies = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", doc, re.S)]
        documented = next(policy for policy in policies if "hosted" in policy)
        self.assertEqual(documented["hosted"], HOSTED_POLICY)
        self.assertEqual(example.example_policy("hosted"), HOSTED_POLICY)
        expected = copy.deepcopy(self.f.receipt)
        for run_id, stage in enumerate(("ci", "e2e", "deploy"), 1):
            expected[stage].update(run_id=run_id, run_attempt=1)
        self.assertEqual(example.build_receipt(self.f.event, consumer="hosted"), expected)
        with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps(documented)}):
            self.assertEqual(receipts.profile("hosted"), HOSTED_POLICY)
        self.assertEqual(self.f.verify()["state"], "verified")


class HostedPostmergeCompatibilityTests(unittest.TestCase):
    def test_old_hosted_coordinator_receipt_still_verifies(self):
        f = coordinator.ReceiptFixture()
        public = f.verify()
        self.assertEqual(public["state"], "verified")
        self.assertEqual(public["preview"]["source"], f.pr["head"])
        self.assertEqual(f.receipt["e2e"]["sha"], f.pr["base_sha"])
        self.assertEqual(f.downloads, [f.binding_url])

    def test_existing_application_post_merge_tests_still_pass(self):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(coordinator.ApplicationPostMergeReceiptTests)
        expected_count = suite.countTestCases()
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, expected_count)
        self.assertTrue(result.wasSuccessful(), f"errors={result.errors!r}; failures={result.failures!r}")


if __name__ == "__main__":
    unittest.main()
