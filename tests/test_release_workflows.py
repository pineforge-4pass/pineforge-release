#!/usr/bin/env python3
"""Wiring checks: the release workflows run scripts/release_pair.py where its
rules apply and never fall back to an rc-unsafe version sort, the
notify-consumers decision (no dispatch to the application for a prerelease) is
run under bash, and no workflow image comes from Docker Hub (the emulation and
BuildKit images and the base images go through a mirror). Stdlib only."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _step(text: str, name: str) -> str:
    """The body of the workflow step whose `- name:` line starts with name."""
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


def _run_script(step: str) -> str:
    """The shell script of a step's `run: |` block, as the runner writes it out."""
    marker = "\n        run: |\n"
    return textwrap.dedent(step[step.index(marker) + len(marker):]).rstrip("\n") + "\n"


# Docker Hub's own registry hosts. A name with no registry host at all (postgres:16,
# library/node) also means Docker Hub: it is Docker's default registry.
_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
# `docker run|create|pull` options that take the next word as their value (--opt=value carries its own).
_VALUE_OPTIONS = {
    "-e", "--env", "--env-file", "-h", "--hostname", "-l", "--label", "-m", "--memory", "-p", "--publish",
    "-u", "--user", "-v", "--volume", "-w", "--workdir", "--add-host", "--cap-add", "--cap-drop", "--cpus",
    "--device", "--entrypoint", "--gpus", "--group-add", "--ip", "--mount", "--name", "--net", "--network",
    "--platform", "--pull", "--restart", "--security-opt", "--shm-size", "--tmpfs", "--ulimit",
    "--volumes-from",
}


def _workflows() -> list[Path]:
    """Every workflow file in the repository."""
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))


def _steps_using(text: str, action: str) -> list[str]:
    """The body of every workflow step that runs `action`, at any version."""
    steps = text.split("\n      - ")[1:]
    return [s for s in steps if re.search(r"(?m)^ *uses: " + re.escape(action) + "@", s)]


def _inputs(step: str) -> dict[str, str]:
    """A step's `with:` inputs: a one-line value as written, a `|` block dedented."""
    pad = " " * 10
    raw: dict[str, list[str]] = {}
    key = None
    for line in step.partition("\n        with:\n")[2].split("\n"):
        if line.strip() and not line.startswith(pad):
            break  # back at the step's own keys
        if line.startswith(pad + "#"):
            continue  # a YAML comment between two inputs
        m = re.match(pad + r"([A-Za-z0-9_-]+):[ ]?(.*)$", line)
        if m:
            key = m.group(1)
            raw[key] = [m.group(2)]
        elif key is not None:
            raw[key].append(line)
    return {k: textwrap.dedent("\n".join(v[1:])).strip("\n") if v[0].startswith("|")
            else v[0].split(" #")[0].strip().strip("\"'")
            for k, v in raw.items()}


def _names_docker_hub(ref: str) -> bool:
    """True when an image reference has no registry host (Docker Hub is the default) or names Docker Hub's.
    As Docker reads a reference, the part before the first `/` is a host only if it holds a `.` or a `:`
    or is `localhost`."""
    first, slash, _ = ref.partition("/")
    has_host = bool(slash) and ("." in first or ":" in first or first == "localhost")
    return not has_host or first in _HUB_HOSTS


def _hub_images(text: str) -> list[str]:
    """The Docker Hub names in workflow text: the value of an `image:` or `container:` key (a service, a job
    container, an action input), a `uses: docker://` step, and the image a `docker pull|run|create` command
    names. Comments are skipped. A name with no readable registry host counts, a shell variable included:
    where it points cannot be told, so write the host out."""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    code = re.sub(r"\\\n[ \t]*", " ", code)  # a command split over lines is one command
    refs = re.findall(r"(?m)^ *(?:- )?(?:image|container): *(\S+)", code)
    refs += re.findall(r"(?m)^ *(?:- )?uses: *docker://(\S+)", code)
    for args in re.findall(r"\bdocker +(?:(?:image|container) +)?(?:pull|run|create)\b([^;&|\n]*)", code):
        words = args.split()
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in _VALUE_OPTIONS else 1
        if i < len(words):
            refs.append(words[i])
    names = [r.strip("\"'") for r in refs]
    return [n for n in names if _names_docker_hub(n)]


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


class DockerHubFreeTest(unittest.TestCase):
    """The publish job pulls nothing from Docker Hub: the emulation and BuildKit images and every base image
    come through a mirror. An action's own default image (tonistiigi/binfmt, moby/buildkit) is a Docker Hub
    name that never appears in the workflow, so these read the inputs that replace it."""

    BINFMT = "mirror.gcr.io/tonistiigi/binfmt:latest"
    BUILDKIT = "image=mirror.gcr.io/moby/buildkit:buildx-stable-1"
    DOCKER_IO = '[registry."docker.io"]'
    MIRRORS = 'mirrors = ["mirror.gcr.io"]'
    # (workflow text, the one Docker Hub name it holds): the check must see each of these.
    HUB_NAMES = (
        ("    services:\n      db:\n        image: postgres:16\n", "postgres:16"),
        ("    container: node:20\n", "node:20"),
        ("    container:\n      image: library/node:20\n", "library/node:20"),
        ("      - uses: docker://alpine:3\n", "alpine:3"),
        ("        with:\n          image: tonistiigi/binfmt:latest\n", "tonistiigi/binfmt:latest"),
        ("        image: docker.io/library/redis:7\n", "docker.io/library/redis:7"),
        ("          docker pull alpine\n", "alpine"),
        ('          docker run --rm -v "$PWD:/w" -e A=b node:20 npm test\n', "node:20"),
        ("          docker run --rm \\\n            -p 5432:5432 \\\n            postgres:16\n", "postgres:16"),
        ("          docker version && docker pull busybox:1\n", "busybox:1"),
    )
    # Text the check must let through: a registry host, a comment, another docker command.
    CLEAN = (
        "      - uses: docker/setup-qemu-action@v3\n"
        "        with:\n          image: mirror.gcr.io/tonistiigi/binfmt:latest\n",
        "    services:\n      db:\n        image: mirror.gcr.io/library/postgres:16\n",
        "    container: ghcr.io/owner/tool:1\n",
        "          docker pull ghcr.io/owner/tool:1\n",
        '          docker run --rm -v "$PWD:/w" localhost:5000/tool:1 --help\n',
        "          # docker pull alpine would come from Docker Hub\n",
        "          docker login ghcr.io\n",
    )

    @classmethod
    def setUpClass(cls):
        cls.workflows = {p.name: p.read_text(encoding="utf-8") for p in _workflows()}

    def _steps(self, action):
        """(workflow, step body) of every step, in any workflow, that runs `action`."""
        return [(name, step) for name, text in self.workflows.items() for step in _steps_using(text, action)]

    def test_publish_sets_up_emulation_and_buildx(self):
        # The checks below look at every such step; publish.yml must have some for them to look at.
        for action in ("docker/setup-qemu-action", "docker/setup-buildx-action"):
            with self.subTest(action=action):
                self.assertTrue(_steps_using(self.workflows["publish.yml"], action))

    def test_every_qemu_step_pulls_the_emulation_image_from_the_mirror(self):
        for name, step in self._steps("docker/setup-qemu-action"):
            with self.subTest(workflow=name):
                self.assertEqual(_inputs(step).get("image"), self.BINFMT)

    def test_every_buildx_step_pulls_buildkit_from_the_mirror(self):
        for name, step in self._steps("docker/setup-buildx-action"):
            with self.subTest(workflow=name):
                options = [ln.strip() for ln in _inputs(step).get("driver-opts", "").splitlines()]
                self.assertIn(self.BUILDKIT, options)

    def test_every_buildx_step_mirrors_docker_io_for_base_images(self):
        # BuildKit asks the mirror first for every Docker Hub name: the FROM lines in
        # docker/Dockerfile and any `# syntax=` frontend.
        for name, step in self._steps("docker/setup-buildx-action"):
            with self.subTest(workflow=name):
                config = _inputs(step).get("buildkitd-config-inline", "")
                lines = [ln.strip() for ln in config.splitlines()]
                self.assertIn(self.DOCKER_IO, lines)
                rest = lines[lines.index(self.DOCKER_IO) + 1:]
                table = rest[:next((i for i, ln in enumerate(rest) if ln.startswith("[")), len(rest))]
                self.assertIn(self.MIRRORS, table)

    def test_no_workflow_names_a_docker_hub_image(self):
        for name, text in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(_hub_images(text), [])

    def test_the_check_finds_a_docker_hub_name_wherever_it_hides(self):
        for text, ref in self.HUB_NAMES:
            with self.subTest(text=text):
                self.assertEqual(_hub_images(text), [ref])

    def test_the_check_lets_a_registry_host_and_a_comment_through(self):
        for text in self.CLEAN:
            with self.subTest(text=text):
                self.assertEqual(_hub_images(text), [])


class PythonTestGateTest(unittest.TestCase):
    def test_release_rules_run_in_their_own_job(self):
        text = (WORKFLOWS / "python-test.yml").read_text(encoding="utf-8")
        job = text[text.index("\n  release-rules:\n"):]
        self.assertIn("python3 -m unittest discover -s tests -p 'test_release_*.py' -v", job)
        fingerprint = text[text.index("\n  fingerprint-canonical:\n"):text.index("\n  release-rules:\n")]
        self.assertNotIn("test_release_", fingerprint)


if __name__ == "__main__":
    unittest.main()
