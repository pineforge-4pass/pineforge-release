#!/usr/bin/env python3
"""Focused expression, dummy-environment and fail-closed runner regressions."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
