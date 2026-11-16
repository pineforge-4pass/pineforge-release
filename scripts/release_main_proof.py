#!/usr/bin/env python3
"""Require this hub's complete successful main-push CI on an exact commit.

The proof reader only GETs public hub identities. Publication still uses the
existing release pipeline; candidate reads and manifest storage are guarded
here so scheduled reconciliation cannot publish an unproved bound commit.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

from release_artifacts import Client
from release_contract import (ASSET, CANDIDATE, COMMIT, HUB, Invalid, Pending,
                              decode, keys, match, payload, sha256, validate_candidate)


# These are the existing jobs, not new checks or caller-provided success flags.
# The inventory test compares them with the actual main-push workflows.
WORKFLOWS = {
    "python-test.yml": frozenset({
        "authz", "changes", "fingerprint-canonical", "release-rules",
        "description-renderer", "release-ready-replay", "release/ready",
    }),
    "facts-validate.yml": frozenset({"authz", "validate"}),
}
PREFIX = f"repos/{HUB}"


class ProofError(Invalid):
    """No publication is authorized by this proof."""


class ProofPending(ProofError, Pending):
    """Wait or retry later for a missing or unfinished main-push run."""


def insist(condition, message):
    if not condition:
        raise ProofError(message)


def commit_sha(value):
    try:
        return match(COMMIT, value)
    except Invalid:
        raise ProofError("main proof requires an exact commit SHA") from None


def identifier(value):
    insist(type(value) is int and 0 < value <= 2**53 - 1, "main proof has an invalid numeric identity")
    return value


class MainProof:
    def __init__(self, client):
        self.client = client
        # Successful snapshots live only for this operation, never on disk or
        # in a workflow input. Failed and pending results are never cached.
        self._proven = {}
        self._repository = None

    def read(self, route):
        insist(route == PREFIX or route.startswith(PREFIX + "/"), "main proof origin differs")
        try:
            return self.client.api(route)
        except (Pending, Invalid, ValueError, OSError, KeyError, TypeError):
            raise ProofError("main proof API unavailable or invalid") from None

    def pages(self, route, field):
        result, total = [], None
        for page in range(1, 11):
            obj = self.read(route + ("&" if "?" in route else "?") + f"per_page=100&page={page}")
            insist(type(obj) is dict and type(obj.get(field)) is list
                   and type(obj.get("total_count")) is int
                   and 0 <= obj["total_count"] <= 1000, "main proof pagination is incomplete")
            if total is None:
                total = obj["total_count"]
            insist(total == obj["total_count"] and len(obj[field]) <= 100,
                   "main proof pagination changed")
            result.extend(obj[field])
            insist(len(result) <= total, "main proof pagination count differs")
            if len(result) == total:
                return result
            insist(len(obj[field]) == 100, "main proof pagination is truncated")
        raise ProofError("main proof pagination limit exceeded")

    def repository(self):
        if self._repository is None:
            obj = self.read(PREFIX)
            insist(type(obj) is dict and obj.get("full_name") == HUB
                   and obj.get("private") is False and obj.get("default_branch") == "main",
                   "main proof hub identity differs")
            identifier(obj.get("id"))
            self._repository = obj
        return self._repository

    def run_identity(self, row, workflow, commit):
        insist(type(row) is dict, "main proof run is malformed")
        for name in ("id", "run_attempt", "run_number"):
            identifier(row.get(name))
        insist(row.get("workflow_id") == workflow["id"] and type(row.get("workflow_id")) is int
               and row.get("path") == workflow["path"], "main proof workflow identity differs")
        insist(row.get("event") == "push" and row.get("head_branch") == "main"
               and row.get("head_sha") == commit and row.get("pull_requests") == [],
               "main proof is not this exact main-push commit")
        for field in ("repository", "head_repository"):
            repo = row.get(field)
            insist(type(repo) is dict and repo.get("full_name") == HUB
                   and type(repo.get("id")) is int and repo["id"] == self.repository()["id"]
                   and repo.get("private") is False, "main proof run repository differs")

    @staticmethod
    def completed(row):
        if row.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}:
            raise ProofPending("main proof is still running")
        insist(row.get("status") == "completed" and row.get("conclusion") == "success",
               "main proof did not complete successfully")

    def workflow_run(self, filename, expected_jobs, commit):
        workflow = self.read(f"{PREFIX}/actions/workflows/{filename}")
        insist(type(workflow) is dict, "main proof workflow is malformed")
        wid = identifier(workflow.get("id"))
        insist(workflow.get("path") == ".github/workflows/" + filename
               and workflow.get("name") == filename.removesuffix(".yml")
               and workflow.get("state") == "active", "main proof workflow definition differs")
        runs = self.pages(f"{PREFIX}/actions/workflows/{wid}/runs"
                          f"?branch=main&event=push&head_sha={commit}&exclude_pull_requests=true", "workflow_runs")
        if not runs:
            raise ProofPending("main proof main-push run is missing")
        for row in runs:
            self.run_identity(row, workflow, commit)
        insist(len({row["id"] for row in runs}) == len(runs), "main proof has duplicate runs")
        # Never fall back to an earlier green run when the newest one is red
        # or queued. A re-run's current attempt is read from the run endpoint.
        selected = max(runs, key=lambda row: (row["run_number"], row["id"]))
        self.completed(selected)
        run = self.read(f"{PREFIX}/actions/runs/{selected['id']}")
        self.run_identity(run, workflow, commit)
        insist(all(run[field] == selected[field] for field in ("id", "run_number", "run_attempt")),
               "main proof run attempt changed")
        self.completed(run)
        jobs = self.pages(f"{PREFIX}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
        names, ids = set(), set()
        for job in jobs:
            insist(type(job) is dict, "main proof job is malformed")
            jid = identifier(job.get("id"))
            name = job.get("name")
            insist(type(name) is str and name not in names and jid not in ids,
                   "main proof job identity is ambiguous")
            insist(type(job.get("run_id")) is int and job["run_id"] == run["id"]
                   and job.get("head_sha") == commit, "main proof job belongs to another commit or attempt")
            # The attempt-specific endpoint is the authoritative binding.
            # GitHub's optional per-job attempt field must agree if supplied.
            if "run_attempt" in job:
                insist(type(job["run_attempt"]) is int and job["run_attempt"] == run["run_attempt"],
                       "main proof job attempt differs")
            self.completed(job)
            names.add(name)
            ids.add(jid)
        insist(names == expected_jobs, "main proof full job inventory differs")
        return {"run_id": run["id"], "run_attempt": run["run_attempt"]}

    def require_commit(self, commit):
        commit_sha(commit)
        if commit in self._proven:
            return self._proven[commit]
        self.repository()
        comparison = self.read(f"{PREFIX}/compare/{commit}...main")
        insist(type(comparison) is dict and comparison.get("status") in {"ahead", "identical"}
               and type(comparison.get("base_commit")) is dict
               and comparison["base_commit"].get("sha") == commit
               and type(comparison.get("merge_base_commit")) is dict
               and comparison["merge_base_commit"].get("sha") == commit,
               "main proof commit is not reachable from main")
        runs = {filename: self.workflow_run(filename, names, commit) for filename, names in WORKFLOWS.items()}
        result = {"commit": commit, "runs": runs}
        self._proven[commit] = result
        return result

    def require_tag(self, tag, expected_sha=None):
        try:
            commit = self.client.tag_commit(HUB, tag)
        except (Pending, Invalid, ValueError, OSError, KeyError, TypeError):
            raise ProofError("main proof release tag is unavailable or invalid") from None
        commit_sha(commit)
        if expected_sha is not None:
            insist(commit == commit_sha(expected_sha), "main proof tag and release commit differ")
        return self.require_commit(commit)


class GuardedStore:
    """Check each candidate before the real pipeline can publish its manifest."""

    def __init__(self, store, checker):
        self.store, self.checker = store, checker
        self.candidates = set()

    def __getattr__(self, name):
        return getattr(self.store, name)

    def manifest(self, row, raw):
        event = payload(raw)
        insist(row["tag_name"] == "v" + event["release_version"], "main proof manifest tag differs")
        self.checker.require_tag(row["tag_name"], event["hub_commit"])
        self.checker.require_commit(event["facts"]["commit"])

    def read(self, row, name):
        raw = self.store.read(row, name)
        if raw is not None and name == CANDIDATE:
            candidate = validate_candidate(decode(raw))
            insist(row["tag_name"] == "v" + candidate["release_version"], "main proof candidate tag differs")
            try:
                self.checker.require_tag(row["tag_name"], candidate["hub_commit"])
            except ProofError:
                if self.store.read(row, "release-ready.json") is not None:
                    return None  # Historical readiness authorizes no new work.
                raise
            self.candidates.add(row["tag_name"])
        elif raw is not None and name == ASSET and row["tag_name"] in self.candidates:
            # Existing manifests can replay aliases too; they need proof for
            # their bound facts commit, rather than whatever main is now.
            self.manifest(row, raw)
        return raw

    def put(self, row, name, raw):
        if name == ASSET:
            self.manifest(row, raw)
        return self.store.put(row, name, raw)


def require_delivery(checker, tag):
    import release_pipeline as pipeline

    store = pipeline.Store(checker.client)
    row = store.get(tag)
    insist(not row["draft"] and not any(asset["name"] == "release-rejected.json" for asset in row["assets"]),
           "main proof delivery source is not published")
    raw = store.read(row, ASSET)
    insist(raw is not None, "main proof delivery manifest is missing")
    guarded = GuardedStore(store, checker)
    guarded.manifest(row, raw)


def require_facts_source(checker, directory, wait_seconds=0):
    obj = decode((directory / "payload.json").read_bytes())
    keys(obj, ("commit", "sha256", "dry_run"))
    insist(type(obj["dry_run"]) is bool and obj["sha256"] == sha256((directory / "facts.json").read_bytes()),
           "main proof facts snapshot differs")
    return wait_for_commit(checker, obj["commit"], wait_seconds)


def finalize(checker, tag, output):
    import release_pipeline as pipeline

    client = checker.client
    store = GuardedStore(pipeline.Store(client), checker)
    # Keep the existing per-release reconciliation, alias, reservation and
    # consumer policy code. Guarded reads also cover the empty-tag sweep.
    pipeline.finalize_all(client, store, pipeline.Registry(), tag, output)


def wait_for_commit(checker, commit, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return checker.require_commit(commit)
        except ProofPending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(15, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("commit", allow_abbrev=False)
    command.add_argument("--sha", required=True)
    command.add_argument("--wait-seconds", type=int, default=0)
    command = sub.add_parser("tag", allow_abbrev=False)
    command.add_argument("--tag", required=True)
    command.add_argument("--sha", required=True)
    command = sub.add_parser("delivery", allow_abbrev=False)
    command.add_argument("--tag", required=True)
    command = sub.add_parser("facts-source", allow_abbrev=False)
    command.add_argument("--source-dir", type=Path, required=True)
    command.add_argument("--wait-seconds", type=int, default=0)
    command = sub.add_parser("finalize", allow_abbrev=False)
    command.add_argument("--tag", default="")
    command.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    checker = MainProof(Client(token=os.environ.get("MAIN_PROOF_TOKEN", os.environ.get("GH_TOKEN", ""))))
    try:
        if args.command == "commit":
            insist(0 <= args.wait_seconds <= 3600, "main proof wait is out of bounds")
            wait_for_commit(checker, args.sha, args.wait_seconds)
        elif args.command == "tag":
            checker.require_tag(args.tag, args.sha)
        elif args.command == "delivery":
            require_delivery(checker, args.tag)
        elif args.command == "facts-source":
            insist(0 <= args.wait_seconds <= 3600, "main proof wait is out of bounds")
            require_facts_source(checker, args.source_dir, args.wait_seconds)
        else:
            finalize(checker, args.tag, args.output)
    except (ProofError, Pending, Invalid, ValueError, OSError, KeyError, TypeError):
        # Never echo API bodies, credentials, target repositories or paths.
        print("::error::release main proof refused; exact main-push CI and facts must be complete and successful", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
