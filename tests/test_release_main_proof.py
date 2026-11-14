#!/usr/bin/env python3
"""Exact main-push proof refusals and the publication boundaries that consume it.

Fixtures model GitHub responses independently of the production inventory. No
test transport or proof override is exposed through the production CLI.
"""
from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_main_proof as proof  # noqa: E402
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
from test_release_binding import (FinalizeClient, MemoryRegistry, MemoryStore,  # noqa: E402
                                  fixture_manifest)

HUB = "pineforge-4pass/pineforge-release"
COMMIT = "a1" * 20
OTHER = "b2" * 20
JOBS = {
    "python-test.yml": (
        "authz", "changes", "fingerprint-canonical", "release-rules",
        "description-renderer", "release-ready-replay", "release/ready",
    ),
    "facts-validate.yml": ("authz", "validate"),
}


class GitHubProof:
    """Read-only endpoint fixture; changes to response data decide every result."""

    def __init__(self, commit=COMMIT, *, run_offset=0):
        self.commit = commit
        self.repo = {"id": 704, "full_name": HUB, "private": False, "default_branch": "main"}
        self.workflows, self.runs, self.details, self.jobs = {}, {}, {}, {}
        self.calls = []
        self.compare = {"status": "ahead", "base_commit": {"sha": commit},
                        "merge_base_commit": {"sha": commit}}
        for index, (filename, names) in enumerate(JOBS.items(), 1):
            wid, rid = 100 + index, 900 + index + run_offset
            path = ".github/workflows/" + filename
            self.workflows[filename] = {"id": wid, "path": path, "state": "active",
                                        "name": filename.removesuffix(".yml")}
            run = {"id": rid, "workflow_id": wid, "path": path,
                   "event": "push", "head_branch": "main", "head_sha": commit,
                   "repository": copy.deepcopy(self.repo), "head_repository": copy.deepcopy(self.repo),
                   "pull_requests": [], "status": "completed", "conclusion": "success",
                   "run_attempt": 2, "run_number": 17 + index + run_offset}
            self.runs[filename] = [run]
            self.details[rid] = copy.deepcopy(run)
            self.jobs[rid] = [
                {"id": rid * 10 + offset, "run_id": rid, "run_attempt": 2,
                 "head_sha": commit, "name": name, "status": "completed", "conclusion": "success"}
                for offset, name in enumerate(names)
            ]
        self.error = None

    def api(self, route, *, method="GET", data=None):
        if method != "GET" or data is not None:
            raise AssertionError("proof attempted a write")
        self.calls.append(route)
        if self.error:
            raise self.error
        parsed = urlsplit(route)
        path, query = parsed.path, parse_qs(parsed.query)
        prefix = "repos/" + HUB
        if path == prefix:
            return copy.deepcopy(self.repo)
        if path.startswith(prefix + "/compare/"):
            return copy.deepcopy(self.compare)
        if path.startswith(prefix + "/actions/workflows/"):
            suffix = path.removeprefix(prefix + "/actions/workflows/")
            if suffix.endswith("/runs"):
                filename = suffix.removesuffix("/runs")
                if filename.isdigit():
                    filename = next(name for name, row in self.workflows.items() if row["id"] == int(filename))
                if query.get("head_sha") != [self.commit] or query.get("branch") != ["main"] or query.get("event") != ["push"]:
                    raise AssertionError("proof must request this exact main-push SHA")
                return self.page(self.runs[filename], "workflow_runs", query)
            return copy.deepcopy(self.workflows[suffix])
        if path.startswith(prefix + "/actions/runs/"):
            suffix = path.removeprefix(prefix + "/actions/runs/")
            rid = int(suffix.split("/")[0])
            if "/jobs" in suffix:
                if suffix != f"{rid}/attempts/{self.details[rid]['run_attempt']}/jobs":
                    raise AssertionError("proof must read the selected attempt's jobs")
                return self.page(self.jobs[rid], "jobs", query)
            return copy.deepcopy(self.details[rid])
        raise AssertionError("unmodeled proof endpoint: " + route)

    @staticmethod
    def page(rows, key, query):
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("per_page", ["100"])[0])
        return {"total_count": len(rows), key: copy.deepcopy(rows[(page - 1) * size:page * size])}

    def tag_commit(self, repo, tag):
        if repo != HUB or tag != "v1.6.0":
            raise AssertionError("unexpected tag identity")
        return self.commit


class ReleaseProof:
    """Serve independent genuine-shaped proof responses for both hub SHAs."""

    def __init__(self, release_commit, facts_commit):
        self.release_commit = release_commit
        commits = dict.fromkeys((release_commit, facts_commit))
        self.sources = {commit: GitHubProof(commit, run_offset=index * 1000)
                        for index, commit in enumerate(commits)}
        self.active = self.sources[release_commit]

    def api(self, route, **kwargs):
        if "/compare/" in route:
            commit = route.split("/compare/", 1)[1].split("...", 1)[0]
            self.active = self.sources[commit]
        return self.active.api(route, **kwargs)

    def tag_commit(self, repo, tag):
        return self.sources[self.release_commit].tag_commit(repo, tag)


class MainProofTests(unittest.TestCase):
    def test_full_green_acceptance_uses_both_real_workflows_and_selected_attempts(self):
        api = GitHubProof()
        result = proof.MainProof(api).require_commit(COMMIT)
        self.assertEqual(result["commit"], COMMIT)
        self.assertEqual(set(result["runs"]), {"python-test.yml", "facts-validate.yml"})
        self.assertEqual({row["run_id"] for row in result["runs"].values()}, {901, 902})
        self.assertTrue(any("/attempts/2/jobs" in route for route in api.calls))

    def test_each_missing_workflow_and_each_missing_job_refuses(self):
        for filename, names in JOBS.items():
            with self.subTest(workflow=filename):
                api = GitHubProof()
                api.runs[filename] = []
                with self.assertRaises(proof.ProofError):
                    proof.MainProof(api).require_commit(COMMIT)
            for name in names:
                with self.subTest(workflow=filename, job=name):
                    api = GitHubProof()
                    rid = api.runs[filename][0]["id"]
                    api.jobs[rid] = [job for job in api.jobs[rid] if job["name"] != name]
                    with self.assertRaises(proof.ProofError):
                        proof.MainProof(api).require_commit(COMMIT)

    def test_red_running_skipped_and_other_non_success_proof_refuses(self):
        states = (("completed", "failure"), ("in_progress", None), ("queued", None),
                  ("completed", "skipped"), ("completed", "cancelled"),
                  ("completed", "neutral"), ("completed", "timed_out"),
                  ("completed", "action_required"), ("completed", None))
        for filename in JOBS:
            for status, conclusion in states:
                for target in ("run", "job"):
                    with self.subTest(workflow=filename, status=status, conclusion=conclusion, target=target):
                        api = GitHubProof()
                        run = api.runs[filename][0]
                        rows = (run, api.details[run["id"]]) if target == "run" else (api.jobs[run["id"]][0],)
                        for row in rows:
                            row.update(status=status, conclusion=conclusion)
                        with self.assertRaises(proof.ProofError):
                            proof.MainProof(api).require_commit(COMMIT)

    def test_wrong_sha_pr_merge_queue_branch_repository_or_workflow_identity_refuses(self):
        mutations = (
            {"head_sha": OTHER}, {"event": "pull_request"}, {"event": "merge_group"},
            {"event": "workflow_dispatch"}, {"head_branch": "feature"},
            {"pull_requests": [{"number": 42}]}, {"workflow_id": 999},
            {"path": ".github/workflows/other.yml"},
            {"repository": {"id": 705, "full_name": "example/other", "private": False}},
            {"head_repository": {"id": 705, "full_name": HUB, "private": False}},
        )
        for filename in JOBS:
            for change in mutations:
                for target in ("list", "detail"):
                    with self.subTest(workflow=filename, change=change, target=target):
                        api = GitHubProof()
                        run = api.runs[filename][0]
                        (run if target == "list" else api.details[run["id"]]).update(change)
                        with self.assertRaises(proof.ProofError):
                            proof.MainProof(api).require_commit(COMMIT)

    def test_old_green_cannot_hide_a_new_failed_or_running_attempt_or_run(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            api = GitHubProof()
            old = api.runs["python-test.yml"][0]
            latest = {**copy.deepcopy(old), "id": 1901, "run_number": old["run_number"] + 1,
                      "status": status, "conclusion": conclusion}
            api.runs["python-test.yml"].append(latest)
            api.details[1901] = copy.deepcopy(latest)
            with self.subTest(status=status), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)
        api = GitHubProof()
        api.details[901].update(run_attempt=3, status="in_progress", conclusion=None)
        with self.assertRaises(proof.ProofError):
            proof.MainProof(api).require_commit(COMMIT)

    def test_run_pagination_cannot_hide_a_new_red_run(self):
        api = GitHubProof()
        base = api.runs["python-test.yml"][0]
        api.runs["python-test.yml"] = []
        for index in range(101):
            run = {**copy.deepcopy(base), "id": 3000 + index, "run_number": 100 + index}
            if index == 100:
                run.update(conclusion="failure")
            api.runs["python-test.yml"].append(run)
            api.details[run["id"]] = copy.deepcopy(run)
        with self.assertRaises(proof.ProofError):
            proof.MainProof(api).require_commit(COMMIT)
        self.assertTrue(any("page=2" in route for route in api.calls))

    def test_duplicate_foreign_stale_or_extra_jobs_refuse(self):
        for change in ({"run_id": 900}, {"run_attempt": 1}, {"head_sha": OTHER}, {"id": True}):
            api = GitHubProof()
            api.jobs[901][0].update(change)
            with self.subTest(change=change), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)
        for extra in ("authz", "unexpected"):
            api = GitHubProof()
            job = {**api.jobs[901][0], "id": 99999, "name": extra}
            api.jobs[901].append(job)
            with self.subTest(extra=extra), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)

    def test_attempt_bound_jobs_do_not_require_the_optional_per_job_attempt_field(self):
        api = GitHubProof()
        for jobs in api.jobs.values():
            for job in jobs:
                job.pop("run_attempt")
        result = proof.MainProof(api).require_commit(COMMIT)
        self.assertEqual(result["runs"]["python-test.yml"]["run_attempt"], 2)
        self.assertTrue(any("/attempts/2/jobs" in route for route in api.calls))

    def test_wrong_workflow_metadata_main_ancestry_and_transport_errors_refuse(self):
        for field, value in (("id", True), ("path", ".github/workflows/copied.yml"),
                             ("state", "disabled_manually"), ("name", "copied")):
            api = GitHubProof()
            api.workflows["python-test.yml"][field] = value
            with self.subTest(field=field), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)
        for comparison in ({"status": "diverged"}, {"merge_base_commit": {"sha": OTHER}}):
            api = GitHubProof()
            api.compare.update(comparison)
            with self.subTest(compare=comparison), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)
        for error in (contract.Pending("missing"), contract.Invalid("HTTP 403"), OSError("transport")):
            api = GitHubProof()
            api.error = error
            with self.subTest(error=type(error).__name__), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)

    def test_public_hub_identity_cannot_be_replaced(self):
        for change in ({"id": True}, {"full_name": "example/other"},
                       {"private": True}, {"default_branch": "master"}):
            api = GitHubProof()
            api.repo.update(change)
            with self.subTest(change=change), self.assertRaises(proof.ProofError):
                proof.MainProof(api).require_commit(COMMIT)

    def test_tag_must_bind_the_requested_exact_commit(self):
        api = GitHubProof()
        with self.assertRaises(proof.ProofError):
            proof.MainProof(api).require_tag("v1.6.0", OTHER)
        self.assertEqual(proof.MainProof(api).require_tag("v1.6.0", COMMIT)["commit"], COMMIT)

    def test_cli_has_no_success_override_and_missing_proof_is_nonzero(self):
        api = GitHubProof()
        api.runs["facts-validate.yml"] = []
        with patch.object(proof, "Client", return_value=api), patch("sys.stderr", new_callable=io.StringIO):
            self.assertNotEqual(proof.main(["commit", "--sha", COMMIT]), 0)
        with patch.object(proof, "Client", return_value=GitHubProof()):
            self.assertEqual(proof.main(["commit", "--sha", COMMIT]), 0)
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            proof.main(["commit", "--sha", COMMIT, "--allow-missing"])

    def test_wait_can_only_finish_after_the_missing_run_becomes_full_green(self):
        api = GitHubProof()
        available = api.runs["facts-validate.yml"]
        api.runs["facts-validate.yml"] = []

        def finish(_seconds):
            api.runs["facts-validate.yml"] = available

        with patch.object(proof.time, "sleep", side_effect=finish) as sleep:
            result = proof.wait_for_commit(proof.MainProof(api), COMMIT, 60)
        self.assertEqual(result["runs"]["facts-validate.yml"]["run_id"], 902)
        sleep.assert_called_once()
        api = GitHubProof()
        api.runs["facts-validate.yml"][0].update(conclusion="failure")
        with patch.object(proof.time, "sleep") as sleep, self.assertRaises(proof.ProofError):
            proof.wait_for_commit(proof.MainProof(api), COMMIT, 60)
        sleep.assert_not_called()
        api = GitHubProof()
        api.runs["facts-validate.yml"] = []
        with patch.object(proof.time, "monotonic", side_effect=[0, 2]), self.assertRaises(proof.ProofPending):
            proof.wait_for_commit(proof.MainProof(api), COMMIT, 1)


class PublicationProofTests(unittest.TestCase):
    def test_finalizer_refuses_release_or_facts_proof_before_any_publication(self):
        for target in ("release", "facts"):
            manifest = fixture_manifest()
            store, registry = MemoryStore(manifest), MemoryRegistry()
            client = FinalizeClient(manifest)
            api = ReleaseProof(manifest["hub_commit"], "e" * 40)
            api.sources[manifest["hub_commit"] if target == "release" else "e" * 40].runs["facts-validate.yml"] = []
            checker = proof.MainProof(api)
            guarded = proof.GuardedStore(store, checker)
            guarded_client = proof.GuardedClient(client, checker)
            with self.subTest(target=target), patch.object(pipeline, "npm", return_value=manifest["versions"]["codegen"]), self.assertRaises(proof.ProofError):
                pipeline.finalize(guarded_client, guarded, registry, store.rows["v1.6.0"])
            self.assertEqual(registry.operations, [])
            self.assertEqual(store.operations, [])

    def test_valid_proof_reaches_the_existing_real_finalizer(self):
        manifest = fixture_manifest()
        store, registry = MemoryStore(manifest), MemoryRegistry()
        client = FinalizeClient(manifest)
        api = ReleaseProof(manifest["hub_commit"], "e" * 40)
        checker = proof.MainProof(api)
        with patch.object(pipeline, "npm", return_value=manifest["versions"]["codegen"]):
            event = pipeline.finalize(proof.GuardedClient(client, checker), proof.GuardedStore(store, checker),
                                      registry, store.rows["v1.6.0"])
        self.assertEqual(event["hub_commit"], manifest["hub_commit"])
        self.assertTrue(registry.operations)
        self.assertFalse(store.rows["v1.6.0"]["draft"])

    def test_existing_manifest_replay_requires_its_bound_facts_proof(self):
        manifest = fixture_manifest()
        store, registry = MemoryStore(manifest), MemoryRegistry()
        row = store.rows["v1.6.0"]
        store.put(row, contract.ASSET, contract.canonical(manifest))
        store.operations.clear()
        api = ReleaseProof(manifest["hub_commit"], manifest["facts"]["commit"])
        api.sources[manifest["facts"]["commit"]].runs["facts-validate.yml"] = []
        checker = proof.MainProof(api)
        with self.assertRaises(proof.ProofError):
            pipeline.finalize(proof.GuardedClient(FinalizeClient(manifest), checker),
                              proof.GuardedStore(store, checker), registry, row)
        self.assertEqual(store.operations, [])
        self.assertEqual(registry.operations, [])

    def test_empty_tag_sweep_uses_the_guard_and_exposes_no_delivery_on_refusal(self):
        manifest = fixture_manifest()
        api = GitHubProof(manifest["hub_commit"])
        api.runs["facts-validate.yml"] = []
        store, registry = MemoryStore(manifest), MemoryRegistry()
        env = {"RELEASE_READY_CONSUMERS": "", "RELEASE_READY_EXCLUDE": "offline,hosted,application,web,data,license,compute",
               "FACTS_WEB_DISPATCH_ENABLED": "false", "FACTS_LICENSE_DISPATCH_ENABLED": "false",
               "GITHUB_RUN_ID": "700", "GITHUB_RUN_ATTEMPT": "1"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env), \
                patch.object(pipeline, "Store", return_value=store), \
                patch.object(pipeline, "Registry", return_value=registry), \
                patch.object(pipeline, "report_issue"):
            output = Path(directory) / "deliveries.json"
            with self.assertRaises(contract.Invalid):
                proof.finalize(proof.MainProof(api), "", str(output))
            self.assertEqual(contract.decode(output.read_bytes()), [])
        self.assertEqual(store.operations, [])
        self.assertEqual(registry.operations, [])

    def test_delivery_requires_both_bound_commits_and_accepts_full_green(self):
        for missing in ("release", "facts", None):
            manifest = fixture_manifest()
            store = MemoryStore(manifest)
            row = store.rows["v1.6.0"]
            row["draft"] = False
            store.put(row, contract.ASSET, contract.canonical(manifest))
            store.operations.clear()
            api = ReleaseProof(manifest["hub_commit"], manifest["facts"]["commit"])
            if missing:
                commit = manifest["hub_commit"] if missing == "release" else manifest["facts"]["commit"]
                api.sources[commit].runs["facts-validate.yml"] = []
            with self.subTest(missing=missing), patch.object(pipeline, "Store", return_value=store), \
                    patch.object(store, "get", side_effect=lambda tag: store.rows[tag]):
                if missing:
                    with self.assertRaises(proof.ProofError):
                        proof.require_delivery(proof.MainProof(api), "v1.6.0")
                else:
                    proof.require_delivery(proof.MainProof(api), "v1.6.0")
                    self.assertTrue(all(source.calls for source in api.sources.values()))
            self.assertEqual(store.operations, [])

    def test_facts_signal_checks_its_snapshot_not_the_event_sha(self):
        api = GitHubProof()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b'{"example":1}\n'
            (root / "facts.json").write_bytes(raw)
            (root / "payload.json").write_bytes(contract.canonical({
                "commit": OTHER, "sha256": contract.sha256(raw), "dry_run": False,
            }))
            with self.assertRaises(proof.ProofError):
                proof.require_facts_source(proof.MainProof(api), root)
            (root / "payload.json").write_bytes(contract.canonical({
                "commit": COMMIT, "sha256": contract.sha256(raw), "dry_run": False,
            }))
            self.assertEqual(proof.require_facts_source(proof.MainProof(api), root)["commit"], COMMIT)


class ProofWorkflowTests(unittest.TestCase):
    def workflow(self, filename):
        return yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)

    def test_inventory_is_the_actual_full_main_push_ci_and_facts_inventory(self):
        for filename, names in JOBS.items():
            document = self.workflow(filename)
            actual = {job.get("name", key) for key, job in document["jobs"].items()}
            self.assertEqual(actual, set(names))
            self.assertEqual(set(proof.WORKFLOWS[filename]), actual)

    def test_both_tag_routes_prove_the_final_commit_after_main_push(self):
        steps = self.workflow("handle-upstream.yml")["jobs"]["bump"]["steps"]
        run = next(step["run"] for step in steps if step.get("name") == "Bump VERSION, commit, tag")
        recovery, fresh = run.split('if [ "$LINE" = pair ]; then', 1)
        for body in (recovery, fresh):
            self.assertLess(body.index("scripts/release_main_proof.py commit"), body.index("git tag -a"))
            self.assertIn('--sha "$commit"', body)
        self.assertLess(fresh.index("git push origin HEAD:main"), fresh.index("scripts/release_main_proof.py commit"))

    def test_candidate_reuse_and_new_images_require_proof(self):
        jobs = self.workflow("publish.yml")["jobs"]
        for name in ("prepare", "publish"):
            steps = jobs[name]["steps"]
            gates = [step for step in steps if "scripts/release_main_proof.py tag" in step.get("run", "")]
            self.assertEqual(len(gates), 1)
            self.assertNotIn("if", gates[0])
            self.assertNotIn("continue-on-error", gates[0])
            self.assertEqual(gates[0]["env"]["RELEASE_SHA"], "${{ needs.tag-guard.outputs.sha }}")
            self.assertEqual(jobs[name]["permissions"]["actions"], "read")
            if name == "prepare":
                self.assertLess(steps.index(gates[0]), next(i for i, step in enumerate(steps) if step.get("id") == "candidate"))
            else:
                pushes = [i for i, step in enumerate(steps) if step.get("with", {}).get("push") == "true"]
                self.assertTrue(pushes)
                self.assertTrue(all(steps.index(gates[0]) < i for i in pushes))
        self.assertIn("prepare", jobs["publish"]["needs"])

    def test_every_finalizer_trigger_uses_guarded_candidate_and_facts_reads(self):
        doc = self.workflow("release-finalize.yml")
        self.assertEqual(set(doc["on"]), {"workflow_call", "workflow_dispatch", "push", "schedule"})
        step = next(step for step in doc["jobs"]["finalize"]["steps"] if step.get("id") == "ready")
        self.assertIn("scripts/release_main_proof.py finalize", step["run"])
        self.assertNotIn("continue-on-error", step)
        self.assertEqual(doc["jobs"]["finalize"]["permissions"]["actions"], "read")

    def test_release_senders_and_both_facts_roles_require_proof_before_mint(self):
        targets = (("release-finalize.yml", "notify-consumers", "delivery"),
                   ("release-redeliver.yml", "redeliver", "delivery"),
                   ("facts-fanout.yml", "web", "facts-source"),
                   ("facts-fanout.yml", "license", "facts-source"))
        for filename, name, command in targets:
            with self.subTest(workflow=filename, job=name):
                steps = self.workflow(filename)["jobs"][name]["steps"]
                gate = next(step for step in steps if step.get("id") == "main-proof")
                mint = next(step for step in steps if step.get("id") == "app")
                self.assertIn("scripts/release_main_proof.py " + command, gate["run"])
                self.assertLess(steps.index(gate), steps.index(mint))
                self.assertNotIn("continue-on-error", gate)
                self.assertNotIn("if", gate)
                self.assertEqual(gate["env"]["GH_TOKEN"], "${{ github.token }}")
                if filename == "facts-fanout.yml":
                    send = next(step for step in steps if "scripts/facts_fanout.py dispatch" in step.get("run", ""))
                    self.assertIn("steps.main-proof.outcome == 'success'", send["if"])
                    self.assertIn("steps.main-proof.outcome == 'success'", mint["if"])

    def test_reconciliation_requires_exact_checked_out_main_proof(self):
        job = self.workflow("release-reconcile.yml")["jobs"]["producers"]
        steps = job["steps"]
        gate = next(step for step in steps if step.get("id") == "main-proof")
        self.assertIn('commit --sha "$(git rev-parse HEAD)"', gate["run"])
        self.assertLess(steps.index(gate), next(i for i, step in enumerate(steps) if step.get("id") == "app"))


if __name__ == "__main__":
    unittest.main()
