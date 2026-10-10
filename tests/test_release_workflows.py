#!/usr/bin/env python3
"""Wiring checks: the release workflows run scripts/release_pair.py where its
rules apply and never fall back to an rc-unsafe version sort, the
notify-consumers decision (no dispatch to the application for a prerelease) is
run under bash, and the engine's runtime harness (eight files) is synced from
the pinned engine tag by one script and checked against recorded sha256 values
when the image is built. Stdlib only."""
from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
REPO = WORKFLOWS.parent.parent


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

    def test_the_sums_file_lists_exactly_the_files_present(self):
        path = REPO / "docker" / self.SUMS
        self.assertTrue(path.is_file(), f"docker/{self.SUMS} is not committed")
        text = path.read_bytes().decode("utf-8")
        # the shape scripts/sync-harness.sh writes: "<sha256>  <name>" and a newline per file, no blank line
        self.assertRegex(text, r"\A(?:[0-9a-f]{64}  [^\s/]+\n)+\Z")
        entries = re.findall(r"^([0-9a-f]{64})  (\S+)$", text, re.M)
        for _, name in entries:
            with self.subTest(file=name):
                self.assertFalse(name.endswith("_test.py") or name.upper().startswith("README"), name)
        # the eight files, in the order of the script's list and of the build's check: none missing, extra or twice
        self.assertEqual([name for _, name in entries], list(self.HARNESS))
        for digest, name in entries:
            with self.subTest(file=name):
                self.assertEqual(digest, hashlib.sha256((REPO / "docker" / name).read_bytes()).hexdigest())

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
        # after the last harness COPY, before the user drop
        self.assertLess(self.dockerfile.rindex("\nCOPY docker/"), self.dockerfile.index(check))
        self.assertLess(self.dockerfile.index(check), self.dockerfile.index("\nUSER "))

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


class PythonTestGateTest(unittest.TestCase):
    def test_release_rules_run_in_their_own_job(self):
        text = (WORKFLOWS / "python-test.yml").read_text(encoding="utf-8")
        job = text[text.index("\n  release-rules:\n"):]
        self.assertIn("python3 -m unittest discover -s tests -p 'test_release_*.py' -v", job)
        fingerprint = text[text.index("\n  fingerprint-canonical:\n"):text.index("\n  release-rules:\n")]
        self.assertNotIn("test_release_", fingerprint)


if __name__ == "__main__":
    unittest.main()
