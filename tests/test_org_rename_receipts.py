"""Exact consumer repository aliases through the real coordinator/receipt paths."""
from __future__ import annotations

import os
import json
import unittest
from unittest.mock import patch

from test_release_coordinator_receipts import ReceiptFixture
import release_contract as contract
import release_receipts as receipts

ORGS = ("pineforge-4pass", "tunaxis")
ACCEPTED_NAMES = ("pineforge-4pass/fixture", "tunaxis/fixture", "TUNAXIS/fixture", "PINEFORGE-4PASS/FIXTURE")
REJECTED_NAMES = ("other/fixture", "pineforge-4pass-x/fixture", "xpineforge-4pass/fixture", "tunaxis-x/fixture", "pineforge-4pass/other", "tunaxis/other", "/fixture", "", "tunaxis/fixture/", "tunaxis/fixture/extra")


def fixture(org):
    repo = f"{org}/fixture"
    with patch.object(ReceiptFixture, "repo", repo):
        value = ReceiptFixture()
        value.repo = repo
    return value


class ReceiptOrganizationTests(unittest.TestCase):
    def test_pr_head_repository_accepts_both_orgs_and_preserves_existing_case_folding(self):
        for api_org in ORGS:
            for full_name in ACCEPTED_NAMES:
                with self.subTest(api_org=api_org, full_name=full_name):
                    value = fixture(api_org)
                    value.actual_pr["head"]["repo"]["full_name"] = full_name
                    public = value.verify()
                    self.assertEqual(public["state"], "verified")
                    self.assertEqual(public["pr"]["head"], value.pr["head"])
                    self.assertEqual(value.downloads, [value.binding_url])

    def test_pr_head_repository_rejects_third_org_lookalikes_other_names_and_empty(self):
        for api_org in ORGS:
            for full_name in REJECTED_NAMES:
                with self.subTest(api_org=api_org, full_name=full_name):
                    value = fixture(api_org)
                    value.actual_pr["head"]["repo"]["full_name"] = full_name
                    with self.assertRaisesRegex(contract.Invalid, "^receipt refers to fork PR$"):
                        value.verify()
                    self.assertEqual(value.downloads, [])

    def test_every_coordinator_full_name_accepts_both_orgs_with_existing_case_folding(self):
        for api_org in ORGS:
            for target in ("metadata", "repository", "head_repository"):
                for full_name in ACCEPTED_NAMES:
                    with self.subTest(api_org=api_org, target=target, full_name=full_name):
                        value = fixture(api_org)
                        row = value.metadata if target == "metadata" else value.runs["e2e"][target]
                        row["full_name"] = full_name
                        public = value.verify()
                        self.assertEqual(public["state"], "verified")
                        self.assertEqual(public["e2e"]["run_id"], 52)
                        self.assertEqual(value.downloads, [value.binding_url])

    def test_every_coordinator_full_name_rejects_other_orgs_names_paths_and_empty(self):
        for api_org in ORGS:
            for target in ("metadata", "repository", "head_repository"):
                for full_name in REJECTED_NAMES:
                    with self.subTest(api_org=api_org, target=target, full_name=full_name):
                        value = fixture(api_org)
                        row = value.metadata if target == "metadata" else value.runs["e2e"][target]
                        row["full_name"] = full_name
                        with self.assertRaisesRegex(contract.Invalid, "^coordinator repository differs$"):
                            value.verify()
                        self.assertEqual(value.downloads, [])

    def test_collect_metadata_accepts_both_orgs_and_keeps_configured_api_target(self):
        for full_name in ACCEPTED_NAMES:
            with self.subTest(full_name=full_name):
                value = ReceiptFixture()
                value.metadata["full_name"] = full_name
                route = f"repos/{value.repo}/actions/workflows/{value.policy['workflow']}/runs?per_page=100&page=1"
                value.responses[route] = {"workflow_runs": []}
                with patch.dict(os.environ, TARGET_REPOSITORY="fixture",
                                RELEASE_RECEIPT_POLICY=json.dumps({"hosted": value.policy})):
                    receipts.collect(value, None, None, "hosted")
                self.assertEqual(value.queries, [f"repos/{value.repo}", route])
                self.assertEqual(value.downloads, [])

    def test_collect_metadata_rejects_other_orgs_names_paths_and_empty_before_runs(self):
        for full_name in REJECTED_NAMES:
            with self.subTest(full_name=full_name):
                value = ReceiptFixture()
                value.metadata["full_name"] = full_name
                with patch.dict(os.environ, TARGET_REPOSITORY="fixture",
                                RELEASE_RECEIPT_POLICY=json.dumps({"hosted": value.policy})):
                    with self.assertRaisesRegex(contract.Invalid, "^consumer repository identity differs$"):
                        receipts.collect(value, None, None, "hosted")
                self.assertEqual(value.queries, [f"repos/{value.repo}"])
                self.assertEqual(value.downloads, [])

    def test_invalid_configured_repository_cannot_authorize_a_receipt_alias(self):
        for repo in ("other/fixture", "tunaxis/", "tunaxis/fixture/extra", "fixture", "", None):
            with self.subTest(repo=repo):
                self.assertEqual(receipts.repo_identities(repo), set())
