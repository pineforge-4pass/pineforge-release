#!/usr/bin/env python3
"""CI/cloud replay of actual workflow shell and finalizer wiring, without network."""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
# These imports use the repository paths established above.
import release_pair as pair  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
import release_contract as contract  # noqa: E402
import test_release_workflows as wiring  # noqa: E402
import release_main_proof as main_proof  # noqa: E402
from test_release_main_proof import GitHubProof  # noqa: E402
from test_release_binding import (FinalizeClient, MemoryRegistry, MemoryStore,  # noqa: E402
                                  ProducerFixture, damage_engine_prerequisite, fixture_manifest)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def main_proof_endpoints(commit, *, problem=None, manifest=None, tag=None):
    """Serialize API responses, never the proof reader's decision."""
    commits = [commit]
    if manifest is not None:
        commits.append(manifest["facts"]["commit"])
    sources = {}
    for index, sha in enumerate(dict.fromkeys(commits)):
        api = GitHubProof(sha, run_offset=index * 1000)
        if sha == commit and problem is not None:
            run = api.runs["facts-validate.yml"][0]
            if problem == "missing":
                api.runs["facts-validate.yml"] = []
            else:
                changes = {
                    "red": {"conclusion": "failure"},
                    "running": {"status": "in_progress", "conclusion": None},
                    "skipped": {"conclusion": "skipped"},
                    "pr": {"event": "pull_request", "head_branch": "feature"},
                    "wrong_sha": {"head_sha": "b" * 40},
                }
                if problem not in changes:
                    raise AssertionError("unknown proof fixture damage")
                run.update(changes[problem])
                api.details[run["id"]].update(changes[problem])
        sources[sha] = {field: copy.deepcopy(getattr(api, field)) for field in
                        ("repo", "compare", "workflows", "runs", "details", "jobs")}
    return {"commit": commit, "sources": sources, "manifest": manifest,
            "tag": tag or ("v" + manifest["release_version"] if manifest is not None else None)}


class MainProofEndpointClient:
    """Hub GET transport for shell replays; no proof or publication mocks."""

    def __init__(self, data):
        self.commit, self.tag = data["commit"], data["tag"]
        self.sources, self.calls = {}, []
        for commit, fields in data["sources"].items():
            api = GitHubProof(commit)
            for field, value in fields.items():
                if field in {"details", "jobs"}:
                    value = {int(key): item for key, item in value.items()}
                setattr(api, field, value)
            self.sources[commit] = api
        self.active = self.sources[self.commit]
        manifest = data["manifest"]
        self.store = None
        if manifest is not None:
            self.store = MemoryStore(manifest)
            row = self.store.rows[self.tag]
            row["draft"] = False
            self.store.put(row, contract.ASSET, contract.canonical(manifest))
            self.store.operations.clear()

    def api(self, route, *, method="GET", data=None):
        if method != "GET" or data is not None:
            raise AssertionError("proof replay attempted a hub mutation")
        self.calls.append(route)
        if "/releases/" in route and self.store is not None:
            return self.store.api(route)
        if "/compare/" in route:
            commit = route.split("/compare/", 1)[1].split("...", 1)[0]
            self.active = self.sources[commit]
        return self.active.api(route)

    def pages(self, route):
        self.calls.append(route)
        if self.store is None:
            raise AssertionError("unmodeled proof release list")
        return self.store.pages(route)

    def request(self, url, **kwargs):
        if kwargs.get("method", "GET") != "GET" or kwargs.get("data") is not None or self.store is None:
            raise AssertionError("proof replay attempted an asset mutation")
        self.calls.append(url)
        return self.store.request(url, **kwargs)

    def tag_commit(self, repo, tag):
        if repo != contract.HUB or tag != self.tag:
            raise AssertionError("proof replay selected another tag")
        return self.commit


def main_proof_fixture_cli():
    """Execute the actual proof CLI using serialized GitHub GET responses."""
    root = Path(os.environ["PRODUCER_FIXTURE_ROOT"])
    data = json.loads((root / "endpoints.json").read_bytes())["main_proof"]
    client = MainProofEndpointClient(data)
    # Keep the workflow's bounded wait argument. Advancing the fixture clock
    # expires unfinished proof without a real sleep or an assumed success.
    with patch.object(main_proof, "Client", return_value=client), \
            patch.object(main_proof.time, "monotonic", side_effect=[0, 4001]), \
            patch.object(main_proof.time, "sleep", side_effect=AssertionError("unexpected live proof wait")):
        result = main_proof.main(sys.argv[1:])
    (root / "main-proof-result.json").write_bytes(contract.canonical({
        "status": result, "calls": client.calls,
    }))
    with (root / "git-calls").open("a", encoding="utf-8") as stream:
        stream.write("main-proof " + shlex.join(sys.argv[1:]) + "\n")
    return result


def write_producer_endpoints(root, client, *, proof_problem=None):
    (root / "endpoints.json").write_bytes(contract.canonical({
        "version": str(client.v), "api": client.api_data, "commits": client.commits,
        "downloads": {url: base64.b64encode(raw).decode("ascii")
                      for url, raw in client.downloads.items()},
        "main_proof": main_proof_endpoints("d" * 40, problem=proof_problem, tag="v" + str(client.v)),
    }))


def producer_fixture_client(root):
    """Restore the same endpoint fake for pre-tag and publish shell replays."""
    from test_parity_source_proof import TaggedEngineFixture

    endpoints = json.loads((root / "endpoints.json").read_bytes())
    client = TaggedEngineFixture(endpoints["version"])
    client.api_data = endpoints["api"]
    client.downloads = {url: base64.b64decode(raw) for url, raw in endpoints["downloads"].items()}
    client.commits = endpoints["commits"]
    return client


def producer_fixture_cli():
    """Run the real CLI with serialized public endpoints in a shell replay."""
    import release_artifacts as artifacts

    if sys.argv[0] == "scripts/release_main_proof.py":
        return main_proof_fixture_cli()
    if sys.argv[:2] != ["scripts/release_pipeline.py", "producer"]:
        raise AssertionError("unexpected producer replay command: " + repr(sys.argv))
    root = Path(os.environ["PRODUCER_FIXTURE_ROOT"])
    client = producer_fixture_client(root)
    calls, downloads = [], []
    real_producer = pipeline.producer
    real_download = client.download

    def record_producer(c, component, pin):
        calls.append({"component": component, "content_reads": c.content_calls})
        return real_producer(c, component, pin)

    def download(url, origins):
        downloads.append(url)
        return real_download(url, origins)

    with patch.object(pipeline, "Client", return_value=client), \
            patch.object(pipeline, "producer", side_effect=record_producer), \
            patch.object(client, "download", side_effect=download), \
            patch.object(artifacts, "PARITY_BUNDLE_ROOT", root / "absent-publish-bundle"):
        result = pipeline.main()
    (root / "producer-result.json").write_bytes(contract.canonical({
        "status": result, "calls": calls, "downloads": downloads,
        "content_reads": client.content_calls,
    }))
    return result


def publish_fixture_python():
    """Execute workflow Python unchanged, replacing only endpoint transport and paths."""
    import release_artifacts as artifacts

    if sys.argv[0] == "scripts/release_main_proof.py":
        return main_proof_fixture_cli()
    root = Path(os.environ["PRODUCER_FIXTURE_ROOT"])
    client = producer_fixture_client(root)

    class EndpointClient(artifacts.Client):
        # Client.file must remain the real inherited byte verifier: parity's
        # content reader calls it unbound after importing Client in the heredoc.
        def __new__(cls, *args, **kwargs):
            return client

    try:
        with patch.object(artifacts, "Client", EndpointClient), \
                patch.object(pipeline, "Client", EndpointClient), \
                patch.object(artifacts, "PARITY_BUNDLE_ROOT", root / "docker/parity"):
            if sys.argv == ["-"]:
                exec(compile(sys.stdin.read(), "<publish workflow heredoc>", "exec"),
                     {"__name__": "__main__"})
                return 0
            if sys.argv[:2] != ["scripts/release_pipeline.py", "bindings"]:
                raise AssertionError("unexpected publish Python command: " + repr(sys.argv))
            return pipeline.main()
    except contract.Invalid as error:
        # A product refusal retains its real reason and a nonzero exit.
        # Import/syntax/fixture errors propagate and cannot count as refusals.
        print(str(error), file=sys.stderr)
        return 1


class ProducerPreTagTests(unittest.TestCase):
    def replay(self, root, client, *, component="engine", retag=False, proof_problem=None, release_tail=False, bump_run=None):
        root.mkdir(parents=True)
        steps = {row.get("name"): row for row in workflow("handle-upstream.yml")["jobs"]["bump"]["steps"]}
        completing = steps.get("Verify the completing engine before tagging")
        self.assertIsNotNone(completing)
        gate = (steps["Verify the dispatched producer artifacts"] if component == "engine" else completing)
        bump = steps["Bump VERSION, commit, tag"]
        initial = str(client.v) if retag or release_tail else "1.10.0"
        (root / "VERSION").write_text(initial + "\n", encoding="ascii")
        write_producer_endpoints(root, client, proof_problem=proof_problem)
        script = bump["run"] if bump_run is None else bump_run
        if release_tail:
            # Replay the actual fresh-release publication boundary after its
            # VERSION/harness commit exists. Harness sync has its own controls;
            # this test must distinguish main's base SHA from that final SHA.
            script = script[:script.index('if [ "$RETAG" = true ]; then')] + script[script.index('commit="$(git rev-parse HEAD)"'):]
        bridge_paths = [str(ROOT / "tests"), str(ROOT / "scripts")]
        bridge = (
            f"import sys; sys.path[:0] = {bridge_paths!r}; "
            "from test_release_binding_workflows import producer_fixture_cli; "
            "sys.argv = sys.argv[1:]; raise SystemExit(producer_fixture_cli())"
        )
        # Git transport is recorded; both CLIs and all source/archive/proof
        # decisions execute unchanged against their serialized endpoint data.
        wrappers = (
            "python3() { command " + shlex.quote(sys.executable) + " -c "
            + shlex.quote(bridge) + ' "$@"; }\n'
            'git() {\n printf \'%s\\n\' "$*" >> "$PRODUCER_FIXTURE_ROOT/git-calls";\n'
            ' case "$1" in log|rev-parse) printf \'%s\\n\' "' + "d" * 40 + '";; esac\n}\n'
        )
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-euc", wrappers + gate["run"] + "\n" + script],
            cwd=root, capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PRODUCER_FIXTURE_ROOT": str(root),
                 "COMPONENT": component, "VER": str(client.v), "E": str(client.v), "C": str(client.v),
                 "H": str(client.v), "LINE": "pair", "RELEASE": str(client.v),
                 "next": str(client.v),
                 "RETAG": str(retag).lower(), "GITHUB_STEP_SUMMARY": str(root / "summary")},
        )
        self.assertNotIn("Traceback", done.stderr)
        self.assertTrue((root / "producer-result.json").is_file(), done.stdout + done.stderr)
        result = json.loads((root / "producer-result.json").read_bytes())
        git_calls = (root / "git-calls").read_text().splitlines() if (root / "git-calls").exists() else []
        return done, result, git_calls, initial

    def assert_control(self, root, component):
        from parity_bundle import PARITY_CAPABILITY_FROM
        from test_parity_source_proof import TaggedEngineFixture
        from test_parity_bundle import ROWS

        done, result, git_calls, initial = self.replay(
            root, TaggedEngineFixture(), component=component, retag=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["calls"], [{"component": "engine", "content_reads": len(ROWS) + 1}])
        self.assertEqual(result["content_reads"], len(ROWS) + 1)
        self.assertEqual(len(result["downloads"]), 4)
        proof_result = json.loads((root / "main-proof-result.json").read_bytes())
        self.assertEqual(proof_result["status"], 0)
        self.assertTrue(any("head_sha=" + "d" * 40 in route for route in proof_result["calls"]))
        self.assertTrue(any(call.startswith(f"tag -a v{PARITY_CAPABILITY_FROM} ") for call in git_calls))
        self.assertIn(f"push origin v{PARITY_CAPABILITY_FROM}", git_calls)
        self.assertEqual((root / "VERSION").read_text(), initial + "\n")
        self.assertFalse((root / "absent-publish-bundle").exists())

    def test_engine_last_ordering_prepares_before_published_archive_verification_and_tag(self):
        from parity_bundle import PARITY_CAPABILITY_FROM

        published = set()
        for index, component in enumerate(("codegen", "hpo", "engine")):
            published.add(component)
            decision, _ = pair.decide(
                component, PARITY_CAPABILITY_FROM, "false", "1.10.0", "1.10.0", "1.10.0",
                False, False, False, prev_hpo="1.10.0",
                partners_published={role: role in published for role in ("engine", "codegen", "hpo")})
            self.assertEqual(decision["mode"], "bump" if index == 2 else "wait")
        with tempfile.TemporaryDirectory() as temporary:
            self.assert_control(Path(temporary) / "engine-last", "engine")
            self.assert_control(Path(temporary) / "hpo-last", "hpo")

    def test_bad_engine_prerequisites_refuse_before_version_writes_or_git_on_bump_and_retag(self):
        from test_parity_source_proof import TaggedEngineFixture

        for component in ("engine", "codegen"):
            for retag in (False, True):
                for problem in ("missing_archive", "missing_checksum", "checksum_mismatch",
                                "missing_source", "unapproved_source"):
                    with self.subTest(component=component, retag=retag, problem=problem), \
                            tempfile.TemporaryDirectory() as temporary:
                        base = Path(temporary)
                        self.assert_control(base / "positive", component)
                        client = TaggedEngineFixture()
                        damage_engine_prerequisite(client, problem)
                        root = base / "negative"
                        done, result, git_calls, initial = self.replay(
                            root, client, component=component, retag=retag)
                        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                        self.assertEqual(result["status"], 1)
                        self.assertEqual(git_calls, [])
                        self.assertEqual((root / "VERSION").read_text(), initial + "\n")
                        self.assertFalse((root / "absent-publish-bundle").exists())

    def assert_proof_boundary(self, root, done, git_calls, *, allowed, fresh):
        result_path = root / "main-proof-result.json"
        self.assertTrue(result_path.is_file(), done.stdout + done.stderr)
        result = json.loads(result_path.read_bytes())
        self.assertEqual(done.returncode, 0 if allowed else 1, done.stdout + done.stderr)
        self.assertEqual(result["status"], done.returncode)
        self.assertTrue(result["calls"], "the proof decision must read GitHub identities")
        proof_at = next(i for i, call in enumerate(git_calls) if call.startswith("main-proof commit --sha "))
        self.assertIn("--sha " + "d" * 40, git_calls[proof_at])
        if fresh:
            self.assertLess(git_calls.index("push origin HEAD:main"), proof_at)
        tags = [i for i, call in enumerate(git_calls) if call.startswith("tag -a ")]
        if allowed:
            self.assertEqual(len(tags), 1)
            self.assertLess(proof_at, tags[0])
        else:
            self.assertEqual(tags, [], "unproved final commits must never create a tag")
            self.assertFalse(any(call.startswith("push origin v") for call in git_calls))

    def test_real_tag_boundaries_require_final_commit_full_proof_and_detect_removed_guard(self):
        from test_parity_source_proof import TaggedEngineFixture

        for fresh in (False, True):
            for problem in (None, "missing", "red", "running", "wrong_sha", "pr", "skipped"):
                with self.subTest(fresh=fresh, problem=problem), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "tag-boundary"
                    done, producer, git_calls, initial = self.replay(
                        root, TaggedEngineFixture(), retag=not fresh, release_tail=fresh, proof_problem=problem)
                    self.assertEqual(producer["status"], 0, "producer proof must not hide the hub proof control")
                    self.assert_proof_boundary(root, done, git_calls, allowed=problem is None, fresh=fresh)
                    self.assertEqual((root / "VERSION").read_text(), initial + "\n")
            script = next(step["run"] for step in workflow("handle-upstream.yml")["jobs"]["bump"]["steps"]
                          if step.get("name") == "Bump VERSION, commit, tag")
            guard = 'python3 scripts/release_main_proof.py commit --sha "$commit" --wait-seconds 1800'
            self.assertEqual(script.count(guard), 2)
            mutant = script.replace(guard, ":")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "removed-guard"
                done, _, git_calls, _ = self.replay(
                    root, TaggedEngineFixture(), retag=not fresh, release_tail=fresh,
                    proof_problem="red", bump_run=mutant)
                self.assertEqual(done.returncode, 0, done.stderr)
                with self.assertRaises(AssertionError):
                    self.assert_proof_boundary(root, done, git_calls, allowed=False, fresh=fresh)

    def test_cli_refuses_a_tag_moved_between_source_sync_and_archive_pin(self):
        from parity_bundle import PARITY_CAPABILITY_FROM
        from test_parity_bundle import ENGINE_REPO, ENGINE_TAG, ROWS
        from test_parity_source_proof import TaggedEngineFixture

        with tempfile.TemporaryDirectory() as temporary:
            self.assert_control(Path(temporary) / "positive", "engine")
            client = TaggedEngineFixture()
            with patch.object(pipeline, "Client", return_value=client), \
                    patch.object(client, "tag_commit", side_effect=["a" * 40, "b" * 40]) as resolve, \
                    patch.object(client, "download", wraps=client.download) as downloads, \
                    patch.object(sys, "argv", ["release_pipeline.py", "producer", "engine", PARITY_CAPABILITY_FROM]), \
                    patch.object(sys, "stderr", new_callable=io.StringIO):
                self.assertEqual(pipeline.main(), 1)
            self.assertEqual(client.content_calls, len(ROWS) + 1)
            self.assertEqual(downloads.call_count, 4)
            self.assertEqual(resolve.call_count, 2)
            self.assertTrue(all(call.args == (ENGINE_REPO, ENGINE_TAG)
                                for call in resolve.call_args_list))


class PublishParityGlueTests(unittest.TestCase):
    def replay(self, root, client, step, *, tag=None, proof_problem=None):
        root.mkdir(parents=True, exist_ok=True)
        write_producer_endpoints(root, client, proof_problem=proof_problem)
        steps = {row.get("name"): row
                 for row in workflow("publish.yml")["jobs"]["publish"]["steps"]}
        binding = steps["Bind producer source and package bytes"]["run"]
        if step == "sync":
            script = steps["Sync parity from the immutable engine tag"]["run"]
        elif step == "bind":
            script = binding
        elif step == "main-proof":
            script = steps["Require full main proof before image publication"]["run"]
        elif step == "main-proof-reuse":
            script = next(row["run"] for row in workflow("publish.yml")["jobs"]["prepare"]["steps"]
                          if row.get("name") == "Require full main proof before candidate reuse")
        else:
            self.assertEqual(step, "bind-heredoc")
            # Keep the actual redirection and complete heredoc, leaving out
            # only the preceding CLI so changed binding input is not replaced.
            header = "python3 - <<'PYBIND'"
            self.assertEqual(binding.count(header), 1)
            script = binding[binding.index(header):]
            self.assertTrue(script.endswith("\nPYBIND\n"))
        bridge_paths = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]
        bridge = (
            f"import sys; sys.path[:0] = {bridge_paths!r}; "
            "from test_release_binding_workflows import publish_fixture_python; "
            "sys.argv = sys.argv[1:]; raise SystemExit(publish_fixture_python())"
        )
        wrapper = (
            "python3() { command " + shlex.quote(sys.executable) + " -c "
            + shlex.quote(bridge) + ' "$@"; }\n'
        )
        (root / "github-output").unlink(missing_ok=True)
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-euc", wrapper + script],
            cwd=root, capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PRODUCER_FIXTURE_ROOT": str(root), "TAG": tag or "v" + str(client.v),
                 "E": str(client.v), "C": str(client.v), "H": str(client.v),
                 "HUB_COMMIT": "d" * 40, "RUNNER_TEMP": str(root),
                 "RELEASE_SHA": "d" * 40,
                 "GITHUB_OUTPUT": str(root / "github-output")},
        )
        self.assertNotIn("Traceback", done.stderr, done.stdout + done.stderr)
        return done

    def test_image_and_candidate_reuse_shells_execute_exact_commit_proof(self):
        from test_parity_source_proof import TaggedEngineFixture

        for step in ("main-proof", "main-proof-reuse"):
            for problem in (None, "missing", "red", "running", "wrong_sha", "pr", "skipped"):
                with self.subTest(step=step, problem=problem), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    done = self.replay(root, TaggedEngineFixture(), step, proof_problem=problem)
                    self.assertEqual(done.returncode, 0 if problem is None else 1, done.stdout + done.stderr)
                    result = json.loads((root / "main-proof-result.json").read_bytes())
                    self.assertEqual(result["status"], done.returncode)
                    self.assertTrue(any("head_sha=" + "d" * 40 in route for route in result["calls"]))

    def assert_control(self, root, client, *, capable=True):
        from test_parity_bundle import ROWS

        done = self.replay(root, client, "sync")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        stage = root / "docker/parity"
        if capable:
            self.assertTrue(stage.is_dir())
            self.assertFalse(stage.is_symlink())
            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700,
                             "Sync leaves a private stage; the Dockerfile normalizes its copy")
            self.assertTrue(all(path.is_file() and not path.is_symlink() for path in stage.iterdir()))
            files = {path.name: path.read_bytes() for path in stage.iterdir()}
            payload = {row["destination"]: base64.b64decode(
                client.api_data[client.route(row["source"])]["content"]) for row in ROWS}
            self.assertEqual(set(files), {*payload, "PARITY-MANIFEST.json", "PARITY-SHA256SUMS"})
            self.assertEqual({name: files[name] for name in payload}, payload,
                             "Sync must consume the serialized tagged endpoint bytes")
            descriptor = json.loads(files["PARITY-MANIFEST.json"])
            self.assertEqual(descriptor["engine_commit"], client.commits["engine"])
            manifest_raw = base64.b64decode(
                client.api_data[client.route("scripts/parity-bundle.json")]["content"])
            self.assertEqual(descriptor["source_manifest_sha256"], contract.sha256(manifest_raw))
            hashes = {name: contract.sha256(raw) for name, raw in payload.items()}
            self.assertEqual(descriptor["payload_sha256"], hashes)
            descriptor_hash = contract.sha256(files["PARITY-MANIFEST.json"])
            self.assertEqual(
                files["PARITY-SHA256SUMS"],
                "".join(value + "  " + name + "\n" for name, value in
                        [("PARITY-MANIFEST.json", descriptor_hash), *sorted(hashes.items())]).encode("ascii"))
        else:
            self.assertFalse(stage.exists())
            files, descriptor_hash = {}, ""
        done = self.replay(root, client, "bind")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        binding_path = root / "release-bindings.json"
        output_path = root / "github-output"
        self.assertTrue(binding_path.is_file(), done.stdout + done.stderr)
        self.assertTrue(output_path.is_file(), done.stdout + done.stderr)
        bound = json.loads(binding_path.read_bytes())
        self.assertEqual(bound["release_version"], str(client.v))
        lines = output_path.read_text().splitlines()
        parity_outputs = [line for line in lines if line.startswith("parity_")]
        self.assertEqual(parity_outputs, [
            "parity_descriptor_sha256=" + descriptor_hash,
            "parity_carrier=PARITY_CARRIER=" + ("1" if capable else "0"),
        ])
        if capable:
            self.assertEqual(bound["versions"]["engine"]["parity"]["descriptor_sha256"], descriptor_hash)
            self.assertEqual(bound["inventories"]["parity"],
                             {name: contract.sha256(raw) for name, raw in files.items()})
            self.assertEqual({p.name: p.read_bytes() for p in stage.iterdir()}, files)
        else:
            self.assertNotIn("parity", bound["versions"]["engine"])
            self.assertNotIn("parity", bound["inventories"])
        return files, bound, parity_outputs

    def test_actual_sync_and_bind_follow_changed_capable_bytes_and_legacy_release(self):
        from test_parity_bundle import ROWS
        from test_parity_source_proof import TaggedEngineFixture

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            files, _, original = self.assert_control(base / "positive", TaggedEngineFixture())
            row = ROWS[0]
            changed = files[row["destination"]] + b"changed publish source bytes\n"
            client = TaggedEngineFixture()
            client.mutate_source_bytes(row["source"], changed)
            updated, _, outputs = self.assert_control(base / "changed", client)
            self.assertEqual(updated[row["destination"]], changed)
            self.assertNotEqual(outputs[0], original[0],
                                "Bind must export the hash of the descriptor Sync actually wrote")
            self.assert_control(base / "legacy", TaggedEngineFixture("1.10.0"), capable=False)

    def test_actual_sync_refuses_damaged_endpoint_input_before_install(self):
        from test_parity_bundle import bad_blob_hash
        from test_parity_source_proof import TaggedEngineFixture

        for problem, message in (
                ("missing_source", "parity source file missing"),
                ("unapproved_source", "parity manifest payload differs from approved allowlist"),
                ("bad_blob_hash", "parity payload blob digest differs")):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                self.assert_control(base / "positive", TaggedEngineFixture())
                client = TaggedEngineFixture()
                if problem == "bad_blob_hash":
                    bad_blob_hash(client)
                else:
                    damage_engine_prerequisite(client, problem)
                root = base / "negative"
                done = self.replay(root, client, "sync")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(done.stderr.splitlines(), [message])
                self.assertFalse((root / "docker/parity").exists())
                self.assertFalse((root / "release-bindings.json").exists())
                self.assertFalse((root / "github-output").exists())

    def test_actual_bind_heredoc_refuses_changed_binding_input_without_exporting_arguments(self):
        from test_parity_bundle import ROWS
        from test_parity_source_proof import TaggedEngineFixture

        for problem, message in (
                ("release_version", "producer binding release version differs"),
                ("descriptor_hash", "parity descriptor hash differs"),
                ("inventory", "parity inventory differs from candidate"),
                ("missing_capability", "parity capability missing for release"),
                ("legacy_engine_capability", "unexpected parity capability before cutoff"),
                ("legacy_inventory_capability", "unexpected parity capability before cutoff")):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                client = TaggedEngineFixture()
                _, bound, _ = self.assert_control(base / "positive", client)
                damaged = copy.deepcopy(bound)
                tag = "v" + str(client.v)
                if problem == "release_version":
                    damaged["release_version"] = "1.10.0"
                elif problem == "descriptor_hash":
                    damaged["versions"]["engine"]["parity"]["descriptor_sha256"] = \
                        contract.sha256(b"a different descriptor\n")
                elif problem == "inventory":
                    damaged["inventories"]["parity"][ROWS[0]["destination"]] = \
                        contract.sha256(b"a different payload\n")
                elif problem == "missing_capability":
                    del damaged["versions"]["engine"]["parity"]
                else:
                    damaged["release_version"] = "1.10.0"
                    tag = "v1.10.0"
                    if problem == "legacy_engine_capability":
                        del damaged["inventories"]["parity"]
                    else:
                        del damaged["versions"]["engine"]["parity"]
                root = base / "negative"
                root.mkdir()
                (root / "release-bindings.json").write_bytes(contract.canonical(damaged))
                done = self.replay(root, client, "bind-heredoc", tag=tag)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertEqual(done.stderr.splitlines(), [message])
                self.assertTrue((root / "github-output").is_file())
                self.assertEqual((root / "github-output").read_bytes(), b"",
                                 "refused binding input must not export build arguments")


def assert_ready_wiring(case, publish, finalizer):
    steps = publish["jobs"]["publish"]["steps"]
    named = {row.get("name"): row for row in steps}
    order = ["Check Debian source mirror", "Check pushed Debian source mirror",
             "Verify real candidate runtime on both architectures", "GitHub Release"]
    for name in order:
        case.assertIn(name, named, "required readiness step must exist")
    positions = [steps.index(named[name]) for name in order]
    case.assertEqual(positions, sorted(positions))
    for name in order:
        case.assertNotIn("continue-on-error", named[name])
        case.assertNotIn("if", named[name])
    tags = named["Image metadata"]["with"]["tags"].splitlines()
    case.assertEqual(len(tags), 1)
    case.assertTrue(tags[0].startswith("type=raw,value=candidate-"))
    case.assertEqual((publish["jobs"]["publish"].get("env") or {}).get("RELEASE_TAG"), "${{ needs.prepare.outputs.tag }}")
    case.assertIn("org.opencontainers.image.version=${{ env.RELEASE_TAG }}", named["Image metadata"]["with"]["labels"])
    case.assertIn("org.opencontainers.image.revision=${{ needs.tag-guard.outputs.sha }}",
                  named["Image metadata"]["with"]["labels"])
    case.assertEqual(publish["jobs"]["notify-consumers"]["uses"], "./.github/workflows/release-finalize.yml")
    finalize = finalizer["jobs"]["finalize"]
    ready = next(row for row in finalize["steps"] if row.get("id") == "ready")
    case.assertEqual(ready["run"].splitlines(), [
        "set -euo pipefail",
        'python3 scripts/release_main_proof.py finalize --tag "$TAG" --output "$RUNNER_TEMP/deliveries.json"',
    ])
    case.assertNotIn("if", ready)
    case.assertNotIn("continue-on-error", ready)
    case.assertEqual(finalize["permissions"]["actions"], "read")
    case.assertEqual(publish["jobs"]["notify-consumers"]["permissions"]["actions"], "read")
    dispatch = finalizer["jobs"]["notify-consumers"]
    case.assertEqual(dispatch["needs"], "finalize")
    case.assertEqual(dispatch["strategy"]["fail-fast"], "false")
    case.assertEqual(dispatch["strategy"]["matrix"]["include"], "${{ fromJSON(needs.finalize.outputs.deliveries) }}")
    case.assertNotIn("concurrency", finalizer)
    case.assertEqual(finalize["concurrency"]["group"], "release-finalize")
    case.assertEqual(publish["jobs"]["prepare"]["permissions"]["contents"], "write")


class ReadyWiringTests(unittest.TestCase):
    def aggregate_shell(self, script, results, event="pull_request", *, authz_ok=None):
        import subprocess
        if authz_ok is None:
            authz_ok = str(results.get("authz", {}).get("result") == "success").lower()
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-c", script],
            env={"PATH": os.environ.get("PATH", ""), "RESULTS": json.dumps(results),
                 "EVENT_NAME": event, "AUTHZ_OK": authz_ok,
                 "AUTHZ_STATUS": results.get("authz", {}).get("result", "")},
            capture_output=True, text=True,
        )

    def aggregate_steps_shell(self, job, results, *, first_only=False):
        # Translate the workflow's actual step comparisons for this shell
        # replay. The separate inventory replays below bypass these wrappers
        # so a correct first refusal cannot hide a broken inventory program.
        # These pull-request replays skip the exactly pinned push-only bootstrap.
        self.assertEqual(job["steps"][0], wiring.PrTierWorkflowTest.READY_TOOLS_STEP)
        programs = []
        steps = job["steps"][1:2] if first_only else job["steps"][1:]
        for step in steps:
            condition = step.get("if", "").removeprefix("${{").removesuffix("}}").strip()
            comparison = re.fullmatch(r"needs\.authz\.result (==|!=) 'success'", condition)
            self.assertIsNotNone(comparison, step)
            operator = "=" if comparison[1] == "==" else "!="
            programs.append(f'if [ "$AUTHZ_STATUS" {operator} success ]; then\n{step["run"]}\nfi')
        return self.aggregate_shell("\n".join(programs), results)

    def test_security_819_actual_first_refusal_and_full_aggregate_reject_non_success_authz(self):
        job = workflow("python-test.yml")["jobs"]["release-ready"]
        self.assertEqual(job["steps"][0], wiring.PrTierWorkflowTest.READY_TOOLS_STEP)
        self.assertEqual(job["steps"][1], wiring.PrTierWorkflowTest.AUTHZ_REFUSAL_STEP)
        self.assertEqual(job["steps"][2]["if"], "needs.authz.result == 'success'")
        for docs_only in ("false", "true"):
            expected = {name: {"result": "skipped" if docs_only == "true" else "success"}
                        for name in job["needs"]}
            expected["changes"] = {"result": "success", "outputs": {"docs-only": docs_only}}
            for status in ("success", "skipped", "failure", "cancelled", "", "unknown", "missing"):
                results = copy.deepcopy(expected)
                if status == "missing":
                    results.pop("authz")
                else:
                    results["authz"] = {"result": status}
                for label, first_only in (("first refusal", True), ("complete aggregate", False)):
                    with self.subTest(program=label, docs_only=docs_only, authz=status):
                        done = self.aggregate_steps_shell(job, results, first_only=first_only)
                        self.assertEqual(done.returncode, 0 if status == "success" else 1,
                                         (done.stdout, done.stderr))
                        expected_output = (
                            "" if status == "success" else
                            "::error::authz did not pass; nothing of this run's code is checked out or run\n"
                        )
                        self.assertEqual(done.stdout, expected_output)
                        self.assertEqual(done.stderr, "")

    def test_actual_workflows_require_verified_candidate_before_ready_fanout(self):
        assert_ready_wiring(self, workflow("publish.yml"), workflow("release-finalize.yml"))

    def test_dispatch_origin_and_validated_tag_are_pinned_alongside_readiness(self):
        pins = wiring.ReleaseTriggerWorkflowTest()
        pins.assert_publish((ROOT / ".github/workflows/publish.yml").read_text())
        pins.assert_handler((ROOT / ".github/workflows/handle-upstream.yml").read_text())

    def test_readiness_refuses_lost_validated_tag_or_ref_based_version_label_on_real_text(self):
        pins = wiring.ReleaseTriggerWorkflowTest()
        text = (ROOT / ".github/workflows/publish.yml").read_text()
        for before, after in (
            ("      RELEASE_TAG: ${{ needs.prepare.outputs.tag }}\n", ""),
            ("org.opencontainers.image.version=${{ env.RELEASE_TAG }}",
             "org.opencontainers.image.version=${{ github.ref_name }}"),
            ("org.opencontainers.image.revision=${{ needs.tag-guard.outputs.sha }}",
             "org.opencontainers.image.revision=${{ github.sha }}"),
        ):
            mutant = pins.mutate(text, before, after)
            with self.subTest(anchor=before), self.assertRaises(AssertionError):
                assert_ready_wiring(self, pins.parse(mutant), workflow("release-finalize.yml"))

    def test_real_decide_shell_reuses_a_draft_candidate_without_an_image_probe(self):
        handler = (ROOT / ".github/workflows/handle-upstream.yml").read_text()
        runner = wiring.HpoWorkflowTest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner.fixture(root)
            (root / "VERSION").write_text("1.6.0\n")
            (root / "state/releases-pineforge-release.json").write_text(json.dumps([
                {"tag_name": "v1.6.0", "draft": True, "assets": [{"name": "release-candidate.json"}]}]))
            for _ in range(2):
                done, decision = runner.shell(root, handler, "Decide action", {
                    "COMPONENT": "hpo", "VER": "1.6.0", "PAYLOAD_PRERELEASE": "false", "EPREV": "1.6.0",
                    "CPREV": "1.6.0", "HPREV": "1.6.0", "HPO_PAIRING": "true", "PUBLISHED": "true"})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(decision["mode"], "noop")
            actions = [json.loads(line) for line in (root / "state/actions.jsonl").read_text().splitlines()]
            self.assertFalse(any(row["tool"] == "docker" for row in actions))

    def test_negative_controls_detect_removed_runtime_gate_early_latest_and_skipped_fanout(self):
        original = workflow("publish.yml")
        for mutation in ("runtime", "latest", "fanout", "proof-entry", "proof-softened", "proof-permissions"):
            obj, finalizer = copy.deepcopy(original), workflow("release-finalize.yml")
            if mutation == "runtime":
                obj["jobs"]["publish"]["steps"] = [s for s in obj["jobs"]["publish"]["steps"]
                                                         if s.get("name") != "Verify real candidate runtime on both architectures"]
            elif mutation == "latest":
                step = next(s for s in obj["jobs"]["publish"]["steps"] if s.get("name") == "Image metadata")
                step["with"]["tags"] += "\ntype=raw,value=latest"
            elif mutation == "fanout":
                finalizer["jobs"]["notify-consumers"]["needs"] = "publish"
            else:
                finalize = finalizer["jobs"]["finalize"]
                ready = next(step for step in finalize["steps"] if step.get("id") == "ready")
                if mutation == "proof-entry":
                    ready["run"] = ready["run"].replace("scripts/release_main_proof.py finalize", "scripts/release_pipeline.py finalize")
                elif mutation == "proof-softened":
                    ready["continue-on-error"] = "true"
                else:
                    finalize["permissions"]["actions"] = "write"
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                assert_ready_wiring(self, obj, finalizer)

    def test_required_aggregate_always_runs_and_rejects_failed_cancelled_skipped_or_missing_job(self):
        wf = workflow("python-test.yml")
        job = wf["jobs"]["release-ready"]
        self.assertEqual(job["name"], "release/ready")
        # Cancellation must not skip a required check and count as success.
        self.assertEqual(job.get("if"), "${{ always() }}")
        self.assertEqual(set(job["needs"]), {"authz", "changes", "fingerprint-canonical", "release-rules",
                                            "description-renderer", "release-ready-replay"})
        script = job["steps"][2]["run"]
        # Execute the workflow's actual aggregate program in a child process.
        expected = {name: {"result": "success"} for name in job["needs"]}
        expected["changes"]["outputs"] = {"docs-only": "false"}
        for bad in (None, "failure", "cancelled", "skipped", "missing"):
            results = copy.deepcopy(expected)
            if bad == "missing":
                results.pop("release-rules")
            elif bad:
                results["release-rules"]["result"] = bad
            for event in ("pull_request", "push", "merge_group"):
                with self.subTest(result=bad, event=event):
                    done = self.aggregate_shell(script, results, event)
                    self.assertEqual(done.returncode == 0, bad is None, (bad, done.stderr))

    def test_aggregate_accepts_only_verified_docs_pr_skips_and_rejects_detector_failures(self):
        job = workflow("python-test.yml")["jobs"]["release-ready"]
        expected = {name: {"result": "skipped"} for name in job["needs"]}
        expected["authz"] = {"result": "success"}
        expected["changes"] = {"result": "success", "outputs": {"docs-only": "true"}}
        cases = [("docs PR", "pull_request", expected, True)]
        for event in ("push", "merge_group", "schedule", "workflow_dispatch", "release"):
            cases.append((event, event, copy.deepcopy(expected), False))
        for name in job["needs"]:
            for bad in ("failure", "cancelled", "missing",
                        "skipped" if name in {"authz", "changes"} else "success"):
                results = copy.deepcopy(expected)
                if bad == "missing":
                    results.pop(name)
                else:
                    results[name]["result"] = bad
                cases.append((name + ":" + bad, "pull_request", results, False))
        for value in (None, "", "invalid", "false"):
            results = copy.deepcopy(expected)
            results["changes"]["outputs"] = {} if value is None else {"docs-only": value}
            cases.append(("output:" + str(value), "pull_request", results, False))
        results = copy.deepcopy(expected)
        results["unexpected"] = {"result": "success"}
        cases.append(("extra job", "pull_request", results, False))
        for label, event, results, accepted in cases:
            with self.subTest(case=label):
                done = self.aggregate_shell(job["steps"][2]["run"], results, event)
                self.assertEqual(done.returncode == 0, accepted, done.stderr)

    def assert_aggregate_requires_authz_success(self, job):
        self.assertEqual(job.get("if"), "${{ always() }}")
        self.assertEqual(set(job["needs"]), {
            "authz", "changes", "fingerprint-canonical", "release-rules",
            "description-renderer", "release-ready-replay",
        })
        self.assertEqual(job["steps"][0], wiring.PrTierWorkflowTest.READY_TOOLS_STEP)
        self.assertEqual(job["steps"][1], wiring.PrTierWorkflowTest.AUTHZ_REFUSAL_STEP)
        step = job["steps"][2]
        self.assertEqual(step.get("if"), "needs.authz.result == 'success'")
        self.assertEqual(step["env"]["AUTHZ_OK"], "${{ needs.authz.result == 'success' }}")
        for docs_only in ("false", "true"):
            expected = {name: {"result": "skipped" if docs_only == "true" else "success"}
                        for name in job["needs"]}
            expected["changes"] = {"result": "success", "outputs": {"docs-only": docs_only}}
            for status in ("success", "skipped", "failure", "cancelled", "", "unknown", "missing"):
                results = copy.deepcopy(expected)
                if status == "missing":
                    results.pop("authz", None)
                else:
                    results["authz"] = {"result": status}
                done = self.aggregate_shell(step["run"], results)
                self.assertEqual(done.returncode == 0, status == "success",
                                 (docs_only, status, done.stdout, done.stderr))
                if status not in {"success", "missing"}:
                    done = self.aggregate_shell(step["run"], results, authz_ok="true")
                    self.assertNotEqual(done.returncode, 0, (docs_only, status, done.stderr))
            expected["authz"] = {"result": "success"}
            for authz_ok in ("false", "", "skipped"):
                done = self.aggregate_shell(step["run"], expected, authz_ok=authz_ok)
                self.assertNotEqual(done.returncode, 0, (docs_only, authz_ok, done.stderr))

    def test_actual_aggregate_shell_requires_authz_success_and_refuses_skipped_authz_mutations(self):
        job = workflow("python-test.yml")["jobs"]["release-ready"]
        self.assert_aggregate_requires_authz_success(job)
        condition = "if os.environ['AUTHZ_OK'] != 'true' or results['authz']['result'] != 'success':"
        script = job["steps"][2]["run"]
        self.assertIn(condition, script)
        mutants = []
        for replacement in (
            "if results['authz']['result'] not in {'success', 'skipped'}:",
            "if os.environ['AUTHZ_OK'] != 'true':",
            "if results['authz']['result'] != 'success':",
            "if False:",
        ):
            mutant = copy.deepcopy(job)
            mutant["steps"][2]["run"] = script.replace(condition, replacement, 1)
            mutants.append(mutant)
        mutant = copy.deepcopy(job)
        mutant["steps"][2]["env"]["AUTHZ_OK"] = (
            "${{ needs.authz.result == 'success' || needs.authz.result == 'skipped' }}"
        )
        mutants.append(mutant)
        for condition in (
            None, "success()", "always()", "!cancelled()", "failure()",
            "${{ !cancelled() }}", "${{ success() }}", "${{ failure() }}",
            "always() && needs.authz.result == 'success'",
            "${{ always() && needs.authz.result == 'success' }}",
        ):
            mutant = copy.deepcopy(job)
            if condition is None:
                mutant.pop("if")
            else:
                mutant["if"] = condition
            mutants.append(mutant)
        mutant = copy.deepcopy(job)
        mutant["steps"][2].pop("if")
        mutants.append(mutant)
        mutant = copy.deepcopy(job)
        mutant["steps"][2]["if"] = "needs.authz.result != 'success'"
        mutants.append(mutant)
        for missing_need in job["needs"]:
            mutant = copy.deepcopy(job)
            mutant["needs"] = [name for name in job["needs"] if name != missing_need]
            mutants.append(mutant)
        for index, mutant in enumerate(mutants):
            with self.subTest(mutation=index):
                self.assertNotEqual(mutant, job)
                with self.assertRaises(AssertionError):
                    self.assert_aggregate_requires_authz_success(mutant)

    def test_facts_license_extension_keeps_exact_three_field_payload(self):
        wf = workflow("facts-fanout.yml")
        self.assertEqual(set(wf["jobs"]), {"web", "license"})
        self.assertEqual(wf["jobs"]["license"]["env"]["FACTS_CONSUMER_ROLE"], "license")
        from facts_fanout import payload_fields
        good = {"commit": "a" * 40, "sha256": "b" * 64, "dry_run": False}
        self.assertEqual(payload_fields(good), good)
        with self.assertRaises(ValueError):
            payload_fields({**good, "release_version": "1.6.0"})


class ArrivalReplayTests(unittest.TestCase):
    def test_all_arrival_orders_replay_real_handler_shell_then_finalize_and_repeat(self):
        handler = (ROOT / ".github/workflows/handle-upstream.yml").read_text()
        runner = wiring.HpoWorkflowTest()
        for ver in ("1.6.0-rc.1", "1.6.0"):
            v = pair.parse(ver)
            for order in permutations(("engine", "codegen", "hpo")):
                with self.subTest(version=ver, order=order), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    runner.fixture(root)
                    http, published = {}, set()
                    for index, role in enumerate(order):
                        published.add(role)
                        if role == "engine":
                            for arch in ("x86_64", "aarch64"):
                                http[f"https://github.com/pineforge-4pass/pineforge-engine/releases/download/v{v}/pineforge-v{v}-linux-{arch}.tar.gz"] = {"status": 200}
                        elif role == "codegen":
                            http[f"https://pypi.org/pypi/pineforge-codegen/{v.pep440}/json"] = {"status": 200, "body": "{}"}
                        else:
                            http[f"https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v{v.pep440}/pineforge_hpo-{v.pep440}-py3-none-any.whl"] = {"status": 200}
                        http["https://pypi.org/pypi/pineforge-codegen/json"] = {"status": 200, "body": json.dumps({"releases": [v.pep440] if "codegen" in published else ["1.5.9"]})}
                        (root / "state/http.json").write_text(json.dumps(http))
                        for kind in ("engine", "hpo"):
                            tag = v.pep440 if kind == "hpo" else str(v)
                            rows = [{"tag_name": "v" + tag, "draft": False, "prerelease": v.prerelease}] if kind in published else []
                            (root / f"state/releases-pineforge-{kind}.json").write_text(json.dumps(rows))
                        # The producer itself must pass its real artifact verifier.
                        import release_artifacts
                        release_artifacts.producer(ProducerFixture(ver), role, ver)
                        done, incoming = runner.shell(root, handler, "Validate payload", {
                            "EVENT": role + "-release", "RAW_VERSION": v.pep440 if role == "hpo" else str(v),
                            "RAW_PRERELEASE": str(v.prerelease).lower(), "RUN_ID": "42"})
                        self.assertEqual(done.returncode, 0, done.stderr)
                        done, partners = runner.shell(root, handler, "Probe the partner", {
                            "COMPONENT": role, "VER": str(v), "PYPI": v.pep440, "HPO_PAIRING": "true", "HPO_VERSION": v.pep440})
                        self.assertEqual(done.returncode, 0, done.stderr)
                        values = {"COMPONENT": role, "VER": str(v), "PAYLOAD_PRERELEASE": str(v.prerelease).lower(),
                                  "EPREV": "1.5.9", "CPREV": "1.5.9", "HPREV": "", "HPO_PAIRING": "true",
                                  "PUBLISHED": "false", "PARTNER_LATEST": ""}
                        for kind in ("engine", "codegen", "hpo"):
                            values[kind.upper() + "_PUBLISHED"] = partners.get(kind + "_published", "false")
                            values[kind.upper() + "_LATEST"] = partners.get(kind + "_latest", "")
                        done, decision = runner.shell(root, handler, "Decide action", values)
                        self.assertEqual(done.returncode, 0, done.stderr)
                        self.assertEqual(decision["mode"], "bump" if index == 2 else "wait")
                    manifest = fixture_manifest(ver)
                    client, store, registry = FinalizeClient(manifest), MemoryStore(manifest), MemoryRegistry()
                    tag = "v" + ver
                    with patch.object(pipeline, "npm", side_effect=lambda c, pin, inv: {
                            **pin, "npm_version": ver, "npm_integrity": manifest["versions"]["codegen"]["npm_integrity"]}):
                        event = pipeline.finalize(client, store, registry, store.get(tag))
                        raw = store.read(store.get(tag), contract.ASSET)
                        self.assertEqual(event, pipeline.finalize(client, store, registry, store.get(tag)))
                        self.assertEqual(store.read(store.get(tag), contract.ASSET), raw)


if __name__ == "__main__":
    unittest.main()
