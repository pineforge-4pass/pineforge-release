#!/usr/bin/env python3
"""Hosted receipt regressions for CI/cloud only; all API and ZIP bytes are local.

These fixtures exercise the real validator, not the consumer's proposed copy.
Unexpected API routes or downloads fail immediately without making a request.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import consumer_receipt_example as example  # noqa: E402
import release_contract as contract  # noqa: E402
import release_receipts as receipts  # noqa: E402


# Independently transcribed from the reviewed mcp-public policy at 84a48bf.
HOSTED_POLICY = {
    "workflow": "release-report.yml", "artifact": "release-receipt-hosted", "base": "main",
    "ci": {"workflow": "release-pr.yml", "expected": ["validate", "checks", "container", "candidate"], "approved_skips": []},
    "e2e": {"workflow": "release-coordinator.yml", "expected": ["candidate", "release/ready"], "approved_skips": []},
    "deploy": {"workflow": "release.yml", "expected": ["deploy"], "approved_skips": []},
}


class ReceiptFixture:
    repo = "pineforge-4pass/fixture"

    def __init__(self, consumer="hosted", policy=None):
        self.policy = copy.deepcopy(policy if policy is not None else (
            HOSTED_POLICY if consumer == "hosted" else example.example_policy(consumer)))
        self.event = contract.payload((ROOT / "tests/fixtures/release-manifest.json").read_bytes())
        self.receipt = example.build_receipt(self.event, consumer=consumer)
        self.pr = self.receipt["pr"]
        self.actual_pr = {"base": {"ref": "main", "sha": self.pr["base_sha"]},
                          "head": {"sha": self.pr["head"], "repo": {"full_name": self.repo}},
                          "merged": True, "merge_commit_sha": self.pr["merge"], "merged_at": "2020-01-01T00:05:00Z"}
        self.metadata = {"full_name": self.repo, "default_branch": "main"}
        self.commit = {"parents": [{"sha": self.pr["base_sha"]}, {"sha": self.pr["head"]}]}
        self.responses = {f"repos/{self.repo}": self.metadata, f"repos/{self.repo}/pulls/9": self.actual_pr,
                          f"repos/{self.repo}/commits/{self.pr['merge']}": self.commit}
        self.runs, self.jobs = {}, {}
        coordinator = self.policy["e2e"]["workflow"] == "release-coordinator.yml"
        for stage, run_id, attempt in (("ci", 41, 2), ("e2e", 52, 3), ("deploy", 63, 4)):
            sha = self.pr["merge"] if stage == "deploy" else self.pr["head"]
            if stage == "e2e" and coordinator:
                sha = self.pr["base_sha"]
            expected = self.policy[stage]["expected"]
            self.receipt[stage] = {"run_id": run_id, "run_attempt": attempt, "sha": sha,
                                   "expected": list(expected), "approved_skips": [],
                                   "results": {name: "success" for name in expected}}
            run = {"id": run_id, "run_attempt": attempt,
                   "path": ".github/workflows/" + self.policy[stage]["workflow"],
                   "head_sha": sha, "status": "completed", "conclusion": "success",
                   "event": "workflow_run" if stage == "e2e" and coordinator else (
                       "pull_request" if stage == "ci" else "push"),
                   "head_branch": "main", "repository": {"full_name": self.repo},
                   "head_repository": {"full_name": self.repo}}
            jobs = [{"name": name, "status": "completed", "conclusion": "success",
                     "completed_at": "2020-01-01T00:06:00Z" if stage == "deploy" else "2020-01-01T00:04:00Z"}
                    for name in expected]
            self.runs[stage], self.jobs[stage] = run, jobs
            route = f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}"
            self.responses[route] = run
            self.responses[route + "/jobs?per_page=100&page=1"] = {"jobs": jobs}
        self.reporting_run = {"id": 90, "path": ".github/workflows/" + self.policy["workflow"],
                              "head_sha": self.pr["merge"], "event": "workflow_run"}
        self.candidate = {"id": 501, "name": "candidate-41-2", "expired": False,
                          "digest": "sha256:" + contract.sha256(b"original candidate ZIP fixture")}
        self.binding = {
            "schema": "pineforge/mcp-coordinator-binding/v1", "pr": self.pr["number"],
            "head": self.pr["head"], "base": self.pr["base_sha"],
            "source": {"run_id": 41, "attempt": 2},
            "coordinator": {"run_id": 52, "attempt": 3, "sha": self.pr["base_sha"]},
            "artifact": {"id": 501, "name": self.candidate["name"], "digest": self.candidate["digest"],
                         "manifest_sha256": "a" * 64, "worker_sha256": "b" * 64, "container_sha256": "c" * 64},
        }
        self.binding_artifact = {"id": 601, "name": "coordinator-binding-52-3", "expired": False}
        self.binding_artifacts, self.candidates = [self.binding_artifact], [self.candidate]
        for run_id, artifacts in ((52, self.binding_artifacts), (41, self.candidates)):
            self.responses[f"repos/{self.repo}/actions/runs/{run_id}/artifacts?per_page=100&page=1"] = {"artifacts": artifacts}
        self.binding_url = f"https://api.github.com/repos/{self.repo}/actions/artifacts/601/zip"
        self.downloads, self.queries = [], []
        self.seal_binding()

    def seal_binding(self, names=("binding.json",), raw=None):
        """Recompute the ZIP and GitHub digest so identity mutations reach parsing."""
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as zipped:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                zipped.writestr(info, contract.canonical(self.binding) if raw is None else raw,
                                compress_type=zipfile.ZIP_DEFLATED)
        self.archive = packed.getvalue()
        self.binding_artifact["digest"] = "sha256:" + contract.sha256(self.archive)

    def api(self, route):
        self.queries.append(route)
        if route not in self.responses:
            raise AssertionError("unexpected fixture API route: " + route)
        return copy.deepcopy(self.responses[route])

    def request(self, url, **kwargs):
        if url != self.binding_url or kwargs != {"github": True}:
            raise AssertionError("unexpected fixture download")
        self.downloads.append(url)
        return self.archive

    def verify(self):
        return receipts.validate_receipt(self, self.repo, self.receipt, self.event, self.policy, self.reporting_run)


class HostedCoordinatorReceiptTests(unittest.TestCase):
    def test_artifact_zip_downloads_use_default_github_accept_not_octet_stream(self):
        f = ReceiptFixture()
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as zipped:
            zipped.writestr("release-receipt.json", contract.canonical(f.receipt))
        receipt_zip = packed.getvalue()
        receipt_url = f"https://api.github.com/repos/{f.repo}/actions/artifacts/701/zip"
        f.responses[f"repos/{f.repo}/actions/runs/{f.reporting_run['id']}/artifacts?per_page=100&page=1"] = {
            "artifacts": [{"id": 701, "name": f.policy["artifact"], "expired": False,
                           "digest": "sha256:" + contract.sha256(receipt_zip)}]}
        zips = {receipt_url: receipt_zip, f.binding_url: f.archive}

        def request(url, *, github=False, accept=None, **kwargs):
            if not re.search(r"/actions/artifacts/\d+/zip$", url) or not github or kwargs:
                raise AssertionError("unexpected fixture download")
            if accept == "application/octet-stream":
                raise contract.Invalid("artifact request failed (HTTP 415)")
            f.downloads.append(url)
            return zips[url]

        f.request = request
        with self.subTest(call="fetch_artifact"):
            self.assertEqual(receipts.fetch_artifact(f, f.repo, f.reporting_run, f.policy), f.receipt)
        with self.subTest(call="check_coordinator"):
            run = receipts.check_coordinator(f, f.repo, f.receipt["e2e"], f.policy["e2e"], f.pr, f.receipt["ci"])
            self.assertEqual(run["id"], f.runs["e2e"]["id"])
        self.assertEqual(f.downloads, [receipt_url, f.binding_url])

    def test_truthful_hosted_receipt_at_each_gated_state_is_accepted(self):
        for state in ("ready", "merged", "deploying", "verified"):
            with self.subTest(state=state):
                f = ReceiptFixture()
                f.receipt["state"] = state
                if state == "ready":
                    f.actual_pr["merged"] = False
                    f.pr["merge"] = None
                public = f.verify()
                self.assertEqual(public["state"], state)
                self.assertEqual(f.receipt["ci"]["sha"], f.pr["head"])
                self.assertEqual(f.receipt["e2e"]["sha"], f.pr["base_sha"])
                self.assertEqual(public["preview"]["source"], f.pr["head"])
                self.assertEqual(public["e2e"]["inventory_sha256"], contract.sha256(contract.canonical(f.receipt["e2e"]["results"])))
                self.assertEqual(f.downloads, [f.binding_url])
                raw = contract.canonical(public)
                for private in (f.repo, "release/ready", "coordinator-binding", "manifest_sha256"):
                    self.assertNotIn(private.encode(), raw)

    def test_example_policy_and_documentation_match_the_exact_hosted_contract(self):
        self.assertEqual(example.example_policy("hosted"), HOSTED_POLICY)
        f = ReceiptFixture()
        built = example.build_receipt(f.event, consumer="hosted")
        for stage, source in (("ci", "head"), ("e2e", "base_sha"), ("deploy", "merge")):
            self.assertEqual(built[stage]["sha"], built["pr"][source])
            self.assertEqual(built[stage]["expected"], HOSTED_POLICY[stage]["expected"])
        doc = (ROOT / "docs/release-chain.md").read_text().split("## Supervisor configuration\n", 1)[1]
        policies = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)\n```", doc, re.S)]
        documented = next(policy for policy in policies if "hosted" in policy)
        self.assertEqual(documented["hosted"], HOSTED_POLICY)
        self.assertEqual(documented["offline"], {
            "workflow": "release.yml", "artifact": "release-receipt-offline", "base": "main",
            "ci": {"workflow": "ci.yml", "expected": ["check", "unit"], "approved_skips": []},
            "e2e": {"workflow": "e2e.yml", "expected": ["stdio-parity", "syminfo", "negative"], "approved_skips": []},
            "deploy": {"workflow": "publish.yml", "expected": ["publish", "served-identity"], "approved_skips": []},
        })
        with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps(documented)}):
            self.assertEqual(receipts.profile("hosted"), HOSTED_POLICY)
            self.assertEqual(receipts.profile("offline"), documented["offline"])

    def test_e2e_at_pr_head_is_rejected_in_receipt_and_github_independently(self):
        for target in ("receipt", "github", "both"):
            f = ReceiptFixture()
            if target in ("receipt", "both"):
                f.receipt["e2e"]["sha"] = f.pr["head"]
            if target in ("github", "both"):
                f.runs["e2e"]["head_sha"] = f.pr["head"]
            with self.subTest(target=target), self.assertRaises(contract.Invalid):
                f.verify()

    def test_pr_base_and_coordinator_branch_must_be_the_default_main(self):
        for target in ("pr_base", "run_branch", "default_branch"):
            f = ReceiptFixture()
            if target == "pr_base":
                f.pr["base"] = f.policy["base"] = f.actual_pr["base"]["ref"] = "dev"
            elif target == "run_branch":
                f.runs["e2e"]["head_branch"] = "feature"
            else:
                f.metadata["default_branch"] = "dev"
            with self.subTest(target=target), self.assertRaisesRegex(contract.Invalid, "untrusted coordinator source"):
                f.verify()

    def test_only_workflow_run_can_supply_coordinator_evidence(self):
        for event in ("push", "pull_request", "workflow_dispatch", "repository_dispatch"):
            f = ReceiptFixture()
            f.runs["e2e"]["event"] = event
            with self.subTest(event=event), self.assertRaisesRegex(contract.Invalid, "untrusted coordinator source"):
                f.verify()

    def test_other_repository_is_rejected_in_every_coordinator_identity(self):
        for target in ("metadata", "repository", "head_repository"):
            f = ReceiptFixture()
            row = f.metadata if target == "metadata" else f.runs["e2e"][target]
            row["full_name"] = "pineforge-4pass/other"
            with self.subTest(target=target), self.assertRaisesRegex(contract.Invalid, "coordinator repository differs"):
                f.verify()

    def test_coordinator_response_cannot_substitute_another_run_or_attempt(self):
        for key in ("id", "run_attempt"):
            f = ReceiptFixture()
            f.runs["e2e"][key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(contract.Invalid, "coordinator run identity differs"):
                f.verify()

    def test_missing_expired_duplicate_or_renamed_binding_is_rejected(self):
        for mutation in ("missing", "expired", "duplicate", "another_attempt"):
            f = ReceiptFixture()
            if mutation == "missing":
                f.binding_artifacts.clear()
            elif mutation == "expired":
                f.binding_artifact["expired"] = True
            elif mutation == "duplicate":
                f.binding_artifacts.append({**f.binding_artifact, "id": 602})
            else:
                f.binding_artifact["name"] = "coordinator-binding-52-2"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(contract.Invalid, "missing/ambiguous coordinator binding"):
                f.verify()

    def test_binding_digest_is_mandatory_and_checked_before_zip_parsing(self):
        for digest in (None, "", "sha256:" + "0" * 64, "sha512:" + "0" * 128):
            f = ReceiptFixture()
            f.binding_artifact["digest"] = digest
            # Invalid ZIP bytes must still fail at the digest check first.
            f.archive = b"replaced archive bytes"
            with self.subTest(digest=digest), self.assertRaisesRegex(contract.Invalid, "binding archive digest missing or differs"):
                f.verify()
        f = ReceiptFixture()
        f.binding_artifact.pop("digest")
        with self.assertRaisesRegex(contract.Invalid, "binding archive digest missing or differs"):
            f.verify()

    def test_binding_and_candidate_artifacts_can_be_on_later_pages(self):
        f = ReceiptFixture()
        for run_id, artifact in ((52, f.binding_artifact), (41, f.candidate)):
            route = f"repos/{f.repo}/actions/runs/{run_id}/artifacts?per_page=100&page="
            f.responses[route + "1"] = {"artifacts": [
                {"id": 1000 + i, "name": f"unrelated-{i}", "expired": False} for i in range(100)]}
            f.responses[route + "2"] = {"artifacts": [artifact]}
        self.assertEqual(f.verify()["state"], "verified")

    def test_duplicate_binding_or_candidate_on_another_page_is_rejected(self):
        for kind in ("binding", "candidate"):
            f = ReceiptFixture()
            run_id, artifact = (52, f.binding_artifact) if kind == "binding" else (41, f.candidate)
            route = f"repos/{f.repo}/actions/runs/{run_id}/artifacts?per_page=100&page="
            f.responses[route + "1"] = {"artifacts": [artifact] + [
                {"id": 1000 + i, "name": f"unrelated-{i}", "expired": False} for i in range(99)]}
            f.responses[route + "2"] = {"artifacts": [{**artifact, "id": artifact["id"] + 1}]}
            message = "missing/ambiguous coordinator binding" if kind == "binding" else "verified candidate artifact missing or replaced"
            with self.subTest(kind=kind), self.assertRaisesRegex(contract.Invalid, message):
                f.verify()

    def test_binding_zip_inventory_and_compressed_and_expanded_limits(self):
        for names in (("other.json",), ("nested/binding.json",), ("binding.json", "extra.json")):
            f = ReceiptFixture()
            f.seal_binding(names)
            with self.subTest(names=names), self.assertRaisesRegex(contract.Invalid, "unexpected binding archive inventory"):
                f.verify()
        f = ReceiptFixture()
        f.archive = b"0" * (2 * 1024 * 1024 + 1)
        f.binding_artifact["digest"] = "sha256:" + contract.sha256(f.archive)
        with self.assertRaisesRegex(contract.Invalid, "binding archive too large"):
            f.verify()
        f.seal_binding(raw=b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(contract.Invalid, "binding too large"):
            f.verify()

    def test_binding_pr_head_and_base_cannot_be_replaced_even_with_valid_zip_digest(self):
        for key, value in (("pr", 10), ("head", "f" * 40), ("base", "e" * 40)):
            f = ReceiptFixture()
            f.binding[key] = value
            f.seal_binding()
            with self.subTest(key=key), self.assertRaisesRegex(contract.Invalid, "coordinator verified another PR head/base"):
                f.verify()

    def test_binding_source_must_be_the_exact_ci_run_attempt(self):
        for key in ("run_id", "attempt"):
            f = ReceiptFixture()
            f.binding["source"][key] += 1
            f.seal_binding()
            with self.subTest(key=key), self.assertRaisesRegex(contract.Invalid, "coordinator verified another CI attempt"):
                f.verify()

    def test_replaced_binding_from_another_coordinator_run_attempt_or_sha_is_rejected(self):
        for key, value in (("run_id", 53), ("attempt", 4), ("sha", "f" * 40)):
            f = ReceiptFixture()
            f.binding["coordinator"][key] = value
            f.seal_binding()
            with self.subTest(key=key), self.assertRaisesRegex(contract.Invalid, "coordinator binding source differs"):
                f.verify()

    def test_binding_schema_fields_and_json_are_strict(self):
        for mutation in ("schema", "extra_field", "missing_field", "duplicate_key", "nonfinite"):
            f = ReceiptFixture()
            if mutation == "schema":
                f.binding["schema"] = "pineforge/mcp-coordinator-binding/v2"
            elif mutation == "extra_field":
                f.binding["extra"] = True
            elif mutation == "missing_field":
                f.binding.pop("source")
            raw = contract.canonical(f.binding)
            if mutation == "duplicate_key":
                raw = b'{"pr":9,' + raw[1:]
            elif mutation == "nonfinite":
                raw = raw.replace(b'"pr":9', b'"pr":NaN')
            f.seal_binding(raw=raw)
            with self.subTest(mutation=mutation), self.assertRaises(contract.Invalid):
                f.verify()

    def test_binding_candidate_identity_and_hashes_are_required(self):
        for key, value in (("id", True), ("id", 502), ("name", "candidate-41-1"), ("digest", None),
                           ("manifest_sha256", ""), ("worker_sha256", "g" * 64), ("container_sha256", "a" * 63)):
            f = ReceiptFixture()
            f.binding["artifact"][key] = value
            f.seal_binding()
            with self.subTest(key=key, value=value), self.assertRaises(contract.Invalid):
                f.verify()

    def test_binding_pr_and_run_ids_and_attempts_must_be_json_integers(self):
        for path in (("pr",), ("source", "run_id"), ("source", "attempt"),
                     ("coordinator", "run_id"), ("coordinator", "attempt"), ("artifact", "id")):
            f = ReceiptFixture()
            row = f.binding if len(path) == 1 else f.binding[path[0]]
            key = path[-1]
            row[key] = float(row[key])
            f.seal_binding()
            with self.subTest(path=path), self.assertRaisesRegex(contract.Invalid, "expected positive integer"):
                f.verify()

    def test_original_candidate_cannot_disappear_duplicate_expire_or_be_replaced(self):
        for mutation in ("missing", "duplicate", "expired", "replaced_id", "replaced_digest", "missing_digest"):
            f = ReceiptFixture()
            if mutation == "missing":
                f.candidates.clear()
            elif mutation == "duplicate":
                f.candidates.append({**f.candidate, "id": 502})
            elif mutation == "expired":
                f.candidate["expired"] = True
            elif mutation == "replaced_id":
                f.candidate["id"] = 502
            elif mutation == "replaced_digest":
                f.candidate["digest"] = "sha256:" + "f" * 64
            else:
                f.candidate.pop("digest")
            with self.subTest(mutation=mutation), self.assertRaisesRegex(contract.Invalid, "verified candidate artifact missing or replaced"):
                f.verify()

    def test_extra_missing_reordered_or_skippable_coordinator_inventory_is_rejected(self):
        for owner in ("policy", "receipt", "both"):
            for mutation in ("extra", "missing", "reordered", "skip"):
                f = ReceiptFixture()
                for row in ([f.policy["e2e"]] if owner == "policy" else [f.receipt["e2e"]] if owner == "receipt"
                            else [f.policy["e2e"], f.receipt["e2e"]]):
                    if mutation == "extra":
                        row["expected"].append("merge")
                    elif mutation == "missing":
                        row["expected"].pop()
                    elif mutation == "reordered":
                        row["expected"].reverse()
                    else:
                        row["approved_skips"] = ["candidate"]
                with self.subTest(owner=owner, mutation=mutation), self.assertRaises(contract.Invalid):
                    f.verify()

    def test_missing_duplicate_incomplete_failed_cancelled_or_skipped_job_is_rejected(self):
        for name in ("candidate", "release/ready"):
            for mutation in ("missing", "duplicate", "in_progress", "failure", "cancelled", "skipped"):
                f = ReceiptFixture()
                job = next(row for row in f.jobs["e2e"] if row["name"] == name)
                if mutation == "missing":
                    f.jobs["e2e"].remove(job)
                elif mutation == "duplicate":
                    f.jobs["e2e"].append(copy.deepcopy(job))
                elif mutation == "in_progress":
                    job["status"] = mutation
                else:
                    job["conclusion"] = mutation
                with self.subTest(name=name, mutation=mutation), self.assertRaises(contract.Invalid):
                    f.verify()

    def test_reported_results_cannot_add_or_omit_jobs(self):
        for mutation in ("extra", "missing"):
            f = ReceiptFixture()
            if mutation == "extra":
                f.receipt["e2e"]["results"]["merge"] = "success"
            else:
                f.receipt["e2e"]["results"].pop("candidate")
            with self.subTest(mutation=mutation), self.assertRaisesRegex(contract.Invalid, "reported checks differ from GitHub"):
                f.verify()

    def test_orchestration_jobs_do_not_extend_the_required_e2e_inventory(self):
        f = ReceiptFixture()
        for name in ("pending", "policy", "merge"):
            f.jobs["e2e"].append({"name": name, "status": "completed", "conclusion": "skipped"})
        self.assertEqual(f.verify()["state"], "verified")

    def test_adapter_rejects_every_non_hosted_consumer_before_calling_the_helper(self):
        for consumer in (role for role in contract.RECEIPT_ROLES if role != "hosted"):
            f = ReceiptFixture(consumer, HOSTED_POLICY)
            with self.subTest(consumer=consumer), patch.object(receipts, "check_coordinator") as adapter:
                with self.assertRaisesRegex(contract.Invalid, "unexpected coordinator consumer"):
                    f.verify()
                adapter.assert_not_called()

    def test_adapter_requires_the_source_ci_workflow_and_coordinator_workflow(self):
        for stage in ("ci", "e2e"):
            f = ReceiptFixture()
            f.policy[stage]["workflow"] = "other.yml"
            f.runs[stage]["path"] = ".github/workflows/other.yml"
            with self.subTest(stage=stage), patch.object(receipts, "check_coordinator") as adapter:
                with self.assertRaises(contract.Invalid):
                    f.verify()
                adapter.assert_not_called()

    def test_hosted_ci_and_deploy_keep_independent_exact_sha_checks(self):
        for stage in ("ci", "deploy"):
            for target in ("receipt", "github", "both"):
                f = ReceiptFixture()
                if target in ("receipt", "both"):
                    f.receipt[stage]["sha"] = f.pr["base_sha"]
                if target in ("github", "both"):
                    f.runs[stage]["head_sha"] = f.pr["base_sha"]
                with self.subTest(stage=stage, target=target), self.assertRaises(contract.Invalid):
                    f.verify()

    def test_legacy_receipts_keep_the_original_sha_rules_without_the_adapter(self):
        for consumer in contract.RECEIPT_ROLES:
            policy = example.example_policy("offline")
            policy["artifact"] = "release-receipt-" + consumer
            f = ReceiptFixture(consumer, policy)
            with self.subTest(consumer=consumer), patch.object(receipts, "check_coordinator") as adapter:
                public = f.verify()
                self.assertEqual(public["state"], "verified")
                self.assertEqual(f.downloads, [])
                for stage in ("ci", "e2e", "deploy"):
                    original = f.receipt[stage]["sha"]
                    f.receipt[stage]["sha"] = f.runs[stage]["head_sha"] = f.pr["base_sha"]
                    with self.subTest(stage=stage), self.assertRaises(contract.Invalid):
                        f.verify()
                    f.receipt[stage]["sha"] = f.runs[stage]["head_sha"] = original
                adapter.assert_not_called()

    def test_compute_receipt_is_refused_before_coordinator_or_legacy_checks(self):
        for policy in (HOSTED_POLICY, example.example_policy("offline")):
            f = ReceiptFixture("offline", policy)
            f.receipt["consumer"] = "compute"
            with self.subTest(workflow=policy["e2e"]["workflow"]), \
                    patch.object(receipts, "check_coordinator") as adapter, patch.object(receipts, "check_run") as check:
                with self.assertRaisesRegex(contract.Invalid, "^invalid consumer receipt schema$"):
                    f.verify()
                adapter.assert_not_called()
                check.assert_not_called()
                self.assertEqual(f.queries, [])
                self.assertEqual(f.downloads, [])

    def test_hosted_keeps_premerge_completion_first_parent_and_base_freshness_checks(self):
        for mutation in ("late_ci", "late_e2e", "first_parent", "unmerged_base"):
            f = ReceiptFixture()
            if mutation.startswith("late_"):
                f.jobs[mutation.removeprefix("late_")][-1]["completed_at"] = "2020-01-01T00:06:00Z"
            elif mutation == "first_parent":
                f.commit["parents"][0]["sha"] = "f" * 40
            else:
                f.actual_pr["merged"] = False
                f.actual_pr["base"]["sha"] = "f" * 40
            with self.subTest(mutation=mutation), self.assertRaises(contract.Invalid):
                f.verify()


if __name__ == "__main__":
    unittest.main()
