#!/usr/bin/env python3
"""Recorded-receipt parity: the receipts recorded in this repository, judged before and after the change.

The corpus is the consumer-supplied receipts RECORDED in tests/fixtures/release-receipts (the stored public
outputs are never inputs). The Step 1 corpus is exactly two license facts-only receipts: ready.json and
verified.json. No example-builder receipt counts as parity evidence. Wider recorded parity is Step 2;
this test makes no parity claim for any other role.

Nothing is compared with a hand-written expectation. tests/legacy_receipt_verdicts.py judges each case with ONE
scripts directory and prints canonical verdict lines (the public receipt or the refusal, and every provider
route asked for, in order). It runs once on the pre-change checkout (PINEFORGE_RECEIPTS_BASE_SCRIPTS) and once
on this tree, in separate processes, and the bytes must be identical.

Each case's provider answers are derived from the ORIGINAL receipt's own IDs and SHAs. The judged receipt is
then that receipt with one ID or SHA mutated, so the validator must ask for the mutated ID (an unserved route)
or refuse it, and its verdict must change: the binding is observed, not assumed. The provider answers echo what
the receipt claims, because no recorded provider answers exist; that limit is why this is a regression guard
and not proof that the receipts are true.

Without the pre-change scripts the base comparison is SKIPPED, and a skip is not a pass. Run it with
PINEFORGE_RECEIPTS_BASE_SCRIPTS=<pre-change checkout>/scripts, plus PINEFORGE_REQUIRE_BASE_PARITY=1, which turns
the skip into a failure. The head-only tests always run. Nothing here has been run by its author.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAD_SCRIPTS = ROOT / "scripts"
HELPER = ROOT / "tests" / "legacy_receipt_verdicts.py"
FIXTURES = ROOT / "tests" / "fixtures" / "release-receipts"
sys.path.insert(0, str(HEAD_SCRIPTS))
import release_contract as contract  # noqa: E402
import test_release_receipts as recorded  # noqa: E402

REPO = "pineforge-4pass/fixture"
SCHEMAS = ("pineforge/consumer-receipt/v1", "pineforge/facts-consumer-receipt/v1")
FACTS_SCHEMA = SCHEMAS[1]
# The policy each recorded receipt was recorded under. A recorded receipt of another role has no policy here,
# and test_every_recorded_receipt_has_a_policy_and_a_role fails until one is added.
RECORDED_POLICY = {"license": recorded.POLICY}
STAGES = ("ci", "e2e", "deploy")


def recorded_receipts():
    """(file name, receipt) for each consumer-supplied receipt artifact recorded in the repository."""
    found = []
    for path in (FIXTURES / "ready.json", FIXTURES / "verified.json"):
        receipt = json.loads(path.read_bytes())
        if receipt.get("schema") in SCHEMAS and "evidence_sha256" not in receipt:
            found.append((path.name, receipt))
    return found


def corpus():
    """(name, receipt, event, policy) for every recorded receipt that has a policy; nothing else."""
    items = []
    for filename, receipt in recorded_receipts():
        policy = RECORDED_POLICY.get(receipt["consumer"])
        # A recorded product receipt would need its recorded hub event as well; none exists.
        if policy is not None and receipt["schema"] == FACTS_SCHEMA:
            items.append(("recorded:" + filename, receipt, None, policy))
    return items


def stage_routes(receipt, policy):
    routes = {}
    for stage in STAGES:
        evidence = receipt.get(stage)
        if not isinstance(evidence, dict):
            continue
        route = f"repos/{REPO}/actions/runs/{evidence['run_id']}/attempts/{evidence['run_attempt']}"
        finished = "2020-01-01T00:06:00Z" if stage == "deploy" else "2020-01-01T00:04:00Z"
        routes[route] = {
            "id": evidence["run_id"], "run_attempt": evidence["run_attempt"],
            "path": ".github/workflows/" + policy[stage]["workflow"], "head_sha": evidence["sha"],
            "status": "completed", "conclusion": "success", "event": "push", "head_branch": policy["base"],
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
        }
        routes[route + "/jobs?per_page=100&page=1"] = {"jobs": [
            {"name": name, "status": "completed", "conclusion": evidence["results"][name], "completed_at": finished}
            for name in evidence["expected"]]}
    return routes


def provider_routes(receipt, policy):
    """Everything the provider would say about the receipt's own PR and runs, keyed by route."""
    pr = receipt["pr"]
    merged = pr["merge"] is not None
    routes = {f"repos/{REPO}/pulls/{pr['number']}": {
        "base": {"ref": pr["base"], "sha": pr["base_sha"]},
        "head": {"sha": pr["head"], "repo": {"full_name": REPO}},
        "merged": merged, "merge_commit_sha": pr["merge"], "merged_at": "2020-01-01T00:05:00Z"}}
    if merged:
        routes[f"repos/{REPO}/commits/{pr['merge']}"] = {"parents": [{"sha": pr["base_sha"]}, {"sha": pr["head"]}]}
    routes.update(stage_routes(receipt, policy))
    return routes


def make_case(name, receipt, event, policy, judged=None):
    """The provider is derived from `receipt`; `judged` (default the same receipt) is what is then judged."""
    merge = receipt["pr"]["merge"] or "2" * 40
    return {
        "name": name, "repo": REPO, "receipt": receipt if judged is None else judged, "event": event,
        "policy": policy, "routes": provider_routes(receipt, policy),
        "run": {"id": 9001, "path": ".github/workflows/" + policy["workflow"], "head_sha": merge, "event": "push"},
    }


def plus(amount):
    return lambda value: value + amount if type(value) is int else None


def flip(value):
    if type(value) is not str or len(value) != 40:
        return None
    return ("0" if value[0] != "0" else "1") + value[1:]


# (name, path into the receipt, operation, expectation)
#   unserved: the validator must ask the provider for a route built from the mutated ID, which it lacks
#   refused:  the validator must refuse on content (not merely on a missing route)
#   differs:  the verdict bytes must change
MUTATIONS = (
    ("ci run id", ("ci", "run_id"), plus(1000), "unserved"),
    ("ci run attempt", ("ci", "run_attempt"), plus(1), "unserved"),
    ("e2e run id", ("e2e", "run_id"), plus(1000), "unserved"),
    ("e2e run attempt", ("e2e", "run_attempt"), plus(1), "unserved"),
    ("deploy run id", ("deploy", "run_id"), plus(1000), "unserved"),
    ("deploy run attempt", ("deploy", "run_attempt"), plus(1), "unserved"),
    ("pr number", ("pr", "number"), plus(1000), "unserved"),
    ("pr head", ("pr", "head"), flip, "refused"),
    ("pr merge", ("pr", "merge"), flip, "refused"),
    ("pr base sha", ("pr", "base_sha"), flip, "differs"),
    ("ci sha", ("ci", "sha"), flip, "refused"),
    ("e2e sha", ("e2e", "sha"), flip, "refused"),
    ("deploy sha", ("deploy", "sha"), flip, "refused"),
    ("preview source", ("preview", "source"), flip, "refused"),
    ("deployed source", ("deployed", "source"), flip, "refused"),
)


def mutate(receipt, path, operation):
    """A copy of the receipt with one value changed, or None where the receipt has no such value."""
    copied = copy.deepcopy(receipt)
    node = copied
    for name in path[:-1]:
        node = node.get(name) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    value = operation(node.get(path[-1]))
    if value is None:
        return None
    node[path[-1]] = value
    return copied


def entries():
    """Every case to judge: each recorded receipt as recorded, then with each ID or SHA mutated."""
    found = []
    for source, receipt, event, policy in corpus():
        found.append({"source": source, "mutation": None, "expectation": None, "receipt": receipt,
                      "policy": policy, "case": make_case(source, receipt, event, policy)})
        for name, path, operation, expectation in MUTATIONS:
            mutant = mutate(receipt, path, operation)
            if mutant is not None:
                found.append({"source": source, "mutation": name, "expectation": expectation, "receipt": mutant,
                              "policy": policy, "case": make_case(f"{source}|{name}", receipt, event, policy, mutant)})
    return found


def verdicts(scripts, rows):
    """The helper's stdout bytes and the module path it imported, for `scripts`."""
    with tempfile.TemporaryDirectory() as scratch:
        cases = Path(scratch) / "cases.json"
        cases.write_bytes(contract.canonical([row["case"] for row in rows]))
        done = subprocess.run([sys.executable, "-I", "-B", str(HELPER), str(scripts), str(cases)],
                              capture_output=True, timeout=900, check=False)
    stderr = done.stderr.decode("utf-8", "replace")
    if done.returncode != 0:
        raise AssertionError(f"verdict helper failed for {scripts}: {stderr[-2000:]}")
    imported = [line for line in stderr.splitlines() if line.endswith("release_receipts.py")]
    if len(imported) != 1:
        raise AssertionError(f"verdict helper did not report one validator module for {scripts}: {stderr[-2000:]}")
    return done.stdout, imported[0]


@functools.lru_cache(maxsize=None)
def head_run():
    rows = entries()
    out, imported = verdicts(HEAD_SCRIPTS, rows)
    return rows, out, imported


def base_scripts():
    """The pre-change scripts directory, or None when it was not supplied."""
    path = os.environ.get("PINEFORGE_RECEIPTS_BASE_SCRIPTS")
    return Path(path) if path else None


def parsed(out):
    return [json.loads(line) for line in out.decode("utf-8").splitlines()]


class RecordedCorpusTests(unittest.TestCase):
    def test_every_recorded_receipt_has_a_policy_and_a_role(self):
        found = recorded_receipts()
        self.assertEqual([name for name, _ in found], ["ready.json", "verified.json"])
        for name, receipt in found:
            with self.subTest(file=name):
                self.assertIn(receipt["consumer"], RECORDED_POLICY, "add the policy this receipt was recorded under")
                self.assertEqual(receipt["schema"], FACTS_SCHEMA, "a recorded product receipt needs its recorded event")
                self.assertNotEqual(receipt["consumer"], "application")
        judged = {row["source"] for row in entries() if row["mutation"] is None}
        for name, _ in found:
            self.assertIn("recorded:" + name, judged)

    def test_the_corpus_is_recorded_receipts_only(self):
        """No receipt made by a builder is judged, and none is counted: only what the repository recorded is."""
        rows = entries()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["source"].startswith("recorded:"), row["source"])
        self.assertEqual({row["case"]["receipt"]["consumer"] for row in rows}, {"license"})


class HeadVerdictTests(unittest.TestCase):
    def test_every_baseline_receipt_is_accepted_and_the_supplied_ids_are_what_is_asked(self):
        rows, out, _ = head_run()
        lines = parsed(out)
        self.assertEqual(len(lines), len(rows))
        baselines = 0
        for row, line in zip(rows, lines):
            self.assertEqual(line["name"], row["case"]["name"])
            if row["mutation"] is not None:
                continue
            baselines += 1
            receipt = row["receipt"]
            with self.subTest(case=row["source"]):
                self.assertTrue(line["verdict"]["accepted"], line["verdict"])
                self.assertEqual(line["verdict"]["public"]["state"], receipt["state"])
                self.assertEqual(line["verdict"]["public"]["attempt"], receipt["attempt"])
                asked = set(line["asked"])
                self.assertIn(f"repos/{REPO}/pulls/{receipt['pr']['number']}", asked)
                for stage in STAGES:
                    evidence = receipt.get(stage)
                    if isinstance(evidence, dict):
                        self.assertIn(f"repos/{REPO}/actions/runs/{evidence['run_id']}/attempts/{evidence['run_attempt']}", asked)
                if receipt["state"] == "verified":
                    self.assertIn(f"repos/{REPO}/commits/{receipt['pr']['merge']}", asked)
        self.assertEqual(baselines, len(recorded_receipts()))

    def test_mutating_each_supplied_id_or_sha_changes_what_is_asked_and_the_verdict(self):
        rows, out, _ = head_run()
        lines = parsed(out)
        baseline = {row["source"]: line for row, line in zip(rows, lines) if row["mutation"] is None}
        seen = {}
        for row, line in zip(rows, lines):
            if row["mutation"] is None:
                continue
            verdict = line["verdict"]
            with self.subTest(case=line["name"]):
                self.assertNotEqual(line, baseline[row["source"]] | {"name": line["name"]})
                if row["expectation"] == "unserved":
                    # The route must be built from the mutated ID: absent from what the provider holds
                    # for the original receipt, present in what it would hold for the mutated one.
                    self.assertFalse(verdict["accepted"])
                    asked_for = verdict["unserved"]
                    self.assertNotIn(asked_for, row["case"]["routes"])
                    self.assertIn(asked_for, provider_routes(row["receipt"], row["policy"]))
                    self.assertEqual(line["asked"][-1], asked_for)
                elif row["expectation"] == "refused":
                    self.assertFalse(verdict["accepted"])
                    self.assertNotIn("unserved", verdict)
                    self.assertTrue(verdict["message"])
            seen.setdefault(row["source"], set()).add(row["expectation"])
        # Every recorded receipt exercised all three kinds of mutation, so none passes vacuously.
        for source, kinds in seen.items():
            self.assertEqual(kinds, {"unserved", "refused", "differs"}, source)
        self.assertEqual(set(seen), set(baseline))

    def test_the_comparison_detects_a_changed_verdict(self):
        """A validator whose refusal text moved one word must produce different bytes, or the base/head
        comparison below could not fail."""
        rows, out, _ = head_run()
        with tempfile.TemporaryDirectory() as scratch:
            tampered = Path(scratch) / "scripts"
            shutil.copytree(HEAD_SCRIPTS, tampered, ignore=shutil.ignore_patterns("__pycache__"))
            source = tampered / "release_receipts.py"
            text = source.read_text(encoding="utf-8")
            self.assertEqual(text.count("checks ran against a different source"), 1)
            source.write_text(text.replace("checks ran against a different source",
                                           "checks ran against a DIFFERENT source"), encoding="utf-8")
            changed, imported = verdicts(tampered, rows)
        self.assertEqual(Path(imported).resolve(), source.resolve())
        self.assertNotEqual(changed, out)
        before, after = parsed(out), parsed(changed)
        differing = {new["name"] for old, new in zip(before, after) if old != new}
        # Exactly the cases that were refused with that text changed, and no other.
        refused = {old["name"] for old in before
                   if old["verdict"].get("message") == "checks ran against a different source"}
        self.assertTrue(refused)
        self.assertEqual(differing, refused)


class BaseHeadParityTests(unittest.TestCase):
    def require_base(self):
        base = base_scripts()
        if base is None:
            message = ("PINEFORGE_RECEIPTS_BASE_SCRIPTS is not set: the base/head comparison did NOT run "
                       "(a skip is not a pass)")
            if os.environ.get("PINEFORGE_REQUIRE_BASE_PARITY") == "1":
                self.fail(message)
            self.skipTest(message)
        self.assertTrue((base / "release_receipts.py").is_file(), "base scripts directory has no validator")
        return base

    def test_the_base_scripts_are_the_pre_change_validator(self):
        base = self.require_base()
        self.assertNotEqual(base.resolve(), HEAD_SCRIPTS.resolve())
        before = (base / "release_receipts.py").read_text(encoding="utf-8")
        after = (HEAD_SCRIPTS / "release_receipts.py").read_text(encoding="utf-8")
        self.assertNotEqual(before, after)
        self.assertNotIn("application_reporter", before)
        self.assertIn("application_reporter", after)

    def test_head_and_base_verdicts_are_byte_identical_for_every_recorded_receipt_and_mutation(self):
        base = self.require_base()
        rows, head_out, head_imported = head_run()
        base_out, base_imported = verdicts(base, rows)
        self.assertEqual(Path(base_imported).resolve(), (base / "release_receipts.py").resolve())
        self.assertEqual(Path(head_imported).resolve(), (HEAD_SCRIPTS / "release_receipts.py").resolve())
        self.assertEqual(len(parsed(base_out)), len(rows))
        for row, before, after in zip(rows, parsed(base_out), parsed(head_out)):
            with self.subTest(case=row["case"]["name"]):
                self.assertEqual(json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True))
        self.assertEqual(base_out, head_out)


if __name__ == "__main__":
    unittest.main()
