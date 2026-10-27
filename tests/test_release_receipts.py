#!/usr/bin/env python3
"""Receipt collection regressions for PR CI and the supervisor's spot box only.

The fake serves both old and new discovery routes and records every request;
the route assertions, rather than a fake response shortcut, enforce the fix.
Fixed private/public JSON fixtures pin the pre-change stored receipt bytes.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_contract as contract  # noqa: E402
import release_receipts as receipts  # noqa: E402


FIXTURES = ROOT / "tests/fixtures/release-receipts"
REPO = "pineforge-4pass/fixture"
FACTS_COMMIT = "e" * 40
FACTS_SHA256 = "acc0639894780a571be10383eb22b1fbd150f704e1f0ee5d8906ab8d101d4f7b"
HEAD, MERGE, BASE = "1" * 40, "2" * 40, "3" * 40
POLICY = {
    "workflow": "release-report.yml", "artifact": "release-receipt-license", "base": "main",
    "ci": {"workflow": "ci.yml", "expected": ["check"], "approved_skips": []},
    "e2e": {"workflow": "e2e.yml", "expected": ["check"], "approved_skips": []},
    "deploy": {"workflow": "deploy.yml", "expected": ["check"], "approved_skips": []},
}
STEM = f"facts-consumer-license-{FACTS_COMMIT}-{FACTS_SHA256}-"
EXPECTED_STORED = {
    STEM + "1-1.json": (FIXTURES / "stored-ready.json").read_bytes(),
    STEM + "1-2.json": (FIXTURES / "stored-verified.json").read_bytes(),
}


class HistoryClient:
    """A real history with only two receipt-bearing runs; no network fallback."""

    def __init__(self, history_size=60):
        self.routes, self.files, self.archives = [], [], {}
        self.runs = {
            run_id: {"id": run_id, "path": ".github/workflows/release-report.yml",
                     "status": "completed", "head_sha": MERGE, "event": "push"}
            for run_id in range(1001, 1001 + history_size)
        }
        self.documents = {
            1007: json.loads((FIXTURES / "ready.json").read_bytes()),
            1049: json.loads((FIXTURES / "verified.json").read_bytes()),
        }
        self.artifact_by_run = {
            run_id: {"id": run_id + 1000, "name": POLICY["artifact"], "expired": False,
                     "workflow_run": {"id": run_id}}
            for run_id in self.documents
        }
        # GitHub returns newest first. Collection must still apply old to new.
        self.artifacts = list(self.artifact_by_run.values())[::-1]
        self.artifacts.append({"id": 5001, "name": "build-output", "expired": False,
                               "workflow_run": {"id": 1001}})
        self.responses = {
            f"repos/{REPO}": {"full_name": REPO},
            f"repos/{REPO}/pulls/9": {
                "base": {"ref": "main", "sha": BASE},
                "head": {"sha": HEAD, "repo": {"full_name": REPO}},
                "merged": True, "merge_commit_sha": MERGE, "merged_at": "2020-01-01T00:05:00Z",
            },
            f"repos/{REPO}/commits/{MERGE}": {"parents": [{"sha": BASE}, {"sha": HEAD}]},
            f"repos/{contract.HUB}/git/ref/heads/main": {"object": {"sha": "f" * 40}},
            f"repos/{contract.HUB}/compare/{FACTS_COMMIT}...{'f' * 40}": {"status": "ahead"},
        }
        self.responses.update({f"repos/{REPO}/actions/runs/{run_id}": run
                               for run_id, run in self.runs.items()})
        for stage in ("ci", "e2e", "deploy"):
            evidence = self.documents[1049][stage]
            route = f"repos/{REPO}/actions/runs/{evidence['run_id']}/attempts/1"
            self.responses[route] = {
                "path": ".github/workflows/" + POLICY[stage]["workflow"],
                "head_sha": evidence["sha"], "status": "completed", "conclusion": "success",
            }
            finished = "2020-01-01T00:06:00Z" if stage == "deploy" else "2020-01-01T00:04:00Z"
            self.responses[route + "/jobs?per_page=100&page=1"] = {
                "jobs": [{"name": "check", "status": "completed", "conclusion": "success",
                          "completed_at": finished}]}
        for run_id in self.documents:
            self.seal(run_id)

    def seal(self, run_id, names=("release-receipt.json",)):
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as zipped:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                zipped.writestr(info, contract.canonical(self.documents[run_id]))
        archive = packed.getvalue()
        artifact = self.artifact_by_run[run_id]
        artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
        self.archives[f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact['id']}/zip"] = archive

    def api(self, route):
        self.routes.append(route)
        base, _, query = route.partition("?")
        listings = {
            f"repos/{REPO}/actions/artifacts": ("artifacts", self.artifacts),
            f"repos/{REPO}/actions/workflows/{POLICY['workflow']}/runs":
                ("workflow_runs", [r for r in reversed(self.runs.values())
                                   if r["path"] == ".github/workflows/" + POLICY["workflow"]]),
        }
        for run_id in self.runs:
            listings[f"repos/{REPO}/actions/runs/{run_id}/artifacts"] = (
                "artifacts", [a for a in self.artifacts if a["workflow_run"]["id"] == run_id])
        if base in listings:
            params = parse_qs(query)
            named = base == f"repos/{REPO}/actions/artifacts"
            if set(params) != ({"name", "per_page", "page"} if named else {"per_page", "page"}):
                raise AssertionError("unexpected pagination query: " + route)
            size, page = int(params["per_page"][0]), int(params["page"][0])
            key, rows = listings[base]
            if named:
                rows = [row for row in rows if row["name"] == params["name"][0]]
            return copy.deepcopy({key: rows[(page - 1) * size:page * size]})
        if route not in self.responses:
            raise AssertionError("unexpected fixture API route: " + route)
        return copy.deepcopy(self.responses[route])

    def request(self, url, **kwargs):
        self.routes.append(url)
        if kwargs != {"github": True} or url not in self.archives:
            raise AssertionError("unexpected fixture archive request: " + url)
        return self.archives[url]

    def file(self, repo, commit, path):
        self.files.append((repo, commit, path))
        if (repo, commit, path) != (contract.HUB, FACTS_COMMIT, "facts/facts.json"):
            raise AssertionError("unexpected fixture file request")
        return (ROOT / "tests/fixtures/repo-descriptions-facts.json").read_bytes()


class MemoryStore:
    def __init__(self):
        self.row = {"tag_name": "facts-receipts", "draft": True, "assets": []}
        self.assets, self.writes = {}, []

    def facts(self):
        return self.row

    def releases(self):
        return [self.row]

    def read(self, row, name):
        if row != self.row:
            raise AssertionError("unexpected fixture release")
        return self.assets.get(name)

    def put(self, row, name, raw):
        if row != self.row or name in self.assets:
            raise AssertionError("unexpected fixture asset write")
        self.assets[name] = raw
        row["assets"].append({"id": len(self.assets), "name": name})
        self.writes.append((name, raw))


class ReceiptCollectionTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store = HistoryClient(), MemoryStore()

    def collect(self, run_id=0):
        with patch.dict(os.environ, {"TARGET_REPOSITORY": "fixture",
                                     "RELEASE_RECEIPT_POLICY": json.dumps({"license": POLICY})}):
            receipts.collect(self.client, self.client, self.store, "license", run_id)

    def validation_routes(self):
        return [
            f"repos/{REPO}/pulls/9",
            f"repos/{REPO}/actions/runs/201/attempts/1",
            f"repos/{REPO}/actions/runs/201/attempts/1/jobs?per_page=100&page=1",
            f"repos/{REPO}/actions/runs/202/attempts/1",
            f"repos/{REPO}/actions/runs/202/attempts/1/jobs?per_page=100&page=1",
            f"repos/{contract.HUB}/git/ref/heads/main",
            f"repos/{contract.HUB}/compare/{FACTS_COMMIT}...{'f' * 40}",
        ]

    def schedule_routes(self):
        return [
            f"repos/{REPO}",
            f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=1",
            f"repos/{REPO}/actions/runs/1007",
            f"repos/{REPO}/actions/runs/1049",
            f"https://api.github.com/repos/{REPO}/actions/artifacts/2007/zip",
            f"https://api.github.com/repos/{REPO}/actions/artifacts/2049/zip",
            f"repos/{REPO}/commits/{MERGE}",
            f"repos/{REPO}/actions/runs/203/attempts/1",
            f"repos/{REPO}/actions/runs/203/attempts/1/jobs?per_page=100&page=1",
        ] + self.validation_routes() * 2

    def assert_routes(self, expected):
        self.assertEqual(set(self.client.routes), set(expected))
        # Also reject repeated requests to an allowed route.
        self.assertCountEqual(self.client.routes, expected)

    def test_schedule_requests_only_artifacts_and_candidate_runs_regardless_of_history(self):
        for history_size in (60, 600):
            with self.subTest(history_size=history_size):
                self.client, self.store = HistoryClient(history_size), MemoryStore()
                self.collect()
                # The fake also serves workflow-runs and all per-run artifact
                # listings. The old implementation reaches this route assertion.
                self.assert_routes(self.schedule_routes())

    def test_schedule_writes_the_fixed_pre_change_receipt_bytes_and_replays_idempotently(self):
        self.collect()
        self.assertEqual(self.store.assets, EXPECTED_STORED)
        self.assertEqual(self.store.writes, list(EXPECTED_STORED.items()))
        self.assertEqual(self.client.files, [(contract.HUB, FACTS_COMMIT, "facts/facts.json")] * 2)
        self.collect()
        self.assertEqual(self.store.assets, EXPECTED_STORED)
        self.assertEqual(self.store.writes, list(EXPECTED_STORED.items()))

    def test_repository_artifacts_are_paginated_and_unrelated_or_expired_entries_are_ignored(self):
        unrelated = [{"id": 6000 + i, "name": POLICY["artifact"], "expired": True,
                      "workflow_run": {"id": 1001}} for i in range(100)]
        unrelated.append({"id": 7000, "name": POLICY["artifact"], "expired": True,
                          "workflow_run": {"id": 1011}})
        self.client.artifacts[:0] = unrelated
        self.client.runs[1001]["path"] = self.client.runs[1011]["path"] = ".github/workflows/other.yml"
        self.collect()
        self.assert_routes(self.schedule_routes() + [
            f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=2"])
        self.assertEqual(self.store.assets, EXPECTED_STORED)

    def test_page_boundary_duplicate_artifact_is_downloaded_once(self):
        self.client.artifacts[:0] = [
            {"id": 6000 + i, "name": POLICY["artifact"], "expired": True,
             "workflow_run": {"id": 1001}} for i in range(98)]
        api = self.client.api

        def api_with_page_shift(route):
            response = api(route)
            if route == f"repos/{REPO}/actions/artifacts?name={POLICY['artifact']}&per_page=100&page=1":
                # A new row shifts page one's last artifact to the start of page two.
                self.client.artifacts.insert(0, {
                    "id": 7000, "name": POLICY["artifact"], "expired": True,
                    "workflow_run": {"id": 1001}})
            return response

        with patch.object(self.client, "api", side_effect=api_with_page_shift):
            self.collect()
        self.assertEqual(self.client.routes.count(
            f"https://api.github.com/repos/{REPO}/actions/artifacts/2007/zip"), 1)
        self.assertEqual(self.store.assets, EXPECTED_STORED)

    def test_incomplete_candidate_is_not_read_and_is_collected_after_completion(self):
        for status in ("in_progress", "queued", "waiting"):
            with self.subTest(status=status):
                self.client, self.store = HistoryClient(), MemoryStore()
                self.client.runs[1049]["status"] = status
                self.collect()
                self.assertEqual(self.store.assets, {STEM + "1-1.json": EXPECTED_STORED[STEM + "1-1.json"]})
                self.assertNotIn(f"https://api.github.com/repos/{REPO}/actions/artifacts/2049/zip", self.client.routes)
                self.assertNotIn(f"repos/{REPO}/actions/runs/203/attempts/1", self.client.routes)
                self.client.runs[1049]["status"] = "completed"
                self.collect()
                self.assertEqual(self.store.assets, EXPECTED_STORED)
                self.assertEqual(self.store.writes, list(EXPECTED_STORED.items()))

    def test_schedule_ignores_foreign_workflow_artifact_but_callback_fails_closed(self):
        self.client.artifacts.append({**self.client.artifact_by_run[1007], "id": 8000,
                                      "workflow_run": {"id": 1001}})
        self.client.runs[1001]["path"] = ".github/workflows/other.yml"
        self.collect()
        self.assertEqual(self.store.assets, EXPECTED_STORED)
        self.assertNotIn(f"https://api.github.com/repos/{REPO}/actions/artifacts/8000/zip", self.client.routes)

        self.client.routes.clear()
        self.client.runs[1007]["path"] = ".github/workflows/other.yml"
        with self.assertRaisesRegex(contract.Invalid, "unexpected reporting workflow"):
            self.collect(1007)
        self.assertFalse(any(route.endswith("/zip") for route in self.client.routes))

    def test_duplicate_receipt_artifacts_on_one_run_are_still_ambiguous(self):
        self.client.artifacts.append({**self.client.artifact_by_run[1007], "id": 8000})
        output = io.StringIO()
        with redirect_stderr(output), patch.object(receipts, "report_issue"):
            self.collect()
        self.assertIn("::warning::receipt artifact skipped: role=license run=1007: ambiguous consumer receipt artifact",
                      output.getvalue())
        self.assertEqual(self.store.assets, {STEM + "1-2.json": EXPECTED_STORED[STEM + "1-2.json"]})
        self.assertNotIn(f"https://api.github.com/repos/{REPO}/actions/artifacts/2007/zip", self.client.routes)
        self.assertNotIn(f"https://api.github.com/repos/{REPO}/actions/artifacts/8000/zip", self.client.routes)
        self.assertEqual(self.client.routes.count(
            f"https://api.github.com/repos/{REPO}/actions/artifacts/2049/zip"), 1)

    def test_schedule_preserves_digest_inventory_and_role_checks(self):
        for mutation, message in (("digest", "receipt archive digest differs"),
                                  ("inventory", "unexpected receipt archive inventory"),
                                  ("role", "receipt role differs from scoped target")):
            with self.subTest(mutation=mutation):
                self.client, self.store = HistoryClient(), MemoryStore()
                if mutation == "digest":
                    self.client.artifact_by_run[1007]["digest"] = "sha256:" + "0" * 64
                elif mutation == "inventory":
                    self.client.seal(1007, names=("release-receipt.json", "extra.json"))
                else:
                    self.client.documents[1007]["consumer"] = "web"
                    self.client.seal(1007)
                output = io.StringIO()
                with redirect_stderr(output), patch.object(receipts, "report_issue"):
                    self.collect()
                self.assertIn(f"::warning::receipt artifact skipped: role=license run=1007: {message}", output.getvalue())
                self.assertEqual(self.store.assets, {STEM + "1-2.json": EXPECTED_STORED[STEM + "1-2.json"]})
                self.assertEqual(self.client.routes.count(
                    f"https://api.github.com/repos/{REPO}/actions/artifacts/2007/zip"), 1)
                self.assertEqual(self.client.routes.count(
                    f"https://api.github.com/repos/{REPO}/actions/artifacts/2049/zip"), 1)
                self.store = MemoryStore()
                with self.assertRaisesRegex(contract.Invalid, message):
                    self.collect(1007)
                self.assertEqual(self.store.assets, {})

    def test_callback_keeps_its_per_run_lookup_and_existing_in_flight_behavior(self):
        self.client.runs[1007]["status"] = "in_progress"
        self.collect(1007)
        self.assert_routes([
            f"repos/{REPO}", f"repos/{REPO}/actions/runs/1007",
            f"repos/{REPO}/actions/runs/1007/artifacts?per_page=100&page=1",
            f"https://api.github.com/repos/{REPO}/actions/artifacts/2007/zip",
        ] + self.validation_routes())
        self.assertEqual(self.store.assets, {STEM + "1-1.json": EXPECTED_STORED[STEM + "1-1.json"]})

    def test_callback_without_receipt_artifact_still_fails(self):
        with self.assertRaisesRegex(contract.Invalid, "callback has no receipt artifact"):
            self.collect(1001)
        self.assert_routes([
            f"repos/{REPO}", f"repos/{REPO}/actions/runs/1001",
            f"repos/{REPO}/actions/runs/1001/artifacts?per_page=100&page=1",
        ])
        self.assertEqual(self.store.assets, {})

    def test_catch_all_diagnostic_includes_the_actual_exception_text(self):
        for error_type in (contract.Invalid, contract.Pending, ValueError, OSError,
                           KeyError, TypeError, zipfile.BadZipFile):
            error = error_type("GitHub request failed (HTTP 403)")
            output = io.StringIO()
            with self.subTest(error_type=error_type.__name__), \
                    patch.object(sys, "argv", ["release_receipts.py", "collect", "--consumer", "license"]), \
                    patch.object(receipts, "Client"), patch.object(receipts, "Store"), \
                    patch.object(receipts, "collect", side_effect=error), redirect_stderr(output):
                self.assertEqual(receipts.main(), 1)
            self.assertIn("consumer evidence invalid or unavailable; no verified state recorded", output.getvalue())
            self.assertIn(str(error), output.getvalue())


if __name__ == "__main__":
    unittest.main()
