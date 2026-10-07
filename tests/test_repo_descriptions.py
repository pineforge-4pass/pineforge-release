#!/usr/bin/env python3
"""Offline CLI acceptance; only the third-party GitHub executable is faked."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "repo_descriptions.py"
FACTS = REPO / "facts" / "facts.json"
POLICY = REPO / "facts" / "repo-descriptions.json"


class DescriptionCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="repo-description-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, *arguments, facts=FACTS, policy=POLICY, env=None, timeout=100):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments, "--facts", str(facts),
             "--policy", str(policy)],
            capture_output=True, text=True, check=False, timeout=timeout, env=env,
        )

    def document(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def policy(self, change):
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        change(policy)
        return self.document("policy.json", policy)

    def render(self, **kwargs):
        result = self.invoke("render", **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_canonical_snapshot_and_complete_manifest(self):
        document = self.render()
        self.assertEqual(document["facts_sha256"], hashlib.sha256(FACTS.read_bytes()).hexdigest())
        rows = {row["role"]: row for row in document["repositories"]}
        self.assertEqual(set(rows), {"engine", "codegen-oss", "corpus", "hpo", "release", "backtest-mcp"})
        self.assertEqual(rows["hpo"]["disposition"], "HOLD")
        self.assertIn("all 7,989 graded probes", rows["engine"]["expected"])
        self.assertIn("7,983 excellent, 6 strong", rows["engine"]["expected"])
        self.assertIn("6 strong", rows["engine"]["expected"])
        self.assertIn("309 open-corpus strategies, 741 closed-test scripts",
                      rows["engine"]["expected"])
        self.assertIn("Historical script inventory (1.0.1): 309 scripts", rows["corpus"]["expected"])
        self.assertIn("PineForge Source License 1.2", rows["codegen-oss"]["expected"])
        self.assertIn("scoreboard.graded", rows["engine"]["tokens"])
        self.assertIn("inventory.closedScripts", rows["engine"]["tokens"])
        for row in rows.values():
            for source in row["license_sources"]:
                self.assertIn("/" + source["commit"] + "/LICENSE", source["url"])
        self.assertEqual(self.invoke("render").stdout, self.invoke("render").stdout)

    def test_future_main_does_not_relabel_inventory_or_released_metrics(self):
        facts = json.loads(FACTS.read_text(encoding="utf-8"))
        facts["scoreboard"].update(graded=9000, excellent=8998, strong=2, population=9017)
        future = self.document("future.json", facts)
        rows = {row["role"]: row for row in self.render(facts=future)["repositories"]}
        self.assertIn("all 9,000 graded probes", rows["engine"]["expected"])
        self.assertIn("8,998 excellent, 2 strong", rows["engine"]["expected"])
        self.assertIn("309 open-corpus strategies, 741 closed-test scripts", rows["engine"]["expected"])
        policy = self.policy(lambda value: value["repositories"][0].update(
            template="Release probes: {{facts:releases.1.0.1.scoreboard.graded|int}}; "
                     "current: {{facts:scoreboard.graded|int}}; "
                     "date: {{facts:scoreboard.date|text}}; "
                     "engine: {{facts:scoreboard.engineCommit|text}}."))
        engine = self.render(facts=future, policy=policy)["repositories"][0]
        self.assertEqual(engine["expected"],
                         "Release probes: 7989; current: 9000; date: 2026-10-06; "
                         "engine: 59082e696f0c7a95c1aa0d3179e1aac23f79fc04.")
        self.assertNotEqual(self.render(facts=future)["facts_sha256"],
                            self.render()["facts_sha256"])

    def test_consistent_below_strong_snapshot_is_refused_before_output_or_live_reads(self):
        canonical = json.loads(FACTS.read_text(encoding="utf-8"))
        facts = copy.deepcopy(canonical)
        scoreboard = facts["scoreboard"]
        pair = next(value for value in scoreboard["pairs"]
                    if value["hardProbes"] == 0 and value["corpusProbes"] == 0
                    and value["closedProbes"] > 0)
        for group in (scoreboard, scoreboard["scopes"]["closed"], pair):
            group["excellent"] -= 1
            group["belowStrong"] += 1
            group["tiers"]["excellent"] -= 1
            group["tiers"]["moderate"] += 1
        for field, count in (("excellentPct", scoreboard["excellent"]),
                             ("strongPct", scoreboard["strong"]),
                             ("excellentOrStrongPct", scoreboard["excellent"] + scoreboard["strong"])):
            scoreboard[field] = round(count * 100 / scoreboard["graded"], 2)
        self.assertEqual(facts["inventory"], canonical["inventory"])
        self.assertEqual(facts["releases"], canonical["releases"])
        self.assertEqual(scoreboard["hardLane"], canonical["scoreboard"]["hardLane"])
        self.assertEqual(scoreboard["population"] - scoreboard["anomaliesExcluded"],
                         scoreboard["graded"])
        for group in [scoreboard, *scoreboard["scopes"].values(), *scoreboard["pairs"]]:
            self.assertEqual(sum(group["tiers"].values()), group["graded"])
            self.assertEqual(group["excellent"] + group["strong"] + group["belowStrong"],
                             group["graded"])
            self.assertEqual(group["belowStrong"], sum(value for tier, value in group["tiers"].items()
                                                     if tier not in {"excellent", "strong"}))
        for field in ("graded", "excellent", "strong", "belowStrong", "engineErrors"):
            self.assertEqual(sum(value[field] for value in scoreboard["pairs"]), scoreboard[field])
            self.assertEqual(sum(value[field] for value in scoreboard["scopes"].values()),
                             scoreboard[field])
        future = self.document("future-below-strong.json", facts)
        executable, env, _ = self.fake_gh()
        for arguments in [("render",), ("render", "--format", "commands"),
                          ("check", "--live", "--gh", str(executable))]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, facts=future, env=env)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("all-graded", result.stderr)
        self.assertFalse((self.root / "api-calls.jsonl").exists())

    def test_all_graded_claim_requires_exact_excellent_and_strong_sum(self):
        for difference in (-1, 1):
            with self.subTest(difference=difference):
                facts = json.loads(FACTS.read_text(encoding="utf-8"))
                facts["scoreboard"]["excellent"] += difference
                result = self.invoke("render", "--format", "commands",
                                     facts=self.document("inconsistent-sum.json", facts))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("all-graded", result.stderr)

    def test_changed_historical_tokens_are_read_not_hardcoded(self):
        facts = json.loads(FACTS.read_text(encoding="utf-8"))
        facts["inventory"].update(corpusScripts=411, closedScripts=855)
        engine = self.render(facts=self.document("inventory.json", facts))["repositories"][0]
        self.assertIn("411 open-corpus strategies, 855 closed-test scripts", engine["expected"])

    def test_missing_unknown_and_unresolved_tokens_fail_without_partial_commands(self):
        for template in ["{{facts:scoreboard.unknown|int}}", "{{facts:inventory|text}}",
                         "{{license:unknown}}", "{{facts:scoreboard.graded}}", "{{bad}}",
                         "{{facts:scoreboard.graded|float}}", "{{facts:scoreboard.graded|int}",
                         "Count: 7989", "{{facts:scoreboard.graded|text}}"]:
            with self.subTest(template=template):
                policy = self.policy(lambda value: value["repositories"][0].update(template=template))
                result = self.invoke("render", "--format", "commands", policy=policy)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, "")
        facts = json.loads(FACTS.read_text(encoding="utf-8"))
        del facts["scoreboard"]["graded"]
        result = self.invoke("render", facts=self.document("missing.json", facts))
        self.assertEqual(result.returncode, 2)

    def test_invalid_quantities_and_schema_are_refused(self):
        for invalid in [None, True, -1, 1.5, "7989", [], {}]:
            with self.subTest(invalid=invalid):
                facts = json.loads(FACTS.read_text(encoding="utf-8"))
                facts["scoreboard"]["graded"] = invalid
                result = self.invoke("render", facts=self.document("invalid.json", facts))
                self.assertEqual(result.returncode, 2, result.stdout)
        for change in [lambda value: value.update(schema="other"),
                       lambda value: value.update(unrecognized=1),
                       lambda value: value["scoreboard"].update(excellentPct=101),
                       lambda value: value["scoreboard"].update(date="2026-02-30"),
                       lambda value: value["scoreboard"]["provenance"].update(promotionDate="yesterday")]:
            facts = json.loads(FACTS.read_text(encoding="utf-8"))
            change(facts)
            self.assertEqual(self.invoke("render", facts=self.document("bad-schema.json", facts)).returncode, 2)

    def test_ambiguous_json_and_nonfinite_numbers_are_refused(self):
        for raw in ['{"schema":1,"schema":2}', '{"value":NaN}', '{"value":Infinity}',
                    '{"value":-Infinity}', '[]', '{} trailing', '\ufeff{}', '{"value":1e9999}']:
            with self.subTest(raw=raw):
                path = self.root / "bad.json"
                path.write_text(raw, encoding="utf-8")
                self.assertEqual(self.invoke("render", facts=path).returncode, 2)
        path.write_bytes(b"\xff")
        self.assertEqual(self.invoke("render", facts=path).returncode, 2)

    def test_github_character_boundary_and_control_characters(self):
        for size, expected_exit in [(350, 0), (351, 2)]:
            policy = self.policy(lambda value: value["repositories"][0].update(template="é" * size))
            self.assertEqual(self.invoke("render", policy=policy).returncode, expected_exit)
        for control in ["\n", "\r", "\t", "\0", "\x1f", "\x7f", "\x85", "\u2028", "\u202e"]:
            with self.subTest(control=repr(control)):
                policy = self.policy(lambda value: value["repositories"][0].update(template="bad" + control))
                self.assertEqual(self.invoke("render", policy=policy).returncode, 2)
        policy = self.policy(lambda value: value["repositories"][0].update(template=""))
        self.assertEqual(self.invoke("render", policy=policy).returncode, 2)
        policy = self.policy(lambda value: value["repositories"][0].update(template="   "))
        self.assertEqual(self.invoke("render", policy=policy).returncode, 2)

    def test_repeated_cli_options_are_refused(self):
        self.assertEqual(self.invoke("render", "--facts", str(FACTS)).returncode, 2)
        self.assertEqual(self.invoke("render", "--format=json", "--format", "commands").returncode, 2)

    def test_hpo_cannot_be_unheld_by_swapping_roles(self):
        for identity in ("pineforge-4pass/pineforge-hpo", "pineforge-4pass/PineForge-HPO",
                         "PINEFORGE-4PASS/PINEFORGE-HPO"):
            with self.subTest(identity=identity):
                def change(policy):
                    engine = next(row for row in policy["repositories"] if row["role"] == "engine")
                    held = next(row for row in policy["repositories"] if row["role"] == "hpo")
                    engine.update(role="hpo", disposition="HOLD", approved_text="Held engine")
                    del engine["template"]
                    held["source"]["url"] = held["source"]["url"].replace(held["repo"], identity)
                    held["source"]["repo"] = identity
                    held.update(role="engine", repo=identity, disposition="static",
                                template="Changed held repository")
                    del held["approved_text"]
                result = self.invoke("render", "--format", "commands", policy=self.policy(change))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_case_only_repository_duplicates_fail_without_partial_output(self):
        def change(policy):
            original, duplicate = policy["repositories"][:2]
            identity = original["repo"].swapcase()
            duplicate["repo"] = identity
            duplicate["source"] = copy.deepcopy(original["source"])
            duplicate["source"]["repo"] = identity
            duplicate["source"]["url"] = duplicate["source"]["url"].replace(original["repo"], identity)
        result = self.invoke("render", "--format", "commands", policy=self.policy(change))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertIn("duplicate", result.stderr)

    def test_mixed_case_source_and_live_identities_are_accepted(self):
        def change(policy):
            for row in policy["repositories"]:
                row["repo"] = row["repo"].swapcase()
            for license_value in policy["licenses"].values():
                license_value["repo"] = license_value["repo"].swapcase()
        policy = self.policy(change)
        canonical_rows = self.render()["repositories"]
        rendered_rows = self.render(policy=policy)["repositories"]
        self.assertEqual([row["expected"] for row in canonical_rows],
                         [row["expected"] for row in rendered_rows])
        executable, env, _ = self.fake_gh("case")
        result = self.invoke("check", "--live", "--gh", str(executable), policy=policy, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(row["status"] == "match" for row in json.loads(result.stdout)["repositories"]))
        malformed = self.policy(lambda value: value["licenses"]["engine"].update(
            url=value["licenses"]["engine"]["url"].replace("/LICENSE", "/license")))
        self.assertEqual(self.invoke("render", policy=malformed).returncode, 2)

    def test_policy_refuses_duplicate_repositories_nonpublic_and_unpinned_licenses(self):
        changes = [lambda value: value["repositories"].append(copy.deepcopy(value["repositories"][0])),
                   lambda value: value["repositories"].pop(),
                   lambda value: value["repositories"][0].update(repo="--evil/name"),
                   lambda value: value["repositories"][0].update(public=False),
                   lambda value: value["repositories"][0].update(unknown=True),
                   lambda value: value["licenses"]["codegen"].update(commit="main"),
                   lambda value: value["licenses"]["codegen"].update(url="https://example.com/LICENSE"),
                   lambda value: next(row for row in value["repositories"] if row["role"] == "hpo").update(
                       disposition="managed", template="Mutate HPO")]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.invoke("render", policy=self.policy(change)).returncode, 2)

    def test_commands_are_quoted_text_and_hpo_is_never_a_command(self):
        dangerous = "Compiler's $(touch SHOULD_NOT_EXIST); `id` & shell text"
        policy = self.policy(lambda value: value["repositories"][0].update(template=dangerous))
        result = self.invoke("render", "--format", "commands", policy=policy)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [shlex.split(line) for line in result.stdout.splitlines() if not line.startswith("#")]
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0], ["gh", "repo", "edit", "pineforge-4pass/pineforge-engine",
                                      "--description", dangerous])
        self.assertTrue(all(command[3] != "pineforge-4pass/pineforge-hpo" for command in commands))
        self.assertIn("# HOLD", result.stdout)
        self.assertIn("facts_sha256", result.stdout)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def fake_gh(self, mode="match", role="engine"):
        rendered = self.render()
        rows = rendered["repositories"]
        state = {row["repo"]: {"full_name": row["repo"], "private": False,
                              "description": row["expected"]} for row in rows}
        target = next(row["repo"] for row in rows if row["role"] == role)
        if mode == "drift": state[target]["description"] += " changed"
        if mode == "null": state[target]["description"] = None
        if mode == "empty": state[target]["description"] = ""
        if mode == "nonpublic": state[target]["private"] = True
        if mode == "identity": state[target]["full_name"] = "other/name"
        if mode == "type": state[target]["description"] = 42
        if mode == "missing": del state[target]["description"]
        if mode == "case":
            for response in state.values(): response["full_name"] = response["full_name"].swapcase()
        state_path = self.document("live-state.json", state)
        executable = self.root / "fake third-party gh"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['FAKE_GH_LOG'], 'a') as output: output.write(json.dumps(args) + '\\n')\n"
            "if args[:5] != ['api', '--method', 'GET', '--hostname', 'github.com'] or len(args) != 6:\n"
            "    print('refusing non-read operation', file=sys.stderr); sys.exit(80)\n"
            "repo = args[5].removeprefix('repos/').casefold()\n"
            "if repo == os.environ['FAKE_GH_TARGET']:\n"
            "    mode = os.environ['FAKE_GH_MODE']\n"
            "    if mode == 'error': print('authentication failed', file=sys.stderr); sys.exit(7)\n"
            "    if mode == 'malformed': print('not JSON'); sys.exit(0)\n"
            "    if mode == 'timeout': time.sleep(65)\n"
            "state = json.loads(Path(os.environ['FAKE_GH_STATE']).read_text())\n"
            "print(json.dumps(state[repo]))\n", encoding="utf-8")
        executable.chmod(0o755)
        env = dict(os.environ, FAKE_GH_STATE=str(state_path), FAKE_GH_LOG=str(self.root / "api-calls.jsonl"),
                   FAKE_GH_TARGET=target, FAKE_GH_MODE=mode)
        return executable, env, target

    def test_exact_live_match_and_bounded_read_only_arguments(self):
        executable, env, _ = self.fake_gh()
        result = self.invoke("check", "--live", "--gh", str(executable), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)["repositories"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["status"] == "match" for row in rows))
        calls = [json.loads(line) for line in (self.root / "api-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(call[:5] == ["api", "--method", "GET", "--hostname", "github.com"] for call in calls))

    def test_live_drift_including_held_hpo_and_explicit_null(self):
        for mode, role in [("drift", "engine"), ("drift", "hpo"), ("null", "engine"), ("empty", "engine")]:
            with self.subTest(mode=mode, role=role):
                executable, env, target = self.fake_gh(mode, role)
                result = self.invoke("check", "--live", "--gh", str(executable), env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                row = next(row for row in json.loads(result.stdout)["repositories"] if row["repo"] == target)
                self.assertEqual(row["status"], "drift")
                if mode == "null": self.assertIsNone(row["actual"])
                if mode == "empty": self.assertEqual(row["actual"], "")
                self.assertNotEqual(row["actual"], row["expected"])

    def test_live_api_auth_shape_identity_and_timeout_fail_closed(self):
        for mode in ["error", "malformed", "nonpublic", "identity", "type", "missing", "timeout"]:
            with self.subTest(mode=mode):
                executable, env, target = self.fake_gh(mode)
                result = self.invoke("check", "--live", "--gh", str(executable), env=env)
                self.assertEqual(result.returncode, 2, result.stdout)
                row = next(row for row in json.loads(result.stdout)["repositories"] if row["repo"] == target)
                self.assertEqual(row["status"], "error")
                self.assertNotIn("actual", row)
                self.assertTrue(row["error"])
        self.assertEqual(self.invoke("check", "--live", "--gh", str(self.root / "absent")).returncode, 2)
        self.assertEqual(self.invoke("check").returncode, 2)
        self.assertEqual(self.invoke("apply").returncode, 2)


class ReadOnlyWorkflow(unittest.TestCase):
    def test_live_workflow_is_main_only_read_only_and_always_preserves_proposals(self):
        text = (REPO / ".github" / "workflows" / "repo-description-drift.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [main]", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertIn("contents: read", text)
        self.assertNotIn(": write", text)
        for path in ["facts/facts.json", "facts/facts.schema.json", "facts/repo-descriptions.json",
                     "scripts/repo_descriptions.py"]:
            self.assertIn(path, text)
        self.assertIn("check --facts facts/facts.json --live", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("if: always()", text)
        self.assertNotIn("gh repo edit", text)


if __name__ == "__main__":
    unittest.main()
