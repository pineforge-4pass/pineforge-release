#!/usr/bin/env python3
"""HPO image checks, run only on the authorized box.

The resolution test uses published wheels, never synthesized METADATA. Supply
PINEFORGE_HPO_REJECTED_WHEEL (0.13.2) and PINEFORGE_HPO_ACCEPTED_WHEEL (0.15.0)
as file paths or HTTPS URLs. PINEFORGE_CODEGEN_WHEEL optionally supplies the real
1.5.0 wheel; otherwise it is fetched from PyPI and checked against PyPI's digest.
No Docker build or native compilation is performed by these tests.
"""
from __future__ import annotations

import email.parser
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HPO_WHEEL_SHA256 = "ee09f5fd72fd43c26027c294d0e871ab6f3d856fbc5d28c53910a3bbcfa968a6"


def _metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist()
                 if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise AssertionError("a published wheel must have exactly one METADATA")
        return email.parser.BytesParser().parsebytes(archive.read(names[0]))


def _copy_wheel(location: str, root: Path) -> Path:
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme == "https":
        name = Path(urllib.parse.unquote(parsed.path)).name
        if not name.endswith(".whl"):
            raise AssertionError("the supplied HTTPS URL must name a wheel")
        destination = root / name
        with urllib.request.urlopen(location, timeout=60) as response:
            destination.write_bytes(response.read())
    elif parsed.scheme:
        raise AssertionError("supply a wheel file path or HTTPS URL")
    else:
        source = Path(location)
        if not source.is_file() or source.suffix != ".whl":
            raise AssertionError("the supplied published wheel is missing or not a .whl")
        destination = root / source.name
        shutil.copyfile(source, destination)
    return destination


class HpoResolutionTest(unittest.TestCase):
    """Red case first: the same real pip check must reject the old bound and
    accept the released replacement. Setup failures cannot count as rejection.
    """

    def test_published_transpile_bounds_against_codegen_1_5_0(self):
        rejected = os.environ.get("PINEFORGE_HPO_REJECTED_WHEEL")
        accepted = os.environ.get("PINEFORGE_HPO_ACCEPTED_WHEEL")
        if not rejected and not accepted:
            self.skipTest("supply PINEFORGE_HPO_REJECTED_WHEEL (0.13.2) and "
                          "PINEFORGE_HPO_ACCEPTED_WHEEL (0.15.0); published wheels "
                          "are required for the red/green resolution check")
        self.assertTrue(rejected and accepted,
                        "both real HPO wheels are required; one fixture cannot prove red/green")
        helper = REPO / "scripts" / "check_hpo_resolution.py"
        self.assertTrue(helper.is_file(), "the image's resolution helper is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red_wheel = _copy_wheel(rejected, root)
            green_wheel = _copy_wheel(accepted, root)
            red_metadata = _metadata(red_wheel)
            green_metadata = _metadata(green_wheel)
            # These identify the fixtures; they do not decide compatibility.
            self.assertEqual(red_metadata["Name"], "pineforge-hpo")
            self.assertEqual(red_metadata["Version"], "0.13.2")
            self.assertEqual(green_metadata["Name"], "pineforge-hpo")
            self.assertEqual(green_metadata["Version"], "0.15.0")
            self.assertEqual(hashlib.sha256(green_wheel.read_bytes()).hexdigest(),
                             HPO_WHEEL_SHA256, "the accepted wheel is not the released asset")

            codegen_location = os.environ.get("PINEFORGE_CODEGEN_WHEEL")
            if codegen_location:
                codegen_wheel = _copy_wheel(codegen_location, root)
            else:
                with urllib.request.urlopen(
                        "https://pypi.org/pypi/pineforge-codegen/1.5.0/json",
                        timeout=60) as response:
                    release = json.load(response)
                wheels = [item for item in release["urls"]
                          if item["packagetype"] == "bdist_wheel"
                          and item["filename"].endswith("-py3-none-any.whl")]
                self.assertEqual(len(wheels), 1, "PyPI must identify one real codegen wheel")
                codegen_wheel = _copy_wheel(wheels[0]["url"], root)
                self.assertEqual(hashlib.sha256(codegen_wheel.read_bytes()).hexdigest(),
                                 wheels[0]["digests"]["sha256"])
            codegen_metadata = _metadata(codegen_wheel)
            self.assertEqual(codegen_metadata["Name"], "pineforge-codegen")
            self.assertEqual(codegen_metadata["Version"], "1.5.0")
            codegen_root = root / "codegen"
            codegen_root.mkdir()
            with zipfile.ZipFile(codegen_wheel) as archive:
                for name in archive.namelist():
                    destination = (codegen_root / name).resolve()
                    self.assertTrue(destination.is_relative_to(codegen_root.resolve()),
                                    "wheel members must stay in the temporary directory")
                archive.extractall(codegen_root)
            before = {path.relative_to(codegen_root): path.read_bytes()
                      for path in codegen_root.rglob("*") if path.is_file()}

            def resolve(wheel: Path, metadata, report: Path):
                return subprocess.run(
                    [sys.executable, "-I", str(helper), "--hpo-wheel", str(wheel),
                     "--hpo-version", metadata["Version"],
                     "--codegen-root", str(codegen_root), "--codegen-version", "1.5.0",
                     "--report", str(report)], cwd=root,
                    env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=60)

            # No setup error, missing pip, network error or Python-version error
            # may stand in for the genuine old METADATA dependency conflict.
            red_report = root / "rejected.json"
            red = resolve(red_wheel, red_metadata, red_report)
            red_output = red.stdout + red.stderr
            self.assertNotEqual(red.returncode, 0, red_output)
            self.assertIn("pip resolution failed", red_output)
            self.assertIn("ResolutionImpossible", red_output)
            requirements = red_metadata.get_all("Requires-Dist", [])
            conflicting = [requirement for requirement in requirements
                           if requirement.startswith("pineforge-codegen")]
            self.assertTrue(conflicting, "the rejected fixture has no codegen requirement")
            for requirement in conflicting:
                self.assertIn(requirement, red_output)
            self.assertIn("<1.4", red_output)
            self.assertIn("pineforge-codegen==1.5.0", red_output)
            self.assertFalse(red_report.exists(), "a failed resolution must not leave a report")

            green_report = root / "accepted.json"
            green = resolve(green_wheel, green_metadata, green_report)
            self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
            report = json.loads(green_report.read_text(encoding="utf-8"))
            installs = report["install"]
            hpo = [item for item in installs
                   if item["metadata"]["name"].replace("_", "-").lower() == "pineforge-hpo"]
            self.assertEqual(len(hpo), 1, "pip must actually resolve the supplied HPO wheel")
            self.assertEqual(hpo[0]["metadata"]["version"], green_metadata["Version"])
            replacements = [item for item in installs
                            if item["metadata"]["name"].replace("_", "-").lower()
                            == "pineforge-codegen"]
            self.assertEqual(replacements, [], "pip must keep the installed codegen wheel")
            after = {path.relative_to(codegen_root): path.read_bytes()
                     for path in codegen_root.rglob("*") if path.is_file()}
            self.assertEqual(after, before, "dry-run resolution must not write to codegen")


def _run_block(dockerfile: str, needle: str) -> str:
    """The real RUN instruction containing needle, as in HarnessSyncTest."""
    at = dockerfile.index(needle)
    start = dockerfile.rindex("\nRUN ", 0, at) + 1
    end = dockerfile.find("\n\n", at)
    return dockerfile[start:] if end < 0 else dockerfile[start:end]


class DispatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise AssertionError("bash is required to exercise the actual dispatcher")

    def _dispatch(self, arguments: list[str], status: int = 0) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = root / "argv"
            for name in ("python3", "entrypoint.sh"):
                stub = root / name
                stub.write_text(
                    f"#!{self.bash}\n"
                    + "printf '%s\\0' " + shlex.quote(name)
                    + ' "$@" > "$HPO_TEST_RECORD"\nexit "$HPO_TEST_EXIT"\n',
                    encoding="utf-8")
                stub.chmod(0o755)
            # PATH cannot intercept an absolute path. This exec function maps
            # just the engine's absolute destination to the PATH stub; all
            # branch/shift/argument logic comes from the unedited real script.
            # Source after shift so the no-argument case really has zero args.
            script = '''exec() {
    if [ "$1" = /opt/pineforge/bin/entrypoint.sh ]; then
        shift
        builtin exec entrypoint.sh "$@"
    else
        builtin exec "$@"
    fi
}
dispatch="$1"
shift
source "$dispatch"
'''
            done = subprocess.run(
                [self.bash, "--noprofile", "--norc", "-c", script, "dispatcher-test",
                 str(REPO / "docker" / "entrypoint-dispatch.sh"), *arguments],
                cwd=root, env={"PATH": str(root) + os.pathsep + os.defpath,
                               "HPO_TEST_RECORD": str(record), "HPO_TEST_EXIT": str(status)},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, status, done.stdout + done.stderr)
            self.assertTrue(record.is_file(), "the dispatcher never reached a stub executable")
            self.assertEqual(done.stdout, "")
            self.assertEqual(done.stderr, "")
            data = record.read_bytes()
            self.assertTrue(data.endswith(b"\0"), "the stub did not record complete argv")
            return [item.decode("utf-8") for item in data.split(b"\0")[:-1]]

    def test_hpo_first_argument_execs_the_module_with_remaining_argv(self):
        for arguments in ([], ["space-info"], ["compile", "a source.pine"],
                          ["run", "study.json", "", "a\nb", "$(never-run)", "*"],
                          ["warm-encode", "--help"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self._dispatch(["hpo", *arguments]),
                                 ["python3", "-m", "pineforge_hpo", *arguments])

    def test_no_argument_and_other_first_arguments_preserve_engine_argv(self):
        for arguments in ([], [""], ["run"], ["compile", "hpo"],
                          ["HPO", "--help"], ["--", "hpo"],
                          ["a source.pine", "", "a\nb", "$(never-run)", "*"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self._dispatch(arguments), ["entrypoint.sh", *arguments])

    def test_exec_preserves_the_selected_commands_exit_status(self):
        for arguments in ([], ["hpo", "run"]):
            for status in (1, 4, 5):
                with self.subTest(arguments=arguments, status=status):
                    expected = (["python3", "-m", "pineforge_hpo", "run"]
                                if arguments else ["entrypoint.sh"])
                    self.assertEqual(self._dispatch(arguments, status), expected)


class DockerfileHarnessTest(unittest.TestCase):
    HARNESS = ("bind_compiled_inventory.py", "entrypoint.sh", "request_feed_inventory.py",
               "run_execution_observer.py", "run_json.py", "run_phase_transport.py",
               "selected_window_plan.py", "selected_window_report.py")
    # Read from the hub base before editing; the complete byte-check RUN is the
    # contract, including the eight names and the check for an extra bin/ file.
    CHECK = r'''RUN set -eu; \
    cd /opt/pineforge/bin; \
    want="bind_compiled_inventory.py entrypoint.sh request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py selected_window_plan.py selected_window_report.py"; \
    listed="$(sed 's/^[0-9a-f]\{64\}  //' HARNESS-SHA256SUMS | tr '\n' ' ' | sed 's/ $//')"; \
    present="$(LC_ALL=C ls -A | grep -v '^HARNESS-SHA256SUMS$' | tr '\n' ' ' | sed 's/ $//')"; \
    if [ "$listed" != "$want" ] || [ "$present" != "$want" ]; then \
      echo "ERROR: HARNESS-SHA256SUMS lists [${listed}] and bin/ holds [${present}]; both must be exactly [${want}]" >&2; exit 1; \
    fi; \
    sha256sum -c --strict HARNESS-SHA256SUMS; \
    echo "harness: the eight files match HARNESS-SHA256SUMS"'''

    def test_harness_copy_list_and_full_build_check_are_unchanged(self):
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        copies = re.findall(r"^COPY docker/(\S+)\s+/opt/pineforge/bin/(\S+)\s*$",
                            dockerfile, re.M)
        self.assertEqual(sorted(copies),
                         sorted((name, name) for name in self.HARNESS + ("HARNESS-SHA256SUMS",)))
        self.assertEqual(_run_block(dockerfile, "sha256sum -c --strict HARNESS-SHA256SUMS"),
                         self.CHECK)
        sums = (REPO / "docker" / "HARNESS-SHA256SUMS").read_text(encoding="utf-8")
        self.assertEqual([line.split("  ", 1)[1] for line in sums.splitlines()], list(self.HARNESS))
        self.assertIn('ENTRYPOINT ["/opt/pineforge/entrypoint-dispatch.sh"]', dockerfile)
        self.assertIn("PF_ABI_VERSION", _run_block(dockerfile, 'echo "harness ABI'))


class LicenceChecksumTest(unittest.TestCase):
    """Exercise the actual Dockerfile RUN on copied bytes in temp directories.

    These fixtures test the checksum protocol, not the upstream licence hashes;
    the real hashes must be filled on the box and checked by the image build.
    """
    SUMS = "LICENSE-SHA256SUMS"
    CHECK = "sha256sum -c --strict LICENSE-SHA256SUMS"

    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        tools = ("bash", "find", "grep", "sed", "sha256sum", "sort")
        found = {tool: shutil.which(tool) for tool in tools}
        missing = [tool for tool in tools if found[tool] is None]
        if missing:
            raise AssertionError("the licence RUN requires " + ", ".join(missing))
        cls.bash = found["bash"]
        cls.path = os.environ.get("PATH", os.defpath)

    def _run(self, licences: Path):
        block = _run_block(self.dockerfile, self.CHECK)
        script = block[len("RUN "):].replace("\\\n", "")
        self.assertNotIn("\n", script, "the checksum RUN must be one logical instruction")
        self.assertEqual(script.count("/opt/pineforge/licenses"), 1)
        script = script.replace("/opt/pineforge/licenses", shlex.quote(str(licences)))
        return subprocess.run([self.bash, "--noprofile", "--norc", "-c", script],
                              cwd=licences.parent, env={"PATH": self.path},
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)

    def _layout(self, root: Path) -> Path:
        licences = root / "licences"
        target = licences / "pineforge-hpo" / "LICENSE"
        target.parent.mkdir(parents=True)
        shutil.copyfile(REPO / "LICENSE", target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        (licences / self.SUMS).write_text(f"{digest}  pineforge-hpo/LICENSE\n", encoding="utf-8")
        done = self._run(licences)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("pineforge-hpo/LICENSE: OK", done.stdout)
        return licences

    def test_mutating_a_copied_file_fails_sha256sum_for_that_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            licences = self._layout(Path(tmp))
            victim = licences / "pineforge-hpo" / "LICENSE"
            data = victim.read_bytes()
            middle = len(data) // 2
            victim.write_bytes(data[:middle] + bytes([data[middle] ^ 1]) + data[middle + 1:])
            done = self._run(licences)
            self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("pineforge-hpo/LICENSE: FAILED", done.stdout)
            self.assertNotIn("licences: every copied file matches", done.stdout)

    def test_a_placeholder_is_rejected_even_with_an_otherwise_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            licences = self._layout(Path(tmp))
            sums = licences / self.SUMS
            sums.write_text(sums.read_text(encoding="utf-8")
                            + "# TO BE FILLED BY THE BOX RUN: pineforge-hpo/NOTICE\n",
                            encoding="utf-8")
            done = self._run(licences)
            self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("still needs approved box-run digests", done.stderr)

    def test_an_unlisted_licence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            licences = self._layout(Path(tmp))
            shutil.copyfile(REPO / "LICENSE", licences / "pineforge-hpo" / "NOTICE")
            done = self._run(licences)
            self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("must list every copied licence exactly once", done.stderr)


class PinnedLicenceDigestsTest(unittest.TestCase):
    """The committed manifest must pin the licence bytes of the approved snapshots.

    The expected values are the full SHA-256 digests of the pinned snapshots held
    outside this repository (the HPO and codegen LICENSE files, HPO's NOTICE and its
    bundled sobol licence). They are the independent source of truth: the manifest
    is compared against them, not against whatever a wheel happens to contain.
    """

    PINNED = {
        "pineforge-hpo/LICENSE": "032d774b3a42b323a621f0380d4dbad2cb377f88c7e3c255f3e845e961750a40",
        "pineforge-hpo/NOTICE": "906b446211797822dcecba20fbc9df8734595ce7fdc6ef655b6b995c86ac32e0",
        "pineforge-hpo/THIRD_PARTY_LICENSES/sobol_joe_kuo.txt":
            "9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3",
        "pineforge-codegen/LICENSE": "8ce6f5370bc4832baa5a0771504ea34ed406459a82cf6eb55acd1dfcb473b6fc",
    }

    def test_manifest_lists_exactly_the_pinned_digests_for_these_files(self):
        listed = {}
        for line in (REPO / "docker" / "LICENSE-SHA256SUMS").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            digest, _, path = line.partition("  ")
            listed[path] = digest
        for path, digest in self.PINNED.items():
            self.assertEqual(listed.get(path), digest, path)


class ReadmePairRuleTest(unittest.TestCase):
    def test_lines_196_to_225_are_byte_identical_to_origin_main(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is unavailable; pair-rule comparison needs a git checkout")
        checkout = subprocess.run([git, "rev-parse", "--is-inside-work-tree"], cwd=REPO,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        if checkout.returncode or checkout.stdout.strip() != b"true":
            self.skipTest("not in a git checkout; cannot read origin/main:README.md")
        baseline = subprocess.run([git, "show", "origin/main:README.md"], cwd=REPO,
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(baseline.returncode, 0,
                         "git checkout must provide origin/main for the pair-rule byte comparison")
        before = baseline.stdout.splitlines(keepends=True)
        after = (REPO / "README.md").read_bytes().splitlines(keepends=True)
        self.assertGreaterEqual(len(before), 225)
        self.assertGreaterEqual(len(after), 225)
        self.assertEqual(after[195:225], before[195:225])


if __name__ == "__main__":
    unittest.main()
