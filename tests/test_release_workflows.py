#!/usr/bin/env python3
"""Wiring checks: the release workflows run scripts/release_pair.py where its
rules apply and never fall back to an rc-unsafe version sort, the
notify-consumers decision (no dispatch to the application for a prerelease) is
run under bash, and the engine's runtime harness (eight files) is synced from
the pinned engine tag by one script and checked against recorded sha256 values
when the image is built (the Dockerfile's check is run under bash on temporary
directories). The source mirror wiring checks use PyYAML to parse the workflows."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
REPO = WORKFLOWS.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import release_pair as rp  # noqa: E402


def _step(text: str, name: str) -> str:
    """The body of the workflow step whose `- name:` line starts with name."""
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


def _run_script(step: str) -> str:
    """The shell script of a step's `run: |` block, as the runner writes it out."""
    marker = "\n        run: |\n"
    return textwrap.dedent(step[step.index(marker) + len(marker):]).rstrip("\n") + "\n"


def _run_block(dockerfile: str, needle: str) -> str:
    """The RUN instruction of the Dockerfile that holds needle (an instruction ends at a blank line)."""
    at = dockerfile.index(needle)
    start = dockerfile.rindex("\nRUN ", 0, at) + 1
    end = dockerfile.find("\n\n", at)
    return dockerfile[start:] if end < 0 else dockerfile[start:end]


def _workflow(name: str):
    # PyYAML is imported here, not at module level: tests/test_facts_fanout.py imports helpers from
    # this module and facts-validate.yml runs it with only facts/requirements-validation.txt installed.
    # BaseLoader preserves the literal `on` key and keeps scalar comparisons
    # independent of YAML 1.1's surprising yes/no/on boolean conversions.
    import yaml
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class HandleUpstreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WORKFLOWS / "handle-upstream.yml").read_text(encoding="utf-8")

    def test_no_rc_unsafe_version_sort_left(self):
        self.assertNotIn("sort -V", self.text)
        self.assertNotIn("--sort=-v:refname", self.text)

    def test_validates_the_prerelease_flag_and_the_version(self):
        body = _step(self.text, "Validate payload")
        self.assertIn("RAW_PRERELEASE: ${{ github.event.client_payload.prerelease }}", body)
        self.assertIn("python3 scripts/release_pair.py info", body)

    def test_run_id_is_matched_whole_not_per_line(self):
        body = _step(self.text, "Validate payload")
        self.assertIn('[[ "$RUN_ID" =~ ^[0-9]+$ ]]', body)
        self.assertNotIn("grep -Eq '^[0-9]+$'", body)

    def test_landed_pins_come_from_the_semver_latest_tag(self):
        body = _step(self.text, "Resolve the landed")
        self.assertIn("git tag -l 'v*' | python3 scripts/release_pair.py latest-tag", body)

    def test_partner_is_probed_only_on_the_pair_line(self):
        body = _step(self.text, "Probe the partner")
        self.assertIn("if: steps.in.outputs.line == 'pair'", body)
        self.assertIn("pypi.org/pypi/pineforge-codegen/${PYPI}/json", body)
        self.assertIn("pineforge-v${VER}-linux-aarch64.tar.gz", body)

    def test_probe_tells_missing_from_unreachable(self):
        # Only the completing event probes; a flaky "no" would stall the pair
        # green, so anything but 200/404 fails the run instead.
        body = _step(self.text, "Probe the partner")
        self.assertIn("%{http_code}", body)
        self.assertIn("--max-time", body)
        self.assertIn("could not tell whether", body)
        self.assertIn('[ -n "${codes// /}" ]', body)  # no status at all is not "published"

    def test_probe_reads_the_partners_newest_release(self):
        body = _step(self.text, "Probe the partner")
        self.assertIn("https://pypi.org/pypi/pineforge-codegen/json", body)
        self.assertIn("python3 scripts/release_pair.py latest-pep440", body)
        self.assertIn("repos/pineforge-4pass/pineforge-engine/releases", body)
        self.assertIn('echo "partner_latest=', body)

    def test_decision_is_the_script(self):
        body = _step(self.text, "Decide action")
        self.assertIn("python3 scripts/release_pair.py decide", body)
        self.assertIn('--other-published="${PUBLISHED:-false}"', body)
        self.assertIn("PARTNER_LATEST: ${{ steps.partner.outputs.partner_latest }}", body)
        self.assertIn('--partner-latest="${PARTNER_LATEST:-}"', body)

    def test_first_event_of_a_pair_waits_without_building(self):
        body = _step(self.text, "Wait for the partner")
        self.assertIn("if: steps.decide.outputs.mode == 'wait'", body)
        self.assertNotIn("git push", body)

    def test_pair_release_version_is_the_pair_version(self):
        body = _step(self.text, "Bump VERSION")
        self.assertIn("if: steps.decide.outputs.mode == 'bump'", body)
        self.assertIn('if [ "$LINE" = pair ]; then', body)
        self.assertIn('next="$RELEASE"', body)

    def test_half_done_pair_release_is_tagged_in_place(self):
        body = _step(self.text, "Bump VERSION")
        self.assertIn("RETAG:   ${{ steps.decide.outputs.retag }}", body)
        self.assertIn('if [ "$RETAG" = true ]; then', body)
        self.assertLess(body.index('if [ "$RETAG" = true ]; then'), body.index('printf \'%s\\n\' "$next" > VERSION'))

    def test_the_harness_is_synced_by_the_script_from_the_pinned_tag(self):
        body = _step(self.text, "Bump VERSION")
        sync = 'bash scripts/sync-harness.sh "${E}"'
        stage = "git add VERSION ${harness}"
        self.assertIn(sync, body)
        # every synced file, the sums file included, is staged together with VERSION
        self.assertIn('harness="$(bash scripts/sync-harness.sh --files)"', body)
        self.assertIn(stage, body)
        self.assertLess(body.index(sync), body.index(stage))
        # no second copy of the fetch, and nothing in the workflow sets the ref override
        self.assertNotIn("raw.githubusercontent.com", self.text)
        self.assertNotIn("PF_ENGINE_REF", self.text)


class PublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")

    def test_no_rc_unsafe_version_sort_left(self):
        self.assertNotIn("sort -V", self.text)
        self.assertNotIn("sort -uV", self.text)

    def test_pins_fallback_on_the_pair_line_is_the_release_version(self):
        body = _step(self.text, "Read pins")
        self.assertIn('python3 scripts/release_pair.py info "$REF"', body)
        self.assertIn('e="${REF#v}"; c="${REF#v}"', body)

    def test_pair_is_checked_before_anything_is_built(self):
        body = _step(self.text, "Pairing rule")
        self.assertIn("id: pair", body)
        self.assertIn("python3 scripts/release_pair.py check", body)
        self.assertLess(self.text.index("- name: Pairing rule"),
                        self.text.index("docker/build-push-action"))

    def test_waits_for_codegen_under_its_pypi_spelling(self):
        body = _step(self.text, "Wait for upstream artifacts")
        self.assertIn("CP: ${{ steps.pair.outputs.codegen_pypi }}", body)
        self.assertIn("pypi.org/pypi/pineforge-codegen/${CP}/json", body)

    def test_prerelease_never_gets_latest_or_the_moving_minor_tag(self):
        body = _step(self.text, "Image metadata")
        self.assertIn("latest=false", body)
        self.assertIn("type=raw,value=candidate-", body)
        self.assertNotIn("type=semver", body)
        self.assertNotIn("value=latest", body)

    def test_moving_tags_need_the_newest_stable_tag(self):
        body = _step(self.text, "Pairing rule")
        self.assertIn("git tag -l 'v*' | python3 scripts/release_pair.py latest-tag --stable", body)
        self.assertIn('--newest-stable="$newest"', body)

    def test_github_release_list_failure_is_not_a_latest(self):
        import release_pipeline as pipeline
        class FailedStore:
            def releases(self):
                raise pipeline.Invalid("HTTP 503")
        with self.assertRaises(pipeline.Invalid):
            pipeline.finalized_version(FailedStore())

    def test_draft_releases_never_hold_latest(self):
        import release_pipeline as pipeline
        class DraftStore:
            def releases(self):
                return [{"tag_name": "v9.9.9", "draft": True,
                         "assets": [{"name": "release-manifest.json"}]}]
        self.assertIsNone(pipeline.finalized_version(DraftStore()))

    def test_licences_label_covers_the_bundled_transpiler(self):
        # docker/metadata-action's labels override the Dockerfile's, and it
        # derives org.opencontainers.image.licenses from the repository's
        # Apache-2.0, so publish.yml must set the label itself.
        expr = "Apache-2.0 AND LicenseRef-PineForge-Source-License-1.2"
        body = _step(self.text, "Image metadata")
        self.assertIn(f"org.opencontainers.image.licenses={expr}\n", body)
        self.assertIn("labels: ${{ steps.meta.outputs.labels }}", self.text)
        dockerfile = (WORKFLOWS.parent.parent / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f'org.opencontainers.image.licenses="{expr}"', dockerfile)

    def test_github_release_channel(self):
        body = _step(self.text, "GitHub Release")
        self.assertIn("scripts/release_pipeline.py candidate", body)
        self.assertNotIn("gh release create", body)
        call = _workflow("publish.yml")["jobs"]["notify-consumers"]
        self.assertEqual(call["uses"], "./.github/workflows/release-finalize.yml")

    def test_prerelease_flag_is_in_every_dispatch_payload(self):
        finalizer = _workflow("release-finalize.yml")
        steps = finalizer["jobs"]["notify-consumers"]["steps"]
        send = next(step for step in steps if step.get("name", "").startswith("Dispatch pineforge-release"))
        self.assertIn("scripts/release_pipeline.py dispatch", send["run"])
        self.assertNotIn("client_payload[", send["run"])
        self.assertEqual(finalizer["jobs"]["notify-consumers"]["strategy"]["fail-fast"], "false")


class ReleaseTriggerWorkflowTest(unittest.TestCase):
    """Pins and negative controls read/mutate the actual checked-in workflows."""

    VALIDATE_RUN = textwrap.dedent(r"""
        set -euo pipefail
        if [ "$WORKFLOW_ON_MAIN" != true ]; then
          echo '::error::dispatch publish.yml on refs/heads/main'; exit 1
        fi
        if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
          echo '::error::expected an existing vX.Y.Z or vX.Y.Z-rc.N tag'; exit 1
        fi
        git fetch --tags origin '+refs/heads/main:refs/remotes/origin/main'
        if ! TAG_COMMIT="$(git rev-parse -q --verify "refs/tags/${TAG}^{commit}")"; then
          echo "::error::release tag ${TAG} does not exist or does not peel to a commit"; exit 1
        fi
        if ! [[ "$TAG_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
          echo '::error::release tag did not resolve to a 40-character commit SHA'; exit 1
        fi
        if ! git merge-base --is-ancestor "$TAG_COMMIT" refs/remotes/origin/main; then
          echo "::error::release tag ${TAG} is not reachable from main"; exit 1
        fi
        printf 'sha=%s\n' "$TAG_COMMIT" >> "$GITHUB_OUTPUT"
        printf 'tag=%s\n' "$TAG" >> "$GITHUB_OUTPUT"
    """).lstrip("\n")
    REFUSAL_STEP = {
        "name": "Refuse unverified release tag",
        "if": "${{ needs.tag-guard.result != 'success' }}",
        "shell": "bash",
        "run": (
            "echo '::error::tag-guard did not pass; refusing checkout and publication failure reporting'\n"
            "exit 1\n"
        ),
    }
    APT_GUARD_STEP = {
        "name": "Stop automatic apt upgrades (LB-504, until the release image is rebaked)",
        "shell": "bash",
        "run": ("sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer apt-daily.service "
                "apt-daily-upgrade.service unattended-upgrades.service "
                "|| echo \"apt guard: stop failed ($?), continuing\"\n"
                "exit 0\n"),
    }
    FAILURE_IF = (
        "${{ always() && !cancelled() && (needs.tag-guard.result != 'success' || "
        "contains(needs.*.result, 'failure')) }}"
    )
    FINALIZE_IF = (
        "${{ !cancelled() && needs.tag-guard.result == 'success' && "
        "needs.prepare.result == 'success' && "
        "(needs.publish.result == 'success' || needs.publish.result == 'skipped') }}"
    )
    OPERATOR_COMMAND = "gh workflow run publish.yml --repo pineforge-4pass/pineforge-release --ref main -f tag="
    INPUT = (
        '    inputs:\n'
        '      tag:\n'
        '        description: The existing v* tag to build\n'
        '        required: true\n'
        '        type: string\n'
    )

    def parse(self, text):
        import yaml
        return yaml.load(text, Loader=yaml.BaseLoader)

    def mutate(self, text, before, after, count=1):
        self.assertEqual(text.count(before), count, "mutation anchor must match the real workflow exactly")
        mutant = text.replace(before, after)
        self.assertNotEqual(mutant, text, "mutation must change the real workflow")
        return mutant

    def assert_publish(self, text):
        wf = self.parse(text)
        self.assertEqual(wf.get("on"), {"workflow_dispatch": {"inputs": {"tag": {
            "description": "The existing v* tag to build", "required": "true", "type": "string",
        }}}})
        self.assertEqual(re.findall(r"\bgithub\.ref(?:_name)?\b|\bGITHUB_REF_NAME\b", text),
                         ["github.ref", "github.ref"])
        self.assertEqual(text.count("github.ref == 'refs/heads/main'"), 2)
        self.assertNotIn("github.sha", text, "release provenance must use the verified commit")
        self.assertEqual(wf.get("concurrency", {}).get("group"), "release-candidate-${{ inputs.tag }}")
        jobs = wf.get("jobs", {})
        for identity in ("tag-guard", "prepare", "publish", "failure-report"):
            self.assertEqual((jobs.get(identity, {}).get("steps") or [{}])[0], self.APT_GUARD_STEP,
                             identity + " must stop the apt timers before anything else (LB-504)")
        for identity in ("tag-guard", "prepare", "publish", "notify-consumers", "failure-report"):
            self.assertIn(identity, jobs)
        self.assertEqual(next(iter(jobs)), "tag-guard")
        guard = jobs["tag-guard"]
        self.assertEqual(set(guard), {"runs-on", "permissions", "outputs", "steps"})
        self.assertEqual(guard.get("runs-on"), ["self-hosted", "pf-ci", "pf-ci-release"])
        self.assertEqual(guard.get("permissions"), {"contents": "read"})
        self.assertEqual(guard.get("outputs"), {
            "sha": "${{ steps.tag.outputs.sha }}", "tag": "${{ steps.tag.outputs.tag }}",
        })
        self.assertNotIn("secrets.", str(guard))
        self.assertNotIn("github.token", str(guard))
        guard_steps = guard.get("steps") or []
        self.assertEqual(len(guard_steps), 3, "apt guard, then only main checkout, precedes tag verification")
        checkout = guard_steps[1]
        self.assertLessEqual(set(checkout), {"name", "uses", "with"})
        self.assertTrue(checkout.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout.get("with"), {
            "ref": "main", "fetch-depth": "0", "persist-credentials": "false",
        })
        self.assertEqual(guard_steps[2], {
            "name": "Validate release tag", "id": "tag", "shell": "bash",
            "env": {"TAG": "${{ inputs.tag }}",
                    "WORKFLOW_ON_MAIN": "${{ github.ref == 'refs/heads/main' }}"},
            "run": self.VALIDATE_RUN,
        }, "validate before any tagged checkout, secret, or repository code")
        for identity, job in jobs.items():
            if identity != "notify-consumers":
                self.assertEqual(job.get("runs-on"), ["self-hosted", "pf-ci", "pf-ci-release"], identity)
            if identity != "tag-guard":
                needs = job.get("needs") or []
                self.assertIn("tag-guard", [needs] if isinstance(needs, str) else needs, identity)
        prepare = jobs["prepare"]
        self.assertEqual(prepare.get("needs"), "tag-guard")
        self.assertEqual(prepare.get("if"), "github.ref == 'refs/heads/main'")
        self.assertEqual((prepare.get("outputs") or {}).get("tag"), "${{ needs.tag-guard.outputs.tag }}")
        steps = prepare.get("steps", [])
        self.assertEqual(jobs["publish"].get("needs"), ["tag-guard", "prepare"])
        self.assertEqual(jobs["publish"].get("if"), "needs.prepare.outputs.exists == 'false'")
        self.assertEqual((jobs["publish"].get("env") or {}).get("RELEASE_TAG"), "${{ needs.prepare.outputs.tag }}")
        for identity in ("prepare", "publish"):
            checkouts = [row for row in jobs[identity].get("steps", [])
                         if row.get("uses", "").startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 1)
            self.assertEqual((checkouts[0].get("with") or {}).get("ref"),
                             "${{ needs.tag-guard.outputs.sha }}")
            self.assertEqual((checkouts[0].get("with") or {}).get("persist-credentials"), "false")
            if identity == "publish":
                self.assertEqual((checkouts[0].get("with") or {}).get("fetch-depth"), "0")
        candidate = next((row for row in steps if row.get("id") == "candidate"), {})
        self.assertEqual((candidate.get("env") or {}).get("TAG"), "${{ needs.tag-guard.outputs.tag }}")
        named = {row.get("name"): row for row in jobs["publish"].get("steps", [])}
        for name, variable in (("Assert tag matches VERSION", "TAG"), ("Read pins (from the annotated tag message; fallback = the pair version, or resolve latest on 0.x)", "REF"),
                               ("Pairing rule + release channel", "REF"), ("Bind producer source and package bytes", "TAG"),
                               ("Read hpo wheel digest", "REF"), ("GitHub Release", "TAG")):
            self.assertEqual(named.get(name, {}).get("env", {}).get(variable), "${{ env.RELEASE_TAG }}", name)
        labels = named.get("Image metadata", {}).get("with", {}).get("labels", "")
        self.assertIn("org.opencontainers.image.version=${{ env.RELEASE_TAG }}\n", labels)
        self.assertIn("org.opencontainers.image.revision=${{ needs.tag-guard.outputs.sha }}\n", labels)
        self.assertEqual(named.get("Image metadata", {}).get("with", {}).get("tags"),
                         "type=raw,value=candidate-${{ needs.tag-guard.outputs.sha }}-"
                         "${{ github.run_id }}-${{ github.run_attempt }}\n")
        binding = named.get("Bind producer source and package bytes", {})
        self.assertEqual((binding.get("env") or {}).get("HUB_COMMIT"), "${{ needs.tag-guard.outputs.sha }}")
        for name in ("Inspect release amd64", "Inspect release arm64", "Inspect hpo amd64", "Inspect hpo arm64",
                     "Build + push (multi-arch)", "Build + push hpo (multi-arch)"):
            self.assertIn("RELEASE_VERSION=${{ env.RELEASE_TAG }}\n",
                          named.get(name, {}).get("with", {}).get("build-args", ""), name)
            self.assertIn("RELEASE_GIT_SHA=${{ needs.tag-guard.outputs.sha }}\n",
                          named.get(name, {}).get("with", {}).get("build-args", ""), name)
        self.assertEqual(jobs["notify-consumers"].get("needs"), ["tag-guard", "prepare", "publish"])
        self.assertEqual(jobs["notify-consumers"].get("if"), self.FINALIZE_IF)
        self.assertEqual(jobs["notify-consumers"].get("with", {}).get("tag"), "${{ needs.prepare.outputs.tag }}")
        failure = jobs["failure-report"]
        self.assertEqual(failure.get("needs"), ["tag-guard", "prepare", "publish", "notify-consumers"])
        self.assertEqual(failure.get("if"), self.FAILURE_IF)
        failure_steps = failure.get("steps") or []
        self.assertEqual(len(failure_steps), 4)
        self.assertEqual(failure_steps[1], self.REFUSAL_STEP)
        self.assertTrue(failure_steps[2].get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(failure_steps[2].get("with"), {
            "ref": "main", "persist-credentials": "false",
        })
        for row in failure_steps[2:]:
            self.assertEqual(row.get("if"), "needs.tag-guard.result == 'success'")
        # Only the concurrency key and the validation step's env read raw input.
        self.assertEqual(text.count("${{ inputs.tag }}"), 2)
        for job in jobs.values():
            for row in job.get("steps", []):
                self.assertNotIn("${{", row.get("run", ""), "shell reads values from env, never expression interpolation")
                self.assertNotIn("inputs.tag", str(row.get("with", {})))

    def guard_check_mutations(self, text):
        # Remove complete conditionals so the reachability mutant stays valid
        # bash and can actually admit a side-branch tag during the git replay.
        starts = (
            ("main", '          if [ "$WORKFLOW_ON_MAIN" != true ]; then\n'),
            ("pattern", r'          if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then' + "\n"),
            ("existence", '          if ! TAG_COMMIT="$(git rev-parse -q --verify "refs/tags/${TAG}^{commit}")"; then\n'),
            ("sha-shape", '          if ! [[ "$TAG_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then\n'),
            ("reachability", '          if ! git merge-base --is-ancestor "$TAG_COMMIT" refs/remotes/origin/main; then\n'),
        )
        for label, start in starts:
            self.assertEqual(text.count(start), 1, label)
            at = text.index(start)
            end = text.index("          fi\n", at) + len("          fi\n")
            yield label, self.mutate(text, text[at:end], "")

    def assert_no_publish_dispatch(self, text):
        # The operator command is inert notice data. Exempt only a complete
        # quoted assignment, with no command substitution or trailing code.
        code = re.sub(
            r'(?m)^[ \t]*line="(?![^\n]*\$\()[^"`\\\n]*' + re.escape(self.OPERATOR_COMMAND)
            + r'(?:\$\{TAG\}|v\$\{RELEASE\}|v\$\{next\})"$', "", text,
        )
        code = re.sub(r"\\\n[ \t]*", " ", code)
        # No workflow here dispatches any workflow, so the pin does not depend on
        # how the target is named (file, display name, numeric id, variable).
        # Plain repository_dispatch (`repos/<r>/dispatches`) stays allowed.
        for pattern in (r"\bworkflow\s+run\b", r"\bcreateWorkflowDispatch\b",
                        r"\bactions/workflows/[^\s/]+/dispatches\b"):
            self.assertNotRegex(code, pattern, "no workflow may dispatch any workflow")

    def assert_handler(self, text):
        self.assert_no_publish_dispatch(text)
        wf = self.parse(text)
        steps = wf.get("jobs", {}).get("bump", {}).get("steps", [])
        app = next((row for row in steps if row.get("id") == "app"), {})
        self.assertEqual(app.get("with", {}).get("permission-contents"), "write")
        self.assertNotIn("permission-actions", app.get("with", {}))
        self.assertNotRegex(_step(text, "Mint org App token"), r"(?m)^\s*permission-actions\s*:")
        replay = next((row for row in steps if row.get("name", "").startswith("Re-publish")), {})
        bump = next((row for row in steps if row.get("name", "").startswith("Bump VERSION")), {})
        self.assertEqual(replay.get("if"), "steps.decide.outputs.mode == 'republish'")
        self.assertEqual(replay.get("env", {}).get("TAG"), "v${{ steps.decide.outputs.cur_ver }}")
        for step in (replay, bump):
            self.assertNotIn("GH_TOKEN", step.get("env", {}))
        self.assertEqual(bump.get("name"), "Bump VERSION, commit, tag")
        self.assertEqual(bump.get("if"), "steps.decide.outputs.mode == 'bump'")
        summary = 'printf \'%s\\n\' "$line" >> "$GITHUB_STEP_SUMMARY"'
        replay_run = replay.get("run", "")
        self.assertIn(self.OPERATOR_COMMAND + '${TAG}"', replay_run)
        self.assertIn('echo "::warning title=pineforge publish needed::${line}"', replay_run)
        self.assertIn(summary, replay_run)
        self.assertEqual(replay_run.splitlines()[-1], "exit 0")
        run = bump.get("run", "")
        commands = [line.strip() for line in run.splitlines()]
        for variable in ("RELEASE", "next"):
            # Both paths print the operator command only after the tag push succeeds.
            push = 'git push origin "v${' + variable + '}"'
            self.assertIn(push, commands)
            at = commands.index(push)
            self.assertLess(at + 3, len(commands))
            self.assertTrue(commands[at + 1].startswith('line="pushed v${' + variable + '}'))
            self.assertIn(self.OPERATOR_COMMAND + 'v${' + variable + '}"', commands[at + 1])
            self.assertEqual(commands[at + 2], 'echo "::notice title=pineforge publish needed::${line}"')
            self.assertEqual(commands[at + 3], summary)
            if variable == "RELEASE":
                self.assertLess(at + 4, len(commands))
                self.assertEqual(commands[at + 4], "exit 0")

    def assert_release_triggers(self, text):
        wf = self.parse(text)
        declared = wf.get("on")
        self.assertIsInstance(declared, (dict, list, str))
        events = {declared} if isinstance(declared, str) else set(declared)
        self.assertTrue(events)
        self.assertLessEqual(events, {"workflow_dispatch", "schedule", "repository_dispatch", "workflow_call",
                                      "push", "pull_request_target"}, "pf-ci-release needs a main-ref event")
        if "push" in events:
            self.assertIsInstance(declared, dict, "push requires an explicit main branch filter")
            push = declared.get("push")
            self.assertIsInstance(push, dict)
            self.assertEqual(push.get("branches"), ["main"])
            self.assertLessEqual(set(push), {"branches", "paths", "paths-ignore"}, "no tag or branch-ignore filters")

    def test_publish_uses_operator_dispatch_and_handler_prints_the_next_command(self):
        self.assert_publish((WORKFLOWS / "publish.yml").read_text())
        self.assert_handler((WORKFLOWS / "handle-upstream.yml").read_text())

    def test_no_workflow_dispatches_publish(self):
        found = []
        for path in sorted(WORKFLOWS.iterdir()):
            if path.suffix in {".yml", ".yaml"}:
                found.append(path.name)
                with self.subTest(workflow=path.name):
                    self.assert_no_publish_dispatch(path.read_text())
        self.assertIn("handle-upstream.yml", found)
        self.assertIn("publish.yml", found)

    def test_no_dispatch_pin_refuses_cli_javascript_and_api_steps_on_real_text(self):
        text = (WORKFLOWS / "handle-upstream.yml").read_text()
        self.assert_no_publish_dispatch(text)
        anchor = "      - name: Report failed upstream handling\n"
        steps = (
            "        run: " + self.OPERATOR_COMMAND + "v1.6.2\n",
            "        run: gh workflow run --ref main 'publish.yml' -f tag=v1.6.2\n",
            '        env:\n          WORKFLOW: publish.yml\n        run: gh workflow run "$WORKFLOW" --ref main -f tag=v1.6.2\n',
            "        run: |\n          gh workflow run \\\n            publish.yml --ref main -f tag=v1.6.2\n",
            "        uses: actions/github-script@fixture\n        with:\n          script: |\n"
            "            await github.rest.actions.createWorkflowDispatch({\n"
            "              workflow_id: 'publish.yml', ref: 'main', inputs: {tag: 'v1.6.2'}\n"
            "            });\n",
            "        run: gh api --method POST repos/pineforge-4pass/pineforge-release/actions/workflows/publish.yml/dispatches -f ref=main\n",
            "        env:\n          WORKFLOW: publish.yml\n        run: |\n"
            '          curl --request POST "https://api.github.com/repos/pineforge-4pass/pineforge-release/actions/workflows/${WORKFLOW}/dispatches"\n',
            '        run: |\n          line="$(' + self.OPERATOR_COMMAND + 'v1.6.2)"\n',
            '        run: gh workflow run "Publish release candidate" --ref main -f tag=v1.6.2\n',
            "        run: gh api -X POST repos/pineforge-4pass/pineforge-release/actions/workflows/123456789/dispatches -f ref=main\n",
            "        uses: actions/github-script@fixture\n        with:\n          script: |\n"
            "            await github.request(\n"
            "              'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',\n"
            "              {owner, repo, workflow_id: 123456789, ref: 'main'}\n"
            "            );\n",
        )
        for step in steps:
            mutant = self.mutate(text, anchor, "      - name: Forbidden publish dispatch\n" + step + anchor)
            with self.subTest(step=step), self.assertRaises(AssertionError):
                self.assert_no_publish_dispatch(mutant)

    def test_every_workflow_using_the_release_label_has_only_main_ref_trigger_types(self):
        found = []
        for path in sorted(WORKFLOWS.iterdir()):
            if path.suffix in {".yml", ".yaml"}:
                text = path.read_text()
                if "pf-ci-release" in text:
                    found.append(path.name)
                    with self.subTest(workflow=path.name):
                        self.assert_release_triggers(text)
        self.assertIn("publish.yml", found, "the generic scan must inspect the real release workflows")

    def test_publish_refuses_bad_triggers_and_each_broken_input_boundary_on_real_text(self):
        text = (WORKFLOWS / "publish.yml").read_text()
        self.assert_publish(text)
        mutations = [
            ("on:\n", 'on:\n  push:\n    tags: ["v*"]\n', 1),
            ("on:\n", 'on:\n  release:\n    types: [published]\n', 1),
            ("on:\n", 'on:\n  workflow_run:\n    workflows: [publish]\n    types: [completed]\n', 1),
            (self.INPUT, "", 1),
            (self.INPUT, self.INPUT + '      extra:\n        type: string\n', 1),
            ("description: The existing v* tag to build", "description: Any ref to build", 1),
            ("        required: true\n", "        required: false\n", 1),
            ("        type: string\n", "        type: boolean\n", 1),
            ("release-candidate-${{ inputs.tag }}", "release-candidate-${{ github.ref_name }}", 1),
            ("    if: github.ref == 'refs/heads/main'\n", "", 1),
            ("    if: github.ref == 'refs/heads/main'", "    if: startsWith(github.ref, 'refs/tags/v')", 1),
            ("  tag-guard:\n", "  tag-guard:\n    if: github.ref == 'refs/heads/main'\n", 1),
            ("  tag-guard:\n", "  tag-guard:\n    needs: prepare\n", 1),
            ("  tag-guard:\n", "  tag-guard:\n    environment: release-automation\n", 1),
            ("  tag-guard:\n", "  tag-guard:\n    env:\n      GH_TOKEN: ${{ github.token }}\n", 1),
            ("    permissions:\n      contents: read\n    outputs:",
             "    permissions:\n      contents: write\n    outputs:", 1),
            ("            echo '::error::expected an existing vX.Y.Z or vX.Y.Z-rc.N tag'; exit 1",
             "            echo '::error::expected an existing vX.Y.Z or vX.Y.Z-rc.N tag'; exit 0", 1),
            ("          TAG: ${{ inputs.tag }}\n", "", 1),
            ("          WORKFLOW_ON_MAIN: ${{ github.ref == 'refs/heads/main' }}\n", "", 1),
            ("          WORKFLOW_ON_MAIN: ${{ github.ref == 'refs/heads/main' }}",
             "          WORKFLOW_ON_MAIN: ${{ github.ref == 'refs/heads/topic' }}", 1),
            ("      - name: Validate release tag", "      - run: echo skipped validation\n      - name: Validate release tag", 1),
            ('          printf \'tag=%s\\n\' "$TAG"', '          printf \'tag=%s\\n\' "${{ inputs.tag }}"', 1),
            ("      tag: ${{ steps.tag.outputs.tag }}\n", "", 1),
            ("      sha: ${{ steps.tag.outputs.sha }}\n", "", 1),
            ("      sha: ${{ steps.tag.outputs.sha }}", "      sha: ${{ steps.tag.outputs.tag }}", 1),
            ("      tag: ${{ needs.tag-guard.outputs.tag }}\n", "", 1),
            ("      RELEASE_TAG: ${{ needs.prepare.outputs.tag }}\n", "", 1),
            ("      RELEASE_TAG: ${{ needs.prepare.outputs.tag }}", "      RELEASE_TAG: ${{ inputs.tag }}", 1),
            ("          TAG: ${{ needs.tag-guard.outputs.tag }}", "          TAG: ${{ inputs.tag }}", 1),
            ("      tag: ${{ needs.prepare.outputs.tag }}", "      tag: ${{ inputs.tag }}", 1),
            ("    needs: tag-guard\n", "", 1),
            ("    needs: [tag-guard, prepare]", "    needs: prepare", 1),
            ("    if: needs.prepare.outputs.exists == 'false'", "    if: always()", 1),
            ("    needs: [tag-guard, prepare, publish]", "    needs: [prepare, publish]", 1),
            ("    needs: [tag-guard, prepare, publish, notify-consumers]",
             "    needs: [prepare, publish, notify-consumers]", 1),
            ("    if: " + self.FINALIZE_IF, "    if: ${{ always() }}", 1),
            ("    if: " + self.FINALIZE_IF,
             "    if: " + self.FINALIZE_IF.replace("needs.tag-guard.result == 'success' && ", ""), 1),
            ("    if: " + self.FAILURE_IF, "    if: ${{ failure() && !cancelled() }}", 1),
            ("      - name: Refuse unverified release tag\n",
             "      - run: python3 scripts/release_pipeline.py alert --stage publish\n"
             "      - name: Refuse unverified release tag\n", 1),
            ("        if: ${{ needs.tag-guard.result != 'success' }}\n", "", 1),
            ("        if: ${{ needs.tag-guard.result != 'success' }}",
             "        if: ${{ needs.tag-guard.result == 'failure' }}", 1),
            ("          exit 1\n      - uses: actions/checkout@", "          exit 0\n      - uses: actions/checkout@", 1),
            ("          HUB_COMMIT: ${{ needs.tag-guard.outputs.sha }}",
             "          HUB_COMMIT: ${{ github.sha }}", 1),
            ("type=raw,value=candidate-${{ needs.tag-guard.outputs.sha }}",
             "type=raw,value=candidate-${{ github.sha }}", 1),
            ("org.opencontainers.image.revision=${{ needs.tag-guard.outputs.sha }}",
             "org.opencontainers.image.revision=${{ github.sha }}", 1),
            ("org.opencontainers.image.version=${{ env.RELEASE_TAG }}", "org.opencontainers.image.version=${{ github.ref }}", 1),
            ("org.opencontainers.image.version=${{ env.RELEASE_TAG }}", "org.opencontainers.image.version=${{ env.GITHUB_REF_NAME }}", 1),
        ]
        for label, mutant in self.guard_check_mutations(text):
            with self.subTest(check=label), self.assertRaises(AssertionError):
                self.assert_publish(mutant)
        # These anchors repeat: mutate each occurrence independently rather
        # than letting the first checkout/build pin hide a later missing pin.
        for before, after, count in (
            ("          TAG: ${{ env.RELEASE_TAG }}", "          TAG: v0.0.0", 3),
            ("          REF: ${{ env.RELEASE_TAG }}", "          REF: v0.0.0", 3),
            ("RELEASE_VERSION=${{ env.RELEASE_TAG }}", "RELEASE_VERSION=v0.0.0", 6),
            ("RELEASE_GIT_SHA=${{ needs.tag-guard.outputs.sha }}", "RELEASE_GIT_SHA=${{ github.sha }}", 6),
            ("          ref: ${{ needs.tag-guard.outputs.sha }}\n", "", 2),
            ("          ref: ${{ needs.tag-guard.outputs.sha }}",
             "          ref: refs/tags/${{ inputs.tag }}", 2),
            ("          persist-credentials: false\n", "          persist-credentials: true\n", 4),
            ("          fetch-depth: 0\n", "          fetch-depth: 1\n", 2),
            ("          ref: main\n", "          ref: ${{ inputs.tag }}\n", 2),
            # 2 tag consumers + the 4 apt guards (LB-504); every removal must still break a pin
            ("        shell: bash\n", "", 6),
            ("        if: needs.tag-guard.result == 'success'\n", "", 2),
            ('    runs-on: ["self-hosted","pf-ci","pf-ci-release"]\n', "    runs-on: ubuntu-latest\n", 4),
        ):
            self.assertEqual(text.count(before), count, "each real tag consumer must be mutated")
            for index in range(count):
                # Mutate each consumer separately so the first pin cannot hide a missing later one.
                at = [match.start() for match in re.finditer(re.escape(before), text)][index]
                mutant = text[:at] + text[at:].replace(before, after, 1)
                self.assertNotEqual(mutant, text)
                with self.subTest(consumer=before, occurrence=index), self.assertRaises(AssertionError):
                    self.assert_publish(mutant)
        for before, after, count in mutations:
            mutant = self.mutate(text, before, after, count)
            with self.subTest(anchor=before), self.assertRaises(AssertionError):
                self.assert_publish(mutant)
        guard_start = text.index("  tag-guard:\n")
        guard_end = text.index("  prepare:\n", guard_start)
        guard_block = text[guard_start:guard_end]
        late_guard = self.mutate(text, guard_block, "") + "\n" + guard_block
        with self.assertRaises(AssertionError):
            self.assert_publish(late_guard)

    def test_handler_pin_refuses_dispatch_permissions_tokens_or_broken_operator_notices(self):
        text = (WORKFLOWS / "handle-upstream.yml").read_text()
        self.assert_handler(text)
        for tag in ('${TAG}', 'v${RELEASE}', 'v${next}'):
            command = self.OPERATOR_COMMAND + tag
            for replacement in (command.replace("--ref main", '--ref topic'),
                                command.removesuffix(" -f tag=" + tag), ""):
                mutant = self.mutate(text, command, replacement)
                with self.subTest(command=command, replacement=replacement), self.assertRaises(AssertionError):
                    self.assert_handler(mutant)
        for before, after in (
            ("          permission-contents: write\n",
             "          permission-contents: write\n          permission-actions: write\n"),
            ('        env:\n          TAG: v${{ steps.decide.outputs.cur_ver }}',
             '        env:\n          GH_TOKEN: ${{ steps.app.outputs.token }}\n          TAG: v${{ steps.decide.outputs.cur_ver }}'),
            ('        env:\n          E:', '        env:\n          GH_TOKEN: ${{ steps.app.outputs.token }}\n          E:'),
            ("        if: steps.decide.outputs.mode == 'republish'", "        if: always()"),
            ("        if: steps.decide.outputs.mode == 'bump'", "        if: always()"),
            ("          TAG: v${{ steps.decide.outputs.cur_ver }}", "          TAG: v0.0.0"),
            ("      - name: Bump VERSION, commit, tag\n", "      - name: Bump VERSION, commit, tag (fires publish.yml)\n"),
            ('echo "::warning title=pineforge publish needed::${line}"', 'echo "$line"'),
        ):
            mutant = self.mutate(text, before, after)
            with self.subTest(anchor=before), self.assertRaises(AssertionError):
                self.assert_handler(mutant)
        for name, annotation, notices in (("Re-publish", "warning", 1), ("Bump VERSION", "notice", 2)):
            body = _step(text, name)
            for command, count in (
                ('printf \'%s\\n\' "$line" >> "$GITHUB_STEP_SUMMARY"', notices),
                ('echo "::' + annotation + ' title=pineforge publish needed::${line}"', notices),
                ("exit 0", 1),
            ):
                matches = list(re.finditer(re.escape(command), body))
                self.assertEqual(len(matches), count, "each notice mutation must have its real anchor")
                for match in matches:
                    changed = body[:match.start()] + body[match.end():]
                    mutant = self.mutate(text, body, changed)
                    with self.subTest(step=name, command=command, occurrence=match.start()), self.assertRaises(AssertionError):
                        self.assert_handler(mutant)

    def test_generic_trigger_pin_refuses_forbidden_events_and_tag_pushes_in_every_real_workflow(self):
        for path in sorted(WORKFLOWS.iterdir()):
            if path.suffix not in {".yml", ".yaml"}:
                continue
            text = path.read_text()
            if "pf-ci-release" not in text:
                continue
            self.assert_release_triggers(text)
            for event in ('  release:\n    types: [published]\n',
                          '  workflow_run:\n    workflows: [publish]\n    types: [completed]\n',
                          '  pull_request:\n', '  unknown_event:\n'):
                mutant = self.mutate(text, "on:\n", "on:\n" + event)
                with self.subTest(workflow=path.name, event=event), self.assertRaises(AssertionError):
                    self.assert_release_triggers(mutant)
            if "push" in self.parse(text)["on"]:
                anchor = "    branches: [main]\n"
                replacements = ('    tags: ["v*"]\n', "", '    branches: [main, other]\n',
                                '    branches: [main]\n    tags: ["v*"]\n')
            else:
                anchor = "on:\n"
                replacements = ('on:\n  push:\n    tags: ["v*"]\n', 'on:\n  push:\n',
                                'on:\n  push:\n    branches: [other]\n')
            for replacement in replacements:
                mutant = self.mutate(text, anchor, replacement)
                with self.subTest(workflow=path.name, push=replacement), self.assertRaises(AssertionError):
                    self.assert_release_triggers(mutant)
            on_block = re.search(r"(?ms)^on:\n.*?(?=^\S|\Z)", text)
            self.assertIsNotNone(on_block)
            for declaration in ("on: release\n\n", "on: [workflow_dispatch, release]\n\n",
                                "on: push\n\n", "on: [workflow_dispatch, push]\n\n"):
                mutant = self.mutate(text, on_block[0], declaration)
                with self.subTest(workflow=path.name, declaration=declaration), self.assertRaises(AssertionError):
                    self.assert_release_triggers(mutant)


class ReleaseTagGuardGitTest(unittest.TestCase):
    """CI executes the workflow's actual guard with real git, using file remotes."""

    # Literal expectations, independent of the workflow's regex/commands.
    # Commit selectors refer to fixture commits created before the guard runs.
    # Optional trailing (branch, commit selector) fields add local clone refs.
    CASES = (
        ("v1.6.2", "true", 0, "first", ""),
        ("v1.6.3", "true", 0, "second", ""),
        ("not-a-release", "true", 1, None, "expected an existing vX.Y.Z or vX.Y.Z-rc.N tag"),
        ("v9.9.9", "true", 1, None, "does not exist or does not peel to a commit"),
        ("v1.6.4", "true", 1, None, "is not reachable from main"),
        ("v1.6.3-rc.1", "true", 0, "second", ""),
        ("v1.6.6", "true", 1, None, "does not exist or does not peel to a commit"),
        ("v1.6.2\nextra", "true", 1, None, "expected an existing vX.Y.Z or vX.Y.Z-rc.N tag"),
        ("v1.6.2", "false", 1, None, "dispatch publish.yml on refs/heads/main"),
        ("v1.6.2", "", 1, None, "dispatch publish.yml on refs/heads/main"),
        ("v1.6.7", "true", 1, None, "does not exist or does not peel to a commit", ("v1.6.7", "second")),
        ("v1.6.2", "true", 0, "first", "", ("v1.6.2", "second")),
    )

    def setUp(self):
        self.git_binary = shutil.which("git")
        self.assertIsNotNone(self.git_binary, "CI runner cannot run git; tag-guard replay is required")
        self.bash_binary = shutil.which("bash")
        self.assertIsNotNone(self.bash_binary, "CI runner cannot run bash; tag-guard replay is required")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
        }
        self.origin = self.root / "origin"
        self.git(self.root, "init", "--initial-branch=main", "--object-format=sha1", str(self.origin))
        self.git(self.origin, "config", "user.name", "Tag guard fixture")
        self.git(self.origin, "config", "user.email", "tag-guard@example.invalid")
        self.git(self.origin, "config", "commit.gpgsign", "false")
        self.git(self.origin, "config", "tag.gpgsign", "false")
        self.git(self.origin, "commit", "--allow-empty", "-m", "First main commit")
        first = self.git(self.origin, "rev-parse", "HEAD")
        self.git(self.origin, "commit", "--allow-empty", "-m", "Second main commit")
        second = self.git(self.origin, "rev-parse", "HEAD")
        self.commits = {"first": first, "second": second}
        self.git(self.origin, "tag", "v1.6.2", first)
        self.git(self.origin, "tag", "v1.6.3", second)
        self.git(self.origin, "tag", "not-a-release", second)
        self.git(self.origin, "tag", "-a", "v1.6.3-rc.1", second, "-m", "Annotated release")
        self.tag_object = self.git(self.origin, "rev-parse", "refs/tags/v1.6.3-rc.1")
        self.assertNotEqual(self.tag_object, second, "fixture must distinguish tag object from commit")
        tree = self.git(self.origin, "rev-parse", "HEAD^{tree}")
        self.git(self.origin, "tag", "v1.6.6", tree)
        self.git(self.origin, "checkout", "-b", "side", first)
        self.git(self.origin, "commit", "--allow-empty", "-m", "Unmerged side commit")
        self.git(self.origin, "tag", "v1.6.4")
        # Decoy tags named like main on the side commit: git resolves the short
        # name origin/main in refs/tags/ before refs/remotes/, so a guard that
        # names main loosely would accept the side-branch tag v1.6.4.
        for decoy in ("origin/main", "main", "heads/main", "remotes/origin/main"):
            self.git(self.origin, "tag", decoy)
        self.assertEqual(self.git(self.origin, "tag", "--list", "v1.6.7"), "", "branch-only case must have no origin tag")
        self.git(self.origin, "checkout", "main")
        self.checkout_index = 0
        self.text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")

    def git(self, cwd, *args):
        done = subprocess.run(
            [self.git_binary, *args], cwd=cwd, env=self.env,
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, (args, done.stdout, done.stderr))
        return done.stdout.strip()

    def checkout(self):
        self.checkout_index += 1
        work = self.root / f"work-{self.checkout_index}"
        self.git(self.root, "clone", "--no-local", "--no-tags", "--single-branch",
                 "--branch", "main", str(self.origin), str(work))
        self.assertEqual(self.git(work, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(self.git(work, "tag", "--list"), "", "the guard must fetch the real tags")
        return work

    def run_guard(self, text, work, tag, on_main="true"):
        # Extract from the real YAML (or a mutation of that YAML). Do not use
        # VALIDATE_RUN or assert_publish here: execution must kill the mutants.
        wf = ReleaseTriggerWorkflowTest().parse(text)
        guard = (wf.get("jobs") or {}).get("tag-guard") or {}
        step = next((row for row in guard.get("steps", []) if row.get("id") == "tag"), {})
        self.assertIsInstance(step.get("run"), str, "guard script must exist")
        output = self.root / "guard-output"
        output.write_text("", encoding="utf-8")
        done = subprocess.run(
            [self.bash_binary, "--noprofile", "--norc", "-c", step["run"]],
            cwd=work, env={**self.env, "TAG": tag, "WORKFLOW_ON_MAIN": on_main,
                           "GITHUB_OUTPUT": str(output)},
            capture_output=True, text=True,
        )
        return done, output.read_text(encoding="utf-8").splitlines()

    def assert_case(self, text, work, case):
        tag, on_main, expected_code, commit, error, *local_branches = case
        # Extra row fields create LOCAL refs in the work clone before the guard.
        for name, target in local_branches:
            self.git(work, "branch", name, self.commits[target])
        done, output = self.run_guard(text, work, tag, on_main)
        self.assertEqual(done.returncode, expected_code, (case, done.stdout, done.stderr))
        if expected_code == 0:
            self.assertEqual(output, ["sha=" + self.commits[commit], "tag=" + tag], case)
            self.assertNotIn("::error::", done.stdout + done.stderr)
            if tag == "v1.6.3-rc.1":
                self.assertNotEqual(output[0], "sha=" + self.tag_object)
        else:
            self.assertEqual(output, [], "a refused tag must not emit an accepted SHA or tag")
            self.assertIn("::error::", done.stdout, case)
            self.assertIn(error, done.stdout, case)

    def assert_table(self, text):
        # No subTest here: the outer assertRaises must see the failed outcome
        # of a mutant, rather than having a subTest consume the assertion.
        work = self.checkout()
        for case in self.CASES:
            self.assert_case(text, work, case)

    def test_actual_tag_guard_against_real_git_table(self):
        work = self.checkout()
        for case in self.CASES:
            with self.subTest(tag=case[0], on_main=case[1], expected_code=case[2]):
                self.assert_case(self.text, work, case)

    def test_real_git_table_refuses_missing_pattern_existence_reachability_or_main_check(self):
        self.assert_table(self.text)
        pins = ReleaseTriggerWorkflowTest()
        for label, mutant in pins.guard_check_mutations(self.text):
            if label == "sha-shape":
                continue  # Shape is pinned separately; real git always returns a commit SHA.
            with self.subTest(removed_check=label), self.assertRaises(AssertionError):
                self.assert_table(mutant)
        unpeeled = pins.mutate(self.text, '"refs/tags/${TAG}^{commit}"', '"refs/tags/${TAG}"')
        with self.subTest(check="annotated tag must output its commit"), self.assertRaises(AssertionError):
            self.assert_table(unpeeled)
        bare_tag = pins.mutate(self.text, '"refs/tags/${TAG}^{commit}"', '"${TAG}^{commit}"')
        with self.subTest(check="local branch v1.6.7 is not a release tag"), self.assertRaisesRegex(AssertionError, r"v1\.6\.7"):
            self.assert_table(bare_tag)
        short_name = pins.mutate(
            self.text, 'is-ancestor "$TAG_COMMIT" refs/remotes/origin/main', 'is-ancestor "$TAG_COMMIT" origin/main')
        with self.subTest(check="a tag named origin/main must not stand in for main"), self.assertRaises(AssertionError):
            self.assert_table(short_name)

    def test_guard_fetches_updated_main_before_testing_reachability(self):
        work = self.checkout()
        self.git(self.origin, "commit", "--allow-empty", "-m", "Main advanced after checkout")
        self.git(self.origin, "tag", "v2.0.0")
        expected = self.git(self.origin, "rev-parse", "HEAD")
        done, output = self.run_guard(self.text, work, "v2.0.0")
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertEqual(output, ["sha=" + expected, "tag=v2.0.0"])
        self.assertEqual(self.git(work, "rev-parse", "refs/remotes/origin/main"), expected)

    def test_guard_fails_closed_when_fetch_cannot_read_origin(self):
        work = self.checkout()
        self.git(work, "remote", "set-url", "origin", str(self.root / "missing-origin"))
        # An invalid name is refused before even attempting the broken fetch.
        self.assert_case(self.text, work, self.CASES[2])
        done, output = self.run_guard(self.text, work, "v1.6.2")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(output, [])


class ReleaseTagGuardFailureReportTest(unittest.TestCase):
    def assert_refusal(self, text):
        wf = ReleaseTriggerWorkflowTest().parse(text)
        job = (wf.get("jobs") or {}).get("failure-report") or {}
        steps = job.get("steps") or []
        self.assertTrue(steps, "failure reporting must start with a refusal")
        step = steps[0]
        condition = step.get("if", "").removeprefix("${{").removesuffix("}}").strip()
        comparison = re.fullmatch(r"needs\.tag-guard\.result (==|!=) 'success'", condition)
        self.assertIsNotNone(comparison)
        self.assertIsInstance(step.get("run"), str)
        operator = "=" if comparison[1] == "==" else "!="
        script = f'if [ "$GUARD_RESULT" {operator} success ]; then\n{step["run"]}\nfi\n'
        for status, expected_code in (
            ("success", 0), ("failure", 1), ("skipped", 1),
            ("cancelled", 1), ("", 1), ("unknown", 1),
        ):
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                env={"PATH": os.environ.get("PATH", os.defpath), "GUARD_RESULT": status},
                capture_output=True, text=True,
            )
            self.assertEqual(done.returncode, expected_code, (status, done.stdout, done.stderr))
            if expected_code:
                self.assertIn("::error::tag-guard did not pass", done.stdout)

    def test_actual_first_refusal_rejects_failed_skipped_cancelled_or_missing_guard(self):
        self.assert_refusal((WORKFLOWS / "publish.yml").read_text())

    def test_executed_refusal_rejects_successful_exit_or_failure_only_condition(self):
        text = (WORKFLOWS / "publish.yml").read_text()
        self.assert_refusal(text)
        pins = ReleaseTriggerWorkflowTest()
        for before, after in (
            ("          exit 1\n      - uses: actions/checkout@", "          exit 0\n      - uses: actions/checkout@"),
            ("        if: ${{ needs.tag-guard.result != 'success' }}",
             "        if: ${{ needs.tag-guard.result == 'failure' }}"),
        ):
            mutant = pins.mutate(text, before, after)
            with self.subTest(anchor=before), self.assertRaises(AssertionError):
                self.assert_refusal(mutant)


class SourceMirrorWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = _workflow("publish.yml")
        cls.job = cls.workflow["jobs"]["publish"]
        cls.steps = cls.job["steps"]
        cls.named = {step["name"]: step for step in cls.steps if "name" in step}

    def test_toolchain_file_is_validated_and_exported_to_the_hpo_build(self):
        reader = self.named["Read hpo toolchain arguments"]
        self.assertEqual(reader["id"], "toolchain")
        self.assertEqual(reader["if"], "steps.pair.outputs.target == 'hpo'")
        run = reader["run"]
        self.assertIn("set -euo pipefail", run)
        self.assertIn("python3 scripts/release_sources.py toolchain-args docker/hpo-toolchain.args", run)
        self.assertIn("args<<HPO_TOOLCHAIN_ARGS", run)
        self.assertIn('"$GITHUB_OUTPUT"', run)
        self.assertNotRegex(run, r"\b(?:source|eval)\b")
        self.assertNotIn("|| true", run)
        build = self.named["Build + push hpo (multi-arch)"]
        self.assertIn("${{ steps.toolchain.outputs.args }}\n", build["with"]["build-args"])
        for existing in ("ENGINE_VERSION=${{ steps.pins.outputs.engine }}",
                         "CODEGEN_VERSION=${{ steps.pins.outputs.codegen }}",
                         "${{ steps.pair.outputs.hpo_version_arg }}",
                         "${{ steps.binding.outputs.hpo_wheel_arg }}",
                         "${{ steps.binding.outputs.hpo_sdist_arg }}",
                         "RELEASE_VERSION=${{ env.RELEASE_TAG }}",
                         "RELEASE_GIT_SHA=${{ needs.tag-guard.outputs.sha }}"):
            self.assertIn(existing, build["with"]["build-args"])
        self.assertLess(self.steps.index(reader), self.steps.index(build))

    def test_local_inspections_match_each_real_build_on_each_architecture(self):
        expected = (
            ("release", "Build + push (multi-arch)"),
            ("hpo", "Build + push hpo (multi-arch)"),
        )
        guard = self.named["Check Debian source mirror"]
        for target, publish_name in expected:
            real = self.named[publish_name]
            scopes = [f"type=gha,scope={target}-{arch}" for arch in ("amd64", "arm64")]
            self.assertEqual(real["with"]["cache-from"].splitlines(), scopes,
                             "push must reuse each architecture's inspected cache")
            self.assertEqual(real["with"]["cache-to"].splitlines(),
                             [scope + ",mode=max" for scope in scopes])
            for arch in ("amd64", "arm64"):
                with self.subTest(target=target, arch=arch):
                    inspection = self.named[f"Inspect {target} {arch}"]
                    self.assertEqual(inspection["uses"], real["uses"])
                    self.assertEqual(inspection["if"], real["if"])
                    config = inspection["with"]
                    self.assertEqual(config["platforms"], f"linux/{arch}")
                    self.assertEqual(config["load"], "true")
                    self.assertEqual(config["push"], "false")
                    self.assertEqual(config["tags"], f"pineforge-source-inspect:{arch}")
                    for key in ("context", "file", "target", "build-args", "labels"):
                        self.assertEqual(config.get(key), real["with"].get(key), key)
                    scope = f"type=gha,scope={target}-{arch}"
                    self.assertEqual(config["cache-from"], scope,
                                     "inspection cache scope must name its target and architecture")
                    self.assertEqual(config["cache-to"], scope + ",mode=max")
                    self.assertIn(config["cache-from"], real["with"]["cache-from"].splitlines())
                    self.assertIn(config["cache-to"], real["with"]["cache-to"].splitlines())
                    self.assertLess(self.steps.index(inspection), self.steps.index(guard))
                    self.assertNotIn("continue-on-error", inspection)
                    if target == "hpo":
                        self.assertLess(self.steps.index(self.named["Read hpo toolchain arguments"]),
                                        self.steps.index(inspection))
                        self.assertIn("${{ steps.toolchain.outputs.args }}", config["build-args"])

    def test_plain_release_inspections_and_push_receive_the_pinned_version_arguments(self):
        for name in ("Inspect release amd64", "Inspect release arm64", "Build + push (multi-arch)"):
            with self.subTest(step=name):
                arguments = self.named[name]["with"]["build-args"].splitlines()
                for expected in ("ENGINE_VERSION=${{ steps.pins.outputs.engine }}",
                                 "CODEGEN_VERSION=${{ steps.pins.outputs.codegen }}",
                                 "RELEASE_VERSION=${{ env.RELEASE_TAG }}"):
                    self.assertIn(expected, arguments, name + " must receive " + expected)

    def test_guard_is_mandatory_before_every_push_and_release_including_prereleases(self):
        names = ("Read Debian package inventories", "Locate Debian source bundle",
                 "Read Debian source release assets", "Check Debian source mirror")
        order = [self.steps.index(self.named[name]) for name in names]
        self.assertEqual(order, sorted(order))
        for name in names:
            step = self.named[name]
            self.assertNotIn("if", step, f"{name} must apply to every v* tag")
            self.assertNotIn("continue-on-error", step)
            self.assertIn("set -euo pipefail", step["run"])
            self.assertNotIn("|| true", step["run"])
        self.assertNotIn("continue-on-error", self.job)
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(self.workflow["on"]["workflow_dispatch"]["inputs"]["tag"]["required"], "true")
        pushes = [step for step in self.steps if step.get("with", {}).get("push") == "true"]
        self.assertEqual(len(pushes), 2, "test must find both actual publishing actions")
        releases = [step for step in self.steps if "scripts/release_pipeline.py candidate --file" in step.get("run", "")]
        self.assertEqual(len(releases), 1, "test must find the existing release step")
        for step in pushes + releases:
            self.assertLess(order[-1], self.steps.index(step))
            self.assertNotIn("continue-on-error", step)
            self.assertNotRegex(step.get("if", ""), r"\b(?:always|failure|cancelled)\s*\(")
        run = self.named["Check Debian source mirror"]["run"]
        self.assertIn("python3 scripts/release_sources.py check", run)
        for argument in ("--registry docker/SOURCE-SETS.tsv", "--amd64", "--arm64",
                         "--release-assets", "--sources-list"):
            self.assertIn(argument, run)

    def check_arguments(self, name):
        run = re.sub(r"\\\n\s*", " ", self.named[name]["run"])
        commands = [line.strip() for line in run.splitlines()
                    if line.strip().startswith("python3 scripts/release_sources.py check ")]
        self.assertEqual(len(commands), 1, name + " must run the real source check once")
        tokens = shlex.split(commands[0])
        result = {}
        for flag in ("--registry", "--amd64", "--arm64", "--release-assets", "--sources-list"):
            self.assertEqual(tokens.count(flag), 1, name + " must supply " + flag + " once")
            result[flag] = tokens[tokens.index(flag) + 1]
        return result

    def test_pre_and_post_checks_use_distinct_architecture_files_and_the_same_mirror_evidence(self):
        before = self.check_arguments("Check Debian source mirror")
        after = self.check_arguments("Check pushed Debian source mirror")
        for name, arguments, prefix in (("pre-push", before, "debian"),
                                        ("post-push", after, "pushed-debian")):
            with self.subTest(check=name):
                self.assertNotEqual(arguments["--amd64"], arguments["--arm64"],
                                    "amd64 and arm64 must be read from different files")
                for arch in ("amd64", "arm64"):
                    self.assertEqual(arguments["--" + arch], f"$RUNNER_TEMP/{prefix}-{arch}.tsv")
        for flag, value in (("--registry", "docker/SOURCE-SETS.tsv"),
                            ("--release-assets", "$RUNNER_TEMP/source-assets.json"),
                            ("--sources-list", "$RUNNER_TEMP/$SOURCE_LIST")):
            self.assertEqual(before[flag], value)
            self.assertEqual(after[flag], value, "post-push check must reuse pre-push mirror evidence")
        self.assertEqual(self.named["Check pushed Debian source mirror"]["env"]["SOURCE_LIST"],
                         self.named["Check Debian source mirror"]["env"]["SOURCE_LIST"])

    def test_post_push_check_is_mandatory_after_all_pushes_before_release_and_dispatch(self):
        self.assertIn("Check pushed Debian source mirror", self.named,
                      "published images must be checked by digest before release creation")
        post = self.named["Check pushed Debian source mirror"]
        self.assertNotIn("if", post, "post-push check must use the default success condition")
        self.assertNotIn("continue-on-error", post)
        self.assertIn("set -euo pipefail", post["run"])
        self.assertNotIn("|| true", post["run"])
        pushes = [step for step in self.steps if step.get("with", {}).get("push") == "true"]
        self.assertEqual(len(pushes), 2)
        for push in pushes:
            self.assertLess(self.steps.index(push), self.steps.index(post))
            self.assertNotIn("continue-on-error", push)
        releases = [step for step in self.steps if "scripts/release_pipeline.py candidate --file" in step.get("run", "")]
        self.assertEqual(len(releases), 1)
        for release in releases:
            self.assertLess(self.steps.index(post), self.steps.index(release))
            self.assertNotIn("if", release)
            self.assertNotIn("continue-on-error", release)
        notify = self.workflow["jobs"]["notify-consumers"]
        self.assertIn("publish", notify["needs"], "finalization must wait for the post-push check")
        self.assertEqual(notify["uses"], "./.github/workflows/release-finalize.yml")

    def test_post_push_check_pulls_each_architecture_by_its_own_child_digest(self):
        post = self.named["Check pushed Debian source mirror"]
        for name, identity, digest_var, outcome_var in (
                ("Build + push (multi-arch)", "push_release", "RELEASE_DIGEST", "RELEASE_PUSH_OUTCOME"),
                ("Build + push hpo (multi-arch)", "push_hpo", "HPO_DIGEST", "HPO_PUSH_OUTCOME")):
            with self.subTest(push=name):
                self.assertEqual(self.named[name]["id"], identity)
                self.assertEqual(post["env"][digest_var], "${{ steps." + identity + ".outputs.digest }}")
                self.assertEqual(post["env"][outcome_var], "${{ steps." + identity + ".outcome }}")
        run = post["run"]
        self.assertIn('case "${RELEASE_PUSH_OUTCOME}:${HPO_PUSH_OUTCOME}" in', run)
        self.assertIn('success:skipped) digest="$RELEASE_DIGEST" ;;', run)
        self.assertIn('skipped:success) digest="$HPO_DIGEST" ;;', run)
        self.assertIn('*) echo "::error::expected exactly one successful image push"; exit 1 ;;', run)
        self.assertIn('[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]', run)
        self.assertIn('image="${REGISTRY}/${IMAGE}@${digest}"', run)
        logical = re.sub(r"\\\n\s*", " ", run)
        pulls = [shlex.split(line) for line in logical.splitlines()
                 if line.strip().startswith("docker pull ")]
        inventories = [shlex.split(line) for line in logical.splitlines()
                       if line.strip().startswith("docker run ")]
        self.assertEqual(len(pulls), 2)
        self.assertNotEqual(pulls[0][-1], pulls[1][-1],
                            "pulling both platforms by the same index digest conflicts in the local image store")
        references = {arch: "${REGISTRY}/${IMAGE}@${" + arch + "_child}"
                      for arch in ("amd64", "arm64")}
        self.assertEqual(pulls, [["docker", "pull", references[arch]] for arch in ("amd64", "arm64")])
        query = r"-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n"
        self.assertEqual(inventories, [
            ["docker", "run", "--rm", "--platform", f"linux/{arch}", "--entrypoint", "dpkg-query",
             references[arch], "-W", query, ">", f"$RUNNER_TEMP/pushed-debian-{arch}.tsv"]
            for arch in ("amd64", "arm64")])
        self.assertTrue(run.endswith(
            "python3 scripts/release_sources.py check --registry docker/SOURCE-SETS.tsv \\\n"
            '  --amd64 "$RUNNER_TEMP/pushed-debian-amd64.tsv" --arm64 "$RUNNER_TEMP/pushed-debian-arm64.tsv" \\\n'
            '  --release-assets "$RUNNER_TEMP/source-assets.json" --sources-list "$RUNNER_TEMP/$SOURCE_LIST"\n'
        ), "the final source check must stay byte for byte identical")
        self.assertNotIn("pineforge-source-inspect:", run)
        self.assertIn("DETECTS", run)
        self.assertIn("tags are already pushed", run)
        self.assertIn("sources are never deleted", run)

    def test_post_push_check_resolves_unique_non_attestation_linux_child_digests(self):
        run = self.named["Check pushed Debian source mirror"]["run"]
        inspect = 'docker buildx imagetools inspect --raw "$image"'
        self.assertEqual(run.count("docker buildx imagetools inspect --raw "), 1,
                         "read the pushed index once instead of pulling it for both platforms")
        self.assertIn('index="$(' + inspect + ')"', run)
        self.assertIn('|| { echo "::error::could not read pushed image index"; exit 1; }', run)
        resolver = re.search(r"^child_digest\(\) \{\n(.*?)^\}", run, re.M | re.S)
        self.assertIsNotNone(resolver, "both architectures must use the same validated child resolver")
        body = resolver.group(1)
        self.assertIn('local arch="$1" child', body)
        self.assertIn('child="$(jq -er --arg arch "$arch" \'', body)
        self.assertIn('[.manifests[]', body)
        self.assertIn('select(.platform.os == "linux" and .platform.architecture == $arch)', body)
        self.assertIn('select(.annotations["vnd.docker.reference.type"] != "attestation-manifest")', body)
        self.assertIn('if length == 1 then .[0].digest', body)
        self.assertIn('else error("expected exactly one linux/" + $arch + " image manifest") end', body)
        self.assertIn('\' <<< "$index")"', body)
        self.assertIn('|| { echo "::error::expected exactly one linux/${arch} image manifest in pushed index"'
                      ' >&2; return 1; }', body)
        self.assertIn('[[ "$child" =~ ^sha256:[0-9a-f]{64}$ ]]', body)
        self.assertIn('|| { echo "::error::missing or malformed linux/${arch} child digest" >&2; return 1; }',
                      body)
        self.assertIn('printf \'%s\\n\' "$child"', body)
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                assignment = f'{arch}_child="$(child_digest {arch})"'
                self.assertEqual(run.count(assignment), 1)
                self.assertLess(run.index(assignment), run.index("docker pull "),
                                "resolve and validate both child digests before either pull")
        self.assertIn('[[ "$amd64_child" != "$arm64_child" ]]', run)
        self.assertIn('|| { echo "::error::amd64 and arm64 image manifests share a child digest"; exit 1; }', run)

    def test_both_inventories_use_dpkg_query_with_the_source_columns(self):
        run = self.named["Read Debian package inventories"]["run"]
        for arch in ("amd64", "arm64"):
            self.assertIn(f"docker run --rm --platform linux/{arch} --entrypoint dpkg-query", run)
            self.assertIn(f"pineforge-source-inspect:{arch}", run)
            self.assertIn(f'"$RUNNER_TEMP/debian-{arch}.tsv"', run)
        self.assertEqual(run.count("${binary:Package}\\t${Version}\\t${source:Package}\\t${source:Version}\\n"), 2)

    def test_bundle_location_and_metadata_come_from_the_registry_and_get_requests(self):
        locate = self.named["Locate Debian source bundle"]
        self.assertEqual(locate["id"], "source_set")
        self.assertIn("python3 scripts/release_sources.py locate", locate["run"])
        self.assertIn("--registry docker/SOURCE-SETS.tsv", locate["run"])
        for argument in ("--amd64", "--arm64"):
            self.assertIn(argument, locate["run"])
        read = self.named["Read Debian source release assets"]
        self.assertEqual(read["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(read["env"]["SOURCE_TAG"], "${{ steps.source_set.outputs.release-tag }}")
        self.assertEqual(read["env"]["SOURCE_LIST"], "${{ steps.source_set.outputs.list-asset }}")
        self.assertIn('gh release view "$SOURCE_TAG"', read["run"])
        self.assertIn("--json assets", read["run"])
        self.assertIn('"$RUNNER_TEMP/source-assets.json"', read["run"])
        self.assertIn("releases/download/${SOURCE_TAG}/${SOURCE_LIST}", read["run"])
        self.assertIn('"$RUNNER_TEMP/$SOURCE_LIST"', read["run"])
        self.assertIn(self.job["permissions"]["contents"], ("read", "write"))

    def test_release_notes_link_to_the_checked_bundle_even_on_another_release(self):
        release = self.named["GitHub Release"]
        self.assertEqual(release["env"]["SOURCE_TAG"], "${{ steps.source_set.outputs.release-tag }}")
        self.assertEqual(release["env"]["SOURCE_LIST"], "${{ steps.source_set.outputs.list-asset }}")
        self.assertIn("Debian sources:", release["run"])
        self.assertIn("releases/download/%s/%s", release["run"])
        self.assertIn("releases/tag/%s", release["run"])
        self.assertLess(release["run"].index("Debian sources:"),
                        release["run"].index('python3 scripts/release_pipeline.py candidate'))


class PublishedHpoWheelCiTest(unittest.TestCase):
    def test_release_rules_downloads_both_real_wheels_checks_pins_and_exports_paths(self):
        from test_release_hpo_image import HPO_REJECTED_WHEEL_SHA256, HPO_WHEEL_SHA256
        workflow = _workflow("python-test.yml")
        steps = workflow["jobs"]["release-rules"]["steps"]
        download = next(step for step in steps if step.get("name") == "Download published hpo wheels")
        tests = next(step for step in steps if "unittest discover" in step.get("run", ""))
        self.assertLess(steps.index(download), steps.index(tests))
        self.assertNotIn("if", download)
        self.assertNotIn("continue-on-error", download)
        run = download["run"]
        self.assertIn("set -euo pipefail", run)
        self.assertNotIn("|| true", run)
        expected = (("0.15.0", HPO_WHEEL_SHA256, "PINEFORGE_HPO_ACCEPTED_WHEEL"),
                    ("0.13.2", HPO_REJECTED_WHEEL_SHA256, "PINEFORGE_HPO_REJECTED_WHEEL"))
        pinned = re.findall(r"([0-9a-f]{64})  (pineforge_hpo-[0-9.]+-py3-none-any\.whl)", run)
        self.assertEqual(set(pinned), {(digest, f"pineforge_hpo-{version}-py3-none-any.whl")
                                      for version, digest, _ in expected})
        self.assertEqual(run.count("sha256sum -c --strict"), 2)
        hpo_test = (REPO / "tests" / "test_release_hpo_image.py").read_text()
        exported = set(re.findall(r"echo ['\"]?(PINEFORGE_HPO_[A-Z_]+)=", run))
        consumed = set(re.findall(r'os\.environ\.get\("(PINEFORGE_HPO_[A-Z_]+)"\)', hpo_test))
        self.assertEqual(exported, consumed)
        for version, _, variable in expected:
            name = f"pineforge_hpo-{version}-py3-none-any.whl"
            self.assertIn(f"https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v{version}/{name}", run)
            self.assertIn(f'{variable}=$RUNNER_TEMP/{name}', run)
        self.assertIn('"$GITHUB_ENV"', run)
        parser = next(step for step in steps if "pip install" in step.get("run", ""))
        self.assertIn("PyYAML==", parser["run"])
        self.assertLess(steps.index(parser), steps.index(tests))


class HpoWorkflowTest(unittest.TestCase):
    """Run the real payload, probe and publish guard scripts on local fixtures.

    Only these shell steps run: every HTTP/API command resolves to the dry-run
    shims, and git can use local paths only. Nothing builds or publishes here.
    """

    COMPONENTS = ("engine", "codegen", "hpo")
    SHIMS = REPO / "tools" / "release-dry-run" / "shims"

    @classmethod
    def setUpClass(cls):
        cls.handler = (WORKFLOWS / "handle-upstream.yml").read_text(encoding="utf-8")
        cls.publisher = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")

    def fixture(self, root):
        (root / "scripts").mkdir()
        shutil.copyfile(REPO / "scripts" / "release_pair.py", root / "scripts" / "release_pair.py")
        (root / "VERSION").write_text("1.5.9\n", encoding="utf-8")
        (root / "state").mkdir()
        (root / "gitconfig-empty").touch()

    def environment(self, root):
        return {"PATH": f"{self.SHIMS}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                "HARNESS_STATE": str(root / "state"), "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": str(root / "gitconfig-empty"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "TMPDIR": str(root)}

    def shell(self, root, text, step, values):
        out = root / "outputs"
        out.write_text("", encoding="utf-8")
        summary = root / "summary"
        summary.write_text("", encoding="utf-8")
        env = self.environment(root)
        env.update({"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary), **values})
        done = subprocess.run(["bash", "--noprofile", "--norc", "-c", _run_script(_step(text, step))],
                              cwd=root, env=env, capture_output=True, text=True, timeout=30)
        written = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        return done, written

    def test_only_existing_release_triggers_can_reach_the_jobs(self):
        handler = _workflow("handle-upstream.yml")
        self.assertEqual(handler["on"]["repository_dispatch"]["types"],
                         ["engine-release", "codegen-release", "hpo-release"])
        publish = _workflow("publish.yml")
        self.assertEqual(set(publish["on"]), {"workflow_dispatch"})
        self.assertEqual(publish["jobs"]["prepare"]["if"], "github.ref == 'refs/heads/main'")

    def test_hpo_payload_boundary_and_whole_value_allowlist(self):
        cutoff_note = f"hpo joins the pairing at {rp.HPO_PAIRING_FROM}"
        cases = (("1.5.9", "", "123", False, cutoff_note),
                 ("1.6.0-alpha.1", "true", "123", False, cutoff_note),
                 ("1.6.0a1", "true", "123", False, cutoff_note),
                 ("1.6.0-beta.1", "true", "123", False, cutoff_note),
                 ("1.6.0b1", "true", "123", False, cutoff_note),
                 ("1.6.0-rc.1", "true", "123", True, ""),
                 ("1.6.0rc1", "true", "123", True, ""),
                 ("v1.6.0rc1", "true", "123", True, ""),
                 ("v1.6.0-rc.12", "true", "123", True, ""),
                 ("1.6.0rc12", "true", "123", True, ""),
                 ("1.6.1a1", "true", "123", True, ""),
                 ("1.6.1b1", "true", "123", True, ""),
                 ("v1.6.0", "false", "123", True, ""),
                 ("1.6.1-rc.1", "true", "123", True, ""),
                 ("1.6.0", "", "", True, ""),
                 ("1.6", "", "123", False, "rejected untrusted version"),
                 ("1.6.0;id", "", "123", False, "rejected untrusted version"),
                 ("1.6.0-rc1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0.rc1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0RC1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0.post1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0.dev1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0c1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0rc01", "", "123", False, "rejected version"),
                 ("1.6.0rc1\n1.6.0", "", "123", False, "rejected untrusted version"),
                 ("1.6.0\n1.6.1", "", "123", False, "rejected untrusted version"),
                 ("1.6.0", "yes", "123", False, "rejected untrusted prerelease"),
                 ("1.6.0", "false\ntrue", "123", False, "rejected untrusted prerelease"),
                 ("1.6.0", "false", "123\n456", False, "bad run_id"),
                 ("1.6.0", "false", "12x", False, "bad run_id"))
        for version, flag, run_id, accepted, message in cases:
            with self.subTest(version=version, flag=flag, run_id=run_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                done, out = self.shell(root, self.handler, "Validate payload",
                                       {"EVENT": "hpo-release", "RAW_VERSION": version,
                                        "RAW_PRERELEASE": flag, "RUN_ID": run_id})
                self.assertEqual(done.returncode == 0, accepted, done.stdout + done.stderr)
                if accepted:
                    self.assertEqual(out["component"], "hpo")
                    self.assertEqual(out["version"], rp.hpo_version(version, to_semver=True))
                    self.assertEqual(out["hpo_pep440"], rp.hpo_version(out["version"]))
                else:
                    self.assertIn(message, done.stdout + done.stderr)
                    self.assertEqual(out, {})

    def test_engine_codegen_payloads_keep_their_existing_version_rules(self):
        cases = (("1.5.9", True), ("1.6.0-alpha.1", True), ("1.6.0-beta.1", True),
                 ("v1.6.0-rc.1", True), ("1.6.0", True), ("1.6.0rc1", False),
                 ("v1.6.0rc1", False), ("1.6.0a1", False), ("1.6.0b1", False),
                 ("1.6.0.rc1", False), ("1.6.0\n1.6.1", False))
        for component in self.COMPONENTS[:2]:
            for version, accepted in cases:
                with self.subTest(component=component, version=version), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.fixture(root)
                    done, out = self.shell(root, self.handler, "Validate payload",
                                           {"EVENT": f"{component}-release", "RAW_VERSION": version,
                                            "RAW_PRERELEASE": "", "RUN_ID": "123"})
                    self.assertEqual(done.returncode == 0, accepted, done.stdout + done.stderr)
                    if accepted:
                        self.assertEqual(out["version"], str(rp.parse(version)))
                    else:
                        self.assertEqual(out, {})

    def test_every_event_checks_prerelease_agreement_in_the_real_decision_step(self):
        for component in self.COMPONENTS:
            for version, flag in (("1.6.0", "true"), ("1.6.1-rc.1", "false")):
                with self.subTest(component=component, version=version), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.fixture(root)
                    done, out = self.shell(root, self.handler, "Decide action",
                                           {"COMPONENT": component, "VER": version, "PAYLOAD_PRERELEASE": flag,
                                            "EPREV": "1.5.9", "CPREV": "1.5.9", "HPREV": "",
                                            "PUBLISHED": "false", "PARTNER_LATEST": "", "FORCE": ""})
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("contradicts version", done.stderr)
                    self.assertNotIn("mode", out)

    @staticmethod
    def urls(version):
        engine = f"https://github.com/pineforge-4pass/pineforge-engine/releases/download/v{version}"
        hp = rp.hpo_version(version)
        hpo = f"https://github.com/pineforge-4pass/pineforge-hpo/releases/download/v{hp}"
        return {"engine": [f"{engine}/pineforge-v{version}-linux-{arch}.tar.gz" for arch in ("x86_64", "aarch64")],
                "codegen": [f"https://pypi.org/pypi/pineforge-codegen/{rp.parse(version).pep440}/json"],
                "hpo": [f"{hpo}/pineforge_hpo-{hp}-py3-none-any.whl"]}

    def probe(self, root, component, statuses, *, hpo_pairing="true", text=None,
              version="1.6.0", hpo_releases=None):
        urls = self.urls(version)
        http = {url: {"status": statuses.get(name, 200)} for name, paths in urls.items() for url in paths}
        http["https://pypi.org/pypi/pineforge-codegen/json"] = {
            "status": 200, "body": json.dumps({"releases": {"1.5.9": []}})}
        (root / "state" / "http.json").write_text(json.dumps(http), encoding="utf-8")
        for name in ("engine", "hpo"):
            releases = (hpo_releases or []) if name == "hpo" else []
            (root / "state" / f"releases-pineforge-{name}.json").write_text(json.dumps(releases), encoding="utf-8")
        done, out = self.shell(root, text or self.handler, "Probe the partner",
                               {"COMPONENT": component, "VER": version, "PYPI": rp.parse(version).pep440,
                                "HPO_VERSION": rp.hpo_version(version),
                                "HPO_PAIRING": hpo_pairing})
        log = root / "state" / "actions.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return done, out, calls

    def test_each_event_probes_exactly_the_other_two_components(self):
        for version in ("1.6.0-rc.1", "1.6.0-rc.12", "1.6.0"):
            for component in self.COMPONENTS:
                with self.subTest(version=version, component=component), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.fixture(root)
                    done, out, calls = self.probe(root, component, {}, version=version)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    partners = set(self.COMPONENTS) - {component}
                    expected = {url for name in partners for url in self.urls(version)[name]}
                    self.assertEqual({call["url"] for call in calls if call["tool"] == "curl"}, expected)
                    for partner in partners:
                        self.assertEqual(out[f"{partner}_published"], "true")
                    for call in calls:
                        if call.get("url") in self.urls(version)["hpo"]:
                            self.assertEqual(call["method"], "HEAD")

    def test_hpo_latest_probe_counts_prereleases_and_orders_pep440_tags(self):
        cases = [((("v1.6.0rc1", False, True),), "1.6.0-rc.1", False),
                 ((("v1.6.0rc2", False, True), ("v1.6.0rc12", False, True),
                   ("v1.6.0", True, False)), "1.6.0-rc.12", True),
                 ((("v1.6.0", False, False), ("v1.6.0rc12", False, True)), "1.6.0", True)]
        for inventory, expected, mismatched in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                releases = [{"tag_name": tag, "draft": draft, "prerelease": pre}
                            for tag, draft, pre in inventory]
                done, out, _ = self.probe(root, "engine", {"hpo": 404}, version="1.6.0-rc.1",
                                           hpo_releases=releases)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual((out["hpo_published"], out["hpo_latest"]), ("false", expected))
                if not mismatched:
                    decision, _ = rp.decide("engine", "1.6.0-rc.1", "true", "1.5.9", "1.5.9", "1.5.9",
                                            False, True, False, partners_published={"codegen": True},
                                            partners_latest={"hpo": out["hpo_latest"]})
                    self.assertEqual((decision["mode"], decision["awaiting"]), ("wait", "hpo"))
                else:
                    with self.assertRaisesRegex(rp.RuleError, "mismatched pair"):
                        rp.decide("engine", "1.6.0-rc.1", "true", "1.5.9", "1.5.9", "1.5.9",
                                  False, True, False, partners_latest={"hpo": out["hpo_latest"]})

    def test_404_is_pending_and_every_uncertain_status_fails_each_event(self):
        for component in self.COMPONENTS:
            for partner in set(self.COMPONENTS) - {component}:
                for status in (404, 0, 403, 429, 500, 503):
                    with self.subTest(component=component, partner=partner, status=status), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        self.fixture(root)
                        done, out, _ = self.probe(root, component, {partner: status})
                        if status == 404:
                            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                            self.assertEqual(out[f"{partner}_published"], "false")
                        else:
                            self.assertNotEqual(done.returncode, 0)
                            self.assertIn("could not tell whether", done.stdout + done.stderr)

    def test_unmodeled_asset_request_fails_instead_of_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            mutant = self.handler.replace("pineforge_hpo-", "unmodeled_hpo-")
            self.assertNotEqual(mutant, self.handler)
            done, _, calls = self.probe(root, "engine", {}, text=mutant)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("could not tell whether", done.stdout + done.stderr)
            self.assertTrue(any(call.get("unsupported") for call in calls))

    def test_existing_pair_probe_skips_hpo(self):
        for component in self.COMPONENTS[:2]:
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                done, out, calls = self.probe(root, component, {}, hpo_pairing="")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(out["published"], "true")
                self.assertFalse(any("pineforge-hpo" in call.get("url", "") for call in calls))

    def publish_guards(self, root, version, hpo):
        env = self.environment(root)
        def git(*args):
            done = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                                   "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                                   "-c", "core.hooksPath=/dev/null", *args],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
        git("init", "-q")
        git("add", "VERSION", "scripts/release_pair.py")
        git("commit", "-q", "-m", "fixture")
        message = f"pineforge-release v{version}\n\nengine={version}\ncodegen={version}\n"
        if hpo is not None:
            message += f"hpo={hpo}\n"
        git("tag", "-a", f"v{version}", "-m", message)
        pins, out = self.shell(root, self.publisher, "Read pins", {"REF": f"v{version}"})
        if pins.returncode:
            return pins, out
        return self.shell(root, self.publisher, "Pairing rule",
                          {"REF": f"v{version}", "E": out["engine"], "C": out["codegen"],
                           "H": out.get("hpo", "")})

    def test_landed_tag_publish_guards_at_the_cutoff(self):
        cases = (("1.5.0", None, True, ""), ("1.5.9", None, True, ""), ("1.5.9", "bad", True, ""),
                 ("1.6.0-alpha.1", None, True, ""), ("1.6.0-beta.1", "bad", True, ""),
                 ("1.6.0-rc.1", None, False, ""), ("1.6.0-rc.1", "bad", False, ""),
                 ("1.6.0-rc.1", "1.6.0rc1", False, ""), ("1.6.0-rc.1", "1.6.0-rc.2", False, ""),
                 ("1.6.0-rc.1", "1.6.0-rc.1", True, "1.6.0-rc.1"),
                 ("1.6.0", None, False, ""), ("1.6.0", "bad", False, ""), ("1.6.0", "1.6.1", False, ""),
                 ("1.6.0", "1.6.0-rc.1", False, ""), ("1.6.0", "1.6.0", True, "1.6.0"),
                 ("1.6.1-rc.1", None, False, ""), ("1.6.1-rc.1", "1.6.1-rc.2", False, ""),
                 ("1.6.1-rc.1", "1.6.1-rc.1", True, "1.6.1-rc.1"))
        for version, hpo, accepted, expected_hpo in cases:
            with self.subTest(version=version, hpo=hpo), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                done, out = self.publish_guards(root, version, hpo)
                self.assertEqual(done.returncode == 0, accepted, done.stdout + done.stderr)
                if accepted:
                    self.assertEqual(out.get("hpo", ""), expected_hpo)
                else:
                    self.assertIn("hpo", done.stdout + done.stderr)

    def test_real_publish_decision_selects_target_and_arguments_at_the_boundary(self):
        cases = (("1.5.9", "", ()), ("1.6.0-alpha.1", "", ()), ("1.6.0-beta.1", "", ()),
                 ("1.6.0-rc.1", "hpo", ("HPO_VERSION=1.6.0rc1",)),
                 ("1.6.0", "hpo", ("HPO_VERSION=1.6.0",)))
        for version, target, arguments in cases:
            with self.subTest(version=version):
                result = rp.check(version, version, version, hpo=version)
                selected = tuple(result[key] for key in ("hpo_version_arg", "hpo_wheel_arg")
                                 if key in result)
                self.assertEqual(result.get("target", ""), target)
                self.assertEqual(selected, arguments)
                if not target:
                    self.assertNotIn("target", result)
                    self.assertNotIn("hpo", result)

    def test_optional_release_asset_digest_is_a_checked_build_argument(self):
        digest = "a" * 64
        for version in (rp.HPO_PAIRING_FROM, "1.6.1-rc.1"):
            for asset_digest in (digest, f"sha256:{digest}"):
                with self.subTest(version=version, digest=asset_digest):
                    result = rp.check(version, version, version, hpo=version,
                                      hpo_wheel_digest=asset_digest)
                    self.assertEqual(result["target"], "hpo")
                    self.assertEqual(result["hpo_version_arg"], f"HPO_VERSION={rp.hpo_version(version)}")
                    self.assertEqual(result["hpo_wheel_arg"], f"HPO_WHEEL_SHA256={digest}")
            for invalid in ("bad", "sha512:" + digest, digest + "\n", digest + "\n" + digest):
                with self.subTest(version=version, invalid=invalid), self.assertRaises(rp.RuleError):
                    rp.check(version, version, version, hpo=version, hpo_wheel_digest=invalid)
        for version in ("1.5.9", "1.6.0-alpha.1", "1.6.0-beta.1"):
            with self.subTest(version=version):
                result = rp.check(version, version, version, hpo="ignored", hpo_wheel_digest="ignored")
                self.assertNotIn("target", result)
                self.assertNotIn("hpo_version_arg", result)
                self.assertNotIn("hpo_wheel_arg", result)

    def test_waiting_for_the_wheel_has_exact_decision_note_and_run_summary(self):
        version = rp.HPO_PAIRING_FROM
        spellings = {name: rp.hpo_version(version) if name == "hpo" else version for name in self.COMPONENTS}
        cases = (("engine", {}, ["codegen", "hpo"]), ("codegen", {}, ["engine", "hpo"]),
                 ("engine", {"codegen": True}, ["hpo"]), ("codegen", {"engine": True}, ["hpo"]),
                 ("hpo", {}, ["engine", "codegen"]))
        for component, published, missing in cases:
            with self.subTest(component=component, published=published), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                result, note = rp.decide(
                    component, spellings[component], "true", "1.5.9", "1.5.9", "1.5.9",
                    False, True, False, partners_published=published)
                self.assertEqual(result["mode"], "wait")
                self.assertEqual(result["awaiting"].split(","), missing)
                self.assertEqual(result["release"], "")
                pending = ", ".join(f"{name} {spellings[name]}" for name in missing)
                self.assertEqual(note, f"mode=wait: {component} {spellings[component]} recorded; "
                                 f"waiting for {pending}; landed engine 1.5.9 + codegen 1.5.9 + "
                                 "hpo (not paired yet). Nothing is built or published.")
                done, _ = self.shell(root, self.handler, "Wait for the partner",
                                     {"COMPONENT": component, "VER": version, "HPO_VERSION": spellings["hpo"],
                                      "AWAITING": result["awaiting"], "EPREV": result["eprev"],
                                      "CPREV": result["cprev"], "HPREV": result["hprev"]})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                waiting = " + ".join(f"{name} {spellings[name]}" for name in missing)
                expected = (f"pair pending: {component} {spellings[component]} recorded, waiting for {waiting}; "
                            "the landed pair stays engine 1.5.9 + codegen 1.5.9 and nothing is built or published")
                self.assertEqual((root / "summary").read_text(), expected + "\n")
                self.assertEqual(done.stdout, f"::notice title=pineforge pair pending::{expected}\n")

    def test_github_release_notes_name_hpo_only_when_paired(self):
        step = _step(self.publisher.split("\n  notify-consumers:\n", 1)[0], "GitHub Release")
        script = _run_script(step).split("python3 scripts/release_pipeline.py candidate", 1)[0]
        for version, hpo in ((rp.HPO_PAIRING_FROM, rp.HPO_PAIRING_FROM), ("1.5.9", "")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {**self.environment(root), "RUNNER_TEMP": tmp,
                       "E": version, "C": version, "H": hpo,
                       "SOURCE_TAG": "source-bundle", "SOURCE_LIST": "SOURCES.list"}
                done = subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                                      cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                notes = (root / "release-notes.md").read_text()
                expected = f"engine {version} + codegen {version}" + (f" + hpo {hpo}" if hpo else "")
                self.assertEqual(notes.splitlines()[2], expected)

    def test_real_artifact_wait_uses_hpo_pep440_urls(self):
        for version in ("1.6.0-rc.1", "1.6.0-rc.12", "1.6.0"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                urls = self.urls(version)
                http = {url: {"status": 200} for paths in urls.values() for url in paths}
                (root / "state" / "http.json").write_text(json.dumps(http), encoding="utf-8")
                checked = rp.check(version, version, version, hpo=version)
                done, _ = self.shell(root, self.publisher, "Wait for upstream artifacts",
                                     {"E": version, "C": version, "CP": checked["codegen_pypi"],
                                      "H": checked["hpo_pep440"]})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                calls = [json.loads(line) for line in (root / "state" / "actions.jsonl").read_text().splitlines()]
                self.assertEqual({call["url"] for call in calls if call["tool"] == "curl"},
                                 {url for paths in urls.values() for url in paths})
                self.assertTrue(all(call["method"] == "HEAD" for call in calls
                                    if call.get("url") in urls["hpo"]))

    def test_real_asset_digest_step_emits_only_checked_hpo_build_arguments(self):
        version, digest = rp.HPO_PAIRING_FROM, "b" * 64
        pep440 = rp.hpo_version(version)
        for asset_digest, expected, accepted in ((None, "", True), (f"sha256:{digest}", digest, True),
                                                 ("bad", "", False)):
            with self.subTest(digest=asset_digest), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                metadata = {"tag_name": f"v{pep440}", "assets": [
                    {"name": f"pineforge_hpo-{pep440}-py3-none-any.whl", "digest": asset_digest},
                    {"name": "unrelated.whl", "digest": "sha256:" + "c" * 64}]}
                (root / "state" / f"release-pineforge-hpo-v{pep440}.json").write_text(
                    json.dumps(metadata), encoding="utf-8")
                done, result = self.shell(root, self.publisher, "Read hpo wheel digest",
                                          {"REF": f"v{version}", "E": version, "C": version,
                                           "H": version, "HP": pep440})
                self.assertEqual(done.returncode == 0, accepted, done.stdout + done.stderr)
                if accepted:
                    self.assertEqual(result["target"], "hpo")
                    self.assertEqual(result["hpo"], version)
                    self.assertEqual(result["hpo_pep440"], pep440)
                    self.assertEqual(result["hpo_version_arg"], f"HPO_VERSION={pep440}")
                    self.assertEqual(result.get("hpo_wheel_arg", ""),
                                     f"HPO_WHEEL_SHA256={expected}" if expected else "")
                else:
                    self.assertEqual(result, {})

    def test_unavailable_release_metadata_fails_the_digest_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            version = rp.HPO_PAIRING_FROM
            done, result = self.shell(root, self.publisher, "Read hpo wheel digest",
                                      {"REF": f"v{version}", "E": version, "C": version,
                                       "H": version, "HP": rp.hpo_version(version)})
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("could not read hpo", done.stdout + done.stderr)
            self.assertEqual(result, {})


class AppScopeTest(unittest.TestCase):
    def test_release_jobs_use_protected_environment_and_scoped_tokens(self):
        for name in ("release-finalize.yml", "handle-upstream.yml"):
            text = (WORKFLOWS / name).read_text()
            self.assertIn("environment: release-automation", text)
            self.assertNotIn("permission-administration", text)
            self.assertNotIn("create-github-app-token@v1", text)
            self.assertIn("permission-contents: write", text)
            self.assertIn("github-api-url: https://api.github.com", text)
        handler = (WORKFLOWS / "handle-upstream.yml").read_text()
        self.assertIn("repositories: pineforge-release", handler)
        self.assertNotIn("permission-actions", _step(handler, "Mint org App token"))
        self.assertNotIn('git push origin "refs/tags/${TAG}" --force', handler)
        finalizer = (WORKFLOWS / "release-finalize.yml").read_text()
        self.assertIn("repositories: ${{ env.TARGET_REPOSITORY }}", finalizer)
        self.assertLess(finalizer.index("Require one configured consumer"), finalizer.index("Mint App token"))
        job = _workflow("release-finalize.yml")["jobs"]["notify-consumers"]
        self.assertNotIn("packages", job["permissions"])


class NotifyConsumersTest(unittest.TestCase):
    """Dispatch is a finalizer-only operation, with independent role jobs."""
    def test_publication_delegates_to_the_ready_finalizer(self):
        publish = _workflow("publish.yml")["jobs"]["notify-consumers"]
        self.assertEqual(publish["uses"], "./.github/workflows/release-finalize.yml")
        self.assertIn("needs.publish.result == 'success'", publish["if"])

    def test_dispatch_requires_finalized_manifest_plan(self):
        wf = _workflow("release-finalize.yml")
        job = wf["jobs"]["notify-consumers"]
        self.assertEqual(job["needs"], "finalize")
        self.assertEqual(job["strategy"]["fail-fast"], "false")
        self.assertEqual(job["strategy"]["matrix"]["include"], "${{ fromJSON(needs.finalize.outputs.deliveries) }}")
        send = next(s for s in job["steps"] if s.get("name", "").startswith("Dispatch pineforge-release"))
        self.assertIn("release_pipeline.py dispatch", send["run"])
        self.assertNotIn("client_payload[", send["run"])

    def test_all_consumer_roles_have_explicit_registry_and_policy(self):
        import release_pipeline as pipeline
        expected = {"offline", "hosted", "application", "web", "data", "license"}
        self.assertEqual(set(pipeline.configured_roles(",".join(sorted(expected)))), expected)
        self.assertEqual(pipeline.configured_roles(""), [])
        for bad in ("unknown", "offline,offline", "offline, hosted", "offline\nhosted"):
            with self.subTest(bad=bad), self.assertRaises(pipeline.Invalid):
                pipeline.configured_roles(bad)


class HarnessSyncTest(unittest.TestCase):
    """The release image carries exactly the engine's eight runtime harness files. One script holds
    the list and syncs it from the pinned engine tag; the Dockerfile installs that list and checks
    the installed files against the sums the script recorded. Text checks only: nothing is fetched
    or built here."""

    MODULES = ("bind_compiled_inventory.py", "request_feed_inventory.py", "run_execution_observer.py",
               "run_phase_transport.py", "selected_window_plan.py", "selected_window_report.py")
    HARNESS = tuple(sorted(MODULES + ("entrypoint.sh", "run_json.py")))
    SUMS = "HARNESS-SHA256SUMS"

    @classmethod
    def setUpClass(cls):
        cls.script = (REPO / "scripts" / "sync-harness.sh").read_text(encoding="utf-8")
        cls.dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        array = re.search(r"^FILES=\(\n(.*?)^\)$", cls.script, re.S | re.M)
        cls.listed = array.group(1).split() if array else []
        cls.copied = re.findall(r"^COPY docker/(\S+)\s+/opt/pineforge/bin/(\S+)\s*$", cls.dockerfile, re.M)

    def test_the_list_is_the_eight_engine_harness_files(self):
        self.assertEqual(self.listed, list(self.HARNESS))
        for name in self.MODULES:
            with self.subTest(module=name):
                self.assertIn(name, self.listed)

    def test_the_eight_files_are_committed_in_docker(self):
        for name in self.HARNESS:
            with self.subTest(file=name):
                self.assertTrue((REPO / "docker" / name).is_file())

    @classmethod
    def _sums_problems(cls, sums: Path, docker: Path) -> list[str]:
        """Every way the sums file at `sums` fails to be the record of the harness files in `docker`, one
        short entry per defect; an empty list when the record is exactly right. The committed file and the
        forced failures below both go through here."""
        text = sums.read_bytes().decode("utf-8")
        problems = []
        # the shape scripts/sync-harness.sh writes: "<sha256>  <name>" and a newline per file, no blank line
        if re.fullmatch(r"(?:[0-9a-f]{64}  [^\s/]+\n)+", text) is None:
            problems.append("shape")
        entries = re.findall(r"^([0-9a-f]{64})  ([^\s/]+)$", text, re.M)
        names = [name for _, name in entries]
        for name in names:
            if name.endswith("_test.py") or name.upper().startswith("README"):
                problems.append(f"test or readme: {name}")
        # the eight files, in the order of the script's list and of the build's check: none missing, extra or twice
        if names != list(cls.HARNESS):
            problems.append("names")
        for digest, name in entries:
            if not (docker / name).is_file():
                problems.append(f"absent: {name}")
            elif digest != hashlib.sha256((docker / name).read_bytes()).hexdigest():
                problems.append(f"digest: {name}")
        return problems

    def test_the_sums_file_lists_exactly_the_files_present(self):
        docker = REPO / "docker"
        path = docker / self.SUMS
        self.assertTrue(path.is_file(), f"docker/{self.SUMS} is not committed")
        self.assertEqual(self._sums_problems(path, docker), [])
        # Forced failures through the same check, on mutated copies of the committed text in a temp dir. The
        # unmutated copy is checked first, so the one change in each copy is what the check has to report.
        lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        digest, name = lines[3].rstrip("\n").split("  ")
        ghost = "ghost.py"
        self.assertFalse((docker / ghost).exists(), ghost)
        # a real file that is not one of the eight, with its real hash: an extra entry that is neither wrong nor absent
        extra = f"{hashlib.sha256((docker / 'Dockerfile').read_bytes()).hexdigest()}  Dockerfile\n"
        flipped = ("1" if digest[0] == "0" else "0") + digest[1:]
        forced = {
            "an extra entry": (lines + [extra], ["names"]),
            "a missing entry": (lines[:3] + lines[4:], ["names"]),
            "an entry twice": (lines[:4] + lines[3:], ["names"]),
            "a wrong hash": (lines[:3] + [f"{flipped}  {name}\n"] + lines[4:], [f"digest: {name}"]),
            "an entry for a file that is not in docker/": (lines[:3] + [f"{digest}  {ghost}\n"] + lines[4:],
                                                           ["names", f"absent: {ghost}"]),
            "a blank line": (lines[:4] + ["\n"] + lines[4:], ["shape"]),
        }
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / self.SUMS
            copy.write_bytes("".join(lines).encode("utf-8"))
            self.assertEqual(self._sums_problems(copy, docker), [])
            for label, (mutated, expected) in forced.items():
                with self.subTest(case=label):
                    copy.write_bytes("".join(mutated).encode("utf-8"))
                    self.assertEqual(self._sums_problems(copy, docker), expected)

    def test_no_test_file_or_readme_is_synced_or_installed(self):
        self.assertTrue(self.listed, "scripts/sync-harness.sh has no FILES list")
        for name in self.listed + [src for src, _ in self.copied]:
            with self.subTest(file=name):
                self.assertFalse(name.endswith("_test.py") or name.upper().startswith("README"), name)
        self.assertNotIn("_test.py", self.dockerfile)
        self.assertNotIn("README", self.dockerfile)

    def test_the_dockerfile_installs_exactly_the_list_next_to_run_json(self):
        self.assertTrue(self.listed, "scripts/sync-harness.sh has no FILES list")
        self.assertEqual(sorted(src for src, _ in self.copied if src != self.SUMS), self.listed)
        self.assertIn((self.SUMS, self.SUMS), self.copied)
        for src, dst in self.copied:
            with self.subTest(file=src):
                self.assertEqual(src, dst)  # one directory, under its own name: importable from run_json.py's

    def test_the_build_checks_the_installed_files_against_the_recorded_sums(self):
        check = "sha256sum -c --strict HARNESS-SHA256SUMS"
        self.assertIn(check, self.dockerfile)
        block = _run_block(self.dockerfile, check)
        self.assertTrue(block.startswith("RUN set -eu; \\\n"), block[:40])
        self.assertIn("cd /opt/pineforge/bin; \\\n", block)
        self.assertIn('want="' + " ".join(self.HARNESS) + '"; \\\n', block)
        self.assertIn('[ "$listed" != "$want" ] || [ "$present" != "$want" ]', block)
        self.assertIn("exit 1;", block)
        self.assertNotIn("|| true", block)
        # In release, after its last harness COPY and before its user drop.
        # HPO inherits this checked harness and adds its own files later.
        release = re.search(r"^FROM \S+ AS release\n.*?(?=^FROM |\Z)",
                            self.dockerfile, re.M | re.S)
        self.assertIsNotNone(release, "the unchanged runtime must be named release")
        runtime = release.group(0)
        self.assertLess(runtime.rindex("\nCOPY docker/"), runtime.index(check))
        self.assertLess(runtime.index(check), runtime.index("\nUSER "))

    def test_the_default_ref_is_the_pinned_engine_tag(self):
        self.assertIn("set -euo pipefail\n", self.script)
        self.assertIn('REF="${PF_ENGINE_REF:-v${E}}"\n', self.script)
        self.assertEqual(len(re.findall(r"^REF=", self.script, re.M)), 1)
        self.assertIn('BASE="https://raw.githubusercontent.com/pineforge-4pass/pineforge-engine/${REF}/docker"\n',
                      self.script)
        self.assertEqual(self.script.count("raw.githubusercontent.com"), 1)

    def test_the_script_fetches_every_file_and_records_the_sums_of_what_it_fetched(self):
        fetch = 'curl -fsSL "${BASE}/${f}" -o "docker/${f}"'
        write = '( cd docker && "${sha256[@]}" -- "${FILES[@]}" ) > "docker/${SUMS}"'
        self.assertIn('for f in "${FILES[@]}"; do\n  ' + fetch + "\n", self.script)
        self.assertIn(write, self.script)
        self.assertLess(self.script.index(fetch), self.script.index(write))
        # --files names the sums file with the synced files, for the workflow's git add
        self.assertIn('printf \'docker/%s\\n\' "${FILES[@]}" "${SUMS}"', self.script)

    def test_every_module_the_harness_imports_is_in_the_list(self):
        self.assertTrue(self.listed, "scripts/sync-harness.sh has no FILES list")
        allowed = set(sys.stdlib_module_names) | {"pineforge_codegen"} | {n[:-3] for n in self.listed if n.endswith(".py")}
        for name in self.listed:
            if not name.endswith(".py"):
                continue
            tree = ast.parse((REPO / "docker" / name).read_text(encoding="utf-8"), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    with self.subTest(file=name, imports=module):
                        self.assertIn(module.split(".")[0], allowed)

    def test_the_entrypoint_is_the_engines_with_the_selected_window_path(self):
        entry = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('"${PINEFORGE_REPORT_POLICY:-}" == "selected-window/v1"', entry)
        self.assertIn('python3 "${PREFIX}/bin/bind_compiled_inventory.py"', entry)


class HarnessBuildCheckTest(unittest.TestCase):
    """The image build's own check of the harness files, run for real. The RUN instruction of
    docker/Dockerfile that holds `sha256sum -c --strict HARNESS-SHA256SUMS` is run under bash on a
    temporary directory that stands in for /opt/pineforge/bin and holds copies of the committed harness
    files and of the committed sums file. Every test first proves that the check passes on that layout,
    then breaks the layout in one way and proves that the check fails and says why, so a check that
    cannot run never passes for one that caught a defect. Nothing is fetched or built, and nothing
    outside the temporary directories is written."""

    CHECK = "sha256sum -c --strict HARNESS-SHA256SUMS"
    IMAGE_BIN = "/opt/pineforge/bin"
    SUCCESS = "harness: the eight files match HARNESS-SHA256SUMS"
    SUMS = HarnessSyncTest.SUMS
    HARNESS = HarnessSyncTest.HARNESS
    TOOLS = ("bash", "grep", "ls", "sed", "sha256sum", "tr")  # bash runs the check; the rest are the check's own commands

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        found = {tool: shutil.which(tool) for tool in cls.TOOLS}
        missing = [tool for tool, where in found.items() if where is None]
        if missing:
            raise AssertionError("the Dockerfile's harness check cannot run here without " + ", ".join(missing))
        cls.bash = found["bash"]
        cls.path = os.environ.get("PATH", os.defpath)

    @classmethod
    def _script(cls, dockerfile: str, bin_dir: Path) -> str:
        """The check as the build's shell receives it: the RUN instruction of the Dockerfile that holds it,
        its leading `RUN ` dropped and its backslash-continued lines joined (the Dockerfile parser removes
        each backslash and newline and keeps the blanks that lead the next line; so does this). The only
        other change is bin_dir where the image says /opt/pineforge/bin."""
        if cls.CHECK not in dockerfile:
            raise AssertionError(f"docker/Dockerfile holds no instruction that runs `{cls.CHECK}`")
        block = _run_block(dockerfile, cls.CHECK)
        if not block.startswith("RUN "):
            raise AssertionError(f"the instruction that holds the check is not a RUN: {block[:40]!r}")
        joined = block[len("RUN "):].replace("\\\n", "")
        if "\n" in joined:
            raise AssertionError("the RUN instruction that holds the check is not one logical line")
        if joined.count(cls.IMAGE_BIN) != 1:
            raise AssertionError(f"the check must name {cls.IMAGE_BIN} exactly once, in its cd")
        return joined.replace(cls.IMAGE_BIN, shlex.quote(str(bin_dir)))

    def _run(self, bin_dir: Path) -> subprocess.CompletedProcess:
        """Run the Dockerfile's check on the layout in bin_dir: bash gets the script, an environment of
        PATH alone (the build's has no locale or HOME either) and no stdin; the working directory is the
        temporary directory that holds bin_dir."""
        script = self._script(self.dockerfile, bin_dir)
        return subprocess.run([self.bash, "--noprofile", "--norc", "-c", script], cwd=bin_dir.parent,
                              env={"PATH": self.path}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def _layout(self, root: Path) -> Path:
        """root/bin: the committed harness files and the committed sums file, byte for byte, as the
        image's COPY lines install them."""
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in self.HARNESS + (self.SUMS,):
            shutil.copyfile(REPO / "docker" / name, bin_dir / name)
        return bin_dir

    def _names(self, bin_dir: Path) -> tuple[list[str], list[str]]:
        """The two name lists the check compares, read off the layout: the names in its sums file, in the
        file's order, and the names in its directory, in C order, without the sums file."""
        lines = (bin_dir / self.SUMS).read_bytes().decode("utf-8").splitlines()
        listed = [line.split("  ", 1)[1] for line in lines]
        present = sorted(entry.name for entry in bin_dir.iterdir() if entry.name != self.SUMS)
        return listed, present

    def _edit_sums(self, bin_dir: Path, edit) -> None:
        """Rewrite the layout's sums file: `edit` takes its (digest, name) entries and returns the entries to write."""
        path = bin_dir / self.SUMS
        entries = [tuple(line.split("  ")) for line in path.read_bytes().decode("utf-8").splitlines()]
        path.write_bytes("".join(f"{digest}  {name}\n" for digest, name in edit(entries)).encode("utf-8"))

    def _error(self, listed: list[str], present: list[str]) -> str:
        """The line the check prints when the name lists are not the eight, for these two lists."""
        return (f"ERROR: HARNESS-SHA256SUMS lists [{' '.join(listed)}] and bin/ holds [{' '.join(present)}]; "
                f"both must be exactly [{' '.join(self.HARNESS)}]")

    def _passes(self, bin_dir: Path) -> None:
        """The control: the check passes on this layout. Exit 0, and stdout is one `<name>: OK` line per
        file of the layout's sums file, in its order, then the success line."""
        done = self._run(bin_dir)
        listed, _ = self._names(bin_dir)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(listed, list(self.HARNESS))
        self.assertEqual(done.stdout.splitlines(), [f"{name}: OK" for name in listed] + [self.SUCCESS], done.stderr)

    def _fails_on_hash(self, bin_dir: Path, bad: str) -> None:
        """The check fails at its hash step: nonzero exit, and stdout is sha256sum's own verdict, FAILED
        for `bad`, OK for every other file, and no success line."""
        done = self._run(bin_dir)
        listed, _ = self._names(bin_dir)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        verdicts = [f"{name}: {'FAILED' if name == bad else 'OK'}" for name in listed]
        self.assertEqual(done.stdout.splitlines(), verdicts, done.stderr)

    def _fails_on_names(self, bin_dir: Path) -> None:
        """The check fails at its name step: nonzero exit, nothing on stdout (it stopped before it hashed
        anything), and its error line for the two name lists this layout really has."""
        done = self._run(bin_dir)
        listed, present = self._names(bin_dir)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stdout, "")
        self.assertIn(self._error(listed, present), done.stderr.splitlines())

    def test_the_dockerfile_check_passes_on_the_committed_harness_and_sums(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._passes(self._layout(Path(tmp)))

    def test_a_changed_byte_in_one_module_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            victim = bin_dir / "run_phase_transport.py"
            data = victim.read_bytes()
            middle = len(data) // 2
            victim.write_bytes(data[:middle] + bytes([data[middle] ^ 1]) + data[middle + 1:])
            self._fails_on_hash(bin_dir, victim.name)

    def test_a_ninth_file_in_the_directory_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            (bin_dir / "run_extra_stage.py").write_bytes(b"print('a file the sums file does not list')\n")
            self._fails_on_names(bin_dir)

    def test_a_missing_file_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            (bin_dir / "run_json.py").unlink()
            self._fails_on_names(bin_dir)

    def test_a_sums_line_removed_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            self._edit_sums(bin_dir, lambda entries: [entry for entry in entries if entry[1] != "run_execution_observer.py"])
            self._fails_on_names(bin_dir)

    def test_a_sums_line_for_a_ninth_name_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            self._edit_sums(bin_dir, lambda entries: entries + [(entries[0][0], "run_extra_stage.py")])
            self._fails_on_names(bin_dir)

    def test_an_empty_sums_file_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            self._edit_sums(bin_dir, lambda entries: [])
            self.assertEqual((bin_dir / self.SUMS).read_bytes(), b"")
            self._fails_on_names(bin_dir)

    def test_a_wrong_hash_on_one_sums_line_fails_the_check(self):
        def wrong_hash(entries):
            changed = []
            for digest, name in entries:
                if name == "run_json.py":
                    digest = ("1" if digest[0] == "0" else "0") + digest[1:]
                changed.append((digest, name))
            return changed

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._layout(Path(tmp))
            self._passes(bin_dir)
            self._edit_sums(bin_dir, wrong_hash)
            self._fails_on_hash(bin_dir, "run_json.py")


class PythonTestGateTest(unittest.TestCase):
    def test_release_rules_run_in_their_own_job(self):
        text = (WORKFLOWS / "python-test.yml").read_text(encoding="utf-8")
        job = text[text.index("\n  release-rules:\n"):]
        self.assertIn("python3 -m unittest discover -s tests -p 'test_release_*.py' -v", job)
        fingerprint = text[text.index("\n  fingerprint-canonical:\n"):text.index("\n  release-rules:\n")]
        self.assertNotIn("test_release_", fingerprint)


class ReleaseRunnerWorkflowTest(unittest.TestCase):
    RELEASE_JOBS = {
        "facts-descriptions.yml": {"descriptions"},
        "facts-fanout.yml": {"web", "license"},
        "handle-upstream.yml": {"bump"},
        "publish.yml": {"tag-guard", "prepare", "publish", "failure-report"},
        "release-finalize.yml": {"finalize", "notify-consumers"},
        "release-receipts.yml": {"plan", "verify"},
        "release-reconcile.yml": {"producers"},
        "release-rollback.yml": {"restore"},
    }
    RUNNER = ["self-hosted", "pf-ci", "pf-ci-release"]

    def release_workflows(self):
        return {name: _workflow(name) for name in self.RELEASE_JOBS}

    def assert_release_runners(self, workflows):
        for name, identities in self.RELEASE_JOBS.items():
            workflow = workflows[name]
            triggers = workflow["on"]
            if isinstance(triggers, str):
                triggers = [triggers]
            for event in ("pull_request", "pull_request_target"):
                self.assertNotIn(event, triggers, f"{name}: {event} cannot use pf-ci-release")
            # The reusable caller has no runs-on; its callee's jobs are pinned above.
            expected = identities | ({"notify-consumers"} if name == "publish.yml" else set())
            self.assertEqual(set(workflow["jobs"]), expected, f"{name}: release job inventory")
            for identity in sorted(identities):
                self.assertEqual(workflow["jobs"][identity]["runs-on"], self.RUNNER,
                                 f"{name}:{identity}: runs-on must be the isolated release array")

    def test_release_jobs_pin_the_isolated_class_and_never_accept_pr_events(self):
        self.assert_release_runners(self.release_workflows())

    def test_release_finalize_stops_the_apt_timers_before_anything_else(self):
        # LB-504: apt-daily-upgrade.timer re-execs systemd 20-25 minutes after
        # boot and kills the runner, and the finalizer runs hourly on
        # pf-ci-release. Both of its jobs must stop the timers before any
        # checkout, secret or repository code.
        jobs = _workflow("release-finalize.yml")["jobs"]
        for identity in ("finalize", "notify-consumers"):
            with self.subTest(job=identity):
                self.assertEqual((jobs.get(identity, {}).get("steps") or [{}])[0],
                                 ReleaseTriggerWorkflowTest.APT_GUARD_STEP,
                                 identity + " must stop the apt timers before anything else (LB-504)")

    def test_runner_pin_refuses_hosted_labels_other_pools_and_fallbacks_on_every_job(self):
        original = self.release_workflows()
        old_expression = """${{ github.event_name != 'pull_request_target' &&
            github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
            contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
            (github.event_name != 'pull_request' ||
             (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
            fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-24.04' }}"""
        invalid_runners = (
            "ubuntu-latest",
            "ubuntu-24.04",
            ["self-hosted", "pf-ci", "pf-ci-x64"],
            ["self-hosted", "pf-ci", "pf-ci-release", "ubuntu-24.04"],
            """${{ fromJSON('["self-hosted","pf-ci","pf-ci-release"]') || 'ubuntu-24.04' }}""",
            old_expression,
        )
        for name, identities in self.RELEASE_JOBS.items():
            for identity in sorted(identities):
                for runner in invalid_runners:
                    with self.subTest(workflow=name, job=identity, runner=runner):
                        mutant = copy.deepcopy(original)
                        mutant[name]["jobs"][identity]["runs-on"] = runner
                        with self.assertRaisesRegex(AssertionError, "runs-on"):
                            self.assert_release_runners(mutant)

    def test_runner_pin_refuses_pr_triggers_in_mapping_sequence_and_scalar_forms(self):
        original = self.release_workflows()
        for name in self.RELEASE_JOBS:
            for event in ("pull_request", "pull_request_target"):
                declarations = (
                    {**original[name]["on"], event: {}},
                    [*original[name]["on"], event],
                    event,
                )
                for triggers in declarations:
                    with self.subTest(workflow=name, event=event, triggers=triggers):
                        mutant = copy.deepcopy(original)
                        mutant[name]["on"] = triggers
                        with self.assertRaisesRegex(AssertionError, event):
                            self.assert_release_runners(mutant)

    def test_runner_inventory_pin_refuses_missing_and_unlisted_jobs(self):
        original = self.release_workflows()
        for name, workflow in original.items():
            for identity in workflow["jobs"]:
                with self.subTest(workflow=name, missing=identity):
                    mutant = copy.deepcopy(original)
                    mutant[name]["jobs"].pop(identity)
                    with self.assertRaisesRegex(AssertionError, "release job inventory"):
                        self.assert_release_runners(mutant)
            with self.subTest(workflow=name, extra="unreviewed-release-job"):
                mutant = copy.deepcopy(original)
                identity = sorted(self.RELEASE_JOBS[name])[0]
                mutant[name]["jobs"]["unreviewed-release-job"] = copy.deepcopy(workflow["jobs"][identity])
                with self.assertRaisesRegex(AssertionError, "release job inventory"):
                    self.assert_release_runners(mutant)


class PrTierWorkflowTest(unittest.TestCase):
    ROUTED_JOBS = {
        "python-test.yml": {"fingerprint-canonical", "release-rules", "description-renderer",
                            "release-ready-replay", "release-ready"},
        "facts-validate.yml": {"validate"},
    }
    RUNNER = ["self-hosted", "pf-ci", "pf-ci-x64"]
    RUNNER_LINE = '    runs-on: ["self-hosted","pf-ci","pf-ci-x64"]'
    PR_GUARD = ("github.event_name != 'pull_request' || "
                "github.event.pull_request.head.repo.full_name == github.repository")
    AUTHZ_RUNNER_LINE = '    runs-on: ["self-hosted", "pf-ci", "pf-ci-x64"]'
    # Literal ci-labels.md 10-20 16:02, "The `authz` job" (addendum 9).
    # Keep this independent of the workflows: every byte, including comments,
    # permissions and both permission lookups, is pinned.
    AUTHZ_TEXT = """  authz:
    name: authz
    # Pool label (needs no toolchain or Docker). Switch to pf-ci-crj once laneboss's
    # bulletin says the Cloud Run class is live.
    runs-on: ["self-hosted", "pf-ci", "pf-ci-x64"]
    permissions: {}
    # No timeout-minutes: the owner's no-time-limits rule (check-no-time-limits refuses new ones).
    steps:
      - name: Only our own pull requests and our own events run CI
        env:
          EVENT: ${{ github.event_name }}
          REPO: ${{ github.repository }}
          HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}
          HEAD_IS_FORK: ${{ github.event.pull_request.head.repo.fork }}
          ASSOCIATION: ${{ github.event.pull_request.author_association }}
          PR_AUTHOR: ${{ github.event.pull_request.user.login }}
          WR_HEAD_REPO: ${{ github.event.workflow_run.head_repository.full_name }}
          WR_ACTOR: ${{ github.event.workflow_run.triggering_actor.login }}
          GH_TOKEN: ${{ github.token }}   # read-only use: one permission lookup for workflow_run
          # Bot logins whose same-repository pull requests count as ours: dependabot[bot] (CI-DEPBOT A)
          # and the org's own GitHub App bot logins by name (space-separated), nothing else.
          ALLOWED_BOTS: "dependabot[bot] pineforge-app[bot]"
        run: |
          set -eu
          fail() { echo "::error title=authz::$1"; echo "authz: REFUSED: $1" >> "$GITHUB_STEP_SUMMARY"; exit 1; }
          case "$EVENT" in
            push|workflow_dispatch|schedule|merge_group|release|workflow_call|repository_dispatch)
              # Only someone with write access to this repository (or our App's token) can cause these
              # events; none of them runs pull-request code.
              ;;
            workflow_run)
              # The pwn-request path: it runs in THIS repository with secrets even when the triggering run
              # came from a fork. Refuse ALWAYS unless the triggering run's head is this repository, and
              # apply the same identity rule to its triggering actor as to a pull-request author.
              [ "$WR_HEAD_REPO" = "$REPO" ] || fail "workflow_run triggered from another repository (${WR_HEAD_REPO:-unknown})"
              case " $ALLOWED_BOTS " in
                *" $WR_ACTOR "*) ;;
                *)
                  # No author_association exists on workflow_run: ask GitHub for the actor's permission on
                  # this repository; anything but admin/maintain/write (or a failed call) refuses.
                  perm=$(gh api "repos/$REPO/collaborators/$WR_ACTOR/permission" --jq .permission 2>/dev/null || echo none)
                  case "$perm" in admin|maintain|write) ;; *) fail "workflow_run triggering actor ${WR_ACTOR:-unknown} has permission '$perm', not write" ;; esac ;;
              esac ;;
            pull_request)
              [ "$HEAD_REPO" = "$REPO" ] || fail "pull request from another repository (${HEAD_REPO:-unknown})"
              [ "$HEAD_IS_FORK" = "false" ] || fail "pull request from a fork"
              case "$ASSOCIATION" in
                OWNER|MEMBER|COLLABORATOR) ;;
                *)
                  case " $ALLOWED_BOTS " in
                    *" $PR_AUTHOR "*) ;;
                    *)
                      # A member whose org membership is private shows as CONTRIBUTOR: ask GitHub for the
                      # author's permission on this repository; anything but admin/maintain/write (or a
                      # failed call) refuses.
                      perm=$(gh api "repos/$REPO/collaborators/$PR_AUTHOR/permission" --jq .permission 2>/dev/null || echo none)
                      case "$perm" in admin|maintain|write) ;; *) fail "author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) has permission '$perm', not write, and is not an allowlisted bot" ;; esac ;;
                  esac ;;
              esac ;;
            *) fail "event $EVENT does not run CI (pull_request_target and other events are not allowed)" ;;
          esac
          echo "authz: allowed ($EVENT${PR_AUTHOR:+ by $PR_AUTHOR})" >> "$GITHUB_STEP_SUMMARY"
"""
    STATUS_OVERRIDE = re.compile(r"\b(?:always|cancelled|failure)\s*\(\s*\)")
    AUTHZ_SUCCESS = "needs.authz.result == 'success'"
    AUTHZ_REFUSAL_STEP = {
        "name": "Refuse an unauthorised run before anything else",
        "if": "${{ needs.authz.result != 'success' }}",
        "run": (
            'echo "::error::authz did not pass; nothing of this run\'s code is checked out or run"\n'
            "exit 1\n"
        ),
    }
    AUTHZ_REFUSAL_TEXT = """      - name: Refuse an unauthorised run before anything else
        if: ${{ needs.authz.result != 'success' }}
        run: |
          echo "::error::authz did not pass; nothing of this run's code is checked out or run"
          exit 1
"""
    # Retain the old expression only as a negative-control fixture. Release
    # workflows already use the isolated class; this migration does not route
    # them through the PR pool.
    LEGACY_RUNNER = """${{ github.event_name != 'pull_request_target' &&
        github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
        contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
        (github.event_name != 'pull_request' ||
         (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
        fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-24.04' }}"""
    RELEASE_RUNNER = ["self-hosted", "pf-ci", "pf-ci-release"]
    EXEMPT_RUNNERS = {
        "facts-descriptions.yml": {"descriptions": RELEASE_RUNNER},
        "facts-fanout.yml": {"web": RELEASE_RUNNER, "license": RELEASE_RUNNER},
        "handle-upstream.yml": {"bump": RELEASE_RUNNER},
        "publish.yml": {"tag-guard": RELEASE_RUNNER, "prepare": RELEASE_RUNNER,
                        "publish": RELEASE_RUNNER, "notify-consumers": None,
                        "failure-report": RELEASE_RUNNER},
        "release-finalize.yml": {"finalize": RELEASE_RUNNER, "notify-consumers": RELEASE_RUNNER},
        "release-receipts.yml": {"plan": RELEASE_RUNNER, "verify": RELEASE_RUNNER},
        "release-reconcile.yml": {"producers": RELEASE_RUNNER},
        "release-rollback.yml": {"restore": RELEASE_RUNNER},
    }
    LEGACY_LABEL_JOBS = {
        ("publish.yml", "prepare"), ("publish.yml", "failure-report"),
        ("release-receipts.yml", "plan"),
    }

    def assert_runner(self, runner):
        self.assertEqual(runner, self.RUNNER)

    def assert_pool_job(self, job):
        self.assert_runner(job.get("runs-on"))
        self.assertNotIn("PF_CI_LABELS", json.dumps(job))

    def assert_pr_trigger(self, workflow):
        self.assertIn("pull_request", workflow["on"])
        if workflow["name"] == "python-test":
            self.assertEqual(workflow["on"]["pull_request"], {
                "types": ["opened", "synchronize", "reopened", "ready_for_review"],
            })
        else:
            self.assertFalse(workflow["on"]["pull_request"])
        self.assertNotIn("pull_request_target", workflow["on"])

    def assert_no_fork_condition(self, job):
        condition = job.get("if", "")
        for forbidden in ("head.repo.full_name", "github.repository"):
            self.assertNotIn(forbidden, condition)

    def assert_status_condition_requires_authz(self, identity, job):
        condition = job.get("if", "")
        if identity == "release-ready":
            # A skipped required check counts as success; report cancelled needs.
            self.assertEqual(condition, "${{ always() }}")
            self.assert_authz_refusal_first(job)
            self.assert_authz_success_steps(job)
        elif self.STATUS_OVERRIDE.search(condition):
            self.assertIn("needs.authz.result == 'success'", condition)

    def assert_authz_refusal_first(self, job):
        self.assertNotIn("continue-on-error", job)
        self.assertTrue(job.get("steps"), "authz status overrides must have a refusal step")
        # Exact shape pins the failure comparison and exit, and rejects
        # continue-on-error or any action/code inserted before the gate.
        self.assertEqual(job["steps"][0], self.AUTHZ_REFUSAL_STEP)

    def assert_authz_success_steps(self, job):
        # R5b also covers third-party actions here. An existing condition may
        # follow the mandatory success term only inside one balanced group.
        for index, step in enumerate(job["steps"][1:], start=2):
            condition = step.get("if", "")
            self.assertIsInstance(condition, str, f"step {index}")
            if condition == self.AUTHZ_SUCCESS:
                continue
            prefix = self.AUTHZ_SUCCESS + " && "
            self.assertTrue(condition.startswith(prefix), f"step {index}: {condition}")
            remainder = condition[len(prefix):]
            self.assertTrue(remainder.startswith("(") and remainder.endswith(")"),
                            f"step {index}: {condition}")
            tokens = re.findall(r"'(?:[^']|'')*'|[()]|[^'()]+", remainder)
            self.assertEqual("".join(tokens), remainder)
            depth = 0
            for offset, token in enumerate(tokens):
                if token == "(":
                    depth += 1
                elif token == ")":
                    depth -= 1
                if offset == len(tokens) - 1:
                    self.assertEqual(depth, 0, condition)
                else:
                    self.assertGreater(depth, 0, condition)

    def assert_status_override_refusals(self, workflow):
        protected = set()
        for identity, job in workflow["jobs"].items():
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            if "authz" in needs and self.STATUS_OVERRIDE.search(job.get("if", "")):
                self.assert_authz_refusal_first(job)
                self.assert_authz_success_steps(job)
                protected.add(identity)
        return protected

    def assert_authz_refusal_text(self, text, identities):
        for identity in identities:
            block = re.search(rf"(?ms)^  {re.escape(identity)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text)
            self.assertIsNotNone(block, identity)
            steps = block[1].split("    steps:\n", 1)
            self.assertEqual(len(steps), 2, identity)
            self.assertTrue(steps[1].startswith(self.AUTHZ_REFUSAL_TEXT), identity)

    def assert_job_condition(self, identity, job):
        self.assert_no_fork_condition(job)
        self.assert_status_condition_requires_authz(identity, job)
        if identity == "changes":
            self.assertEqual(job.get("if"), "${{ !github.event.pull_request.draft }}")
        elif identity == "validate":
            self.assertNotIn("if", job)
        elif identity == "release-ready":
            self.assertEqual(job["if"], "${{ always() }}")
        else:
            self.assertEqual(job.get("if"),
                             "${{ !github.event.pull_request.draft && "
                             "(needs.changes.outputs.docs-only == 'false') }}")

    def assert_authz_dependency(self, job):
        needs = job.get("needs", [])
        self.assertIn("authz", [needs] if isinstance(needs, str) else needs)

    def assert_job_inventory(self, name, workflow):
        expected = self.ROUTED_JOBS[name] | {"authz"} | ({"changes"} if name == "python-test.yml" else set())
        self.assertEqual(set(workflow["jobs"]), expected)
        self.assertEqual(next(iter(workflow["jobs"])), "authz")
        self.assert_authz(workflow["jobs"]["authz"])
        return expected

    def assert_authz(self, job):
        import yaml
        expected = yaml.load(self.AUTHZ_TEXT, Loader=yaml.BaseLoader)["authz"]
        self.assertEqual(job, expected)
        self.assertEqual(set(job), {"name", "runs-on", "permissions", "steps"})
        self.assertEqual(job["name"], "authz")
        self.assertEqual(job["permissions"], {})
        self.assertEqual(set(job["steps"][0]["env"]), {
            "EVENT", "REPO", "HEAD_REPO", "HEAD_IS_FORK", "ASSOCIATION",
            "PR_AUTHOR", "WR_HEAD_REPO", "WR_ACTOR", "GH_TOKEN", "ALLOWED_BOTS",
        })
        self.assertEqual(job["steps"][0]["env"]["ALLOWED_BOTS"],
                         "dependabot[bot] pineforge-app[bot]")
        self.assert_pool_job(job)
        self.assertNotIn("if", job)
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("timeout-minutes", job)
        for forbidden in ("actions/checkout", "secrets.", "id-token", "environment",
                          "github.actor", "github.triggering_actor"):
            self.assertNotIn(forbidden, json.dumps(job))

    def assert_authz_text(self, text):
        block = re.search(r"(?ms)^  authz:\n.*?(?=^  [\w-]+:\n|\Z)", text)
        self.assertIsNotNone(block)
        self.assertEqual(block[0], self.AUTHZ_TEXT + "\n")

    def assert_literal_routes(self, text, identities):
        for identity in identities:
            block = re.search(rf"(?ms)^  {re.escape(identity)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text)
            self.assertIsNotNone(block, identity)
            runner_lines = re.findall(r"(?m)^    runs-on:.*$", block[1])
            expected = self.AUTHZ_RUNNER_LINE if identity == "authz" else self.RUNNER_LINE
            self.assertEqual(runner_lines, [expected], identity)

    def test_all_pr_jobs_pin_literal_pool_routes_conditions_and_authz(self):
        pr_workflows = {path.name: _workflow(path.name) for path in WORKFLOWS.glob("*.yml")
                        if "pull_request" in _workflow(path.name)["on"]}
        self.assertEqual(set(pr_workflows), set(self.ROUTED_JOBS))
        for name, workflow in pr_workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(workflow["name"], name.removesuffix(".yml"))
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertEqual(workflow["concurrency"], {
                    "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}",
                    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
                })
                self.assert_pr_trigger(workflow)
                expected = self.assert_job_inventory(name, workflow)
                self.assert_status_override_refusals(workflow)
                for identity, job in workflow["jobs"].items():
                    self.assert_no_fork_condition(job)
                    self.assertEqual(job.get("name", identity),
                                     "release/ready" if identity == "release-ready" else identity)
                    self.assertNotIn("uses", job, "PR coverage must not hide a reusable pool job")
                    self.assert_pool_job(job)
                    if identity == "authz":
                        self.assert_authz(job)
                    else:
                        self.assertNotIn("permissions", job)
                        self.assert_job_condition(identity, job)
                        self.assert_authz_dependency(job)
                text = (WORKFLOWS / name).read_text()
                self.assert_literal_routes(text, expected)
                self.assert_authz_text(text)
                for forbidden in ("secrets.", "create-github-app-token", "id-token:", ": write"):
                    self.assertNotIn(forbidden, text)

    def test_each_runner_pin_refuses_hosted_fragments_unavailable_labels_and_labels_env(self):
        for name in self.ROUTED_JOBS:
            text = (WORKFLOWS / name).read_text()
            for identity, job in _workflow(name)["jobs"].items():
                self.assert_pool_job(job)
                for runner in (
                    "ubuntu-24.04", self.LEGACY_RUNNER,
                    """${{ fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') }}""",
                    """${{ fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-24.04' }}""",
                    """${{ fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-latest' }}""",
                    ["self-hosted", "pf-ci", "pf-ci-crj"],
                    ["self-hosted", "pf-ci", "pf-ci-x64-16"],
                    ["self-hosted", "pf-ci"],
                ):
                    mutant = copy.deepcopy(job)
                    mutant["runs-on"] = runner
                    with self.subTest(workflow=name, job=identity, runner=runner):
                        self.assertNotEqual(mutant, job)
                        with self.assertRaises(AssertionError):
                            self.assert_pool_job(mutant)
                mutant = copy.deepcopy(job)
                mutant.setdefault("env", {})["PF_CI_LABELS"] = json.dumps(self.RUNNER)
                with self.subTest(workflow=name, job=identity, mutation="labels env"):
                    with self.assertRaises(AssertionError):
                        self.assert_pool_job(mutant)
                block = re.search(rf"(?ms)^  {re.escape(identity)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text)
                self.assertIsNotNone(block)
                original = self.AUTHZ_RUNNER_LINE if identity == "authz" else self.RUNNER_LINE
                replacement = self.RUNNER_LINE if identity == "authz" else self.AUTHZ_RUNNER_LINE
                altered = block[0].replace(original, replacement, 1)
                mutant_text = text[:block.start()] + altered + text[block.end():]
                self.assertNotEqual(mutant_text, text)
                with self.assertRaises(AssertionError):
                    self.assert_literal_routes(mutant_text, {identity})

    def test_every_job_refuses_reintroduced_job_level_fork_conditions(self):
        for name in self.ROUTED_JOBS:
            for identity, job in _workflow(name)["jobs"].items():
                self.assert_no_fork_condition(job)
                conditions = [
                    self.PR_GUARD, "${{ " + self.PR_GUARD + " }}",
                    f"always() && ({self.PR_GUARD})",
                    f"!cancelled() && ({self.PR_GUARD})",
                    "github.repository != ''",
                    "github.event.pull_request.head.repo.full_name != ''",
                ]
                if "if" in job:
                    conditions.append(f"({job['if']}) && ({self.PR_GUARD})")
                for condition in conditions:
                    mutant = copy.deepcopy(job)
                    mutant["if"] = condition
                    with self.subTest(workflow=name, job=identity, condition=condition):
                        self.assertNotEqual(mutant, job)
                        with self.assertRaises(AssertionError):
                            self.assert_no_fork_condition(mutant)

    def test_every_non_authz_job_pins_existing_conditions_and_requires_authz(self):
        for name in self.ROUTED_JOBS:
            for identity, job in _workflow(name)["jobs"].items():
                if identity == "authz":
                    continue
                self.assert_job_condition(identity, job)
                self.assert_authz_dependency(job)
                if identity in {"changes", "validate"}:
                    conditions = ["always()", "false"]
                elif identity == "release-ready":
                    conditions = [
                        None, "success()", "always()", "!cancelled()", "failure()",
                        "${{ !cancelled() }}", "${{ success() }}", "${{ failure() }}",
                        "always() && needs.authz.result == 'success'",
                        "${{ always() && needs.authz.result == 'success' }}",
                    ]
                else:
                    conditions = [None, "always()", "needs.changes.outputs.docs-only == 'true'"]
                for condition in conditions:
                    mutant = copy.deepcopy(job)
                    if condition is None:
                        mutant.pop("if")
                    else:
                        mutant["if"] = condition
                    with self.subTest(workflow=name, job=identity, condition=condition):
                        self.assertNotEqual(mutant, job)
                        with self.assertRaises(AssertionError):
                            self.assert_job_condition(identity, mutant)
                for missing_needs in (False, True):
                    mutant = copy.deepcopy(job)
                    needs = job["needs"]
                    mutant["needs"] = [need for need in ([needs] if isinstance(needs, str) else needs)
                                       if need != "authz"]
                    if missing_needs:
                        mutant.pop("needs")
                    with self.subTest(workflow=name, job=identity, missing_needs=missing_needs):
                        with self.assertRaises(AssertionError):
                            self.assert_authz_dependency(mutant)

    def test_status_override_conditions_require_authz_success_and_refuse_its_removal(self):
        # Neither routed workflow currently has a non-aggregate status override.
        # Exercise the rule on each real ordinary job so adding one cannot
        # silently bypass a failed dependency. All overrides also refuse first.
        for name in self.ROUTED_JOBS:
            for identity, job in _workflow(name)["jobs"].items():
                if identity in {"authz", "release-ready"}:
                    continue
                for override in ("!cancelled()", "always()", "failure()", "success() || failure()"):
                    guarded = copy.deepcopy(job)
                    guarded["if"] = f"({override}) && needs.authz.result == 'success'"
                    guarded["steps"].insert(0, copy.deepcopy(self.AUTHZ_REFUSAL_STEP))
                    for step in guarded["steps"][1:]:
                        previous = step.get("if")
                        step["if"] = (f"{self.AUTHZ_SUCCESS} && ({previous})"
                                      if previous else self.AUTHZ_SUCCESS)
                    self.assert_status_condition_requires_authz(identity, guarded)
                    self.assertEqual(self.assert_status_override_refusals(
                        {"jobs": {identity: guarded}}), {identity})
                    mutant = copy.deepcopy(guarded)
                    mutant["if"] = mutant["if"].replace(" && needs.authz.result == 'success'", "")
                    with self.subTest(workflow=name, job=identity, override=override):
                        self.assertNotEqual(mutant, guarded)
                        with self.assertRaises(AssertionError):
                            self.assert_status_condition_requires_authz(identity, mutant)

    def test_security_819_all_authz_status_overrides_pin_first_refusal_and_exact_source(self):
        expected = {"python-test.yml": {"release-ready"}, "facts-validate.yml": set()}
        self.assertEqual(set(expected), set(self.ROUTED_JOBS))
        for name, identities in expected.items():
            with self.subTest(workflow=name):
                self.assertEqual(self.assert_status_override_refusals(_workflow(name)), identities)
                self.assert_authz_refusal_text((WORKFLOWS / name).read_text(), identities)

    def test_security_819_refusal_pin_refuses_removal_reordering_bypass_and_changed_test(self):
        for name in self.ROUTED_JOBS:
            workflow = _workflow(name)
            for identity in self.assert_status_override_refusals(workflow):
                job = workflow["jobs"][identity]
                mutants = []
                mutant = copy.deepcopy(job)
                mutant["steps"].pop(0)
                mutants.append(("drop refusal", mutant))
                mutant = copy.deepcopy(job)
                mutant["steps"].append(mutant["steps"].pop(0))
                mutants.append(("move after aggregate", mutant))
                mutant = copy.deepcopy(job)
                mutant["steps"] = []
                mutants.append(("drop all steps", mutant))
                for earlier in (
                    {"uses": "actions/checkout@must-not-run"},
                    {"uses": "./.github/actions/must-not-run"},
                    {"uses": "actions/setup-python@must-not-run"},
                    {"run": "python3 scripts/must-not-run.py"},
                ):
                    protected = copy.deepcopy(job)
                    protected["steps"].insert(1, {**earlier, "if": self.AUTHZ_SUCCESS})
                    self.assertEqual(self.assert_status_override_refusals(
                        {"jobs": {identity: protected}}), {identity})
                    mutant = copy.deepcopy(protected)
                    mutant["steps"][0], mutant["steps"][1] = mutant["steps"][1], mutant["steps"][0]
                    mutants.append(("move after " + str(earlier), mutant))
                for key, value in (
                    ("name", "Authorization is optional"),
                    ("if", "needs.authz.result == 'success'"),
                    ("continue-on-error", "true"),
                    ("uses", "./.github/actions/must-not-run"),
                    ("env", {}),
                    ("env", {"AUTHZ_RESULT": "success"}),
                    ("env", {"AUTHZ_RESULT": "${{ needs.changes.result }}"}),
                    ("if", "needs.authz.result != 'skipped'"),
                    ("if", "needs.changes.result != 'success'"),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"].replace("exit 1", "exit 0")),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"].replace("\nexit 1\n", "; exit 1")),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"].replace("\nexit 1\n", "; exit 1\n")),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"].replace("exit 1\n", "")),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"].removesuffix("\n")),
                    ("run", self.AUTHZ_REFUSAL_STEP["run"] + "\n"),
                    ("run", 'test "$AUTHZ_RESULT" = success || { echo "::error::authz did not succeed"; exit 1; }'),
                    ("run", "echo ignored; exit 1"),
                    ("run", "true"),
                ):
                    mutant = copy.deepcopy(job)
                    mutant["steps"][0][key] = value
                    mutants.append((key + ":" + str(value), mutant))
                mutant = copy.deepcopy(job)
                mutant["continue-on-error"] = "true"
                mutants.append(("job continue-on-error", mutant))
                for mutation, mutant in mutants:
                    with self.subTest(workflow=name, job=identity, mutation=mutation):
                        self.assertNotEqual(mutant, job)
                        with self.assertRaises(AssertionError):
                            self.assert_status_override_refusals({"jobs": {identity: mutant}})

    def test_security_819_source_pin_refuses_missing_late_or_rewritten_refusal(self):
        text = (WORKFLOWS / "python-test.yml").read_text()
        identities = {"release-ready"}
        self.assert_authz_refusal_text(text, identities)
        for replacement in (
            "",
            "      - uses: actions/checkout@must-not-run\n" + self.AUTHZ_REFUSAL_TEXT,
            "      - uses: ./.github/actions/must-not-run\n" + self.AUTHZ_REFUSAL_TEXT,
            self.AUTHZ_REFUSAL_TEXT.replace("Refuse an unauthorised run before anything else",
                                           "Optional authorization"),
            self.AUTHZ_REFUSAL_TEXT.replace("needs.authz.result", "needs.changes.result"),
            self.AUTHZ_REFUSAL_TEXT.replace("!= 'success'", "== 'success'"),
            self.AUTHZ_REFUSAL_TEXT.replace("!= 'success'", "!= 'skipped'"),
            self.AUTHZ_REFUSAL_TEXT.replace("exit 1", "exit 0"),
            self.AUTHZ_REFUSAL_TEXT.replace("          exit 1\n", ""),
            self.AUTHZ_REFUSAL_TEXT.replace("        run: |\n          ", "        run: ", 1)
                                   .replace("\n          exit 1\n", "; exit 1\n", 1),
            self.AUTHZ_REFUSAL_TEXT.replace("        run: |", "        run: |-", 1),
            self.AUTHZ_REFUSAL_TEXT.replace("        run: |", "        run: |+", 1),
            self.AUTHZ_REFUSAL_TEXT.replace("        run:", "        continue-on-error: true\n        run:"),
        ):
            mutant = text.replace(self.AUTHZ_REFUSAL_TEXT, replacement, 1)
            with self.subTest(replacement=replacement):
                self.assertNotEqual(mutant, text)
                with self.assertRaises(AssertionError):
                    self.assert_authz_refusal_text(mutant, identities)

    def test_required_aggregate_source_refuses_skippable_conditions_and_unsafe_steps(self):
        import yaml
        text = (WORKFLOWS / "python-test.yml").read_text()
        match = re.search(r"(?ms)^  release-ready:\n.*?(?=^  [\w-]+:\n|\Z)", text)
        self.assertIsNotNone(match)
        block = match[0]
        condition = "    if: ${{ always() }}\n"
        self.assertEqual(block.count(condition), 1)
        replacements = [
            block.replace(condition, replacement, 1)
            for replacement in (
                "", "    if: ${{ !cancelled() }}\n", "    if: ${{ success() }}\n",
                "    if: ${{ failure() }}\n",
                "    if: ${{ always() && needs.authz.result == 'success' }}\n",
            )
        ]
        replacements.extend((
            block.replace(self.AUTHZ_REFUSAL_TEXT, "", 1),
            block.replace(self.AUTHZ_REFUSAL_TEXT,
                          "      - uses: actions/checkout@must-not-run\n"
                          "        if: needs.authz.result == 'success'\n" + self.AUTHZ_REFUSAL_TEXT, 1),
            block.replace("        if: needs.authz.result == 'success'\n", "", 1),
        ))
        self.assert_job_condition("release-ready", _workflow("python-test.yml")["jobs"]["release-ready"])
        for index, replacement in enumerate(replacements):
            mutant = text[:match.start()] + replacement + text[match.end():]
            with self.subTest(mutation=index):
                self.assertNotEqual(mutant, text)
                job = yaml.load(mutant, Loader=yaml.BaseLoader)["jobs"]["release-ready"]
                with self.assertRaises(AssertionError):
                    self.assert_job_condition("release-ready", job)

    def test_security_819_first_refusal_covers_failure_and_docs_only_status_overrides(self):
        for name in self.ROUTED_JOBS:
            for identity, job in _workflow(name)["jobs"].items():
                if identity == "authz":
                    continue
                for condition in (
                    "always()", "!cancelled()", "failure()", "success() || failure()",
                    "${{ failure() && !cancelled() }}",
                    "${{ !cancelled() && needs.changes.outputs.docs-only == 'false' }}",
                    "always () && needs.authz.result == 'success'",
                ):
                    guarded = copy.deepcopy(job)
                    guarded["if"] = condition
                    if guarded["steps"][0] != self.AUTHZ_REFUSAL_STEP:
                        guarded["steps"].insert(0, copy.deepcopy(self.AUTHZ_REFUSAL_STEP))
                    for step in guarded["steps"][1:]:
                        previous = step.get("if")
                        if previous != self.AUTHZ_SUCCESS:
                            step["if"] = (f"{self.AUTHZ_SUCCESS} && ({previous})"
                                          if previous else self.AUTHZ_SUCCESS)
                    with self.subTest(workflow=name, job=identity, condition=condition):
                        self.assertEqual(self.assert_status_override_refusals(
                            {"jobs": {identity: guarded}}), {identity})
                        mutant = copy.deepcopy(guarded)
                        mutant["steps"].pop(0)
                        self.assertNotEqual(mutant, guarded)
                        with self.assertRaises(AssertionError):
                            self.assert_status_override_refusals({"jobs": {identity: mutant}})

    def test_r5b_every_later_action_and_run_requires_authz_success(self):
        for name in self.ROUTED_JOBS:
            workflow = _workflow(name)
            for identity in self.assert_status_override_refusals(workflow):
                job = workflow["jobs"][identity]
                for index in range(1, len(job["steps"])):
                    mutant = copy.deepcopy(job)
                    mutant["steps"][index].pop("if")
                    with self.subTest(workflow=name, job=identity, step=index + 1):
                        with self.assertRaises(AssertionError):
                            self.assert_status_override_refusals({"jobs": {identity: mutant}})
                for later in (
                    {"uses": "actions/checkout@must-not-run"},
                    {"uses": "./.github/actions/must-not-run"},
                    {"uses": "actions/setup-python@must-not-run"},
                    {"uses": "actions/upload-artifact@must-not-run"},
                    {"run": "python3 scripts/must-not-run.py"},
                ):
                    guarded = copy.deepcopy(job)
                    guarded["steps"].append({**later, "if": self.AUTHZ_SUCCESS})
                    self.assertEqual(self.assert_status_override_refusals(
                        {"jobs": {identity: guarded}}), {identity})
                    combined = copy.deepcopy(guarded)
                    combined["steps"][-1]["if"] += " && (failure() || !cancelled())"
                    self.assertEqual(self.assert_status_override_refusals(
                        {"jobs": {identity: combined}}), {identity})
                    for condition in (
                        None, "failure()", "!cancelled()",
                        "needs.authz.result != 'success'",
                        self.AUTHZ_SUCCESS + " || failure()",
                        self.AUTHZ_SUCCESS + " && (false) || failure()",
                        self.AUTHZ_SUCCESS + " && (false) || (failure())",
                        "failure() || " + self.AUTHZ_SUCCESS,
                        "${{ always() }}",
                    ):
                        mutant = copy.deepcopy(guarded)
                        if condition is None:
                            mutant["steps"][-1].pop("if")
                        else:
                            mutant["steps"][-1]["if"] = condition
                        with self.subTest(workflow=name, job=identity, step=later, condition=condition):
                            self.assertNotEqual(mutant, guarded)
                            with self.assertRaises(AssertionError):
                                self.assert_status_override_refusals({"jobs": {identity: mutant}})

    def test_unfiltered_pr_trigger_pin_refuses_missing_pr_target_paths_and_branches(self):
        for name in self.ROUTED_JOBS:
            workflow = _workflow(name)
            self.assert_pr_trigger(workflow)
            mutants = []
            for key in ("paths", "paths-ignore", "branches", "branches-ignore"):
                mutant = copy.deepcopy(workflow)
                mutant["on"]["pull_request"] = {key: ["main" if "branches" in key else "facts/**"]}
                mutants.append(mutant)
            mutant = copy.deepcopy(workflow)
            mutant["on"].pop("pull_request")
            mutants.append(mutant)
            mutant = copy.deepcopy(workflow)
            mutant["on"]["pull_request_target"] = ""
            mutants.append(mutant)
            for index, mutant in enumerate(mutants):
                with self.subTest(workflow=name, mutation=index):
                    self.assertNotEqual(mutant, workflow)
                    with self.assertRaises(AssertionError):
                        self.assert_pr_trigger(mutant)

    def test_authz_source_is_verbatim_and_refuses_canon_security_mutations(self):
        for name in self.ROUTED_JOBS:
            text = (WORKFLOWS / name).read_text()
            self.assert_authz_text(text)
            block = re.search(r"(?ms)^  authz:\n.*?(?=^  [\w-]+:\n|\Z)", text)[0]
            workflow_run_arm = re.search(
                r"(?ms)^            workflow_run\)\n.*?(?=^            pull_request\)\n)", block)[0]
            pull_request_arm = re.search(
                r"(?ms)^            pull_request\)\n.*?(?=^            \*\) fail)", block)[0]
            fail_line = next(line for line in block.splitlines(keepends=True)
                             if line.lstrip().startswith("fail()"))
            default_arm = next(line for line in block.splitlines(keepends=True)
                               if line.lstrip().startswith('*) fail "event'))
            fork_check = next(line for line in block.splitlines(keepends=True)
                              if line.lstrip().startswith('[ "$HEAD_IS_FORK"'))
            workflow_run_check = next(line for line in block.splitlines(keepends=True)
                                      if line.lstrip().startswith('[ "$WR_HEAD_REPO"'))
            permission_lookup = next(line for line in workflow_run_arm.splitlines(keepends=True)
                                     if line.lstrip().startswith("perm=$(gh api"))
            pr_permission_lookup = next(line for line in pull_request_arm.splitlines(keepends=True)
                                        if line.lstrip().startswith("perm=$(gh api"))
            pr_permission_case = next(line for line in pull_request_arm.splitlines(keepends=True)
                                      if line.lstrip().startswith('case "$perm"'))
            mutations = [
                (fail_line, "          fail() { :; }\n"),
                (fork_check, ""),
                (workflow_run_arm, ""),
                (pull_request_arm, ""),
                (default_arm, ""),
                (workflow_run_check, ""),
                (permission_lookup, ""),
                (permission_lookup, permission_lookup.replace("|| echo none)", "|| echo write)", 1)),
                (pr_permission_lookup, ""),
                (pr_permission_lookup, pr_permission_lookup.replace("|| echo none)", "|| echo write)", 1)),
                (pr_permission_case, pr_permission_case.replace("admin|maintain|write", "admin|maintain|write|read", 1)),
                (pr_permission_case, pr_permission_case.replace("admin|maintain|write", "admin|write", 1)),
                ("push|workflow_dispatch|schedule|merge_group|release|workflow_call|repository_dispatch)",
                 "push|workflow_dispatch|schedule|merge_group|release|workflow_call|repository_dispatch|pull_request_target)"),
                ("    permissions: {}\n", ""),
                ("    permissions: {}\n", "    permissions:\n      contents: write\n"),
                ("    name: authz\n", ""),
                ('          ALLOWED_BOTS: "dependabot[bot] pineforge-app[bot]"',
                 '          ALLOWED_BOTS: "dependabot[bot] pineforge-app[bot] another[bot]"'),
                ("    steps:\n", "    steps:\n      - uses: actions/checkout@must-not-run\n"),
                ("  authz:\n", "  authz:\n    if: always()\n"),
                ('          case "$EVENT" in\n',
                 '          [ "$GITHUB_ACTOR" != "dependabot[bot]" ] || exit 0\n'
                 '          case "$EVENT" in\n'),
                (self.AUTHZ_RUNNER_LINE, self.RUNNER_LINE),
                ("          HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}",
                 "          HEAD_REPO: ${{ github.repository }}"),
            ]
            # Remove each of the eight payload fields, GH_TOKEN and ALLOWED_BOTS
            # from real source, including the mandatory HEAD_IS_FORK deletion.
            for key in ("EVENT", "REPO", "HEAD_REPO", "HEAD_IS_FORK", "ASSOCIATION",
                        "PR_AUTHOR", "WR_HEAD_REPO", "WR_ACTOR", "GH_TOKEN", "ALLOWED_BOTS"):
                env_line = re.search(rf"(?m)^          {key}: .*\n", block)
                self.assertIsNotNone(env_line, key)
                mutations.append((env_line[0], ""))
            for original, replacement in mutations:
                self.assertEqual(block.count(original), 1, original)
                mutant = text.replace(original, replacement, 1)
                with self.subTest(workflow=name, mutation=original):
                    self.assertNotEqual(mutant, text)
                    with self.assertRaises(AssertionError):
                        self.assert_authz_text(mutant)

    def test_authz_pins_refuse_previous_canon_without_pr_permission_lookup(self):
        import yaml
        # This literal is only a stale mutation fixture, never an accepted pin.
        previous_pr_arm = """            pull_request)
              [ "$HEAD_REPO" = "$REPO" ] || fail "pull request from another repository (${HEAD_REPO:-unknown})"
              [ "$HEAD_IS_FORK" = "false" ] || fail "pull request from a fork"
              case "$ASSOCIATION" in
                OWNER|MEMBER|COLLABORATOR) ;;
                *)
                  case " $ALLOWED_BOTS " in
                    *" $PR_AUTHOR "*) ;;
                    *) fail "author ${PR_AUTHOR:-unknown} (${ASSOCIATION:-NONE}) is not an owner, member, collaborator or allowlisted bot" ;;
                  esac ;;
              esac ;;
"""
        for name in self.ROUTED_JOBS:
            text = (WORKFLOWS / name).read_text()
            self.assert_authz_text(text)
            block = re.search(r"(?ms)^  authz:\n.*?(?=^  [\w-]+:\n|\Z)", text)[0]
            current_pr_arm = re.search(
                r"(?ms)^            pull_request\)\n.*?(?=^            \*\) fail)", block)[0]
            self.assertEqual(block.count(current_pr_arm), 1)
            mutant = text.replace(current_pr_arm, previous_pr_arm, 1)
            with self.subTest(workflow=name):
                self.assertNotEqual(mutant, text)
                self.assertNotIn('repos/$REPO/collaborators/$PR_AUTHOR/permission', mutant)
                with self.assertRaises(AssertionError):
                    self.assert_authz_text(mutant)
                stale_job = yaml.load(mutant, Loader=yaml.BaseLoader)["jobs"]["authz"]
                with self.assertRaises(AssertionError):
                    self.assert_authz(stale_job)
                values = {"ASSOCIATION": "CONTRIBUTOR", "PR_AUTHOR": "private-member"}
                script = _workflow(name)["jobs"]["authz"]["steps"][0]["run"]
                self.assert_authz_decision(script, values, True, expect_lookup=True)
                with self.assertRaises(AssertionError):
                    self.assert_authz_decision(
                        stale_job["steps"][0]["run"], values, True, expect_lookup=True)

    def test_authz_pin_refuses_skips_checkout_credentials_extra_steps_and_case_changes(self):
        for name in self.ROUTED_JOBS:
            job = _workflow(name)["jobs"]["authz"]
            self.assert_authz(job)
            mutants = []
            for key, value in (("if", "always()"), ("continue-on-error", "true"),
                               ("timeout-minutes", "30"), ("name", "Optional authorization"),
                               ("permissions", {"contents": "read"}),
                               ("permissions", {"contents": "write"}),
                               ("environment", "release-automation"), ("runs-on", "ubuntu-24.04")):
                mutant = copy.deepcopy(job)
                mutant[key] = value
                mutants.append(mutant)
            for key in ("name", "permissions"):
                mutant = copy.deepcopy(job)
                mutant.pop(key)
                mutants.append(mutant)
            for key, value in (("if", "true"), ("continue-on-error", "true"),
                               ("uses", "actions/checkout@must-not-run")):
                mutant = copy.deepcopy(job)
                mutant["steps"][0][key] = value
                mutants.append(mutant)
            mutant = copy.deepcopy(job)
            mutant["steps"].append({"uses": "actions/checkout@must-not-run"})
            mutants.append(mutant)
            for key, value in (("TOKEN", "${{ secrets.GITHUB_TOKEN }}"), ("TOKEN", "${{ github.token }}")):
                mutant = copy.deepcopy(job)
                mutant["steps"][0]["env"][key] = value
                mutants.append(mutant)
            for key in ("EVENT", "REPO", "HEAD_REPO", "HEAD_IS_FORK", "ASSOCIATION",
                        "PR_AUTHOR", "WR_HEAD_REPO", "WR_ACTOR", "GH_TOKEN", "ALLOWED_BOTS"):
                mutant = copy.deepcopy(job)
                mutant["steps"][0]["env"].pop(key)
                mutants.append(mutant)
            for key, value in (
                ("HEAD_REPO", "${{ github.repository }}"),
                ("REPO", "${{ github.event.pull_request.head.repo.full_name }}"),
                ("ALLOWED_BOTS", "dependabot[bot] pineforge-app[bot] another[bot]"),
                ("ACTOR", "${{ github.actor }}"),
                ("OURS", "${{ github.actor == 'dependabot[bot]' }}"),
            ):
                mutant = copy.deepcopy(job)
                mutant["steps"][0]["env"][key] = value
                mutants.append(mutant)
            script = job["steps"][0]["run"]
            fork_arm = re.search(r"(?ms)^  pull_request\)\n.*?(?=^  \*\) fail)", script)[0]
            workflow_run_arm = re.search(
                r"(?ms)^  workflow_run\)\n.*?(?=^  pull_request\)\n)", script)[0]
            default_arm = next(line for line in script.splitlines(keepends=True)
                               if line.lstrip().startswith('*) fail "event'))
            fail_line = next(line for line in script.splitlines(keepends=True)
                             if line.startswith("fail()"))
            fork_check = next(line for line in script.splitlines(keepends=True)
                              if line.lstrip().startswith('[ "$HEAD_IS_FORK"'))
            pr_permission_lookup = next(line for line in fork_arm.splitlines(keepends=True)
                                        if line.lstrip().startswith("perm=$(gh api"))
            pr_permission_case = next(line for line in fork_arm.splitlines(keepends=True)
                                      if line.lstrip().startswith('case "$perm"'))
            allowed_summary = next(line for line in script.splitlines(keepends=True)
                                   if line.startswith('echo "authz: allowed'))
            for run in (
                script.replace(fork_arm, "", 1),
                script.replace(workflow_run_arm, "", 1),
                script.replace(default_arm, "", 1),
                script.replace(fail_line, "fail() { :; }\n", 1),
                script.replace(fork_check, "", 1),
                script.replace(pr_permission_lookup, "", 1),
                script.replace(pr_permission_lookup,
                               pr_permission_lookup.replace("|| echo none)", "|| echo write)", 1), 1),
                script.replace(pr_permission_case,
                               pr_permission_case.replace("admin|maintain|write", "admin|maintain|write|read", 1), 1),
                script.replace(pr_permission_case,
                               pr_permission_case.replace("admin|maintain|write", "admin|write", 1), 1),
                script.replace("push|workflow_dispatch|", "push|pull_request_target|workflow_dispatch|", 1),
                script.replace("exit 1", "exit 0"),
                script.replace('"$HEAD_REPO" = "$REPO"', '"$HEAD_REPO" != "$REPO"'),
                'if [ "$GITHUB_ACTOR" = "dependabot[bot]" ]; then exit 0; fi\n' + script,
                script.replace(allowed_summary, "", 1),
            ):
                mutant = copy.deepcopy(job)
                mutant["steps"][0]["run"] = run
                mutants.append(mutant)
            for index, mutant in enumerate(mutants):
                with self.subTest(workflow=name, mutation=index):
                    self.assertNotEqual(mutant, job)
                    with self.assertRaises(AssertionError):
                        self.assert_authz(mutant)
            workflow = _workflow(name)
            self.assert_job_inventory(name, workflow)
            mutant = copy.deepcopy(workflow)
            mutant["jobs"].pop("authz")
            with self.assertRaises(AssertionError):
                self.assert_job_inventory(name, mutant)
            mutant = copy.deepcopy(workflow)
            authz = mutant["jobs"].pop("authz")
            mutant["jobs"]["authz"] = authz
            self.assertNotEqual(list(mutant["jobs"]), list(workflow["jobs"]))
            with self.assertRaises(AssertionError):
                self.assert_job_inventory(name, mutant)

    def authz_shell(self, script, values, *, permission="write", lookup_fails=False):
        # Stub only gh inside this child shell for pull_request and workflow_run.
        # Record the actual lookup and capture the workflow summary without network.
        stub = r"""gh() {
  printf '%s\n' "$*" >> "$AUTHZ_LOOKUP_LOG"
  [ "$LOOKUP_FAILS" = false ] || return 9
  printf '%s\n' "$LOOKUP_PERMISSION"
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary"
            lookups = Path(tmp) / "lookups"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "EVENT": "pull_request", "REPO": "pineforge-4pass/pineforge-release",
                "HEAD_REPO": "pineforge-4pass/pineforge-release", "HEAD_IS_FORK": "false",
                "ASSOCIATION": "MEMBER", "PR_AUTHOR": "member",
                "WR_HEAD_REPO": "", "WR_ACTOR": "",
                "GH_TOKEN": "fixture-token",
                "ALLOWED_BOTS": "dependabot[bot] pineforge-app[bot]",
                "GITHUB_STEP_SUMMARY": str(summary), "AUTHZ_LOOKUP_LOG": str(lookups),
                "LOOKUP_PERMISSION": permission, "LOOKUP_FAILS": str(lookup_fails).lower(),
            }
            env.update(values)
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-c", stub + script],
                env=env, capture_output=True, text=True,
            )
            return (done, summary.read_text() if summary.exists() else "",
                    lookups.read_text() if lookups.exists() else "")

    def assert_authz_decision(self, script, values, accepted, *, permission="write",
                              lookup_fails=False, expect_lookup=False):
        done, summary, lookups = self.authz_shell(
            script, values, permission=permission, lookup_fails=lookup_fails)
        self.assertEqual(done.returncode, 0 if accepted else 1, (done.stdout, done.stderr, summary))
        self.assertEqual(done.stderr, "")
        event = values.get("EVENT", "pull_request")
        author = values.get("PR_AUTHOR", "member")
        if accepted:
            suffix = f" by {author}" if author else ""
            self.assertEqual(summary, f"authz: allowed ({event}{suffix})\n")
            self.assertEqual(done.stdout, "")
        else:
            self.assertTrue(done.stdout.startswith("::error title=authz::"), done.stdout)
            self.assertTrue(summary.startswith("authz: REFUSED: "), summary)
            self.assertNotIn("authz: allowed", summary)
            if expect_lookup and event == "pull_request":
                association = values.get("ASSOCIATION", "MEMBER") or "NONE"
                reported_permission = "none" if lookup_fails else permission
                reason = (f"author {author or 'unknown'} ({association}) has permission "
                          f"'{reported_permission}', not write, and is not an allowlisted bot")
                self.assertEqual(done.stdout, f"::error title=authz::{reason}\n")
                self.assertEqual(summary, f"authz: REFUSED: {reason}\n")
        expected_lookup = ""
        if expect_lookup:
            actor = values["WR_ACTOR"] if event == "workflow_run" else author
            expected_lookup = (
                "api repos/pineforge-4pass/pineforge-release/collaborators/"
                + actor + "/permission --jq .permission\n"
            )
        self.assertEqual(lookups, expected_lookup)

    def test_actual_authz_shell_pins_author_identity_events_and_workflow_run_permissions(self):
        repository = "pineforge-4pass/pineforge-release"
        cases = [(association, {"ASSOCIATION": association}, True, {})
                 for association in ("OWNER", "MEMBER", "COLLABORATOR")]
        cases += [(bot, {"PR_AUTHOR": bot, "ASSOCIATION": "NONE"}, True, {})
                  for bot in ("dependabot[bot]", "pineforge-app[bot]")]
        cases += [(association, {"PR_AUTHOR": "outsider", "ASSOCIATION": association}, False,
                   {"permission": "read", "expect_lookup": True})
                  for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "")]
        cases.append(("other bot", {"PR_AUTHOR": "another[bot]", "ASSOCIATION": "NONE"}, False,
                      {"permission": "read", "expect_lookup": True}))
        contributor = {"PR_AUTHOR": "private-member", "ASSOCIATION": "CONTRIBUTOR"}
        for permission in ("admin", "maintain", "write", "read", "triage", "none", "", "unknown"):
            cases.append(("CONTRIBUTOR permission " + permission, contributor,
                          permission in {"admin", "maintain", "write"},
                          {"permission": permission, "expect_lookup": True}))
        cases.append(("CONTRIBUTOR lookup error", contributor, False,
                      {"permission": "write", "lookup_fails": True, "expect_lookup": True}))
        cases += [("other association with write", {
            "PR_AUTHOR": "writer", "ASSOCIATION": association,
        }, True, {"permission": "write", "expect_lookup": True})
                  for association in ("NONE", "FIRST_TIME_CONTRIBUTOR", "")]
        cases.append(("other bot with write", {"PR_AUTHOR": "another[bot]", "ASSOCIATION": "NONE"}, True,
                      {"permission": "write", "expect_lookup": True}))
        for head, fork in (("outside/fork", "false"), (repository, "true")):
            cases.append(("CONTRIBUTOR head refused before lookup",
                          {**contributor, "HEAD_REPO": head, "HEAD_IS_FORK": fork}, False, {}))
        for author in ("member", "dependabot[bot]", "pineforge-app[bot]"):
            for head, fork in (("outside/fork", "true"), ("member/fork", "true"),
                               (repository, "true"), ("", "false")):
                cases.append(("fork or missing head", {
                    "HEAD_REPO": head, "HEAD_IS_FORK": fork, "PR_AUTHOR": author,
                }, False, {}))
        cases.append(("missing fork flag", {"HEAD_IS_FORK": ""}, False, {}))
        cases += [(event, {"EVENT": event, "PR_AUTHOR": ""}, True, {})
                  for event in ("push", "workflow_dispatch", "schedule", "merge_group",
                                "release", "workflow_call", "repository_dispatch")]
        cases += [(event, {"EVENT": event, "PR_AUTHOR": ""}, False, {})
                  for event in ("unknown", "pull_request_target", "")]
        for actor in ("member", "dependabot[bot]"):
            cases.append(("target with author", {
                "EVENT": "pull_request_target", "PR_AUTHOR": actor,
            }, False, {}))
        workflow_run = {"EVENT": "workflow_run", "PR_AUTHOR": "",
                        "WR_HEAD_REPO": repository, "WR_ACTOR": "member"}
        for permission in ("admin", "maintain", "write", "read", "triage", "none", "", "unknown"):
            cases.append(("workflow_run permission " + permission, workflow_run,
                          permission in {"admin", "maintain", "write"},
                          {"permission": permission, "expect_lookup": True}))
        cases.append(("failed permission lookup", workflow_run, False,
                      {"permission": "write", "lookup_fails": True, "expect_lookup": True}))
        cases.append(("non-member actor", {**workflow_run, "WR_ACTOR": "outsider"}, False,
                      {"permission": "read", "expect_lookup": True}))
        for actor in ("dependabot[bot]", "pineforge-app[bot]"):
            cases.append(("workflow_run allowlisted bot", {**workflow_run, "WR_ACTOR": actor}, True,
                          {"permission": "none", "lookup_fails": True}))
        for head in ("outside/fork", ""):
            for actor in ("member", "dependabot[bot]", "pineforge-app[bot]"):
                cases.append(("workflow_run foreign or missing head",
                              {**workflow_run, "WR_HEAD_REPO": head, "WR_ACTOR": actor}, False, {}))
        for name in self.ROUTED_JOBS:
            job = _workflow(name)["jobs"]["authz"]
            self.assert_authz(job)
            step = job["steps"][0]
            for label, values, accepted, options in cases:
                values = {**values, "ALLOWED_BOTS": step["env"]["ALLOWED_BOTS"]}
                with self.subTest(workflow=name, case=label, values=values):
                    self.assert_authz_decision(step["run"], values, accepted, **options)

    def test_actual_authz_decisions_refuse_fail_function_made_a_noop(self):
        cases = (
            ({"HEAD_REPO": "outside/fork"}, {}),
            ({"HEAD_IS_FORK": "true"}, {}),
            ({"ASSOCIATION": "NONE", "PR_AUTHOR": "outsider"},
             {"permission": "read", "expect_lookup": True}),
            ({"ASSOCIATION": "CONTRIBUTOR", "PR_AUTHOR": "private-member"},
             {"permission": "write", "lookup_fails": True, "expect_lookup": True}),
            ({"EVENT": "workflow_run", "PR_AUTHOR": "",
              "WR_HEAD_REPO": "outside/fork", "WR_ACTOR": "member"}, {}),
            ({"EVENT": "pull_request_target", "PR_AUTHOR": ""}, {}),
        )
        for name in self.ROUTED_JOBS:
            step = _workflow(name)["jobs"]["authz"]["steps"][0]
            script = step["run"]
            fail_line = next(line for line in script.splitlines(keepends=True)
                             if line.startswith("fail()"))
            mutant = script.replace(fail_line, "fail() { :; }\n", 1)
            self.assertNotEqual(mutant, script)
            for values, options in cases:
                values = {**values, "ALLOWED_BOTS": step["env"]["ALLOWED_BOTS"]}
                with self.subTest(workflow=name, values=values):
                    self.assert_authz_decision(script, values, False, **options)
                    with self.assertRaises(AssertionError):
                        self.assert_authz_decision(mutant, values, False, **options)

    def test_actual_pr_permission_cases_detect_lookup_and_permission_mutations(self):
        values = {"ASSOCIATION": "CONTRIBUTOR", "PR_AUTHOR": "private-member"}
        for name in self.ROUTED_JOBS:
            script = _workflow(name)["jobs"]["authz"]["steps"][0]["run"]
            pr_arm = re.search(r"(?ms)^  pull_request\)\n.*?(?=^  \*\) fail)", script)[0]
            lookup = next(line for line in pr_arm.splitlines(keepends=True)
                          if line.lstrip().startswith("perm=$(gh api"))
            permission_case = next(line for line in pr_arm.splitlines(keepends=True)
                                   if line.lstrip().startswith('case "$perm"'))
            mutations = (
                ("delete PR lookup", lookup, "", True, {"permission": "write"}),
                ("allow read", permission_case,
                 permission_case.replace("admin|maintain|write", "admin|maintain|write|read", 1),
                 False, {"permission": "read"}),
                ("refuse maintain", permission_case,
                 permission_case.replace("admin|maintain|write", "admin|write", 1),
                 True, {"permission": "maintain"}),
                ("lookup error passes", lookup,
                 lookup.replace("|| echo none)", "|| echo write)", 1),
                 False, {"permission": "write", "lookup_fails": True}),
            )
            for label, original, replacement, accepted, options in mutations:
                self.assertEqual(script.count(original), 1)
                mutant = script.replace(original, replacement, 1)
                with self.subTest(workflow=name, mutation=label):
                    self.assertNotEqual(mutant, script)
                    self.assert_authz_decision(script, values, accepted, expect_lookup=True, **options)
                    with self.assertRaises(AssertionError):
                        self.assert_authz_decision(mutant, values, accepted, expect_lookup=True, **options)

    def assert_exception_runner(self, workflow, identity, job):
        expected = self.EXEMPT_RUNNERS[workflow][identity]
        actual = job.get("runs-on")
        self.assertNotEqual(actual, self.RUNNER, "pf-ci-release must not join the shared pool")
        self.assertEqual(actual, expected)
        if (workflow, identity) in self.LEGACY_LABEL_JOBS:
            self.assertEqual((job.get("env") or {}).get("PF_CI_LABELS"),
                             '["self-hosted","pf-ci","pf-ci-x64"]')

    def test_credential_and_release_routes_stay_unchanged_and_refuse_the_pool_array(self):
        # This repository has no macOS jobs; its complete workflow/job inventory
        # below also refuses adding an unreviewed macOS/pool matrix.
        self.assertEqual({path.name for path in WORKFLOWS.glob("*.yml")},
                         set(self.ROUTED_JOBS) | set(self.EXEMPT_RUNNERS))
        for name, expected in self.EXEMPT_RUNNERS.items():
            jobs = _workflow(name)["jobs"]
            self.assertEqual(set(jobs), set(expected))
            for identity, job in jobs.items():
                with self.subTest(workflow=name, job=identity):
                    self.assert_exception_runner(name, identity, job)
                    mutant = copy.deepcopy(job)
                    mutant["runs-on"] = self.RUNNER.copy()
                    with self.assertRaises(AssertionError):
                        self.assert_exception_runner(name, identity, mutant)
                    if (name, identity) in self.LEGACY_LABEL_JOBS:
                        mutant = copy.deepcopy(job)
                        mutant["env"].pop("PF_CI_LABELS")
                        with self.assertRaises(AssertionError):
                            self.assert_exception_runner(name, identity, mutant)

    def test_docs_only_gates_keep_existing_checks_and_main_coverage(self):
        workflow = _workflow("python-test.yml")
        self.assertEqual(set(workflow["on"]), {"pull_request", "push", "merge_group"})
        self.assertEqual(workflow["on"]["push"]["branches"], ["main", "master"])
        changes = workflow["jobs"]["changes"]
        self.assertEqual(changes["outputs"], {"docs-only": "${{ steps.paths.outputs.docs-only }}"})
        self.assertEqual(changes["steps"][0]["with"]["fetch-depth"], "0")
        self.assertEqual(changes["steps"][0]["if"], "github.event_name == 'pull_request'")
        for identity in self.ROUTED_JOBS["python-test.yml"] - {"release-ready"}:
            job = workflow["jobs"][identity]
            self.assertEqual(job["needs"], ["authz", "changes"])
            self.assert_job_condition(identity, job)
        self.assertEqual(changes["needs"], "authz")
        self.assert_job_condition("changes", changes)
        aggregate = workflow["jobs"]["release-ready"]
        self.assertEqual(aggregate["name"], "release/ready")
        self.assert_job_condition("release-ready", aggregate)
        self.assertEqual(set(aggregate["needs"]),
                         self.ROUTED_JOBS["python-test.yml"] - {"release-ready"} | {"authz", "changes"})
        self.assert_authz_refusal_first(aggregate)
        self.assertEqual(aggregate["steps"][1]["env"]["AUTHZ_OK"], "${{ needs.authz.result == 'success' }}")
        facts = _workflow("facts-validate.yml")
        self.assertEqual(set(facts["on"]), {"pull_request", "push"})
        self.assertEqual(facts["on"]["push"]["branches"], ["main"])
        self.assertEqual(facts["on"]["push"]["paths"], [
            "facts/**", "scripts/repo_descriptions.py", "scripts/facts_*.py",
            "tests/test_repo_descriptions.py", "tests/test_facts_*.py",
            "tests/test_release_workflows.py", ".github/workflows/**",
        ])
        validate = facts["jobs"]["validate"]
        self.assert_job_condition("validate", validate)
        self.assertEqual(validate["needs"], "authz")
        self.assertEqual(validate["steps"][0]["with"]["fetch-depth"],
                         "${{ github.event_name == 'pull_request' && '0' || '1' }}")
        for step in validate["steps"][2:]:
            self.assertEqual(step["if"], "steps.paths.outputs.docs-only == 'false'")

    def detector(self, workflow):
        jobs = _workflow(workflow)["jobs"]
        job = jobs["changes" if workflow == "python-test.yml" else "validate"]
        return next(step for step in job["steps"] if step.get("id") == "paths")["run"]

    def detect(self, workflow, paths, event="pull_request", *, fail=False, script=None):
        if script is None:
            script = self.detector(workflow)
        self.assertTrue(script.startswith("python3 - <<'PY'\n"))
        self.assertTrue(script.endswith("PY\n"))
        program = script.removeprefix("python3 - <<'PY'\n").removesuffix("PY\n")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outputs"
            env = {"EVENT_NAME": event, "BASE_SHA": "base", "HEAD_SHA": "head",
                   "GITHUB_OUTPUT": str(output)}
            changed = b"".join(os.fsencode(path) + b"\0" for path in paths)
            with patch.dict(os.environ, env, clear=True), patch("subprocess.check_output", return_value=changed) as diff:
                if fail:
                    diff.side_effect = subprocess.CalledProcessError(1, "git diff")
                    with self.assertRaises(subprocess.CalledProcessError):
                        exec(compile(program, workflow, "exec"), {})
                    self.assertFalse(output.exists())
                else:
                    exec(compile(program, workflow, "exec"), {})
                if event == "pull_request":
                    diff.assert_called_once_with([
                        "git", "diff", "--name-only", "--no-renames", "-z", "base...head", "--",
                    ])
                else:
                    diff.assert_not_called()
            return output.read_text() if output.exists() else None

    def test_docs_detection_executes_actual_scripts_on_docs_mixed_and_unknown_paths(self):
        self.assertEqual(self.detector("python-test.yml"), self.detector("facts-validate.yml"))
        cases = (
            (["README.md", "docs/release-chain.md", "LICENSE"], False),
            (["README.md"], False),
            (["docs/release-chain.md"], False),
            (["LICENSE"], False),
            (["tools/release-dry-run/README.md"], False),
            (["scripts/guide.rst"], False),
            ([".github/workflows/README.md"], False),
            (["docs/guide.md", "docs/index.rst"], True),
            (["docs/screenshot with spaces.png", "docs/note\ncontinued.txt"], True),
            (["facts/README.md"], True),
            ([], False),
            (["README.md", "scripts/release_pair.py"], False),
            (["facts/README.md", "docs/release-chain.md"], False),
            (["facts/README.md", "tools/release-dry-run/README.md"], False),
            (["docs/release-chain.md", "docs/renamed.md"], False),
            (["requirements.txt"], False),
            (["docs/check.py"], False),
            (["facts/facts.json"], False),
            ([".github/workflows/python-test.yml"], False),
            (["VERSION"], False),
            (["scripts/renamed.py", "docs/renamed.md"], False),
        )
        for workflow in self.ROUTED_JOBS:
            for paths, expected in cases:
                with self.subTest(workflow=workflow, paths=paths):
                    self.assertEqual(self.detect(workflow, paths), f"docs-only={str(expected).lower()}\n")
            self.detect(workflow, ["README.md"], fail=True)
            for event in ("push", "merge_group", "schedule", "workflow_dispatch", "release"):
                with self.subTest(workflow=workflow, event=event):
                    self.assertEqual(self.detect(workflow, ["README.md"], event), "docs-only=false\n")

    def test_test_input_exclusions_are_exact_and_each_is_needed(self):
        rules = (
            "test_inputs = {'README.md', 'docs/release-chain.md', 'LICENSE'}",
            "str(path) not in test_inputs and",
            "path.parts[:2] != ('.github', 'workflows') and",
            "path.parts[0] not in {'scripts', 'tools'} and",
        )
        mutations = (
            ("'README.md', ", "", "README.md"),
            ("'docs/release-chain.md', ", "", "docs/release-chain.md"),
            (", 'LICENSE'}", "}", "LICENSE"),
            ("path.parts[:2] != ('.github', 'workflows') and", "",
             ".github/workflows/README.md"),
            ("{'scripts', 'tools'}", "{'tools'}", "scripts/guide.rst"),
            ("{'scripts', 'tools'}", "{'scripts'}", "tools/release-dry-run/README.md"),
        )
        for workflow in self.ROUTED_JOBS:
            script = self.detector(workflow)
            for rule in rules:
                self.assertIn(rule, script)
            for before, after, path in mutations:
                with self.subTest(workflow=workflow, path=path):
                    self.assertIn(before, script)
                    self.assertEqual(self.detect(workflow, [path]), "docs-only=false\n")
                    mutant = script.replace(before, after, 1)
                    self.assertNotEqual(mutant, script)
                    with self.assertRaises(AssertionError):
                        self.assertEqual(self.detect(workflow, [path], script=mutant),
                                         "docs-only=false\n")

    @staticmethod
    def named_repo_files(source):
        """Conservatively collect file literals, joined Paths, aliases and globs.

        Looking at names as well as read calls covers copyfile and files passed
        to subprocesses. The classifier's own synthetic path cases are excluded.
        """
        tree = ast.parse(source)
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.ClassDef) and node.name == "PrTierWorkflowTest")]
        bases = {"ROOT": REPO, "REPO": REPO, "WORKFLOWS": WORKFLOWS}

        def path_value(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value and "\n" not in node.value and "\0" not in node.value:
                    return Path(node.value)
            elif isinstance(node, ast.Name):
                return bases.get(node.id)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left, right = path_value(node.left), path_value(node.right)
                if left is not None and right is not None:
                    return left / right
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in {"Path", "PurePosixPath"}):
                parts = [path_value(arg) for arg in node.args]
                if parts and all(part is not None for part in parts):
                    return Path(*parts)
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = path_value(node.value)
                if value is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bases[target.id] = value

        candidates = set()
        for node in ast.walk(tree):
            value = path_value(node)
            if value is not None:
                candidates.add(value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"glob", "rglob"} and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                base = path_value(node.func.value)
                if base is not None:
                    base = (REPO / base).resolve()
                    if base.is_relative_to(REPO) and base.is_dir():
                        candidates.update(getattr(base, node.func.attr)(node.args[0].value))
        files = set()
        for candidate in candidates:
            try:
                path = (REPO / candidate).resolve()
                if path.is_relative_to(REPO) and path.is_file():
                    files.add(path.relative_to(REPO).as_posix())
            except (OSError, ValueError):
                # A non-path test literal can contain invalid/overlong components.
                continue
        return files

    def assert_test_inputs_run(self, workflow, inputs):
        for path in sorted(inputs):
            self.assertEqual(self.detect(workflow, [path]), "docs-only=false\n",
                             f"{workflow}: {path} is named or scanned by a test; keep its suites on PRs")

    def test_no_test_input_is_skipped_as_documentation(self):
        sources = sorted((REPO / "tests").rglob("test_*.py"))
        sources += sorted((REPO / "tools" / "release-dry-run").glob("test_*.py"))
        inputs = set()
        for source in sources:
            inputs.update(self.named_repo_files(source.read_text(encoding="utf-8")))
        # These are found in other tests, not injected into the scan's result.
        self.assertTrue({"README.md", "docs/release-chain.md", "LICENSE",
                         "tools/release-dry-run/README.md"} <= inputs)
        for workflow in self.ROUTED_JOBS:
            self.assert_test_inputs_run(workflow, inputs)

    def test_test_input_guard_rejects_new_document_reads(self):
        sources = (
            'text = open("facts/README.md").read()',
            'text = Path("facts/README.md").read_text()',
            'text = (ROOT / "facts" / "README.md").read_bytes()',
            'doc = REPO / "facts"\ntext = (doc / "README.md").read_text()',
            'shutil.copyfile(REPO / "facts/README.md", destination)',
            'for path in (ROOT / "facts").glob("*.md"):\n    text = path.read_text()',
            'for path in (ROOT / "facts").rglob("*.md"):\n    text = path.read_text()',
        )
        for source in sources:
            inputs = self.named_repo_files(source)
            self.assertIn("facts/README.md", inputs)
            for workflow in self.ROUTED_JOBS:
                with self.subTest(workflow=workflow, source=source):
                    with self.assertRaises(AssertionError):
                        self.assert_test_inputs_run(workflow, inputs)
        self.assertEqual(self.named_repo_files('# open("facts/README.md").read()\n'), set())


if __name__ == "__main__":
    unittest.main()
