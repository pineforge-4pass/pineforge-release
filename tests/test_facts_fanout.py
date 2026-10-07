"""Real fanout CLI and workflow acceptance; only the GitHub boundary is fake."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import test_facts_descriptions as descriptions
from test_release_workflows import _step

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/facts_fanout.py"
WORKFLOW = ROOT / ".github/workflows/facts-fanout.yml"


class FanoutCLI(unittest.TestCase):
    def setUp(self):
        # Reuse the existing immutable-source HTTP fixture, including its blob
        # corruption controls. Extend only the new external POST endpoint.
        self.github = descriptions.DescriptionsCLI("runTest")
        self.github.setUp()
        self.addCleanup(self.github.doCleanups)
        self.directory = self.github.directory
        self.calls = self.github.calls
        self.env = dict(self.github.env, FACTS_WEB_REPOSITORY="test-consumer",
                        PINEFORGE_APP_ID="test-app", PINEFORGE_APP_PRIVATE_KEY="test-key",
                        FACTS_DISPATCH_TOKEN="test-dispatch", FACTS_WEB_DISPATCH_ENABLED="true")
        self.env.pop("FACTS_WRITE_TOKEN", None)
        self.env.pop("FACTS_DESCRIPTIONS_APPLY_ENABLED", None)
        self.post_status = 204
        self.disconnect = False
        self.read_status = None
        self.main_reads = 0
        self.move_at = None
        self.proof_index = 0
        test = self
        handler = self.github.server.RequestHandlerClass

        class GitHub(handler):
            def do_GET(self):
                if self.path.endswith("/commits/main"):
                    test.main_reads += 1
                    if test.main_reads == test.move_at:
                        test.github.commit = "b" * 40
                if test.read_status:
                    test.calls.append(("GET", self.path, b"", self.headers.get("Authorization")))
                    self.send_response(test.read_status)
                    self.send_header("Location", "https://private.invalid/test-consumer")
                    self.end_headers()
                    self.wfile.write(b"private.invalid/test-consumer sensitive error body")
                else:
                    super().do_GET()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                test.calls.append(("POST", self.path, body, self.headers.get("Authorization")))
                if test.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(test.post_status)
                self.send_header("Location", "https://private.invalid/test-consumer")
                self.end_headers()
                if test.post_status != 204:
                    self.wfile.write(b"private.invalid/test-consumer sensitive error body")

        self.github.server.RequestHandlerClass = GitHub

    def invoke(self, command, *args, env=None):
        argv = [sys.executable, str(SCRIPT), command, "--test-api", self.github.api, *args]
        result = subprocess.run(argv, env=self.env if env is None else env,
                                capture_output=True, text=True, timeout=30)
        if os.environ.get("FACTS_FANOUT_PROOF_DIR"):
            directory = Path(os.environ["FACTS_FANOUT_PROOF_DIR"]) / self.id().split(".")[-1]
            directory.mkdir(parents=True, exist_ok=True)
            self.proof_index += 1
            (directory / f"{self.proof_index:03}.json").write_text(json.dumps({
                "argv": argv, "exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr,
                "calls": [{"method": c[0], "path": c[1], "body": c[2].decode(),
                           "authorization": c[3]} for c in self.calls]}, indent=2) + "\n")
        return result

    def prepare(self, dry_run="false", env=None, output="source"):
        return self.invoke("prepare", "--facts", str(descriptions.FACTS), "--dry-run", dry_run,
                           "--output-dir", str(self.directory / output),
                           "--github-output", str(self.directory / "outputs"), env=env)

    def dispatch(self, mint="success", env=None):
        return self.invoke("dispatch", "--source-dir", str(self.directory / "source"),
                           "--mint-outcome", mint, "--receipt", str(self.directory / "receipt.json"),
                           env=env)

    def prepared(self, dry_run="false", env=None):
        result = self.prepare(dry_run, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def no_post(self):
        self.assertFalse(any(c[0] != "GET" for c in self.calls))

    def assert_redacted(self, result):
        text = result.stdout + result.stderr
        for secret in ("test-consumer", "test-key", "test-dispatch", "private.invalid", "sensitive error body"):
            self.assertNotIn(secret, text)

    def test_current_source_exact_payload_post_and_repeated_signal(self):
        source = self.prepared()
        expected = {"commit": self.github.commit,
                    "sha256": hashlib.sha256(descriptions.FACTS.read_bytes()).hexdigest(), "dry_run": False}
        self.assertEqual(source["commit"], expected["commit"])
        self.assertNotEqual(source["commit"], self.env["GITHUB_SHA"])
        self.assertEqual(json.loads((self.directory / "source/payload.json").read_bytes()), expected)
        facts_before = descriptions.FACTS.read_bytes()
        for _ in range(2):
            result = self.dispatch()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "dispatched")
            self.assertEqual(receipt["http_status"], 204)
            self.assertEqual(receipt["commit"], expected["commit"])
            self.assertEqual(receipt["sha256"], expected["sha256"])
            self.assertFalse(receipt["installed"])
            self.assert_redacted(result)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        exact = json.dumps({"event_type": "facts-update", "client_payload": expected},
                           separators=(",", ":"), sort_keys=True).encode()
        for call in posts:
            self.assertEqual(call[1], "/repos/pineforge-4pass/test-consumer/dispatches")
            self.assertEqual(call[2], exact)
            self.assertEqual(call[3], "Bearer test-dispatch")
        self.assertEqual(descriptions.FACTS.read_bytes(), facts_before)
        self.assertTrue(all(c[0] in ("GET", "POST") for c in self.calls))

    def test_disabled_dryrun_and_missing_setup_emit_plan_without_mint_or_post(self):
        for index, gate in enumerate((None, "", "false")):
            env = dict(self.env, FACTS_WEB_REPOSITORY="", PINEFORGE_APP_ID="",
                       PINEFORGE_APP_PRIVATE_KEY="", FACTS_DISPATCH_TOKEN="")
            env.pop("FACTS_WEB_DISPATCH_ENABLED", None)
            if gate is not None:
                env["FACTS_WEB_DISPATCH_ENABLED"] = gate
            result = self.prepare("true", env, f"source-{index}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "setup-required")
            self.assertFalse(receipt["mint_allowed"])
            self.assertTrue(receipt["dry_run"])
            self.assertFalse(receipt["installed"])
            self.assertTrue(receipt["alert"])
        self.assertNotIn("mint_allowed=true", (self.directory / "outputs").read_text())
        self.no_post()

    def test_disabled_with_ready_setup_cannot_dispatch_even_false_payload(self):
        env = dict(self.env, FACTS_WEB_DISPATCH_ENABLED="false", FACTS_DISPATCH_TOKEN="")
        receipt = self.prepared(env=env)
        self.assertFalse(receipt["mint_allowed"])
        result = self.dispatch(mint="skipped", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "planned")
        self.no_post()

    def test_enabled_dispatch_preserves_true_dry_run_for_receiver(self):
        self.prepared(dry_run="true")
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        post = next(c for c in self.calls if c[0] == "POST")
        self.assertIs(json.loads(post[2])["client_payload"]["dry_run"], True)

    def test_invalid_targets_fail_before_source_or_mint(self):
        for target in ("", "other/repo", "pineforge-4pass/repo", "https://private.invalid/repo",
                       "a,b", "a\nb", "a\rb", "a\tb", " a", "a ", ".", "..", "a%2fb", "a\\b", "é", "a" * 101):
            with self.subTest(target=repr(target)):
                self.calls.clear()
                result = self.prepare(env=dict(self.env, FACTS_WEB_REPOSITORY=target))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.calls, [])
                self.assertFalse((self.directory / "source").exists())
                self.assertFalse((self.directory / "outputs").exists())
                self.assert_redacted(result)

    def test_payload_unknown_keys_missing_keys_strict_types_and_duplicate_keys(self):
        self.prepared()
        path = self.directory / "source/payload.json"
        original = json.loads(path.read_bytes())
        malformed = [dict(original, target="test-consumer"), {k: v for k, v in original.items() if k != "sha256"}]
        for field in original:
            for value in (None, 0, 1, [], {}, "true" if field == "dry_run" else "bad"):
                malformed.append(dict(original, **{field: value}))
        for payload in malformed:
            with self.subTest(payload=payload):
                self.calls.clear()
                path.write_text(json.dumps(payload))
                result = self.dispatch()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.calls, [])
                self.assert_redacted(result)
        path.write_text(json.dumps(original)[:-1] + ',"dry_run":false}')
        self.assertEqual(self.dispatch().returncode, 2)
        self.no_post()

    def test_bad_blob_schema_semantics_or_private_source_never_prepares_mint(self):
        original = dict(self.github.files)
        cases = ("blob", "schema", "semantics", "private", "identity", "main-sha")
        for case in cases:
            with self.subTest(case=case):
                self.calls.clear()
                self.github.files = dict(original)
                self.github.bad_blob = case == "blob"
                self.github.commit = "bad" if case == "main-sha" else "a" * 40
                self.github.state[descriptions.HUB].update(
                    private=case == "private",
                    full_name="untrusted/identity" if case == "identity" else descriptions.HUB)
                if case == "schema":
                    self.github.files["facts/facts.schema.json"] = b"{}"
                if case == "semantics":
                    facts = json.loads(original["facts/facts.json"])
                    facts["scoreboard"]["graded"] += 1
                    self.github.files["facts/facts.json"] = json.dumps(facts).encode()
                result = self.prepare()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((self.directory / "source").exists())
                self.assertFalse((self.directory / "outputs").exists())
                self.no_post()

    def test_snapshot_hash_or_schema_tamper_fails_without_calls(self):
        self.prepared()
        for name in ("facts.json", "facts.schema.json"):
            path = self.directory / "source" / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            self.calls.clear()
            result = self.dispatch()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(self.calls, [])
            path.write_bytes(original)

    def test_source_movement_before_mint_and_before_post_holds_then_stale_wakeup_recovers(self):
        self.move_at = 2
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "superseded")
        self.assertNotIn("mint_allowed=true", (self.directory / "outputs").read_text())
        self.assertFalse((self.directory / "source").exists())
        self.move_at = None
        self.prepared()
        self.github.commit = "c" * 40
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "superseded")
        self.no_post()
        result = self.prepare(output="recovered")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["commit"], self.github.commit)

    def test_final_main_recheck_after_immutable_reads_prevents_post(self):
        self.prepared()
        self.move_at = self.main_reads + 2
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "superseded")
        self.no_post()

    def test_mint_failures_missing_token_and_context_fail_visibly(self):
        self.prepared()
        for mint, change in (("failure", {}), ("skipped", {}), ("success", {"FACTS_DISPATCH_TOKEN": ""}),
                             ("success", {"GITHUB_REF": "refs/heads/topic"}),
                             ("success", {"GITHUB_REF_PROTECTED": "false"}),
                             ("success", {"GITHUB_EVENT_NAME": "pull_request"})):
            self.calls.clear()
            result = self.dispatch(mint, dict(self.env, **change))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["alert"])
            self.assertFalse(json.loads(result.stdout)["permission_unavailable_proven"])
            self.no_post()
            self.assert_redacted(result)

    def test_http_failures_redirects_network_errors_and_unexpected_success_are_redacted(self):
        self.prepared()
        for status in (200, 202, 301, 401, 403, 404, 422, 429, 500, 503):
            self.post_status = status
            result = self.dispatch()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["http_status"], status)
            self.assertFalse(receipt["permission_unavailable_proven"])
            self.assertTrue(receipt["alert"])
            self.assert_redacted(result)
        self.disconnect = True
        result = self.dispatch()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["category"], "network")
        self.assert_redacted(result)

    def test_source_http_failure_keeps_status_and_no_post(self):
        for status in (401, 403, 429, 503):
            self.read_status = status
            result = self.prepare()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["http_status"], status)
            self.assert_redacted(result)
            self.no_post()

    def test_actual_workflow_prepare_and_dispatch_shells(self):
        text = WORKFLOW.read_text()
        env = dict(self.env, RUNNER_TEMP=str(self.directory),
                   GITHUB_OUTPUT=str(self.directory / "outputs"), DRY_RUN="true",
                   FACTS_WEB_DISPATCH_ENABLED="false", FACTS_READ_TOKEN="test-read",
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        # Execute the literal workflow shell. Only point the HTTP transport at
        # the existing external fixture; do not replace the parser or helper.
        for name, enabled in (("Validate source and plan dispatch", False),
                              ("Send the validated signal and record its status", True)):
            script = textwrap.dedent(_step(text, name).split("run: |\n", 1)[1])
            script = script.rstrip() + ' --test-api "$TEST_API"\n'
            if enabled:
                env.update(FACTS_WEB_DISPATCH_ENABLED="true", MINT_OUTCOME="success")
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    cwd=ROOT, env=dict(env, TEST_API=self.github.api),
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_redacted(result)
            if not enabled:
                self.assertEqual((self.directory / "outputs").read_text(), "mint_allowed=false\n")
                self.no_post()
        self.assertTrue((self.directory / "fanout-audit/prepare.json").exists())
        self.assertTrue((self.directory / "fanout-audit/dispatch.json").exists())
        self.assertEqual(len([c for c in self.calls if c[0] == "POST"]), 1)

    def test_missing_local_input_and_enabled_missing_credentials_stop_before_calls(self):
        for change in ({"PINEFORGE_APP_ID": ""}, {"PINEFORGE_APP_PRIVATE_KEY": ""},
                       {"GITHUB_REF": "refs/heads/topic"}, {"GITHUB_REF_PROTECTED": "false"},
                       {"GITHUB_EVENT_NAME": "pull_request"}, {"FACTS_WEB_DISPATCH_ENABLED": "yes"}):
            result = self.prepare(env=dict(self.env, **change))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)["mint_allowed"])
            self.assertEqual(self.calls, [])
        result = self.invoke("prepare", "--facts", str(self.directory / "absent"),
                             "--output-dir", str(self.directory / "source"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls, [])

    def test_authoritative_bytes_and_payload_digest_must_match_before_post(self):
        self.prepared()
        self.github.files["facts/facts.json"] += b"\n"
        result = self.dispatch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.no_post()

    def test_test_transport_rejects_real_credentials_and_non_loopback_origins(self):
        result = self.prepare(env=dict(self.env, FACTS_READ_TOKEN="not-a-test-credential"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls, [])
        for origin in ("https://private.invalid", "http://localhost:1234", "http://127.0.0.1:1234/path"):
            result = subprocess.run([sys.executable, str(SCRIPT), "prepare", "--facts", str(descriptions.FACTS),
                                     "--output-dir", str(self.directory / "source"), "--test-api", origin],
                                    env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assert_redacted(result)

    def test_real_parser_rejects_unknown_repeated_and_wrong_type_options_safely(self):
        for args in (("--dry-run", "test-consumer"), ("--unknown", "test-consumer"),
                     ("--dry-run", "true", "--dry-run", "false")):
            result = self.invoke("prepare", "--facts", str(descriptions.FACTS),
                                 "--output-dir", str(self.directory / "source"), *args)
            self.assertEqual(result.returncode, 2)
            self.assert_redacted(result)
            self.assertEqual(self.calls, [])


class FanoutWorkflow(unittest.TestCase):
    def test_protected_independent_serialized_workflow_and_scoped_action(self):
        text = WORKFLOW.read_text()
        descriptions_text = (WORKFLOW.parent / "facts-descriptions.yml").read_text()
        self.assertIn("branches: [main]", text)
        self.assertIn("'facts/facts.json'", text)
        self.assertIn("default: true", text)
        self.assertIn("cron: '7,22,37,52 * * * *'", text)
        self.assertIn("group: facts-web-dispatch", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertNotIn("facts-web-dispatch", descriptions_text)
        self.assertNotIn("needs:", text)
        self.assertIn("environment: release-automation", text)
        self.assertIn("contents: read", text)
        self.assertIn("ref: main", text)
        self.assertIn("persist-credentials: false", text)
        mint = _step(text, "Mint consumer token")
        self.assertIn("steps.source.outputs.mint_allowed == 'true'", mint)
        self.assertIn("steps.source.outcome == 'success'", mint)
        self.assertIn("github.ref_protected", mint)
        self.assertIn("vars.FACTS_WEB_DISPATCH_ENABLED == 'true'", mint)
        self.assertIn("actions/create-github-app-token@v2", mint)
        self.assertIn("repositories: ${{ secrets.FACTS_WEB_REPOSITORY }}", mint)
        self.assertIn("owner: pineforge-4pass", mint)
        self.assertIn("permission-contents: write", mint)
        self.assertIn("github-api-url: https://api.github.com", mint)
        self.assertNotIn("permission-administration", text)
        self.assertNotIn("DESCRIPTIONS_APP", text)
        self.assertNotIn("CLOUDFLARE", text)
        self.assertNotIn("wrangler", text)
        self.assertIn("if-no-files-found: error", text)
        self.assertIn("!cancelled()", text)
        self.assertLess(text.index("scripts/facts_fanout.py prepare"), text.index("actions/create-github-app-token"))

    def test_prepare_shell_is_real_cli_and_receipt_is_required(self):
        text = WORKFLOW.read_text()
        body = _step(text, "Validate source and plan dispatch")
        self.assertIn('PINEFORGE_APP_ID: ${{ secrets.PINEFORGE_APP_ID }}', body)
        self.assertIn('PINEFORGE_APP_PRIVATE_KEY: ${{ secrets.PINEFORGE_APP_PRIVATE_KEY }}', body)
        self.assertIn('FACTS_WEB_REPOSITORY: ${{ secrets.FACTS_WEB_REPOSITORY }}', body)
        script = textwrap.dedent(body.split("run: |\n", 1)[1])
        self.assertIn('python3 scripts/facts_fanout.py prepare', script)
        self.assertIn('--github-output "$GITHUB_OUTPUT"', script)
        self.assertIn('--receipt "$RUNNER_TEMP/fanout-audit/prepare.json"', script)
        self.assertIn('toJSON(inputs.dry_run)', text)
        self.assertNotIn("&& inputs.dry_run", text)  # False must survive expression evaluation.


class FanoutStatusCLI(unittest.TestCase):
    def invoke(self, runs):
        return subprocess.run([sys.executable, str(SCRIPT), "status"],
                              input=json.dumps({"workflow_runs": runs}),
                              capture_output=True, text=True, timeout=30)

    def run_record(self, **changes):
        record = {"id": 7, "run_number": 2, "head_branch": "main", "status": "completed",
                  "conclusion": "success", "created_at": datetime.now(timezone.utc).isoformat(),
                  "path": ".github/workflows/facts-fanout.yml"}
        return dict(record, **changes)

    def test_bounded_run_status_never_claims_installation(self):
        result = self.invoke([self.run_record()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "workflow-observed")
        self.assertFalse(receipt["installed"])
        self.assertEqual(receipt["served_freshness"], "unverified")

    def test_missing_stale_failed_stalled_and_invalid_telemetry_never_healthy(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        cases = ([], [self.run_record(created_at=old)], [self.run_record(conclusion="failure")],
                 [self.run_record(conclusion="cancelled")],
                 [self.run_record(status="queued", conclusion=None, created_at=old)],
                 [self.run_record(id="private.invalid/test-consumer")],
                 [self.run_record(path="private.invalid/test-consumer")],
                 [self.run_record()] * 21)
        for records in cases:
            result = self.invoke(records)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["alert"])
            self.assertNotIn("private.invalid", result.stdout + result.stderr)
            self.assertNotIn("test-consumer", result.stdout + result.stderr)

    def test_cancelled_newer_run_does_not_alert_or_hide_recent_failure(self):
        result = self.invoke([self.run_record(id=8, conclusion="cancelled"), self.run_record()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.invoke([self.run_record(), self.run_record(id=6, conclusion="failure")])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["failed_runs"], [6])


if __name__ == "__main__":
    unittest.main()
