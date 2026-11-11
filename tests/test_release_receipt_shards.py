#!/usr/bin/env python3
"""Base-file shard plans using the existing receipt API fixture."""
from __future__ import annotations

import base64
import copy
import json
import os
import unittest
from unittest.mock import patch

import test_release_coordinator_receipts as coordinator

contract = coordinator.contract
receipts = coordinator.receipts

FORMAT = "suite ({i}/{n})"
FILE = "config/shards.json"
SHARDS = {"format": FORMAT, "file": FILE, "path": ["matrix", "count"], "max_minutes": 15}
SECTIONS = ("ci", "e2e", "deploy", "post_merge")


def owner_policy():
    return {
        "workflow": "example-report.yml", "artifact": "example-receipt", "base": "main",
        **{stage: {"workflow": f"example-{stage}.yml", "expected": ["build"], "approved_skips": []}
           for stage in SECTIONS},
    }


def shard_row():
    return {
        "workflow": "example-ci.yml", "expected": ["build", FORMAT, "merge"],
        "approved_skips": [], "shards": copy.deepcopy(SHARDS),
    }


def names(count):
    return [f"suite ({i}/{count})" for i in range(1, count + 1)]


def file_contents(value):
    return {
        "type": "file", "encoding": "base64",
        "content": base64.b64encode(contract.canonical(value)).decode("ascii"),
    }


def shard_evidence(count, base_count):
    inventory = sum(range(1, count + 1))
    return {
        "manifest": {
            "shard_count": count,
            "shards": {
                name: {"declared_count": i, "timeout_minutes": SHARDS["max_minutes"]}
                for i, name in enumerate(names(count), 1)
            },
            "disjoint": True, "union_count": inventory, "inventory_count": inventory,
            "union_equals_inventory": True,
            "inventory_sha256": "a" * 64, "plan_sha256": "b" * 64,
        },
        "n_change": None if base_count == count else {
            "base_n": base_count, "candidate_n": count, "path": FILE,
        },
    }


class ShardFixture(coordinator.ReceiptFixture):
    def __init__(self, count=12, base_count=12):
        policy = owner_policy()
        policy["ci"] = shard_row()
        super().__init__("application", policy)
        self.row = self.policy["ci"]
        self.evidence = self.receipt["ci"]
        self.file_routes = {
            source: f"repos/{self.repo}/contents/{FILE}?ref={self.pr[source]}"
            for source in ("base_sha", "head")
        }
        self.run_route = (f"repos/{self.repo}/actions/runs/{self.evidence['run_id']}"
                          f"/attempts/{self.evidence['run_attempt']}")
        self.jobs_route = self.run_route + "/jobs?per_page=100&page=1"
        self.responses[self.file_routes["base_sha"]] = (
            contract.Pending("published artifact unavailable") if base_count is None
            else file_contents({"matrix": {"count": base_count}}))
        self.responses[self.file_routes["head"]] = (
            contract.Pending("published artifact unavailable") if count is None
            else file_contents({"matrix": {"count": count}}))
        actual_count = count if count is not None else (base_count if base_count is not None else 6)
        self.set_names(actual_count)
        self.evidence.update(shard_evidence(actual_count, base_count))

    def api(self, route):
        if isinstance(self.responses.get(route), Exception):
            self.queries.append(route)
            raise self.responses[route]
        return super().api(route)

    def set_names(self, count):
        expected = ["build", *names(count), "merge"]
        self.evidence.update(expected=expected, results={name: "success" for name in expected})
        self.jobs["ci"][:] = [
            {"name": name, "status": "completed", "conclusion": "success",
             "started_at": "2020-01-01T00:03:00Z",
             "completed_at": "2020-01-01T00:04:00Z"}
            for name in expected
        ]
        for i, job in enumerate(self.jobs["ci"][1:-1]):
            job["started_at"] = f"2020-01-01T00:03:{i:02d}Z"

    def check(self):
        return self.verify()

    def plan_routes(self):
        return [f"repos/{self.repo}/pulls/9",
                self.file_routes["base_sha"], self.file_routes["head"]]


class ShardReceiptTests(unittest.TestCase):
    def assert_refused(self, fixture, message):
        with self.assertRaises(contract.Invalid) as caught:
            fixture.check()
        self.assertEqual(str(caught.exception), message)

    def test_steady_complete_family_uses_base_and_head_for_each_count(self):
        for count in (12, 1, 6):
            with self.subTest(count=count):
                f = ShardFixture(count, count)
                original = copy.deepcopy(f.policy)
                with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps({"application": f.policy})}):
                    self.assertEqual(receipts.profile("application"), original)
                public = f.check()
                self.assertEqual(public["ci"]["shards"], {
                    "count": count, "seconds": [60 - i for i in range(count)],
                    "inventory": sum(range(1, count + 1)), "change": None,
                })
                self.assertEqual(f.policy, original)
                self.assertEqual(f.queries[:5], f.plan_routes() + [f.run_route, f.jobs_route])
                self.assertEqual(f.queries.count(f.jobs_route), 1)

    def test_one_missing_job_is_refused(self):
        f = ShardFixture()
        f.jobs["ci"][:] = [job for job in f.jobs["ci"] if job["name"] != "suite (12/12)"]
        self.assert_refused(f, "required check missing or incomplete")

    def test_extra_family_job_is_refused(self):
        f = ShardFixture()
        f.jobs["ci"].append({**f.jobs["ci"][1], "name": "suite (13/12)"})
        self.assert_refused(f, "unexpected job in shard family")

    def test_jobs_of_another_total_are_refused_before_missing_jobs(self):
        for count, other in ((12, 6), (6, 12)):
            with self.subTest(count=count, other=other):
                f = ShardFixture(count, count)
                f.jobs["ci"][:] = [
                    {**f.jobs["ci"][1], "name": name} for name in names(other)
                ]
                self.assert_refused(f, "unexpected job in shard family")
                self.assertEqual(f.queries, f.plan_routes() + [f.run_route, f.jobs_route])

    def test_evidence_length_must_equal_selected_count(self):
        f = ShardFixture()
        f.evidence["expected"] = ["build", *names(12)[:-1], "merge"]
        self.assert_refused(f, "shard evidence list length differs")
        self.assertEqual(f.queries, f.plan_routes())

    def test_same_names_in_another_order_are_refused(self):
        f = ShardFixture()
        f.evidence["expected"] = ["build", *names(12)[::-1], "merge"]
        self.assert_refused(f, "required check inventory differs from owner policy")
        self.assertEqual(f.queries, f.plan_routes())

    def test_count_requires_an_exact_positive_integer_at_either_commit(self):
        for source in ("base_sha", "head"):
            for value in (0, -1, 2.5, "12", True, None):
                with self.subTest(source=source, value=value):
                    f = ShardFixture()
                    f.responses[f.file_routes[source]] = file_contents({"matrix": {"count": value}})
                    self.assert_refused(f, "shard count must be a positive integer")
                    self.assertNotIn(f.run_route, f.queries)

    def test_count_path_requires_objects_and_existing_keys(self):
        for value in ({}, {"matrix": {}}, {"matrix": []}, {"matrix": None}):
            with self.subTest(value=value):
                f = ShardFixture()
                f.responses[f.file_routes["base_sha"]] = file_contents(value)
                self.assert_refused(f, "shard count path missing")
                self.assertEqual(f.queries, f.plan_routes()[:2])

    def test_file_uses_strict_json_decode(self):
        for raw in (
                b'{"matrix":{"count":12,"count":6}}',
                b'{"matrix":{"count":NaN}}',
                b'{"matrix":{"count":Infinity}}', b'{"matrix":', b"\xff"):
            with self.subTest(raw=raw):
                f = ShardFixture()
                f.responses[f.file_routes["base_sha"]]["content"] = base64.b64encode(raw).decode("ascii")
                self.assert_refused(f, "invalid shard file JSON")
                self.assertEqual(f.queries, f.plan_routes()[:2])

    def test_huge_count_is_refused_before_name_allocation_or_jobs(self):
        f = ShardFixture()
        for route in f.file_routes.values():
            f.responses[route] = file_contents({"matrix": {"count": 10**9}})
        with patch.object(receipts, "range", create=True,
                          side_effect=AssertionError("names built before length check")) as expand:
            self.assert_refused(f, "shard evidence list length differs")
        expand.assert_not_called()
        self.assertEqual(f.queries, f.plan_routes())
        self.assertFalse(any("/jobs" in route for route in f.queries))

    def test_duplicate_shard_is_refused(self):
        f = ShardFixture()
        f.jobs["ci"].append(copy.deepcopy(f.jobs["ci"][1]))
        self.assert_refused(f, "required check missing or incomplete")

    def test_failed_cancelled_or_skipped_shard_is_refused(self):
        for conclusion in ("failure", "cancelled", "skipped"):
            with self.subTest(conclusion=conclusion):
                f = ShardFixture()
                f.jobs["ci"][1]["conclusion"] = conclusion
                self.assert_refused(f, "required check failed/cancelled/skipped")

    def test_changed_head_count_selects_candidate_count(self):
        f = ShardFixture(6, 12)
        f.evidence["n_change"] = None
        self.assert_refused(f, "shard count change record is required")
        self.assertEqual(f.queries, f.plan_routes())

    def test_unchanged_file_cannot_use_another_count(self):
        f = ShardFixture()
        f.set_names(6)
        self.assert_refused(f, "shard evidence list length differs")
        self.assertEqual(f.queries, f.plan_routes())

    def test_files_are_read_only_at_recorded_base_and_head(self):
        f = ShardFixture()
        other = f"repos/{f.repo}/contents/{FILE}?ref={'f' * 40}"
        f.responses[other] = file_contents({"matrix": {"count": 6}})
        public = f.check()
        self.assertEqual(public["ci"]["shards"]["count"], 12)
        self.assertEqual(f.queries[1:3], [
            f"repos/{f.repo}/contents/config/shards.json?ref={f.pr['base_sha']}",
            f"repos/{f.repo}/contents/config/shards.json?ref={f.pr['head']}",
        ])
        self.assertNotIn(other, f.queries)

    def test_manifest_is_required_in_steady_and_changed_counts(self):
        for base in (12, 6):
            with self.subTest(base=base):
                f = ShardFixture(12, base)
                f.evidence = f.receipt["ci"] = {
                    key: value for key, value in f.evidence.items() if key != "manifest"
                }
                self.assert_refused(f, "shard manifest evidence is required")
                self.assertEqual(f.queries, f.plan_routes())

    def test_null_manifest_is_refused_in_steady_and_changed_counts(self):
        for base in (12, 6):
            with self.subTest(base=base):
                f = ShardFixture(12, base)
                f.evidence["manifest"] = None
                self.assert_refused(f, "unexpected shard manifest fields")

    def test_manifest_rechecks_sum_even_with_true_disjointness(self):
        for base in (12, 6):
            for offset in (-1, 1):
                with self.subTest(base=base, offset=offset):
                    f = ShardFixture(12, base)
                    manifest = f.evidence["manifest"]
                    manifest["union_count"] += offset
                    manifest["inventory_count"] = manifest["union_count"]
                    self.assertIs(manifest["disjoint"], True)
                    self.assert_refused(f, "shard manifest union differs from shard sum")

    def test_manifest_requires_exact_true_disjointness(self):
        for base in (12, 6):
            for value, offset in ((False, 0), (False, -1), (1, 0)):
                with self.subTest(base=base, value=value, offset=offset):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["disjoint"] = value
                    f.evidence["manifest"]["union_count"] += offset
                    self.assert_refused(f, "shard manifest is not disjoint")

    def test_manifest_rechecks_union_against_independent_inventory(self):
        for base in (12, 6):
            for offset in (-1, 1):
                with self.subTest(base=base, offset=offset):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["inventory_count"] += offset
                    self.assertIs(f.evidence["manifest"]["union_equals_inventory"], True)
                    self.assert_refused(f, "shard manifest union differs from inventory")

    def test_manifest_requires_exact_true_inventory_equality(self):
        for base in (12, 6):
            for value in (False, 1):
                with self.subTest(base=base, value=value):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["union_equals_inventory"] = value
                    self.assert_refused(f, "shard manifest inventory equality is not true")

    def test_manifest_shard_count_must_equal_selected_count(self):
        for base in (12, 6):
            for value in (11, 13, 12.0, True, "12", None):
                with self.subTest(base=base, value=value):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["shard_count"] = value
                    self.assert_refused(f, "shard manifest count differs")

    def test_manifest_shards_must_have_exact_size(self):
        original = shard_evidence(12, 12)["manifest"]["shards"]
        values = (
            None, list(original),
            {name: entry for name, entry in original.items() if name != "suite (12/12)"},
            {**original, "suite (13/12)": {"declared_count": 1, "timeout_minutes": 15}},
        )
        for base in (12, 6):
            for value in values:
                with self.subTest(base=base, value=value):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["shards"] = value
                    self.assert_refused(f, "shard manifest size differs")

    def test_manifest_shard_name_set_is_rechecked(self):
        for base in (12, 6):
            for replacement in ("gate", "suite (1/6)", "Suite (1/12)"):
                with self.subTest(base=base, replacement=replacement):
                    f = ShardFixture(12, base)
                    entries = f.evidence["manifest"]["shards"]
                    f.evidence["manifest"]["shards"] = {
                        replacement if name == "suite (1/12)" else name: entry
                        for name, entry in entries.items()
                    }
                    self.assert_refused(f, "shard manifest names differ")

    def test_manifest_counts_require_exact_positive_integers(self):
        for field, message in (
                ("declared_count", "shard declared count must be a positive integer"),
                ("union_count", "shard manifest union must be a positive integer"),
                ("inventory_count", "shard manifest inventory must be a positive integer")):
            for value in (0, -1, 2.5, "9", True, None):
                with self.subTest(field=field, value=value):
                    f = ShardFixture()
                    target = (f.evidence["manifest"]["shards"]["suite (1/12)"]
                              if field == "declared_count" else f.evidence["manifest"])
                    target[field] = value
                    self.assert_refused(f, message)

    def test_manifest_shard_entries_have_exact_fields(self):
        original = shard_evidence(12, 12)["manifest"]["shards"]["suite (1/12)"]
        values = [None, [], {**original, "extra": True}]
        values.extend({key: value for key, value in original.items() if key != missing}
                      for missing in original)
        for value in values:
            with self.subTest(value=value):
                f = ShardFixture()
                f.evidence["manifest"]["shards"]["suite (1/12)"] = value
                self.assert_refused(f, "unexpected shard entry fields")

    def test_manifest_has_exact_fields(self):
        f = ShardFixture()
        manifest = f.evidence["manifest"]
        values = [{**manifest, "extra": True}]
        values.extend({key: value for key, value in manifest.items() if key != missing}
                      for missing in manifest)
        for value in values:
            with self.subTest(value=value):
                f = ShardFixture()
                f.evidence["manifest"] = value
                self.assert_refused(f, "unexpected shard manifest fields")

    def test_timeouts_require_positive_integers(self):
        for value in (0, -1, 2.5, "15", True, None):
            with self.subTest(value=value):
                f = ShardFixture()
                f.evidence["manifest"]["shards"]["suite (1/12)"]["timeout_minutes"] = value
                self.assert_refused(f, "shard timeouts must be positive integers")

    def test_timeout_limit_is_enforced_in_steady_and_changed_counts(self):
        for base in (12, 6):
            for limit in (1, 15, 100):
                with self.subTest(base=base, limit=limit):
                    f = ShardFixture(12, base)
                    f.row["shards"]["max_minutes"] = limit
                    for entry in f.evidence["manifest"]["shards"].values():
                        entry["timeout_minutes"] = limit
                    f.evidence["manifest"]["shards"]["suite (1/12)"]["timeout_minutes"] = limit + 1
                    self.assert_refused(f, "shard timeout exceeds policy limit")

    def test_timeout_policy_boundary_passes_in_steady_and_changed_counts(self):
        for base in (12, 6):
            with self.subTest(base=base):
                f = ShardFixture(12, base)
                self.assertEqual(f.row["shards"]["max_minutes"], 15)
                self.assertTrue(all(entry["timeout_minutes"] == 15
                                    for entry in f.evidence["manifest"]["shards"].values()))
                self.assertEqual(f.check()["ci"]["shards"]["count"], 12)

    def test_manifest_hashes_require_lowercase_sha256(self):
        for field, message in (
                ("inventory_sha256", "invalid shard inventory hash"),
                ("plan_sha256", "invalid shard plan hash")):
            for value in ("A" * 64, "a" * 63, "a" * 65, "g" + "a" * 63, 12, None):
                with self.subTest(field=field, value=value):
                    f = ShardFixture()
                    f.evidence["manifest"][field] = value
                    self.assert_refused(f, message)

    def test_observed_runtime_over_declared_timeout_is_recorded_not_refused(self):
        for base in (12, 6):
            for start, end, seconds in (
                    ("2019-12-31T23:48:00Z", "2020-01-01T00:04:00Z", 960),
                    ("2019-12-31T23:49:00Z", "2020-01-01T00:04:00.000001Z", 900),
                    ("2019-12-31T23:24:00Z", "2020-01-01T00:04:00Z", 2400),
                    ("2019-12-31T21:04:00Z", "2020-01-01T00:04:00Z", 10800)):
                with self.subTest(base=base, start=start, end=end):
                    f = ShardFixture(12, base)
                    f.jobs["ci"][1].update(started_at=start, completed_at=end)
                    self.assertEqual(f.check()["ci"]["shards"]["seconds"], [seconds, *range(59, 48, -1)])

    def test_observed_runtime_equal_to_declared_timeout_is_accepted_and_recorded(self):
        for base in (12, 6):
            with self.subTest(base=base):
                f = ShardFixture(12, base)
                f.jobs["ci"][1]["started_at"] = "2019-12-31T23:49:00Z"
                self.assertEqual(f.check()["ci"]["shards"]["seconds"][0], 15 * 60)

    def test_declared_timeout_over_limit_is_refused_whatever_the_observed_time(self):
        for base in (12, 6):
            for start in ("2020-01-01T00:03:00Z", "2019-12-31T21:04:00Z"):
                with self.subTest(base=base, start=start):
                    f = ShardFixture(12, base)
                    f.evidence["manifest"]["shards"]["suite (1/12)"]["timeout_minutes"] = 16
                    f.jobs["ci"][1]["started_at"] = start
                    self.assert_refused(f, "shard timeout exceeds policy limit")

    def test_changed_count_with_all_three_requirements_passes(self):
        f = ShardFixture(12, 6)
        self.assertEqual(f.check()["ci"]["shards"], {
            "count": 12, "seconds": list(range(60, 48, -1)), "inventory": 78,
            "change": {"from": 6, "to": 12},
        })

    def test_lower_count_keeps_the_full_inventory(self):
        f = ShardFixture(6, 12)
        manifest = f.evidence["manifest"]
        for entry in manifest["shards"].values():
            entry["declared_count"] = 13
        manifest.update(union_count=78, inventory_count=78)
        self.assertEqual(f.check()["ci"]["shards"]["change"], {"from": 12, "to": 6})

    def test_changed_count_requires_change_record(self):
        f = ShardFixture(12, 6)
        f.evidence["n_change"] = None
        self.assert_refused(f, "shard count change record is required")

    def test_changed_count_compares_each_change_field(self):
        for field, value, message in (
                ("base_n", 5, "shard change base count differs"),
                ("base_n", None, "shard change base count differs"),
                ("base_n", 6.0, "shard change base count differs"),
                ("base_n", True, "shard change base count differs"),
                ("candidate_n", 11, "shard change candidate count differs"),
                ("candidate_n", 12.0, "shard change candidate count differs"),
                ("candidate_n", True, "shard change candidate count differs"),
                ("path", "config/shards.json.other", "shard change file path differs")):
            with self.subTest(field=field, value=value):
                f = ShardFixture(12, 6)
                f.evidence["n_change"][field] = value
                self.assert_refused(f, message)

    def test_change_record_has_exact_fields(self):
        original = shard_evidence(12, 6)["n_change"]
        values = [[], "change", 12, False, {**original, "extra": True}]
        values.extend({key: value for key, value in original.items() if key != missing}
                      for missing in original)
        for value in values:
            with self.subTest(value=value):
                f = ShardFixture(12, 6)
                f.evidence["n_change"] = value
                self.assert_refused(f, "unexpected shard change fields")

    def test_steady_cannot_claim_count_change(self):
        f = ShardFixture()
        f.evidence["n_change"] = {"base_n": 12, "candidate_n": 12, "path": FILE}
        self.assert_refused(f, "unchanged shard count cannot claim change")

    def test_count_file_deletion_is_refused(self):
        f = ShardFixture(None, 12)
        self.assert_refused(f, "shard count file deletion is forbidden")
        self.assertEqual(f.queries, f.plan_routes())

    def test_public_numbers_come_from_jobs_and_exclude_private_strings(self):
        for count, base in ((12, 12), (12, 6)):
            with self.subTest(count=count, base=base):
                f = ShardFixture(count, base)
                f.jobs["ci"][1]["started_at"] = "2020-01-01T00:03:59.250Z"
                public = f.check()
                self.assertEqual(public["ci"]["shards"]["seconds"], [0, *range(59, 48, -1)])
                raw = contract.canonical(public).decode("utf-8")
                for text in ("suite (", FILE, "example-ci", "base_n", "candidate_n",
                             "timeout_minutes", "plan_sha256"):
                    self.assertNotIn(text, raw)
                summary = public["ci"]["shards"]
                self.assertEqual(set(summary), {"count", "seconds", "inventory", "change"})
                self.assertEqual(summary["count"], count)
                self.assertEqual(summary["inventory"], 78)
                self.assertEqual(summary["change"], None if base == count else {"from": base, "to": count})
                self.assertTrue(all(type(value) is int for value in summary["seconds"]))

    def test_shard_job_needs_start_and_completion_timestamps(self):
        for field, message in (
                ("started_at", "shard job timestamps missing"),
                ("completed_at", "required check completion time missing")):
            with self.subTest(field=field):
                f = ShardFixture()
                f.jobs["ci"][1] = {key: value for key, value in f.jobs["ci"][1].items() if key != field}
                self.assert_refused(f, message)

    def test_invalid_or_negative_job_duration_is_refused(self):
        for value, message in (
                ("bad", "invalid shard job timestamps"),
                ("2020-01-01T00:03:00", "invalid shard job timestamps"),
                ("2020-01-01T00:05:00Z", "shard job duration is negative")):
            with self.subTest(value=value):
                f = ShardFixture()
                f.jobs["ci"][1]["started_at"] = value
                self.assert_refused(f, message)

    def test_file_must_be_an_inline_base64_file(self):
        for value, message in (
                ([], "shard source is not a file"), (None, "shard source is not a file"),
                ({"type": "dir"}, "shard source is not a file"),
                ({"type": "symlink"}, "shard source is not a file"),
                ({"type": "file"}, "shard file has no inline base64 content"),
                ({"type": "file", "encoding": "none", "content": "e30="}, "shard file has no inline base64 content"),
                ({"type": "file", "encoding": "base64", "content": None}, "shard file has no inline base64 content")):
            with self.subTest(value=value):
                f = ShardFixture()
                f.responses[f.file_routes["base_sha"]] = value
                self.assert_refused(f, message)

    def test_malformed_base64_is_refused(self):
        for value in ("!", "e30", "é", "e 30="):
            with self.subTest(value=value):
                f = ShardFixture()
                f.responses[f.file_routes["base_sha"]]["content"] = value
                self.assert_refused(f, "invalid shard file base64")

    def test_wrapped_base64_and_unrelated_head_content_pass(self):
        f = ShardFixture()
        f.responses[f.file_routes["head"]] = file_contents({"matrix": {"count": 12}, "other": True})
        content = f.responses[f.file_routes["head"]]["content"]
        f.responses[f.file_routes["head"]]["content"] = content[:8] + "\n" + content[8:] + "\n"
        self.assertEqual(f.check()["ci"]["shards"]["count"], 12)

    def test_request_errors_propagate_from_either_file_read(self):
        for source in ("base_sha", "head"):
            for message in ("artifact request failed (HTTP 403)", "artifact transport failed"):
                with self.subTest(source=source, message=message):
                    f = ShardFixture()
                    f.responses[f.file_routes[source]] = receipts.RequestError(message)
                    with self.assertRaises(receipts.RequestError) as caught:
                        f.check()
                    self.assertEqual(str(caught.exception), message)
                    self.assertNotIn(f.run_route, f.queries)

    def test_evidence_expected_must_be_a_list(self):
        for value in (None, FORMAT, {}, tuple(names(12))):
            with self.subTest(value=value):
                f = ShardFixture()
                f.evidence["expected"] = value
                self.assert_refused(f, "shard evidence expected must be a list")

    def test_effective_inventory_cannot_collide_with_a_static_name(self):
        f = ShardFixture()
        f.row["expected"] = ["suite (1/12)", FORMAT, "merge"]
        self.assert_refused(f, "empty or duplicate required inventory")
        self.assertEqual(f.queries, f.plan_routes())

    def test_static_approved_skip_survives_expansion(self):
        f = ShardFixture()
        f.row["approved_skips"] = ["build"]
        f.evidence["approved_skips"] = ["build"]
        f.evidence["results"]["build"] = "skipped"
        f.jobs["ci"][0]["conclusion"] = "skipped"
        self.assertEqual(f.check()["ci"]["shards"]["count"], 12)

    def test_approved_skips_must_still_match_policy(self):
        f = ShardFixture()
        f.evidence["approved_skips"] = ["build"]
        self.assert_refused(f, "required check inventory differs from owner policy")
        self.assertEqual(f.queries, f.plan_routes())

    def test_family_pattern_uses_escaped_literals_and_full_names(self):
        f = ShardFixture()
        for name in ("suite (13/12) details", "suite 13/12", "suite (x/12)", "gate"):
            f.jobs["ci"].append({**f.jobs["ci"][1], "name": name})
        self.assertEqual(f.check()["ci"]["shards"]["count"], 12)

    def test_numeric_spellings_outside_effective_names_are_refused(self):
        for name in ("suite (0/12)", "suite (01/12)", "suite (1/012)"):
            with self.subTest(name=name):
                f = ShardFixture()
                f.jobs["ci"].append({**f.jobs["ci"][1], "name": name})
                self.assert_refused(f, "unexpected job in shard family")

    def test_evidence_objects_have_exact_fields(self):
        for base in (12, 6):
            with self.subTest(base=base):
                f = ShardFixture(12, base)
                f.evidence["extra"] = True
                self.assert_refused(f, "unexpected shard check fields")
        for missing in ("run_id", "run_attempt", "sha", "expected", "approved_skips", "results", "n_change"):
            with self.subTest(missing=missing):
                f = ShardFixture()
                f.evidence = f.receipt["ci"] = {
                    key: value for key, value in f.evidence.items() if key != missing
                }
                self.assert_refused(f, "unexpected shard check fields")


class TransitionalShardTests(unittest.TestCase):
    def fixture(self, count=None):
        f = ShardFixture(count, None)
        f.row["shards"]["absent_count"] = 6
        if count is None:
            f.evidence = f.receipt["ci"] = {
                key: value for key, value in f.evidence.items() if key not in ("manifest", "n_change")
            }
        else:
            f.evidence["n_change"] = {"base_n": 6, "candidate_n": count, "path": FILE}
        return f

    def assert_refused(self, fixture, message):
        with self.assertRaises(contract.Invalid) as caught:
            fixture.check()
        self.assertEqual(str(caught.exception), message)

    def test_transitional_absent_files_accept_legacy_and_record_durations(self):
        f = self.fixture()
        self.assertEqual(f.check()["ci"]["shards"], {
            "count": 6, "seconds": list(range(60, 54, -1)), "inventory": None, "change": None,
        })
        self.assertEqual(set(f.evidence), {"run_id", "run_attempt", "sha", "expected", "approved_skips", "results"})
        self.assertEqual(f.queries[:5], f.plan_routes() + [f.run_route, f.jobs_route])

    def test_transitional_legacy_rejects_new_or_extra_fields(self):
        for field in ("manifest", "n_change", "extra"):
            with self.subTest(field=field):
                f = self.fixture()
                f.evidence[field] = None
                self.assert_refused(f, "transitional shard evidence must use legacy fields")

    def test_transitional_legacy_refuses_another_count(self):
        f = self.fixture()
        f.set_names(12)
        self.assert_refused(f, "shard evidence list length differs")

    def test_transitional_legacy_refuses_extra_family_job(self):
        f = self.fixture()
        f.jobs["ci"].append({**f.jobs["ci"][1], "name": "suite (7/6)"})
        self.assert_refused(f, "unexpected job in shard family")

    def test_transitional_absent_base_requires_policy_count(self):
        for head in (None, 12):
            with self.subTest(head=head):
                f = ShardFixture(head, None)
                self.assert_refused(f, "shard base file absent without transitional count")
                self.assertEqual(f.queries, f.plan_routes())

    def test_transitional_added_file_requires_manifest(self):
        f = self.fixture(12)
        f.evidence = f.receipt["ci"] = {
            key: value for key, value in f.evidence.items() if key != "manifest"
        }
        self.assert_refused(f, "shard manifest evidence is required")

    def test_transitional_added_file_enforces_timeout(self):
        f = self.fixture(12)
        f.evidence["manifest"]["shards"]["suite (1/12)"]["timeout_minutes"] = SHARDS["max_minutes"] + 1
        self.assert_refused(f, "shard timeout exceeds policy limit")

    def test_transitional_added_file_with_all_requirements_passes(self):
        f = self.fixture(12)
        public = f.check()
        self.assertEqual(public["ci"]["shards"], {
            "count": 12, "seconds": list(range(60, 48, -1)), "inventory": 78,
            "change": {"from": 6, "to": 12},
        })

    def test_transitional_added_file_compares_configured_base(self):
        for value in (None, 5, 6.0, True):
            with self.subTest(value=value):
                f = self.fixture(12)
                f.evidence["n_change"]["base_n"] = value
                self.assert_refused(f, "shard change base count differs")

    def test_transitional_addition_with_same_count_still_requires_change(self):
        f = self.fixture(6)
        self.assertEqual(f.check()["ci"]["shards"]["change"], {"from": 6, "to": 6})
        f.evidence["n_change"] = None
        self.assert_refused(f, "shard count change record is required")

    def test_transitional_file_deletion_is_always_refused(self):
        f = self.fixture()
        f.responses[f.file_routes["base_sha"]] = file_contents({"matrix": {"count": 6}})
        self.assert_refused(f, "shard count file deletion is forbidden")

    def test_transitional_policy_count_requires_positive_integer(self):
        for value in (0, -1, 2.5, "6", True, None):
            with self.subTest(value=value):
                policy = owner_policy()
                policy["ci"] = shard_row()
                policy["ci"]["shards"]["absent_count"] = value
                with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps({"application": policy})}):
                    with self.assertRaises(contract.Invalid) as caught:
                        receipts.profile("application")
                self.assertEqual(str(caught.exception), "invalid transitional shard count")

    def test_transitional_policy_count_has_no_builtin_value(self):
        f = self.fixture()
        f.row["shards"]["absent_count"] = 1
        f.set_names(1)
        self.assertEqual(f.check()["ci"]["shards"]["count"], 1)


class ShardPolicyTests(unittest.TestCase):
    def read_profile(self, row, stage="ci"):
        policy = owner_policy()
        policy[stage] = row
        with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps({"application": policy})}):
            return receipts.profile("application")

    def assert_refused(self, row, message, stage="ci"):
        with self.assertRaises(contract.Invalid) as caught:
            self.read_profile(row, stage)
        self.assertEqual(str(caught.exception), message)

    def test_shards_policy_is_only_allowed_in_ci(self):
        for stage in ("e2e", "deploy", "post_merge"):
            with self.subTest(stage=stage):
                self.assert_refused(shard_row(), "shards policy is only valid for ci", stage)

    def test_format_requires_one_of_each_placeholder_and_no_other_braces(self):
        for value in (
                None, 12, "", "suite ({n})", "suite ({i})",
                "suite ({i}/{i}/{n})", "suite ({i}/{n}/{n})",
                "suite ({i}/{n}/{x})", "suite ({{i}}/{n})",
                "suite ({i}/{n}){", "suite ({i}/{n})}"):
            with self.subTest(value=value):
                row = shard_row()
                row["shards"]["format"] = value
                self.assert_refused(row, "invalid shard format")

    def test_file_requires_safe_relative_components(self):
        for value in (
                None, 12, "", "/config/shards.json", "..", "../config/shards.json",
                "config/../shards.json", "config\\shards.json", "config//shards.json",
                "config/./shards.json", "./config/shards.json", "config/shards.json/",
                "config/shards.json?ref=main", "config/shards json"):
            with self.subTest(value=value):
                row = shard_row()
                row["shards"]["file"] = value
                self.assert_refused(row, "invalid shard file path")

    def test_path_requires_nonempty_string_keys(self):
        for value in (None, "matrix", [], [""], ["matrix", ""], ["matrix", 12], [True]):
            with self.subTest(value=value):
                row = shard_row()
                row["shards"]["path"] = value
                self.assert_refused(row, "invalid shard count path")

    def test_max_minutes_requires_positive_integer(self):
        for value in (0, -1, 2.5, "15", True, None):
            with self.subTest(value=value):
                row = shard_row()
                row["shards"]["max_minutes"] = value
                self.assert_refused(row, "invalid shard time limit")

    def test_shards_has_exact_required_fields(self):
        values = [None, [], {}, {**SHARDS, "extra": True}]
        values.extend({key: value for key, value in SHARDS.items() if key != missing}
                      for missing in SHARDS)
        for value in values:
            with self.subTest(value=value):
                row = shard_row()
                row["shards"] = value
                self.assert_refused(row, "unexpected shard policy fields")

    def test_placeholder_must_appear_exactly_once(self):
        for expected in (["build", "merge"], ["build", FORMAT, FORMAT, "merge"]):
            with self.subTest(expected=expected):
                row = shard_row()
                row["expected"] = expected
                self.assert_refused(row, "shard placeholder must appear exactly once")

    def test_placeholder_cannot_be_skipped(self):
        row = shard_row()
        row["approved_skips"] = [FORMAT]
        self.assert_refused(row, "shard checks cannot be skipped")


class StaticReceiptTests(unittest.TestCase):
    def test_static_six_job_policy_keeps_exact_routes_and_public_ci(self):
        policy = owner_policy()
        policy["ci"] = {"workflow": "example-ci.yml", "expected": names(6), "approved_skips": []}
        f = coordinator.ReceiptFixture("application", policy)
        with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps({"application": policy})}):
            self.assertEqual(receipts.profile("application"), policy)
        public = f.verify()
        self.assertEqual(f.queries, [
            "repos/pineforge-4pass/fixture/pulls/9",
            "repos/pineforge-4pass/fixture/actions/runs/41/attempts/2",
            "repos/pineforge-4pass/fixture/actions/runs/41/attempts/2/jobs?per_page=100&page=1",
            "repos/pineforge-4pass/fixture/actions/runs/52/attempts/3",
            "repos/pineforge-4pass/fixture/actions/runs/52/attempts/3/jobs?per_page=100&page=1",
            "repos/pineforge-4pass/fixture/commits/2222222222222222222222222222222222222222",
            "repos/pineforge-4pass/fixture/actions/runs/63/attempts/4",
            "repos/pineforge-4pass/fixture/actions/runs/63/attempts/4/jobs?per_page=100&page=1",
            "repos/pineforge-4pass/fixture/actions/runs/74/attempts/5",
            "repos/pineforge-4pass/fixture/actions/runs/74/attempts/5/jobs?per_page=100&page=1",
        ])
        self.assertEqual(public["ci"], {
            "run_id": 41, "run_attempt": 2,
            "inventory_sha256": contract.sha256(contract.canonical(f.receipt["ci"]["results"])),
        })


if __name__ == "__main__":
    unittest.main()
