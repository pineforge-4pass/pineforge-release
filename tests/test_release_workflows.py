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
        # moving = stable AND the newest stable tag: latest / X.Y never go back.
        stable = "enable=${{ steps.pair.outputs.moving == 'true' }}"
        body = _step(self.text, "Image metadata")
        self.assertIn("latest=false", body)
        self.assertIn(f"type=raw,value=latest,{stable}", body)
        self.assertIn("type=semver,pattern={{major}}.{{minor}},value=${{ github.ref_name }},"
                      + stable, body)
        self.assertEqual(body.count("value=latest"), 1)

    def test_moving_tags_need_the_newest_stable_tag(self):
        body = _step(self.text, "Pairing rule")
        self.assertIn("git tag -l 'v*' | python3 scripts/release_pair.py latest-tag --stable", body)
        self.assertIn('--newest-stable="$newest"', body)

    def test_github_release_list_failure_is_not_a_latest(self):
        body = _step(self.text, "GitHub Release")
        self.assertIn('releases="$(gh release list --exclude-drafts --limit 100 --json tagName -q \'.[].tagName\')"',
                      body)

    def test_draft_releases_never_hold_latest(self):
        # GitHub never marks a draft Latest; counting one (say a hand-made
        # v9.9.9) would keep a new stable release off Latest.
        body = _step(self.text, "GitHub Release")
        calls = [ln for ln in body.splitlines()
                 if "gh release list" in ln and not ln.lstrip().startswith("#")]
        self.assertTrue(calls)
        for ln in calls:
            self.assertIn("--exclude-drafts", ln)

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
        self.assertIn("PRERELEASE: ${{ steps.pair.outputs.prerelease }}", body)
        self.assertIn("--prerelease --latest=false", body)
        self.assertIn("python3 scripts/release_pair.py latest-tag --stable", body)

    def test_prerelease_flag_is_in_every_dispatch_payload(self):
        self.assertIn("prerelease: ${{ steps.pair.outputs.prerelease }}", self.text)
        body = _step(self.text, "Dispatch pineforge-release")
        self.assertIn("PRERELEASE: ${{ needs.publish.outputs.prerelease }}", body)
        self.assertIn('-F "client_payload[prerelease]=${PRERELEASE}"', body)
        self.assertIn("consumer: [offline, hosted, application]",
                      self.text)


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
                         "${{ steps.wheel.outputs.hpo_wheel_arg }}",
                         "RELEASE_VERSION=${{ github.ref_name }}",
                         "RELEASE_GIT_SHA=${{ github.sha }}"):
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
                                 "RELEASE_VERSION=${{ github.ref_name }}"):
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
        self.assertEqual(self.workflow["on"]["push"]["tags"], ["v*"])
        pushes = [step for step in self.steps if step.get("with", {}).get("push") == "true"]
        self.assertEqual(len(pushes), 2, "test must find both actual publishing actions")
        releases = [step for step in self.steps if "gh release create" in step.get("run", "")]
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
        releases = [step for step in self.steps if "gh release create" in step.get("run", "")]
        self.assertEqual(len(releases), 1)
        for release in releases:
            self.assertLess(self.steps.index(post), self.steps.index(release))
            self.assertNotIn("if", release)
            self.assertNotIn("continue-on-error", release)
        notify = self.workflow["jobs"]["notify-consumers"]
        self.assertEqual(notify["needs"], "publish", "dispatch must wait for the post-push check")
        self.assertNotIn("if", notify)
        dispatches = [step for step in notify["steps"] if "/dispatches" in step.get("run", "")]
        self.assertEqual(len(dispatches), 1, "test must find the existing consumer dispatch")

    def test_post_push_check_pulls_both_architectures_by_the_successful_push_digest(self):
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
        self.assertEqual(pulls, [["docker", "pull", "--platform", f"linux/{arch}", "$image"]
                                 for arch in ("amd64", "arm64")])
        query = r"-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n"
        self.assertEqual(inventories, [
            ["docker", "run", "--rm", "--platform", f"linux/{arch}", "--entrypoint", "dpkg-query",
             "$image", "-W", query, ">", f"$RUNNER_TEMP/pushed-debian-{arch}.tsv"]
            for arch in ("amd64", "arm64")])
        self.assertNotIn("pineforge-source-inspect:", run)
        self.assertIn("DETECTS", run)
        self.assertIn("tags are already pushed", run)
        self.assertIn("sources are never deleted", run)

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
                        release["run"].index('if [ "$PRERELEASE" = true ]'))


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
        self.assertIn("types: [engine-release, codegen-release, hpo-release]", self.handler)
        self.assertIn('tags: ["v*"]', self.publisher)
        for text in (self.handler, self.publisher):
            triggers = text[text.index("\non:\n"):text.index("\npermissions:\n")]
            for forbidden in ("workflow_dispatch:", "workflow_run:", "pull_request:", "schedule:"):
                self.assertNotIn(forbidden, triggers)

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
    def test_release_jobs_use_protected_environment_and_contents_only_tokens(self):
        for name in ("publish.yml", "handle-upstream.yml"):
            text = (WORKFLOWS / name).read_text()
            self.assertIn("environment: release-automation", text)
            self.assertNotIn("permission-administration", text)
            self.assertNotIn("create-github-app-token@v1", text)
            self.assertIn("permission-contents: write", text)
            self.assertIn("github-api-url: https://api.github.com", text)
        handler = (WORKFLOWS / "handle-upstream.yml").read_text()
        self.assertIn("repositories: pineforge-release", handler)
        publish = (WORKFLOWS / "publish.yml").read_text()
        self.assertIn("repositories: ${{ env.TARGET_REPOSITORY }}", publish)
        self.assertIn("repository_secret: RELEASE_HOSTED_MCP_REPOSITORY", publish)
        self.assertIn("repository_secret: RELEASE_APPLICATION_REPOSITORY", publish)
        self.assertIn("secrets[matrix.repository_secret]", publish)
        self.assertLess(publish.index("Require one configured consumer"), publish.index("Mint App token"))
        self.assertNotIn("echo \"dispatched pineforge-release -> ${REPO}", publish)
        notify = publish[publish.index("  notify-consumers:"):]
        self.assertIn("permissions:\n      contents: read", notify)
        self.assertNotIn("packages: write", notify)


class NotifyConsumersTest(unittest.TestCase):
    """A prerelease is not dispatched to the application; the other five
    consumer/channel combinations still are. The decision step's own shell runs
    here, so the test follows the workflow rather than a copy of its rule."""

    CONSUMERS = ("offline", "hosted", "application")
    HANDOFF = "Application is not notified for a prerelease; it receives the release by hand-off."
    GATE = "\n        if: steps.notify.outputs.dispatch == 'true'\n"
    GATED = ("Require one configured consumer repository",
             "Mint App token",
             "Dispatch pineforge-release")

    @classmethod
    def setUpClass(cls):
        text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")
        cls.notify = text[text.index("\n  notify-consumers:\n"):]
        at = cls.notify.index("\n    steps:\n") + 1
        cls.job_head, cls.job_steps = cls.notify[:at], cls.notify[at:]
        cls.decide = _step(cls.notify, "Decide consumer notification")
        cls.script = _run_script(cls.decide)
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise AssertionError("bash is needed to run the notify-consumers decision step")

    def _decide(self, consumer, flag):
        """Run the decision script the way the runner does: its two inputs and a
        GITHUB_OUTPUT file, nothing else. PATH is an empty directory, so only
        shell builtins can run (no gh, curl or token). A flag of None leaves
        PRERELEASE unset. Returns (completed process, GITHUB_OUTPUT text)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "no-programs").mkdir()
            script = root / "decide.sh"
            script.write_text(self.script, encoding="utf-8")
            out = root / "github_output"
            out.write_text("", encoding="utf-8")
            env = {"PATH": str(root / "no-programs"), "GITHUB_OUTPUT": str(out), "CONSUMER": consumer}
            if flag is not None:
                env["PRERELEASE"] = flag
            done = subprocess.run([self.bash, "--noprofile", "--norc", str(script)], cwd=root, env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            return done, out.read_text(encoding="utf-8")

    def test_all_three_consumers_stay_visible_legs(self):
        # The application's prerelease leg is decided by steps, not by a
        # job-level condition (the matrix context is not available there) or a
        # matrix edit, so all three legs still appear and end green.
        self.assertIn("\n        consumer: [offline, hosted, application]\n", self.job_head)
        self.assertIn("\n      fail-fast: false\n", self.job_head)
        self.assertNotIn("exclude:", self.job_head)
        self.assertNotIn("\n    if:", self.job_head)
        self.assertIn("\n      TARGET_REPOSITORY: ${{ matrix.consumer == 'offline' && 'pineforge-backtest-mcp'"
                      " || secrets[matrix.repository_secret] }}\n", self.job_head)

    def test_the_decision_is_the_first_step_and_always_runs(self):
        first = self.job_steps.split("\n      - ", 1)[1]
        self.assertTrue(first.startswith("name: Decide consumer notification\n        id: notify\n"), first[:80])
        self.assertNotIn("\n        if:", self.decide)

    def test_the_decision_reads_the_consumer_and_the_publish_flag(self):
        self.assertIn("\n        env:\n          CONSUMER: ${{ matrix.consumer }}\n"
                      "          PRERELEASE: ${{ needs.publish.outputs.prerelease }}\n"
                      "        run: |\n", self.decide)

    def test_the_decision_uses_no_secret_token_or_network(self):
        self.assertNotIn("uses:", self.decide)
        for word in ("secrets.", "GH_TOKEN", "TARGET_REPOSITORY", "steps.app"):
            self.assertNotIn(word, self.decide)
        self.assertIsNone(re.search(r"\b(gh|curl|wget)\b", self.script))

    def test_every_dispatch_step_is_gated_on_the_decision(self):
        # Dropping the condition from any one of them lets a prerelease reach
        # that step for the application: the repository check, the token mint
        # or the dispatch itself.
        for name in self.GATED:
            with self.subTest(step=name):
                self.assertEqual(_step(self.notify, name).count(self.GATE), 1)
        self.assertEqual(self.notify.count("steps.notify.outputs.dispatch"), len(self.GATED))
        order = [self.job_steps.index("- name: " + name) for name in ("Decide consumer notification",) + self.GATED]
        self.assertEqual(order, sorted(order))

    def test_the_dispatch_still_checks_the_flag_and_sends_the_same_payload(self):
        body = _step(self.notify, "Dispatch pineforge-release")
        self.assertIn('*) echo "::error::publish job gave no prerelease flag (\'${PRERELEASE}\')"; exit 1 ;;', body)
        self.assertIn('-F "client_payload[prerelease]=${PRERELEASE}"', body)

    def test_only_the_application_on_a_prerelease_is_withheld(self):
        for consumer in self.CONSUMERS:
            for flag in ("true", "false"):
                withheld = consumer == "application" and flag == "true"
                with self.subTest(consumer=consumer, prerelease=flag):
                    done, written = self._decide(consumer, flag)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(written, "dispatch=false\n" if withheld else "dispatch=true\n")
                    self.assertEqual(done.stdout.splitlines(), [self.HANDOFF] if withheld else [])

    def test_a_missing_or_malformed_flag_fails_every_consumer_before_any_output(self):
        for consumer in self.CONSUMERS:
            for flag in (None, "", "True", "TRUE", "yes", "null", " true", "false ", "false\n", "true\nfalse"):
                with self.subTest(consumer=consumer, prerelease=flag):
                    done, written = self._decide(consumer, flag)
                    self.assertNotEqual(done.returncode, 0)
                    self.assertEqual(written, "")
                    if flag is not None:
                        self.assertIn("::error::publish job gave no prerelease flag ('" + flag + "')", done.stdout)


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


if __name__ == "__main__":
    unittest.main()
