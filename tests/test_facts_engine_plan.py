"""Native loopback HTTP and Node 20 controller tests; no live services."""
import base64
import copy
import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from facts_engine_plan import (API_ORIGIN, ENGINE, HUB, OWNER, SCHEMA, PublicGitHub,
                               build_engine_plan, checked_render, render_with_node)
from facts_validate import InputError, MAX_INPUT_BYTES

FACTS = ROOT / "tests/fixtures/repo-descriptions-facts.json"
CLI = ROOT / "scripts/facts_engine_plan.py"
BRIDGE = ROOT / "scripts/facts_engine_render.mjs"
TOKEN = "test-engine-reader-do-not-echo"
PRIVATE_TEXT = "untrusted-response-/home/private/credential-do-not-echo"
README = (
    "\ufeff# Engine\r\n\r\nUnmarked café — exact  spaces.\r\n"
    "Inventory trades: <!-- pf:inventory.tvTrades|int -->old<!-- /pf --> trades.\r\n"
    "Commit: <!-- pf:inventory.sourceCommit|short-code -->old<!-- /pf -->.\r\n"
    "Release: <!-- pf:releases[1.1.0].source -->old<!-- /pf -->.\r\n"
    "\r\nUnmarked **tail** without a final newline"
).encode("utf-8")
# Handwritten values from the public fixture, independent of the production renderer.
EXPECTED = README.replace(b"-->old<!-- /pf --> trades.", b"-->4,776,328<!-- /pf --> trades.")
EXPECTED = EXPECTED.replace(b"-->old<!-- /pf -->.\r\nRelease:",
                            b"-->`d1d18867`<!-- /pf -->.\r\nRelease:")
EXPECTED = EXPECTED.replace(
    b"-->old<!-- /pf -->.\r\n\r\n",
    b"-->https://github.com/pineforge-4pass/pineforge-engine/blob/v1.1.0/README.md#validation-scoreboard"
    b"<!-- /pf -->.\r\n\r\n")


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def metadata(name, number, default="trunk"):
    owner, short = name.split("/")
    return {"id": number, "name": short, "full_name": name, "owner": {"login": owner},
            "private": False, "visibility": "public", "default_branch": default,
            "html_url": "https://github.com/" + name, "url": API_ORIGIN + "/repos/" + name}


def envelope(name, raw):
    return {"type": "file", "path": name, "name": name.rsplit("/", 1)[-1], "size": len(raw),
            "encoding": "base64", "content": base64.b64encode(raw).decode("ascii"),
            "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}


class EnginePlanHTTP(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = []
        self.counts = {}
        self.outputs = 0
        self.hub = "a" * 40
        self.target = "b" * 40
        self.head = "d" * 40
        self.hub_moved = False
        self.target_moved = False
        self.default_moved = False
        self.repos = {HUB: metadata(HUB, 11, "main"), ENGINE: metadata(ENGINE, 22)}
        self.files = {"facts/facts.json": FACTS.read_bytes(),
                      "facts/facts.schema.json": (ROOT / "facts/facts.schema.json").read_bytes(),
                      "VERSION": b"1.1.0\n"}
        self.readmes = {self.target: README, self.head: EXPECTED}
        self.refs = {"v1.0.1": {"type": "commit", "sha": "c" * 40},
                     "v1.1.0": {"type": "commit", "sha": "1" * 40},
                     "v1.2.0": {"type": "commit", "sha": "2" * 40}}
        self.tags = {}
        self.prs = []
        self.detail = None
        self.proposal_head = None
        self.transform = None
        self.raw_response = None
        self.status_code = 200
        self.omit_length = False
        self.extra_headers = {}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.calls.append(("GET", self.path, self.headers.get("Authorization")))
                test.counts[self.path] = test.counts.get(self.path, 0) + 1
                try:
                    data = test.route(self.path)
                    if test.transform:
                        data = test.transform(self.path, copy.deepcopy(data))
                    raw = test.raw_response if test.raw_response is not None else encode(data)
                    self.send_response(test.status_code)
                    self.send_header("Content-Type", "application/json")
                    if not test.omit_length and "Content-Length" not in test.extra_headers:
                        self.send_header("Content-Length", str(len(raw)))
                    for key, value in test.extra_headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                except (AssertionError, KeyError):
                    self.send_error(500)

            def refused_method(self):
                test.calls.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_error(405)

            do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = refused_method

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        # Registered last, so it checks the recorded requests before server cleanup.
        self.addCleanup(self.assert_get_only)

    def assert_get_only(self):
        self.assertTrue(all(call[0] == "GET" for call in self.calls), self.calls)

    def route(self, raw_path):
        parsed = urlsplit(raw_path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path in ("/repos/" + HUB, "/repos/" + ENGINE):
            name = path.removeprefix("/repos/")
            result = copy.deepcopy(self.repos[name])
            if name == ENGINE and self.default_moved and self.counts[raw_path] > 1:
                result["default_branch"] = "next/default"
            return result
        if path == f"/repos/{HUB}/git/ref/heads/main":
            value = "f" * 40 if self.hub_moved and self.counts[raw_path] > 1 else self.hub
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": value}}
        if path.startswith(f"/repos/{ENGINE}/git/ref/heads/"):
            name = path.split("/git/ref/heads/", 1)[1]
            assert name in (self.repos[ENGINE]["default_branch"], "next/default", self.proposal_head)
            if name == self.proposal_head:
                value = self.head
            else:
                value = "e" * 40 if self.target_moved and self.counts[raw_path] > 1 else self.target
            return {"ref": "refs/heads/" + name, "object": {"type": "commit", "sha": value}}
        if path.startswith(f"/repos/{ENGINE}/git/ref/tags/"):
            name = path.split("/git/ref/tags/", 1)[1]
            return {"ref": "refs/tags/" + name, "object": self.refs[name]}
        if path.startswith(f"/repos/{ENGINE}/git/tags/"):
            return self.tags[path.rsplit("/", 1)[-1]]
        if path.startswith(f"/repos/{HUB}/contents/"):
            assert query == {"ref": [self.hub]}
            name = path.split("/contents/", 1)[1]
            return envelope(name, self.files[name])
        if path.startswith(f"/repos/{ENGINE}/contents/"):
            assert path.endswith("/contents/README.md") and set(query) == {"ref"}
            return envelope("README.md", self.readmes[query["ref"][0]])
        if path == f"/repos/{ENGINE}/pulls":
            assert set(query) == {"head", "base", "state", "per_page", "page"}
            assert query["state"] == ["open"] and query["per_page"] == ["100"]
            assert query["base"] == [self.repos[ENGINE]["default_branch"]]
            assert query["head"][0].startswith(OWNER + ":automation/facts-engine-")
            return self.prs if query["page"] == ["1"] else []
        if path == f"/repos/{ENGINE}/pulls/7":
            return self.detail if self.detail is not None else self.prs[0]
        raise AssertionError("unexpected request")

    def environment(self, **extra):
        env = dict(os.environ)
        env.update(FACTS_READ_TOKEN=TOKEN, GITHUB_SHA="9" * 40,
                   GITHUB_EVENT_NAME="repository_dispatch", NODE_OPTIONS="--invalid-option-must-not-reach-bridge")
        env.update(extra)
        return env

    def plan(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            return build_engine_plan(PublicGitHub(self.origin), render_with_node)

    def invoke(self, env=None, api=None, output=None, file_limit=None):
        if output is None:
            self.outputs += 1
            output = self.directory / ("output-" + str(self.outputs))
        def limit_files():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))

        result = subprocess.run([sys.executable, str(CLI), "plan", "--output-dir", str(output),
                                 "--test-api", self.origin if api is None else api],
                                env=self.environment(**(env or {})), capture_output=True,
                                preexec_fn=limit_files if file_limit is not None else None)
        return result, output

    def assert_refused(self, **arguments):
        result, output = self.invoke(**arguments)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr,
                         b"engine-readme-plan: input unavailable or refused; no remote changes\n")
        self.assertFalse(output.exists())
        for sensitive in (TOKEN, PRIVATE_TEXT, str(output)):
            self.assertNotIn(sensitive.encode(), result.stdout + result.stderr)

    def add_pull(self, plan):
        self.proposal_head = plan["target"]["head"]
        self.prs = [{"id": 7007, "number": 7, "state": "open", "title": "Title is not an identity",
                     "html_url": f"https://github.com/{ENGINE}/pull/7",
                     "url": f"{API_ORIGIN}/repos/{ENGINE}/pulls/7",
                     "body": plan["proposed_pr"]["body"],
                     "head": {"ref": self.proposal_head, "label": OWNER + ":" + self.proposal_head,
                              "sha": self.head, "repo": copy.deepcopy(self.repos[ENGINE])},
                     "base": {"ref": "trunk", "label": OWNER + ":trunk", "sha": self.target,
                              "repo": copy.deepcopy(self.repos[ENGINE])}}]

    def test_create_plan_binds_real_source_renderer_and_unmarked_utf8_bytes(self):
        plan = self.plan()
        self.assertEqual(plan["schema"], SCHEMA)
        self.assertEqual(plan["status"], "create-needed")
        self.assertEqual(plan["proposed_readme"].encode(), EXPECTED)
        self.assertEqual(plan["readme"], {"path": "README.md", "before_sha256": sha256(README),
                         "after_sha256": sha256(EXPECTED), "markers": 3, "changed": True})
        self.assertEqual(plan["snapshot"]["commit"], self.hub)
        self.assertEqual(plan["snapshot"]["facts_sha256"], sha256(self.files["facts/facts.json"]))
        self.assertEqual(base64.b64decode(plan["snapshot"]["version_bytes_base64"]), b"1.1.0\n")
        self.assertEqual(plan["release"]["commit"], "1" * 40)
        self.assertEqual(plan["target"]["base_commit"], self.target)
        self.assertNotEqual(plan["release"]["commit"], plan["target"]["base_commit"])
        identity = {"repository_role": "engine", "repository": ENGINE, "release_tag": "v1.1.0",
                    "facts_sha256": sha256(self.files["facts/facts.json"])}
        expected_id = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(plan["plan_identity"], expected_id)
        self.assertEqual(plan["target"]["head"], "automation/facts-engine-" + expected_id[:24])
        self.assertEqual(plan["proposed_pr"]["body_sha256"], sha256(plan["proposed_pr"]["body"].encode()))
        binding = json.loads(plan["proposed_pr"]["body"].split("```json\n")[1].split("\n```")[0])
        self.assertEqual(binding["tag_commit"], "1" * 40)
        self.assertEqual(binding["base_commit"], self.target)
        self.assertEqual(binding["readme_after_sha256"], sha256(EXPECTED))
        paths = [path for _, path, _ in self.calls]
        self.assertEqual([path for path in paths if "/contents/" in path], [
            f"/repos/{HUB}/contents/facts/facts.json?ref={self.hub}",
            f"/repos/{HUB}/contents/facts/facts.schema.json?ref={self.hub}",
            f"/repos/{HUB}/contents/VERSION?ref={self.hub}",
            f"/repos/{ENGINE}/contents/README.md?ref={self.target}"])
        self.assertNotIn("facts-source", plan["proposed_readme"])
        self.assertTrue(all(auth == "Bearer " + TOKEN for _, _, auth in self.calls))
        self.assertEqual(plan["remote_writes"], 0)
        json.dumps(plan, allow_nan=False)

    def test_cli_outputs_review_manifest_and_proposed_bytes_after_rechecks(self):
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        manifest = json.loads((output / "plan.json").read_bytes())
        self.assertEqual(receipt["status"], "create-needed")
        self.assertEqual((output / "README.md").read_bytes(), EXPECTED)
        self.assertNotIn("proposed_readme", manifest)
        self.assertTrue(manifest["review_only"])
        self.assertEqual(manifest["recheck"]["hub_commit"], manifest["snapshot"]["commit"])
        self.assertEqual(manifest["recheck"]["base_commit"], self.target)
        self.assertNotIn(TOKEN, (output / "plan.json").read_text())
        self.assertEqual(set(path.name for path in output.iterdir()), {"plan.json", "README.md"})

    def test_current_metadata_selects_non_main_default_branch_and_distinct_commits(self):
        for base, target, version in (("stable/docs", "3" * 40, b"1.0.1"),
                                      ("readme-next", "4" * 40, b"1.2.0\n")):
            with self.subTest(base=base):
                self.hub = "5" * 40
                self.target = target
                self.files["VERSION"] = version
                self.repos[ENGINE]["default_branch"] = base
                self.readmes[target] = README
                self.calls.clear()
                plan = self.plan()
                self.assertEqual(plan["snapshot"]["commit"], "5" * 40)
                self.assertEqual(plan["target"]["base"], base)
                self.assertEqual(plan["target"]["base_commit"], target)
                self.assertEqual(plan["release"]["tag"], "v" + version.decode().strip())
                self.assertNotEqual(plan["release"]["commit"], target)
                self.assertEqual([path for _, path, _ in self.calls if f"/repos/{ENGINE}/contents/" in path],
                                 [f"/repos/{ENGINE}/contents/README.md?ref={target}"])

    def test_new_tag_same_facts_fetches_new_tag_and_repairs_drift(self):
        first = self.plan()
        self.calls.clear()
        self.hub = "7" * 40
        self.files["VERSION"] = b"1.2.0"
        second = self.plan()
        self.assertEqual(second["status"], "create-needed")
        self.assertEqual(first["snapshot"]["facts_sha256"], second["snapshot"]["facts_sha256"])
        self.assertNotEqual(first["plan_identity"], second["plan_identity"])
        self.assertNotEqual(first["target"]["head"], second["target"]["head"])
        self.assertIn(f"/repos/{ENGINE}/git/ref/tags/v1.2.0", [path for _, path, _ in self.calls])
        self.assertIn(f"/repos/{ENGINE}/contents/README.md?ref={self.target}",
                      [path for _, path, _ in self.calls])
        # The existing README's historic token scope is preserved even under a new trigger tag.
        self.assertEqual(second["proposed_readme"].encode(), EXPECTED)

    def test_new_tag_without_readme_drift_is_full_identity_noop(self):
        before = self.plan()
        self.hub = "8" * 40
        self.files["VERSION"] = b"1.2.0\n"
        self.readmes[self.target] = EXPECTED
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads((output / "plan.json").read_bytes())
        self.assertEqual(plan["status"], "noop")
        self.assertEqual(plan["release"], {"tag": "v1.2.0", "commit": "2" * 40,
                         "source_url": "https://github.com/pineforge-4pass/pineforge-engine/blob/v1.2.0/README.md#validation-scoreboard"})
        self.assertNotEqual(before["plan_identity"], plan["plan_identity"])
        self.assertFalse((output / "README.md").exists())
        self.assertNotIn("proposed_pr", plan)

    def test_changed_facts_same_tag_changes_render_identity_and_branch(self):
        first = self.plan()
        facts = json.loads(self.files["facts/facts.json"])
        facts["inventory"]["tvTrades"] = 4776345
        self.hub = "7" * 40
        self.files["facts/facts.json"] = encode(facts)
        second = self.plan()
        self.assertEqual(second["release"]["tag"], first["release"]["tag"])
        self.assertNotEqual(second["plan_identity"], first["plan_identity"])
        self.assertNotEqual(second["target"]["head"], first["target"]["head"])
        self.assertEqual(second["proposed_readme"].encode(), EXPECTED.replace(b"4,776,328", b"4,776,345"))
        self.assertIn("superseded open generations", second["limitations"][0])

    def test_reserialized_identical_facts_bind_original_raw_hash(self):
        first = self.plan()
        self.hub = "8" * 40
        self.files["facts/facts.json"] = b"\n" + self.files["facts/facts.json"] + b" "
        second = self.plan()
        self.assertNotEqual(first["snapshot"]["facts_sha256"], second["snapshot"]["facts_sha256"])
        self.assertNotEqual(first["plan_identity"], second["plan_identity"])
        self.assertEqual(first["readme"]["after_sha256"], second["readme"]["after_sha256"])

    def test_repeat_plan_inspects_current_pr_head_and_body_before_already_planned(self):
        first = self.plan()
        self.add_pull(first)
        self.calls.clear()
        second = self.plan()
        self.assertEqual(second["status"], "already-planned")
        self.assertEqual(second["plan_identity"], first["plan_identity"])
        self.assertEqual(second["target"]["head"], first["target"]["head"])
        self.assertEqual(second["existing_pr"]["url"], f"https://github.com/{ENGINE}/pull/7")
        paths = [path for _, path, _ in self.calls]
        self.assertIn(f"/repos/{ENGINE}/pulls/7", paths)
        self.assertIn(f"/repos/{ENGINE}/contents/README.md?ref={self.head}", paths)
        self.assertIn(f"/repos/{ENGINE}/git/ref/heads/" + self.proposal_head.replace("/", "%2F"), paths)
        query = parse_qs(urlsplit(next(path for path in paths if "/pulls?" in path)).query)
        self.assertEqual(query["head"], [OWNER + ":" + first["target"]["head"]])
        self.assertEqual(query["base"], ["trunk"])

    def test_existing_pr_readme_or_body_drift_requires_update(self):
        first = self.plan()
        self.add_pull(first)
        for kind in ("readme", "body", "base"):
            with self.subTest(kind=kind):
                self.readmes[self.head] = README if kind == "readme" else EXPECTED
                self.prs[0]["body"] = first["proposed_pr"]["body"] + ("\n" if kind == "body" else "")
                self.prs[0]["base"]["sha"] = "3" * 40 if kind == "base" else self.target
                plan = self.plan()
                self.assertEqual(plan["status"], "update-needed")
                self.assertEqual(plan["existing_pr"]["number"], 7)
                self.assertEqual(plan["target"]["head"], first["target"]["head"])

    def test_pr_detail_and_actual_head_ref_are_distinct_evidence(self):
        first = self.plan()
        self.add_pull(first)
        self.detail = copy.deepcopy(self.prs[0])
        self.prs[0]["head"]["sha"] = "4" * 40
        self.assertEqual(self.plan()["status"], "already-planned")
        self.head = "5" * 40
        self.readmes[self.head] = EXPECTED
        plan = self.plan()
        self.assertEqual(plan["status"], "update-needed")
        self.assertEqual(plan["existing_pr"]["head_commit"], "5" * 40)
        self.assertEqual(plan["existing_pr"]["reported_head_commit"], "d" * 40)

    def test_source_target_or_default_branch_movement_supersedes_without_proposal(self):
        for flag in ("hub_moved", "target_moved", "default_moved"):
            with self.subTest(flag=flag):
                self.counts.clear()
                self.hub_moved = self.target_moved = self.default_moved = False
                setattr(self, flag, True)
                result, output = self.invoke()
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads((output / "plan.json").read_bytes())
                self.assertEqual(plan["status"], "superseded")
                self.assertNotIn("proposed_pr", plan)
                self.assertNotIn("proposed_readme", plan)
                self.assertFalse((output / "README.md").exists())

    def test_direct_and_eight_annotated_tags_bind_exact_release_commit(self):
        for depth in (0, 1, 8):
            with self.subTest(depth=depth):
                self.tags.clear()
                pointers = [f"{20 + i:040x}" for i in range(depth)]
                for index, sha in enumerate(pointers):
                    child = {"type": "tag", "sha": pointers[index + 1]} if index + 1 < depth else {
                        "type": "commit", "sha": "6" * 40}
                    self.tags[sha] = {"sha": sha, "tag": "v1.1.0", "object": child}
                self.refs["v1.1.0"] = {"type": "tag", "sha": pointers[0]} if depth else {
                    "type": "commit", "sha": "6" * 40}
                plan = self.plan()
                self.assertEqual(plan["release"]["commit"], "6" * 40)
                self.assertEqual(plan["target"]["base_commit"], "b" * 40)

    def test_annotated_tag_cycles_excess_depth_and_wrong_types_refuse(self):
        for kind in ("cycle", "depth", "type", "identity"):
            with self.subTest(kind=kind):
                self.calls.clear()
                pointers = [f"{30 + i:040x}" for i in range(9)]
                self.refs["v1.1.0"] = {"type": "tag", "sha": pointers[0]}
                self.tags = {sha: {"sha": sha, "tag": "v1.1.0", "object": {
                    "type": "tag", "sha": pointers[(index + 1) % 9]}}
                    for index, sha in enumerate(pointers)}
                if kind == "cycle":
                    self.tags[pointers[0]]["object"]["sha"] = pointers[0]
                elif kind == "type":
                    self.tags[pointers[0]]["object"]["type"] = "tree"
                elif kind == "identity":
                    self.tags[pointers[0]]["sha"] = "7" * 40
                self.assert_refused()
                tag_reads = [path for _, path, _ in self.calls if f"/repos/{ENGINE}/git/tags/" in path]
                self.assertEqual(len(tag_reads), 8 if kind == "depth" else 1)

    def test_private_mismatched_and_malformed_repository_metadata_refuse(self):
        for repo in (HUB, ENGINE):
            original = copy.deepcopy(self.repos[repo])
            for field, value in (("private", True), ("full_name", "untrusted/repository"),
                                 ("id", True), ("owner", {"login": "foreign"}),
                                 ("default_branch", "unsafe/../name"), ("url", "http://127.0.0.1/private")):
                with self.subTest(repo=repo, field=field):
                    self.repos[repo] = dict(original, **{field: value})
                    self.assert_refused()
            self.repos[repo] = original

    def test_contents_path_type_name_size_encoding_and_blob_identity_refuse(self):
        for name in ("facts/facts.json", "facts/facts.schema.json", "VERSION", "README.md"):
            for field, value in (("path", "foreign/path"), ("type", "symlink"), ("name", "foreign"),
                                 ("size", True), ("size", 1), ("sha", "0" * 40),
                                 ("encoding", "none"), ("content", "%%%")):
                with self.subTest(name=name, field=field, value=value):
                    def corrupt(path, data, name=name, field=field, value=value):
                        if urlsplit(path).path.endswith("/contents/" + name):
                            data[field] = value
                        return data
                    self.transform = corrupt
                    self.assert_refused()
        self.transform = None

    def test_remote_schema_change_and_external_ref_are_refused_before_renderer(self):
        original = self.files["facts/facts.schema.json"]
        for raw in (b" " + original, encode({"$ref": self.origin + "/private-schema"})):
            with self.subTest(raw=raw[:24]):
                self.files["facts/facts.schema.json"] = raw
                self.calls.clear()
                self.assert_refused()
                self.assertFalse(any("/git/ref/tags/" in path for _, path, _ in self.calls))
                self.assertFalse(any("private-schema" in path for _, path, _ in self.calls))

    def test_invalid_actual_facts_and_version_fail_without_selector_fallback(self):
        original = dict(self.files)
        for filename, raw in (("facts/facts.json", b'{"schema":"a","schema":"b"}'),
                              ("facts/facts.json", b'{"schema":"wrong"}'),
                              ("VERSION", b"999.1.0"), ("VERSION", b"1.1.0\r\n"),
                              ("VERSION", b"1.1.0/unsafe"), ("VERSION", b"1.1.0\n\n")):
            with self.subTest(filename=filename, raw=raw):
                self.files = dict(original, **{filename: raw})
                self.assert_refused()

    def test_wrong_ref_name_commit_or_tag_target_never_qualifies(self):
        for field, value in (("ref", "refs/tags/wrong"), ("sha", "A" * 40),
                             ("sha", "1" * 39), ("type", "blob")):
            with self.subTest(field=field, value=value):
                def corrupt(path, data, field=field, value=value):
                    if path.endswith("/git/ref/tags/v1.1.0"):
                        if field == "ref":
                            data[field] = value
                        else:
                            data["object"][field] = value
                    return data
                self.transform = corrupt
                self.assert_refused()

    def test_malformed_unsafe_zero_and_unknown_markers_fail_real_bridge(self):
        for raw in (b"No markers", b"Count: <!-- pf:inventory.tvTrades -->old",
                    b"<!-- pf:inventory.tvTrades|int -->old<!-- /pf -->\n",
                    b"Count: `<!-- pf:inventory.tvTrades|int -->old<!-- /pf -->`",
                    b"Count: <!-- pf:missing.value -->old<!-- /pf -->",
                    b"Count: <!-- pf:inventory.tvTrades|int -->outer <!-- pf:inventory.tvTrades|int -->"
                    b"nested<!-- /pf --><!-- /pf -->", b"\xff<!-- pf:inventory.tvTrades -->x<!-- /pf -->"):
            with self.subTest(raw=raw[:50]):
                self.readmes[self.target] = raw
                self.assert_refused()

    def test_pr_duplicates_ambiguity_and_foreign_ownership_refuse(self):
        first = self.plan()
        self.add_pull(first)
        original = copy.deepcopy(self.prs[0])
        cases = ("duplicate", "ambiguous", "head", "base", "repo", "private", "url", "state", "body", "marker")
        for case in cases:
            with self.subTest(case=case):
                self.prs = [copy.deepcopy(original)]
                row = self.prs[0]
                if case in ("duplicate", "ambiguous"):
                    other = copy.deepcopy(row)
                    if case == "ambiguous":
                        other.update(id=7008, number=8, html_url=f"https://github.com/{ENGINE}/pull/8",
                                     url=f"{API_ORIGIN}/repos/{ENGINE}/pulls/8")
                    self.prs.append(other)
                elif case in ("head", "base"):
                    row[case]["ref"] = "foreign"
                elif case == "repo":
                    row["head"]["repo"]["full_name"] = "foreign/engine"
                elif case == "private":
                    row["head"]["repo"]["private"] = True
                elif case == "url":
                    row["html_url"] = self.origin + "/private"
                elif case == "state":
                    row["state"] = "closed"
                elif case == "body":
                    row["body"] = None
                else:
                    row["body"] = row["body"].replace(first["plan_identity"], "0" * 64)
                self.assert_refused()

    def test_current_pr_detail_is_validated_even_when_listing_matches(self):
        first = self.plan()
        self.add_pull(first)
        for kind in ("id", "private", "marker"):
            with self.subTest(kind=kind):
                self.detail = copy.deepcopy(self.prs[0])
                if kind == "id":
                    self.detail["id"] += 1
                elif kind == "private":
                    self.detail["base"]["repo"]["private"] = True
                else:
                    self.detail["body"] += "\n<!-- pineforge:engine-readme-plan/v1 plan_identity=" + "0" * 64 + " -->"
                self.assert_refused()

    def test_pr_page_malformed_truncated_foreign_and_over_bound_links_refuse(self):
        for kind in ("malformed", "truncated", "foreign", "bound", "missing-next", "duplicate"):
            with self.subTest(kind=kind):
                def pagination(path, data, kind=kind):
                    if "/pulls?" in path:
                        parsed = urlsplit(path)
                        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                        query["page"] = "21" if kind == "bound" else "2"
                        origin = "http://127.0.0.1:1" if kind == "foreign" else self.origin
                        url = origin + parsed.path + "?" + urlencode(query)
                        link = f'<{url}>; rel="last"' if kind in ("bound", "missing-next") else f'<{url}>; rel="next"'
                        if kind == "malformed":
                            link = "malformed"
                        elif kind == "duplicate":
                            link += ", " + link
                        self.extra_headers["Link"] = link
                    return data
                self.transform = pagination
                self.extra_headers.clear()
                self.assert_refused()

    def test_full_pr_pages_stop_at_twenty_without_silent_selection(self):
        first = self.plan()
        self.add_pull(first)
        original = copy.deepcopy(self.prs[0])

        def full_pages(path, data):
            if "/pulls?" not in path:
                return data
            page = int(parse_qs(urlsplit(path).query)["page"][0])
            rows = []
            for offset in range(100):
                number = (page - 1) * 100 + offset + 7
                row = copy.deepcopy(original)
                row.update(id=10000 + number, number=number,
                           html_url=f"https://github.com/{ENGINE}/pull/{number}",
                           url=f"{API_ORIGIN}/repos/{ENGINE}/pulls/{number}")
                rows.append(row)
            return rows

        self.transform = full_pages
        self.calls.clear()
        self.assert_refused()
        pages = [int(parse_qs(urlsplit(path).query)["page"][0])
                 for _, path, _ in self.calls if "/pulls?" in path]
        self.assertEqual(pages, list(range(1, 21)))

    def test_pr_page_wrong_shape_and_more_than_100_rows_refuse(self):
        for rows in ({"items": []}, [None] * 101):
            with self.subTest(shape=type(rows).__name__):
                self.transform = lambda path, data, rows=rows: rows if "/pulls?" in path else data
                self.assert_refused()

    def test_http_errors_bad_json_and_oversize_are_fixed_safe_refusals(self):
        for kind in ("error", "json", "oversize", "undeclared-oversize", "truncated", "encoding"):
            with self.subTest(kind=kind):
                self.status_code = 503 if kind == "error" else 200
                self.raw_response = (PRIVATE_TEXT.encode() if kind in ("error", "json") else
                                     b" " * (MAX_INPUT_BYTES + 1) if "oversize" in kind else b"{}")
                self.extra_headers = {"Content-Encoding": "gzip"} if kind == "encoding" else {}
                self.omit_length = kind == "undeclared-oversize"
                if kind == "undeclared-oversize":
                    # Exercise the actual bounded read of an HTTP/1.0 close-delimited body.
                    self.raw_response = b" " * MAX_INPUT_BYTES + b"!"
                elif kind == "truncated":
                    self.extra_headers["Content-Length"] = "1234"
                self.assert_refused()

    def test_redirect_never_sends_token_to_another_origin(self):
        foreign_calls = []

        class Foreign(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                foreign_calls.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        foreign = ThreadingHTTPServer(("127.0.0.1", 0), Foreign)
        thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        try:
            self.status_code = 302
            self.extra_headers["Location"] = f"http://127.0.0.1:{foreign.server_port}/private"
            self.raw_response = PRIVATE_TEXT.encode()
            self.assert_refused()
            self.assertEqual(foreign_calls, [])
        finally:
            foreign.shutdown()
            foreign.server_close()
            thread.join(5)

    def test_proxy_environment_is_ignored_by_production_http_transport(self):
        result, _ = self.invoke(env={"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                                     "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
                                     "NO_PROXY": "", "no_proxy": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls)

    def test_test_origin_and_real_token_refuse_before_any_request(self):
        for origin in (self.origin + "/", "http://localhost:1234", "https://127.0.0.1:1234",
                       self.origin + "?query=1", "http://user@127.0.0.1:1234",
                       "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:bad"):
            with self.subTest(origin=origin):
                self.assert_refused(api=origin)
        self.assert_refused(env={"FACTS_READ_TOKEN": "github-real-credential"})
        self.assertEqual(self.calls, [])

    def test_existing_output_directory_file_and_symlink_are_never_overwritten(self):
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                output = self.directory / kind
                if kind == "directory":
                    output.mkdir()
                    (output / "keep").write_bytes(b"keep")
                elif kind == "file":
                    output.write_bytes(b"keep")
                else:
                    output.symlink_to(self.directory / "missing")
                result, _ = self.invoke(output=output)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                if kind == "directory":
                    self.assertEqual((output / "keep").read_bytes(), b"keep")
                elif kind == "file":
                    self.assertEqual(output.read_bytes(), b"keep")
                else:
                    self.assertTrue(output.is_symlink())
        self.assertEqual(self.calls, [])

    def test_native_artifact_write_failure_leaves_no_partial_success_directory(self):
        # The real filesystem rejects the larger manifest after the README was written.
        self.assert_refused(file_limit=1024)
        self.assertTrue(any("/contents/README.md?ref=" in path for _, path, _ in self.calls))


class EngineRenderBridge(unittest.TestCase):
    def bridge(self, raw):
        return subprocess.run(["node", str(BRIDGE)], input=raw, capture_output=True,
                              env={"PATH": os.environ.get("PATH", os.defpath)})

    def test_real_bridge_returns_actual_bytes_and_count_for_changed_public_facts(self):
        facts = json.loads(FACTS.read_bytes())
        for count, expected in ((4776328, EXPECTED),
                                (4776345, EXPECTED.replace(b"4,776,328", b"4,776,345"))):
            with self.subTest(count=count):
                facts["inventory"]["tvTrades"] = count
                result = self.bridge(encode({"text": README.decode(), "facts": facts}))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                value = json.loads(result.stdout)
                self.assertEqual(value["markers"], 3)
                self.assertEqual(value["text"].encode(), expected)

    def test_bridge_invalid_input_nonzero_exit_has_only_fixed_stderr(self):
        for raw in (b"\xff", b"{", b"[]", encode({"text": "x", "facts": []}),
                    encode({"text": README.decode(), "facts": {}, "file": PRIVATE_TEXT}),
                    encode({"text": "No markers", "facts": {}}),
                    encode({"text": "Count: <!-- pf:missing -->x<!-- /pf -->", "facts": {}}),
                    encode({"text": "\ud800", "facts": {}}), b" " * (MAX_INPUT_BYTES + 1)):
            with self.subTest(raw=raw[:40]):
                result = self.bridge(raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"engine-readme-render: invalid input or render failure\n")

    def test_python_rejects_invalid_renderer_protocol_values(self):
        # Protocol unit negatives only; every accepted controller plan above executes the real bridge.
        for value in (None, {}, {"text": "x", "markers": 0}, {"text": "x", "markers": True},
                      {"text": "\ud800", "markers": 1}, {"text": "x", "markers": 1, "extra": 1},
                      {"text": "x" * (MAX_INPUT_BYTES + 1), "markers": 1}):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(InputError):
                    checked_render(value)


if __name__ == "__main__":
    unittest.main()
