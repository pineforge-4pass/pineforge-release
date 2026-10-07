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

    def notify(self, consumer, secret=None, **kwargs):
        matrix = {"consumer": consumer}
        if secret:
            matrix["repository_secret"] = secret
        return wfrun.run_job(self.workflow, "notify-consumers", event={"ref": "refs/tags/v1.0.0"},
                             repo="pineforge-4pass/pineforge-release", workdir=self.root, state=self.state,
                             event_name="push", ref="refs/tags/v1.0.0", matrix=matrix,
                             needs={"publish": {"result": "success", "outputs": {"prerelease": "false"}}},
                             echo=None, **kwargs)

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


if __name__ == "__main__":
    unittest.main()
