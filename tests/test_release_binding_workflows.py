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
from test_release_binding import (FinalizeClient, MemoryRegistry, MemoryStore,  # noqa: E402
                                  ProducerFixture, damage_engine_prerequisite, fixture_manifest)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def producer_fixture_cli():
    """Run the real CLI with serialized public endpoints in a shell replay."""
    from test_parity_source_proof import TaggedEngineFixture
    import release_artifacts as artifacts

    root = Path(os.environ["PRODUCER_FIXTURE_ROOT"])
    endpoints = json.loads((root / "endpoints.json").read_bytes())
    client = TaggedEngineFixture(endpoints["version"])
    client.api_data = endpoints["api"]
    client.downloads = {url: base64.b64decode(raw) for url, raw in endpoints["downloads"].items()}
    client.commits = endpoints["commits"]
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


class ProducerPreTagTests(unittest.TestCase):
    def replay(self, root, client, *, component="engine", retag=False):
        root.mkdir(parents=True)
        steps = {row.get("name"): row for row in workflow("handle-upstream.yml")["jobs"]["bump"]["steps"]}
        completing = steps.get("Verify the completing engine before tagging")
        self.assertIsNotNone(completing)
        gate = (steps["Verify the dispatched producer artifacts"] if component == "engine" else completing)
        bump = steps["Bump VERSION, commit, tag"]
        initial = str(client.v) if retag else "1.10.0"
        (root / "VERSION").write_text(initial + "\n", encoding="ascii")
        (root / "endpoints.json").write_bytes(contract.canonical({
            "version": str(client.v), "api": client.api_data, "commits": client.commits,
            "downloads": {url: base64.b64encode(raw).decode("ascii")
                          for url, raw in client.downloads.items()},
        }))
        bridge_paths = [str(ROOT / "tests"), str(ROOT / "scripts")]
        bridge = (
            f"import sys; sys.path[:0] = {bridge_paths!r}; "
            "from test_release_binding_workflows import producer_fixture_cli; "
            "sys.argv = sys.argv[1:]; raise SystemExit(producer_fixture_cli())"
        )
        # Only git transport is recorded. The selected workflow CLI, sync,
        # tagged-source verifier and archive/checksum verifier all run for real.
        wrappers = (
            "python3() { command " + shlex.quote(sys.executable) + " -c "
            + shlex.quote(bridge) + ' "$@"; }\n'
            'git() {\n printf \'%s\\n\' "$*" >> "$PRODUCER_FIXTURE_ROOT/git-calls";\n'
            ' case "$1" in log) printf \'%s\\n\' "' + "d" * 40 + '";; esac\n}\n'
        )
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-euc", wrappers + gate["run"] + "\n" + bump["run"]],
            cwd=root, capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PRODUCER_FIXTURE_ROOT": str(root),
                 "COMPONENT": component, "VER": str(client.v), "E": str(client.v), "C": str(client.v),
                 "H": str(client.v), "LINE": "pair", "RELEASE": str(client.v),
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
    case.assertTrue(any("scripts/release_pipeline.py finalize" in row.get("run", "") for row in finalize["steps"]))
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
        for mutation in ("runtime", "latest", "fanout"):
            obj, finalizer = copy.deepcopy(original), workflow("release-finalize.yml")
            if mutation == "runtime":
                obj["jobs"]["publish"]["steps"] = [s for s in obj["jobs"]["publish"]["steps"]
                                                         if s.get("name") != "Verify real candidate runtime on both architectures"]
            elif mutation == "latest":
                step = next(s for s in obj["jobs"]["publish"]["steps"] if s.get("name") == "Image metadata")
                step["with"]["tags"] += "\ntype=raw,value=latest"
            else:
                finalizer["jobs"]["notify-consumers"]["needs"] = "publish"
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
