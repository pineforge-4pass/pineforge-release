"""Subprocess integration against GitHub's HTTP boundary, never live writes."""
import base64
import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
HUB = "pineforge-4pass/pineforge-release"
FACTS = ROOT / "facts/facts.json"
POLICY = ROOT / "facts/repo-descriptions.json"


class DescriptionsCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calls = []
        self.commit = "a" * 40
        self.files = {name: (ROOT / name).read_bytes() for name in
                      ("facts/facts.json", "facts/repo-descriptions.json", "facts/facts.schema.json")}
        rendered = subprocess.run([sys.executable, str(ROOT / "scripts/repo_descriptions.py"),
                                   "render", "--facts", str(FACTS)], capture_output=True, text=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.expected = json.loads(rendered.stdout)["repositories"]
        self.state = {row["repo"]: {"full_name": row["repo"].swapcase(), "private": False,
                                    "description": "old public description"} for row in self.expected}
        self.fail_patch = None
        self.disconnect_patch = False
        self.fail_read = False
        self.bad_blob = False
        self.mismatch = False
        self.private_readback = False
        self.move_on_read = False
        self.patch_count = 0
        test = self

        class GitHub(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond("GET")

            def do_PATCH(self):
                self.respond("PATCH")

            def respond(self, method):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                test.calls.append((method, self.path, body, self.headers.get("Authorization")))
                url = urlsplit(self.path)
                code = 200
                if url.path == f"/repos/{HUB}/commits/main":
                    data = {"sha": test.commit}
                elif url.path.startswith(f"/repos/{HUB}/contents/"):
                    name = url.path.split("/contents/", 1)[1]
                    if parse_qs(url.query).get("ref") != [test.commit]:
                        code, data = 404, {"message": "immutable commit missing"}
                    else:
                        raw = test.files[name]
                        data = {"type": "file", "path": name, "encoding": "base64",
                                "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
                                "content": base64.b64encode(raw).decode()}
                        if test.bad_blob:
                            data["sha"] = "0" * 40
                elif url.path.removeprefix("/repos/").casefold() in test.state:
                    repo = url.path.removeprefix("/repos/").casefold()
                    if method == "PATCH":
                        test.patch_count += 1
                        if test.disconnect_patch:
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                        if test.fail_patch:
                            code, data = test.fail_patch, {"message": "refused; sensitive diagnostics omitted"}
                        else:
                            payload = json.loads(body)
                            if not test.mismatch:
                                test.state[repo]["description"] = payload["description"]
                            if test.private_readback:
                                test.state[repo]["private"] = True
                            data = test.state[repo]
                    else:
                        data = test.state[repo]
                        if test.fail_read:
                            code, data = 503, {"message": "unavailable"}
                        if test.move_on_read and repo != HUB:
                            test.commit = "b" * 40
                else:
                    code, data = 404, {"message": "not found"}
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.api = f"http://127.0.0.1:{self.server.server_port}"
        self.env = dict(os.environ, FACTS_READ_TOKEN="test-read", FACTS_WRITE_TOKEN="test-write",
                        GITHUB_REF="refs/heads/main", GITHUB_REF_PROTECTED="true",
                        GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_SHA="0" * 40,
                        FACTS_DESCRIPTIONS_APPLY_ENABLED="true", GH_HOST="ignored.invalid")

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def invoke(self, command, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/facts_descriptions.py"),
                               command, "--test-api", self.api, *args], capture_output=True,
                              text=True, env=self.env if env is None else env, timeout=30)

    def prepare(self, facts=FACTS, policy=POLICY):
        result = self.invoke("prepare", "--facts", str(facts), "--policy", str(policy),
                             "--output-dir", str(self.directory / "source"))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def reconcile(self, dry_run="false", setup="ready", mint="success", env=None):
        return self.invoke("reconcile", "--source-dir", str(self.directory / "source"),
                           "--dry-run", dry_run, "--setup", setup, "--mint-outcome", mint, env=env)

    def test_current_main_stale_event_exact_patch_readback_holds_and_duplicate_noop(self):
        source = self.prepare()
        self.assertEqual(source["source_commit"], self.commit)
        self.assertEqual(source["facts_sha256"], hashlib.sha256(FACTS.read_bytes()).hexdigest())
        self.assertNotEqual(source["source_commit"], self.env["GITHUB_SHA"])
        result = self.reconcile()
        self.assertEqual(result.returncode, 1, result.stderr)  # audited static/HOLD drift is an alert
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["source_commit"], self.commit)
        patches = [call for call in self.calls if call[0] == "PATCH"]
        managed = [row for row in self.expected if row["disposition"] == "managed"]
        self.assertEqual(len(patches), len(managed))
        self.assertEqual(patches[0][1], "/repos/" + managed[0]["repo"])
        for call, row in zip(patches, managed):
            self.assertEqual(call[1], "/repos/" + row["repo"])
            self.assertEqual(json.loads(call[2]), {"description": row["expected"]})
            self.assertEqual(call[2], json.dumps({"description": row["expected"]},
                                                ensure_ascii=True, separators=(",", ":")).encode())
            self.assertEqual(call[3], "Bearer test-write")
            index = self.calls.index(call)
            self.assertEqual(self.calls[index + 1][:2], ("GET", call[1]))
        self.calls.clear()
        second = self.reconcile()
        self.assertEqual(second.returncode, 1)
        self.assertFalse(any(call[0] == "PATCH" for call in self.calls))
        self.assertTrue(all(row["status"] == "match" for row in json.loads(second.stdout)["repositories"]
                            if row["disposition"] == "managed"))

    def test_dry_run_and_authorization_gates_never_patch(self):
        self.prepare()
        for change in ({}, {"GITHUB_REF": "refs/heads/topic"}, {"GITHUB_REF_PROTECTED": "false"},
                       {"GITHUB_EVENT_NAME": "pull_request"}, {"FACTS_DESCRIPTIONS_APPLY_ENABLED": ""}):
            with self.subTest(change=change):
                self.calls.clear()
                result = self.reconcile(dry_run="true" if not change else "false",
                                        env=dict(self.env, **change))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[0] == "PATCH" for call in self.calls))

    def test_mint_failure_is_unclassified_error_and_missing_setup_is_report_only(self):
        self.prepare()
        for setup, mint, diagnostic, code in (("missing", "skipped", "missing-setup", 1),
                                              ("ready", "failure", "mint-failure-unclassified", 2),
                                              ("ready", "skipped", "mint-not-attempted", 2)):
            with self.subTest(mint=mint, setup=setup):
                self.calls.clear()
                result = self.reconcile(setup=setup, mint=mint)
                self.assertEqual(result.returncode, code, result.stderr)
                document = json.loads(result.stdout)
                self.assertEqual(document["diagnostic"], diagnostic)
                self.assertFalse(document["permission_unavailable_proven"])
                self.assertTrue(document["alert"])
                self.assertFalse(any(call[0] == "PATCH" for call in self.calls))
                self.assertNotIn("test-write", result.stdout + result.stderr)

    def test_successful_mint_does_not_reclassify_patch_403_429_or_server_failure(self):
        self.prepare()
        for status in (403, 429, 500):
            with self.subTest(status=status):
                self.fail_patch = status
                result = self.reconcile()
                self.assertEqual(result.returncode, 2, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertFalse(receipt["permission_unavailable_proven"])
                self.assertIn("error", [row["status"] for row in receipt["repositories"]])
                self.assertNotIn("sensitive diagnostics", result.stdout)

    def test_readback_mismatch_and_visibility_change_fail(self):
        self.prepare()
        self.mismatch = True
        result = self.reconcile()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("readback", result.stdout)
        self.mismatch = False
        self.private_readback = True
        result = self.reconcile()
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_source_movement_before_first_write_is_superseded_without_alert(self):
        self.prepare()
        self.move_on_read = True
        result = self.reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["status"], "superseded")
        self.assertFalse(document["alert"])
        self.assertFalse(any(call[0] == "PATCH" for call in self.calls))

    def test_new_run_after_supersession_converges_to_newest_source(self):
        self.prepare()
        self.move_on_read = True
        self.assertEqual(json.loads(self.reconcile().stdout)["status"], "superseded")
        self.move_on_read = False
        fresh = self.directory / "new-source"
        result = self.invoke("prepare", "--facts", str(FACTS), "--output-dir", str(fresh))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["source_commit"], self.commit)
        result = self.invoke("reconcile", "--source-dir", str(fresh), "--dry-run", "false",
                             "--setup", "ready", "--mint-outcome", "success")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.patch_count, 3)

    def test_equal_descriptions_and_dry_run_match_are_success_without_mutations(self):
        for row in self.expected:
            self.state[row["repo"]]["description"] = row["expected"]
        self.prepare()
        for dry in ("true", "false"):
            result = self.reconcile(dry_run=dry)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["alert"])
        self.assertEqual(self.patch_count, 0)

    def test_prepare_uses_current_main_bytes_instead_of_stale_checkout(self):
        facts = json.loads(FACTS.read_text())
        facts["scoreboard"] = copy.deepcopy(facts["releases"]["1.3.0"]["scoreboard"])
        raw = json.dumps(facts).encode()
        self.files["facts/facts.json"] = raw
        source = self.prepare()
        self.assertEqual(source["facts_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(source["facts_sha256"], hashlib.sha256(FACTS.read_bytes()).hexdigest())
        self.assertEqual((self.directory / "source/facts.json").read_bytes(), raw)
        result = self.reconcile(dry_run="true")
        self.assertEqual(result.returncode, 1)
        engine = next(row for row in json.loads(result.stdout)["repositories"] if row["role"] == "engine")
        self.assertIn(f'{facts["scoreboard"]["excellent"]:,} excellent', engine["expected"])
        self.assertEqual(self.patch_count, 0)

    def test_remote_invalid_facts_or_blob_digest_fail_before_artifacts_or_consumer_calls(self):
        for malformed in (True, False):
            with self.subTest(malformed=malformed):
                self.calls.clear()
                self.bad_blob = not malformed
                self.files["facts/facts.json"] = b'{}' if malformed else FACTS.read_bytes()
                result = self.invoke("prepare", "--facts", str(FACTS),
                                     "--output-dir", str(self.directory / "source"))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.directory / "source").exists())
                self.assertTrue(all("/contents/" in call[1] or call[1].endswith("/commits/main")
                                    or call[1] == "/repos/" + HUB
                                    for call in self.calls))

    def test_nonpublic_hub_source_is_refused_before_snapshot_output(self):
        self.state[HUB]["private"] = True
        result = self.invoke("prepare", "--facts", str(FACTS),
                             "--output-dir", str(self.directory / "source"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.directory / "source").exists())
        self.assertFalse(any("/contents/" in call[1] for call in self.calls))

    def test_network_error_after_mint_and_failed_read_never_succeed(self):
        self.prepare()
        self.disconnect_patch = True
        result = self.reconcile()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("network failure", result.stdout)
        self.fail_read = True
        self.calls.clear()
        result = self.reconcile()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any(call[0] == "PATCH" for call in self.calls))

    def test_nonpublic_or_wrong_identity_stops_all_writes_without_leaking_api_identity(self):
        self.prepare()
        target = self.expected[-1]["repo"]
        for mutation in ({"private": True}, {"private": False, "full_name": "untrusted/identity"}):
            self.state[target].update(mutation)
            self.calls.clear()
            result = self.reconcile()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn("untrusted/identity", result.stdout + result.stderr)
            self.assertFalse(any(call[0] == "PATCH" for call in self.calls))

    def test_targets_are_engine_first_and_exclude_audit_rows_even_if_policy_is_reordered(self):
        policy = json.loads(POLICY.read_text())
        policy["repositories"].reverse()
        self.files["facts/repo-descriptions.json"] = json.dumps(policy).encode()
        output = self.directory / "outputs"
        result = self.invoke("prepare", "--facts", str(FACTS), "--output-dir", str(self.directory / "source"),
                             "--github-output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
        targets = lines["repositories"].split(",")
        managed = [row for row in self.expected if row["disposition"] == "managed"]
        self.assertEqual(targets, [row["repo"].split("/")[1] for row in managed])
        self.assertEqual(self.reconcile().returncode, 1)
        self.assertEqual([call[1] for call in self.calls if call[0] == "PATCH"],
                         ["/repos/" + row["repo"] for row in managed])

    def test_invalid_inputs_fail_without_output_or_calls(self):
        bad = self.directory / "bad.json"
        bad.write_text('{"schema": "bad"}')
        for option in ("--facts", "--policy"):
            self.calls.clear()
            args = {"--facts": str(FACTS), "--policy": str(POLICY)}
            args[option] = str(bad)
            result = self.invoke("prepare", *[item for pair in args.items() for item in pair],
                                 "--output-dir", str(self.directory / "source"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.calls, [])
            self.assertFalse((self.directory / "source").exists())
        self.prepare()
        self.calls.clear()
        (self.directory / "source/facts.json").write_text(bad.read_text())
        result = self.reconcile()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls, [])

    def test_policy_casing_and_api_casing_are_independent(self):
        policy = json.loads(POLICY.read_text())
        for row in policy["repositories"]:
            row["repo"] = row["repo"].swapcase()
        self.files["facts/repo-descriptions.json"] = json.dumps(policy).encode()
        self.prepare()
        self.assertEqual(self.reconcile().returncode, 1)
        self.assertEqual(self.patch_count, 3)

    def test_policy_cannot_promote_static_or_retarget_a_public_role(self):
        for role, change in (("release", {"disposition": "managed"}),
                             ("engine", {"repo": "example/other"})):
            self.calls.clear()
            policy = json.loads(POLICY.read_text())
            next(row for row in policy["repositories"] if row["role"] == role).update(change)
            path = self.directory / "policy.json"
            path.write_text(json.dumps(policy))
            result = self.invoke("prepare", "--facts", str(FACTS), "--policy", str(path),
                                 "--output-dir", str(self.directory / "source"))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
