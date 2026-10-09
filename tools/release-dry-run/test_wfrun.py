#!/usr/bin/env python3
"""Focused expression, dummy-environment and fail-closed runner regressions."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dry_run
import wfrun


class ExpressionTest(unittest.TestCase):
    def evaluate(self, expression, context=None):
        return wfrun.Expr(expression, context or {}).parse()

    def test_dynamic_consumer_secret_and_missing_values(self):
        expression = "matrix.consumer == 'offline' && 'pineforge-backtest-mcp' || secrets[matrix.repository_secret]"
        for consumer, secret, target in (
                ("offline", None, "pineforge-backtest-mcp"),
                ("hosted", "RELEASE_HOSTED_MCP_REPOSITORY", "release-fixture-hosted"),
                ("application", "RELEASE_APPLICATION_REPOSITORY", "release-fixture-application")):
            with self.subTest(consumer=consumer):
                context = {"matrix": {"consumer": consumer}, "secrets": dict(wfrun.DUMMY_REPOSITORY_SECRETS)}
                if secret:
                    context["matrix"]["repository_secret"] = secret
                self.assertEqual(self.evaluate(expression, context), target)
        self.assertEqual(self.evaluate(expression, {"matrix": {"consumer": "hosted"}, "secrets": {}}), "")
        self.assertEqual(self.evaluate("steps.missing.outputs.value"), "")
        self.assertEqual(self.evaluate("secrets['missing']", {"secrets": {}}), "")
        self.assertTrue(self.evaluate("steps.missing.outputs.value == ''"))
        self.assertTrue(self.evaluate("steps.missing.outputs.value == false"))
        self.assertEqual(wfrun.interpolate("value=${{ steps.missing.outputs.value }}", {}), "value=")

    def test_short_circuit_preserves_values_and_does_not_evaluate_json(self):
        self.assertIs(self.evaluate("false && fromJSON('invalid')"), False)
        self.assertIs(self.evaluate("true || fromJSON('invalid')"), True)
        self.assertEqual(self.evaluate("'offline' || (false && fromJSON('invalid'))"), "offline")
        self.assertEqual(self.evaluate("true && 'chosen' || 'fallback'"), "chosen")
        self.assertEqual(self.evaluate("true && '' || 'fallback'"), "fallback")
        self.assertEqual(self.evaluate("false || fromJSON('42')"), 42)
        with self.assertRaises(ValueError):
            self.evaluate("true && fromJSON('invalid')")

    def test_offline_role_does_not_lookup_a_secret(self):
        class UnreadableSecrets(dict):
            def get(self, key, default=None):
                raise AssertionError("offline role evaluated a secret")
        context = {"matrix": {"consumer": "offline"}, "secrets": UnreadableSecrets()}
        self.assertEqual(self.evaluate("matrix.consumer == 'offline' && 'pineforge-backtest-mcp' || "
                                       "secrets[matrix.repository_secret]", context), "pineforge-backtest-mcp")

    def test_unsupported_and_malformed_syntax_is_not_hidden(self):
        for expression in ("true || unknown()", "false && unknown()", "true ? 'a' : 'b'",
                           "secrets[", "contains('a' 'b')", "true || (false", "false && fromJSON('x',)"):
            with self.subTest(expression=expression), self.assertRaises(SyntaxError):
                self.evaluate(expression)


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wfrun-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"

    # Target configuration the consumer job must refuse (the missing one leaves the secret out).
    BAD_TARGETS = (("missing", None), ("empty", ""), ("owner/path", "fixture/target"),
                   ("multiline", "fixture\ntarget"))

    def notify(self, consumer, secret=None, *, prerelease="false", **kwargs):
        """One notify-consumers leg of a publish run whose prerelease output is PRERELEASE (None: not given)."""
        matrix = {"consumer": consumer}
        if secret:
            matrix["repository_secret"] = secret
        ref = "refs/tags/v1.0.0-rc.1" if prerelease == "true" else "refs/tags/v1.0.0"
        return wfrun.run_job(self.workflow, "notify-consumers", event={"ref": ref},
                             repo="pineforge-4pass/pineforge-release", workdir=self.root, state=self.state,
                             event_name="push", ref=ref, matrix=matrix,
                             needs={"publish": {"result": "success",
                                                "outputs": {} if prerelease is None else {"prerelease": prerelease}}},
                             echo=None, **kwargs)

    @staticmethod
    def application_targets(value):
        """Dummy repository configuration whose application target is VALUE (None: not configured)."""
        return {} if value is None else {"RELEASE_APPLICATION_REPOSITORY": value}

    def actions(self):
        logfile = self.state / "actions.jsonl"
        return [json.loads(line) for line in logfile.read_text().splitlines()] if logfile.exists() else []

    def test_environment_never_supplies_repository_names_or_credentials(self):
        marker = "caller-value-must-not-enter"
        poison = {key: marker for key in (*wfrun.DUMMY_REPOSITORY_SECRETS, "GH_TOKEN", "GITHUB_TOKEN",
                                         "PINEFORGE_APP_ID", "PINEFORGE_APP_PRIVATE_KEY",
                                         "GOOGLE_APPLICATION_CREDENTIALS", "AWS_SECRET_ACCESS_KEY")}
        with patch.dict(os.environ, poison):
            self.assertFalse(set(poison) & set(wfrun.clean_env()))
            for consumer, secret in (("hosted", "RELEASE_HOSTED_MCP_REPOSITORY"),
                                     ("application", "RELEASE_APPLICATION_REPOSITORY")):
                result = self.notify(consumer, secret)
                self.assertTrue(result.ok, result.text())
                self.assertNotIn(marker, result.text())
        dispatches = [entry for entry in self.actions() if entry.get("action") == "dispatch"]
        self.assertEqual([entry["repo"] for entry in dispatches],
                         ["release-fixture-hosted", "release-fixture-application"])
        self.assertTrue(all(entry["hostname"] == "github.com" for entry in dispatches))

    def test_missing_configuration_is_not_filled_from_environment(self):
        with patch.dict(os.environ, wfrun.DUMMY_REPOSITORY_SECRETS):
            result = self.notify("hosted", "RELEASE_HOSTED_MCP_REPOSITORY", repository_secrets={})
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "Require one configured consumer repository")
        self.assertFalse(self.actions())
        self.assertTrue(self.notify("offline", repository_secrets={}).ok)

    def test_application_on_a_prerelease_is_withheld_whatever_its_target(self):
        for label, value in (("configured", "release-fixture-application"), *self.BAD_TARGETS):
            with self.subTest(target=label):
                result = self.notify("application", "RELEASE_APPLICATION_REPOSITORY", prerelease="true",
                                     repository_secrets=self.application_targets(value))
                self.assertTrue(result.ok, result.text())
                self.assertEqual(result.steps["notify"]["outputs"], {"dispatch": "false"})
                self.assertEqual(result.log.count(f"   | {dry_run.HANDOFF}"), 1)
                for step in ("Require one configured consumer repository", "Mint App token (scoped to one target)",
                             "Dispatch pineforge-release to consumer"):
                    self.assertTrue(any(line.startswith(f"-- skip  : {step}") for line in result.log), step)
        self.assertFalse(self.actions())   # no App token, no dispatch, for any target

    def test_stable_application_with_a_bad_target_still_fails_before_the_token(self):
        for label, value in self.BAD_TARGETS:
            with self.subTest(target=label):
                result = self.notify("application", "RELEASE_APPLICATION_REPOSITORY",
                                     repository_secrets=self.application_targets(value))
                self.assertFalse(result.ok)
                self.assertEqual(result.failed_step, "Require one configured consumer repository")
                self.assertEqual(result.steps["notify"]["outputs"], {"dispatch": "true"})
        self.assertFalse(self.actions())

    def test_prerelease_still_reaches_offline_and_hosted_and_hosted_still_needs_its_target(self):
        for consumer, secret in (("offline", None), ("hosted", "RELEASE_HOSTED_MCP_REPOSITORY")):
            with self.subTest(consumer=consumer):
                result = self.notify(consumer, secret, prerelease="true")
                self.assertTrue(result.ok, result.text())
                self.assertEqual(result.steps["notify"]["outputs"], {"dispatch": "true"})
        dispatches = [entry for entry in self.actions() if entry.get("action") == "dispatch"]
        self.assertEqual([entry["repo"] for entry in dispatches], ["pineforge-backtest-mcp", "release-fixture-hosted"])
        self.assertTrue(all(entry["body"]["client_payload"]["prerelease"] is True for entry in dispatches))
        broken = self.notify("hosted", "RELEASE_HOSTED_MCP_REPOSITORY", prerelease="true", repository_secrets={})
        self.assertFalse(broken.ok)
        self.assertEqual(broken.failed_step, "Require one configured consumer repository")

    def test_malformed_flags_fail_before_any_token_for_every_consumer(self):
        for consumer, secret in (("offline", None), ("hosted", "RELEASE_HOSTED_MCP_REPOSITORY"),
                                 ("application", "RELEASE_APPLICATION_REPOSITORY")):
            for flag in (None, "", "True", "yes", "null", "true\nfalse"):
                with self.subTest(consumer=consumer, flag=flag):
                    result = self.notify(consumer, secret, prerelease=flag)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.failed_step, "Decide consumer notification")
                    self.assertEqual(result.steps["notify"]["outputs"], {})
        self.assertFalse(self.actions())

    def test_dispatch_condition_is_evaluated_by_the_runner(self):
        spec = wfrun.yaml.load(self.workflow.read_text(encoding="utf-8"), Loader=wfrun.yaml.BaseLoader)
        steps = {step["name"]: step for step in spec["jobs"]["notify-consumers"]["steps"]}
        self.assertEqual(next(iter(steps)), "Decide consumer notification")
        for name in ("Require one configured consumer repository", "Mint App token (scoped to one target)",
                     "Dispatch pineforge-release to consumer"):
            with self.subTest(step=name):
                condition = steps[name]["if"]
                for value, expected in (("true", True), ("false", False), ("", False)):
                    context = {"steps": {"notify": {"outputs": {"dispatch": value}}}}
                    self.assertIs(wfrun.evaluate_if(condition, context), expected)
                self.assertIs(wfrun.evaluate_if(condition, {"steps": {}}), False)

    def test_credential_overrides_are_refused(self):
        with self.assertRaises(ValueError):
            self.notify("offline", repository_secrets={"GITHUB_TOKEN": "not-a-dummy"})

    def test_unknown_action_fails_instead_of_noop(self):
        workflow = self.root / "unsupported.yml"
        workflow.write_text("jobs:\n  check:\n    steps:\n      - uses: unknown/action@v1\n")
        result = wfrun.run_job(workflow, "check", event={}, repo="fixture/hub", workdir=self.root,
                               state=self.state, echo=None)
        self.assertFalse(result.ok)
        self.assertIn("unsupported action unknown/action@v1", result.text())

    def test_unknown_hostname_endpoint_and_api_forms_fail_closed(self):
        env = {**wfrun.clean_env(), "HARNESS_STATE": str(self.state)}
        shim = wfrun.SHIMS / "gh"
        endpoint = "repos/fixture/consumer/dispatches"
        for arguments in (("api", "--hostname", "example.invalid", endpoint),
                          ("api", "--hostname", "github.com", "repos/fixture/consumer/unknown"),
                          ("api", "--hostname", "github.com", endpoint, "--unknown"),
                          ("api", "--hostname", "github.com", endpoint, "-X", "DELETE")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(shim), *arguments], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported", result.stderr)
        self.assertFalse(any(entry.get("action") == "dispatch" for entry in self.actions()))

    def test_api_fields_preserve_json_types_and_nested_objects(self):
        env = {**wfrun.clean_env(), "HARNESS_STATE": str(self.state)}
        for typed, raw in (("-F", "-f"), ("--field", "--raw-field")):
            with self.subTest(typed=typed):
                result = subprocess.run([str(wfrun.SHIMS / "gh"), "api", "--hostname", "github.com",
                                         "repos/fixture/consumer/dispatches",
                                         raw, "event_type=pineforge-release",
                                         typed, "client_payload[prerelease]=true",
                                         typed, "client_payload[run_id]=4242",
                                         typed, "client_payload[optional]=null",
                                         typed, "client_payload[nested][stable]=false",
                                         typed, "client_payload[offset]=-7",
                                         raw, "client_payload[raw_bool]=true",
                                         raw, "client_payload[raw_number]=4242",
                                         raw, "client_payload[raw_null]=null"],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.actions()[-1]
                self.assertEqual(call.get("body"), {
                    "event_type": "pineforge-release", "client_payload": {
                        "prerelease": True, "run_id": 4242, "optional": None,
                        "nested": {"stable": False}, "offset": -7,
                        "raw_bool": "true", "raw_number": "4242", "raw_null": "null"}})

    def test_unmodeled_or_conflicting_fields_fail_without_dispatch(self):
        env = {**wfrun.clean_env(), "HARNESS_STATE": str(self.state)}
        for fields in (("-F", "missing-value"), ("-F",),
                       ("-F", "payload[]=value"), ("-F", "payload[broken=value"),
                       ("-F", "payload=@input.json"), ("-F", "payload={repo}"),
                       ("-f", "payload=value", "-F", "payload[nested]=true"),
                       ("-f", "payload[nested]=raw", "-F", "payload[nested]=true")):
            with self.subTest(fields=fields):
                result = subprocess.run([str(wfrun.SHIMS / "gh"), "api", "--hostname", "github.com",
                                         "repos/fixture/consumer/dispatches", *fields],
                                        env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported", result.stderr)
        self.assertFalse(any(entry.get("action") == "dispatch" for entry in self.actions()))

    def test_long_equals_fields_and_gh_integer_conversion_boundaries(self):
        env = {**wfrun.clean_env(), "HARNESS_STATE": str(self.state)}
        result = subprocess.run([str(wfrun.SHIMS / "gh"), "api", "--hostname=github.com",
                                 "repos/fixture/consumer/dispatches",
                                 "--raw-field=event_type=pineforge-release",
                                 "--field=client_payload[leading_zero]=00042",
                                 "--field=client_payload[positive]=+7",
                                 "--field=client_payload[overflow]=9223372036854775808",
                                 "--field=client_payload[decimal]=1.5",
                                 "--raw-field=client_payload[empty]="],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions()[-1]["body"], {
            "event_type": "pineforge-release", "client_payload": {
                "leading_zero": 42, "positive": 7, "overflow": "9223372036854775808",
                "decimal": "1.5", "empty": ""}})


class FanoutContractTest(unittest.TestCase):
    """Execute the real consumer shell through the same fanout as the five worlds."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fanout-contract-")
        self.addCleanup(temp.cleanup)
        opts = SimpleNamespace(out=Path(temp.name),
                               checkout=Path(__file__).resolve().parents[2],
                               metadata_action=None, verbose=False, describe="contract fixture")
        self.world = dry_run.World("contract", opts, [])
        self.addCleanup(self.world.transcript.close)
        self.published = dry_run.Result(wfrun.JobResult(True, outputs={"prerelease": "true"}), [])

    def fanout(self, old=None, new=None):
        work = self.world.clone("refs/heads/main")
        workflow = work / ".github/workflows/publish.yml"
        if old is not None:
            text = workflow.read_text()
            self.assertEqual(text.count(old), 1)
            workflow.write_text(text.replace(old, new))
        return self.world.fanout(self.published, "v1.0.0-rc.1", work=work)

    def test_failure_policy_requires_explicit_false_before_any_consumer(self):
        for replacement in ("      fail-fast: true\n", ""):
            with self.subTest(replacement=replacement):
                before = (self.world.state / "actions.jsonl").read_bytes()
                with self.assertRaisesRegex(RuntimeError, "fail-fast"):
                    self.fanout("      fail-fast: false\n", replacement)
                self.assertEqual((self.world.state / "actions.jsonl").read_bytes(), before)

    def test_fanout_asserts_typed_nested_dispatch_body(self):
        result = self.fanout()
        self.assertTrue(self.world.fanout_matches(result, "1.0.0-rc.1", True), result.text())
        for call in result.calls(tool="gh", action="dispatch"):
            self.assertEqual(call.get("body"), {"event_type": "pineforge-release", "client_payload": {
                "release_version": "1.0.0-rc.1", "prerelease": True, "run_id": 4242}})
            self.assertIs(type(call["body"]["client_payload"]["prerelease"]), bool)
            self.assertIs(type(call["body"]["client_payload"]["run_id"]), int)

    def test_raw_prerelease_and_run_id_are_detected(self):
        self.assertTrue(self.world.fanout_matches(self.fanout(), "1.0.0-rc.1", True))
        for field in ("prerelease", "run_id"):
            with self.subTest(field=field):
                result = self.fanout(f'-F "client_payload[{field}]=', f'-f "client_payload[{field}]=')
                self.assertTrue(result.ok, result.text())
                self.assertFalse(self.world.fanout_matches(result, "1.0.0-rc.1", True))
                for call in result.calls(tool="gh", action="dispatch"):
                    self.assertIs(type(call["body"]["client_payload"][field]), str)

    def stable_fanout(self):
        published = dry_run.Result(wfrun.JobResult(True, outputs={"prerelease": "false"}), [])
        return self.world.fanout(published, "v1.0.0", work=self.world.clone("refs/heads/main"))

    def test_a_prerelease_leaves_the_application_out_and_a_stable_release_does_not(self):
        prerelease, stable = self.fanout(), self.stable_fanout()
        self.assertEqual(len(prerelease.notify), len(dry_run.CONSUMERS))   # all three legs stay visible
        self.assertEqual([call["repo"] for call in prerelease.calls(tool="gh", action="dispatch")],
                         list(dry_run.CONSUMERS[:-1]))
        self.assertEqual([call["repositories"] for call in prerelease.calls(tool="actions/create-github-app-token")],
                         list(dry_run.CONSUMERS[:-1]))
        self.assertTrue(prerelease.ok and self.world.withheld(prerelease.notify[-1]), prerelease.text())
        self.assertEqual([call["repo"] for call in stable.calls(tool="gh", action="dispatch")],
                         list(dry_run.CONSUMERS))
        self.assertTrue(self.world.fanout_matches(prerelease, "1.0.0-rc.1", True), prerelease.text())
        self.assertFalse(self.world.fanout_matches(prerelease, "1.0.0-rc.1", False))
        self.assertTrue(self.world.fanout_matches(stable, "1.0.0", False), stable.text())
        self.assertFalse(self.world.fanout_matches(stable, "1.0.0", True))

    def test_oracle_rejects_an_application_dispatch_on_a_prerelease(self):
        # Only the decision's first condition changes: every guard stays, so the whole run is green.
        result = self.fanout(dry_run.DECISION_CONJUNCTION, dry_run.DECISION_MUTANT)
        self.assertTrue(result.ok, result.text())
        self.assertEqual([call["repo"] for call in result.calls(tool="gh", action="dispatch")],
                         list(dry_run.CONSUMERS))
        self.assertFalse(self.world.fanout_matches(result, "1.0.0-rc.1", True))


class SyntheticEditTest(unittest.TestCase):
    """The harness edits copies of the real workflow only where it finds exactly one target."""

    @classmethod
    def setUpClass(cls):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"
        cls.text = workflow.read_text(encoding="utf-8")

    def test_the_dispatch_guard_is_rewritten_in_place_not_duplicated(self):
        edited = dry_run.replace_in_step(self.text, dry_run.DISPATCH_STEP, dry_run.DISPATCH_GUARD,
                                         "        if: false\n", "a skipped dispatch")
        step = edited[edited.index(f"- name: {dry_run.DISPATCH_STEP}"):]
        self.assertEqual(step.count("\n        if:"), 1)
        self.assertEqual(step.count("\n        if: false\n"), 1)
        self.assertEqual(edited.count(dry_run.DISPATCH_GUARD), self.text.count(dry_run.DISPATCH_GUARD) - 1)

    def test_the_decision_mutant_changes_only_its_conjunction(self):
        edited = dry_run.replace_in_step(self.text, dry_run.DECISION_STEP, dry_run.DECISION_CONJUNCTION,
                                         dry_run.DECISION_MUTANT, "an application dispatch")
        self.assertEqual(edited.count(dry_run.DECISION_MUTANT), 1)
        self.assertEqual(edited.count(dry_run.DECISION_CONJUNCTION), 0)
        self.assertEqual(edited.count(dry_run.DISPATCH_GUARD), self.text.count(dry_run.DISPATCH_GUARD))

    def test_a_missing_or_repeated_target_is_a_harness_error(self):
        for label, step, old in (("a missing step", "No such step", dry_run.DISPATCH_GUARD),
                                 ("a missing guard", dry_run.DISPATCH_STEP, "        if: no such guard\n"),
                                 ("a repeated target", dry_run.DISPATCH_STEP, "\n")):
            with self.subTest(label), self.assertRaisesRegex(RuntimeError, "cannot inject"):
                dry_run.replace_in_step(self.text, step, old, "x", "a test edit")
        with self.assertRaisesRegex(RuntimeError, "cannot inject"):
            dry_run.replace_once(self.text, "no such text", "x", "a test edit")
        with self.assertRaisesRegex(RuntimeError, "cannot inject"):
            dry_run.replace_once(self.text + self.text, "  notify-consumers:\n", "x", "a test edit")


if __name__ == "__main__":
    unittest.main()
