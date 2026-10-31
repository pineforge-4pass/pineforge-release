#!/usr/bin/env python3
"""Guard policy digest contract for the public release hub.

Layer 1 checks the committed pin, build wiring, and the Dockerfile's own shell
assertion without Docker. It cannot recompute B: the render is private, so it
checks consistency with the pin only. The documented codegen 1.9.1 pin moves
only when the guard policy sets move; recompute privately.

Layer 2 needs PINEFORGE_POLICY_TEST_IMAGE on a build host; optionally select
PINEFORGE_POLICY_TEST_PLATFORM. Skipping layer 2 is NOT a pass of the PR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
PIN = REPO / "docker" / "GUARD-POLICY-SETS.pin"
DOCKERFILE = REPO / "docker" / "Dockerfile"
PUBLISH = REPO / ".github" / "workflows" / "publish.yml"
ARG = "GUARD_POLICY_SETS_SHA256"
IMAGE_FILE = "/opt/pineforge/guard-policy.sha256"
LABEL = "io.pineforge.guard.policy-sets-sha256"
VALID_DIGEST = "0123456789abcdef" * 4  # Synthetic input, not an expected policy pin.
DIFFERENT_DIGEST = VALID_DIGEST[1:] + VALID_DIGEST[:1]
PIN_EXPRESSION = "${{ steps.pins.outputs.guard_policy_sets }}"

# Match the agreed printf command, allowing whitespace and quoted destinations.
# Mutations change only the captured operand; the comparison remains untouched.
WRITE_COMMAND = re.compile(
    r"""printf\s+(['"])%s\\n\1\s+"""
    r'(?P<value>"\$\{GUARD_POLICY_SETS_SHA256\}")\s*>\s*'
    r"""(?:"/opt/pineforge/guard-policy\.sha256"|'"""
    r"""/opt/pineforge/guard-policy\.sha256'|/opt/pineforge/guard-policy\.sha256)"""
    r"(?=\s|;|$)"
)
CHOWN_COMMAND = re.compile(
    r"""\bchown\s+0:0\s+(?:"/opt/pineforge/guard-policy\.sha256"|'"""
    r"""/opt/pineforge/guard-policy\.sha256'|/opt/pineforge/guard-policy\.sha256)"""
    r"\s*(?:;|&&)"
)


def validate_digest_file(data: bytes) -> str:
    """Return the digest only for exactly 64 lowercase hex bytes plus one LF."""
    if re.fullmatch(rb"[0-9a-f]{64}\n", data) is None:
        raise ValueError("expected exactly 64 lowercase hex bytes followed by one LF")
    return data[:-1].decode("ascii")


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise AssertionError(f"required artifact is missing: {path.relative_to(REPO)}")
    return path.read_bytes()


def _pin() -> str:
    try:
        return validate_digest_file(_read_bytes(PIN))
    except ValueError as exc:
        raise AssertionError(f"docker/GUARD-POLICY-SETS.pin: {exc}") from exc


def _logical(text: str) -> str:
    return re.sub(r"\\\r?\n", " ", text)


def _instructions():
    """Keep shell bodies verbatim, including their backslash continuations."""
    lines = iter(_read_bytes(DOCKERFILE).decode("utf-8").splitlines(keepends=True))
    stage = None
    result = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*([A-Za-z]+)\s+(.*)", line, re.S)
        if match is None:
            raise AssertionError(f"unrecognized Dockerfile instruction: {line!r}")
        opcode, body = match.group(1).upper(), match.group(2)
        while body.rstrip("\r\n").endswith("\\"):
            following = next(lines, None)
            if following is None:
                raise AssertionError("unterminated Dockerfile continuation")
            body += following
        if opcode == "FROM":
            alias = re.search(r"\s+AS\s+(\S+)\s*$", _logical(body), re.I)
            stage = alias.group(1).lower() if alias else None
        result.append((stage, opcode, body.rstrip("\r\n")))
    return result


def _guard_run() -> str:
    matches = [
        body for stage, opcode, body in _instructions()
        if stage == "release" and opcode == "RUN"
        and ARG in body and "guard-policy.sha256" in body
    ]
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly ONE release-stage RUN mentioning both "
            f"{ARG} and guard-policy.sha256; found {len(matches)}"
        )
    return matches[0]


def _workflow():
    # Match the existing workflow tests: BaseLoader preserves scalar spellings.
    import yaml
    return yaml.load(_read_bytes(PUBLISH).decode("utf-8"), Loader=yaml.BaseLoader)


def _tracked_paths():
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, stdin=subprocess.DEVNULL,
        capture_output=True, timeout=30,
    )
    if result.returncode:
        raise AssertionError("cannot list tracked files: " + result.stderr.decode(errors="replace"))
    paths = {Path(os.fsdecode(path)) for path in result.stdout.split(b"\0") if path}
    if not paths:
        raise AssertionError("tracked-file scan must not silently inspect an empty inventory")
    return paths


class GuardPolicyDigestTest(unittest.TestCase):
    def test_validate_digest_file_accepts_exact_format(self):
        self.assertEqual(
            validate_digest_file((VALID_DIGEST + "\n").encode("ascii")), VALID_DIGEST,
        )

    def test_validate_digest_file_rejects_other_formats(self):
        invalid = {
            "sha256sum form": VALID_DIGEST + "  -\n",
            "uppercase": VALID_DIGEST.upper() + "\n",
            "missing LF": VALID_DIGEST,
            "CRLF": VALID_DIGEST + "\r\n",
            "trailing space": VALID_DIGEST + " \n",
            "63 characters": VALID_DIGEST[:-1] + "\n",
            "65 characters": VALID_DIGEST + "0\n",
            "empty": "",
            "extra LF": VALID_DIGEST + "\n\n",
            "embedded LF": VALID_DIGEST[:32] + "\n" + VALID_DIGEST[32:] + "\n",
            "non-hex": "g" * 64 + "\n",
        }
        for name, text in invalid.items():
            with self.subTest(format=name), self.assertRaises(ValueError):
                validate_digest_file(text.encode("ascii"))

    def test_no_private_policy_leaks(self):
        # Assemble the search tokens at runtime so neither this source nor its
        # compiled bytecode needs an exception to the content scan.
        marker = b"".join((b"render_", b"policy"))
        forbidden_names = {
            os.fsdecode(marker) + ".py", "".join(("policy", ".inc")),
        }
        paths = _tracked_paths()
        for directory, dirs, files in os.walk(REPO):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in files:
                if name != ".git":
                    paths.add((Path(directory) / name).relative_to(REPO))
        for relative in sorted(paths):
            if ".git" in relative.parts:
                continue
            with self.subTest(path=relative.as_posix()):
                self.assertNotIn(relative.name, forbidden_names, "private policy artifact leaked")
                path = REPO / relative
                if path.is_file():
                    self.assertNotIn(marker, path.read_bytes(), "private policy reference leaked")

    def test_pin_file_has_exact_digest_format(self):
        self.assertEqual(len(_pin()), 64)

    def test_pin_for_codegen_1_9_1(self):
        """Documented pin: moves only when guard policy sets move; recompute privately."""
        self.assertEqual(
            _pin(), "02235ead051bdd9e6a53d74fc62048f58611c11469199b9febb31cf104f433c3",
        )


class GuardPolicyRunTest(unittest.TestCase):
    def _execute(self, original, value, root):
        # These are the only normal execution edits. In particular, no Python
        # implementation of the shell's validation or comparison is substituted.
        command = original
        if os.getuid() != 0:
            command, count = CHOWN_COMMAND.subn("", command)
            self.assertEqual(count, 1, "cannot remove exactly one chown 0:0 command")
        prefix = root / "pineforge"
        prefix.mkdir()
        command = command.replace("/opt/pineforge", str(prefix))
        environment = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
        if value is not None:
            environment[ARG] = value
        result = subprocess.run(
            ["/bin/sh", "-c", command], cwd=root, env=environment,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
        return result, prefix / "guard-policy.sha256"

    def _assert_valid(self, original, root):
        result, path = self._execute(original, VALID_DIGEST, root)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(path.is_file(), "successful RUN must create the digest file")
        self.assertEqual(path.read_bytes(), (VALID_DIGEST + "\n").encode("ascii"))
        self.assertEqual(validate_digest_file(path.read_bytes()), VALID_DIGEST)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_release_run_accepts_valid_arg_and_sets_file_mode(self):
        original = _guard_run()
        with tempfile.TemporaryDirectory() as temporary:
            self._assert_valid(original, Path(temporary))

    def test_release_run_rejects_missing_or_malformed_args(self):
        invalid = {
            "unset": None,
            "empty": "",
            "uppercase": VALID_DIGEST.upper(),
            "63 characters": VALID_DIGEST[:-1],
            "65 characters": VALID_DIGEST + "0",
            "trailing space": VALID_DIGEST + " ",
            "embedded newline": VALID_DIGEST[:32] + "\n" + VALID_DIGEST[32:],
            "non-hex letters": "g" * 64,
        }
        original = _guard_run()
        # A shell/tool failure is not evidence that an invalid argument was
        # refused: first prove the unchanged command can succeed in this row.
        with tempfile.TemporaryDirectory() as temporary:
            self._assert_valid(original, Path(temporary))
        for name, value in invalid.items():
            with self.subTest(argument=name), tempfile.TemporaryDirectory() as temporary:
                result, path = self._execute(original, value, Path(temporary))
                self.assertNotEqual(
                    result.returncode, 0, f"RUN admitted {name}: {result.stdout!r}",
                )
                if path.exists():
                    self.assertTrue(path.is_file(), "RUN left a non-file digest artifact")
                    with self.assertRaises(ValueError, msg="refused argument left a usable digest"):
                        validate_digest_file(path.read_bytes())

    def test_release_run_final_assert_rejects_a_different_valid_written_digest(self):
        original = _guard_run()
        matches = list(WRITE_COMMAND.finditer(original))
        self.assertEqual(
            len(matches), 1,
            "mutation requires exactly one printf '%s\\n' of the arg to the digest path",
        )
        match = matches[0]
        start, end = match.span("value")
        mutated = original[:start] + shlex.quote(DIFFERENT_DIGEST) + original[end:]
        self.assertNotEqual(mutated, original, "mutation must change the write command")
        self.assertNotEqual(DIFFERENT_DIGEST, VALID_DIGEST, "mutation needs a different digest")
        with tempfile.TemporaryDirectory() as temporary:
            self._assert_valid(original, Path(temporary))
        with tempfile.TemporaryDirectory() as temporary:
            result, path = self._execute(mutated, VALID_DIGEST, Path(temporary))
            self.assertTrue(path.is_file(), "mutated write must execute before the assertion")
            self.assertEqual(path.read_bytes(), (DIFFERENT_DIGEST + "\n").encode("ascii"))
            self.assertEqual(validate_digest_file(path.read_bytes()), DIFFERENT_DIGEST)
            self.assertNotEqual(result.returncode, 0, "final shell assertion admitted a different digest")


class GuardPolicyDockerfileTest(unittest.TestCase):
    def test_release_declares_required_arg_without_a_default(self):
        declarations = [
            (stage, body.strip()) for stage, opcode, body in _instructions()
            if opcode == "ARG" and re.match(r"GUARD_POLICY_SETS_SHA256(?:\s|=|$)", body)
        ]
        self.assertIn(("release", ARG), declarations, "release must declare the required build arg")
        for stage, body in declarations:
            with self.subTest(stage=stage):
                self.assertEqual(body, ARG, "guard policy argument must have no source default")

    def test_release_label_uses_arg_next_to_codegen_label_without_overrides(self):
        labels = []
        for stage, opcode, body in _instructions():
            if opcode == "LABEL":
                fields = shlex.split(_logical(body))
                for field in fields:
                    if field.split("=", 1)[0] == LABEL:
                        labels.append((stage, field))
                        self.assertIn(
                            "io.pineforge.codegen.version=${CODEGEN_VERSION}", fields,
                            "guard policy label belongs next to the codegen version label",
                        )
        self.assertEqual(labels, [("release", LABEL + "=${" + ARG + "}")])

    def test_original_run_has_permissions_final_assert_and_no_build_dependencies(self):
        original = _logical(_guard_run())
        self.assertRegex(original, r"^\s*set\s+-eu\s*;")
        for command in ("chmod 0644", "chown 0:0"):
            self.assertIn(command, original)
            self.assertRegex(
                original, re.escape(command) + r"""\s+["']?"""
                + re.escape(IMAGE_FILE) + r"""["']?(?=\s|;|$)""",
            )
        self.assertEqual(len(list(WRITE_COMMAND.finditer(original))), 1)
        assertion = 'test "$(cat ' + IMAGE_FILE + ')" = "${' + ARG + '}"'
        self.assertIn(assertion, original, "RUN must compare the installed file with the supplied arg")
        self.assertTrue(
            original.rstrip().rstrip(";").rstrip().endswith(assertion),
            "file comparison must be the final command, with its exit status preserved",
        )
        for forbidden in ("curl", "wget", "pip", "apt", "render", "codegen"):
            self.assertNotIn(forbidden, original.lower(), "digest assertion must be self-contained")

    def test_hpo_inherits_release(self):
        bases = [
            shlex.split(_logical(body)) for stage, opcode, body in _instructions()
            if stage == "hpo" and opcode == "FROM"
        ]
        self.assertEqual(len(bases), 1, "expected exactly one hpo stage")
        self.assertEqual(bases[0][0].lower(), "release")

    def test_dockerfile_contains_no_digest_literal(self):
        self.assertNotRegex(_read_bytes(DOCKERFILE).decode("utf-8"), r"[0-9a-fA-F]{64}")


class GuardPolicyPublishTest(unittest.TestCase):
    def _pins(self):
        found = [
            (job, step) for job in _workflow()["jobs"].values()
            for step in job.get("steps", []) if step.get("id") == "pins"
        ]
        self.assertEqual(len(found), 1, "publish.yml must have exactly one pins step")
        return found[0]

    def test_pins_step_reads_validates_and_exports_the_committed_digest(self):
        _, pins = self._pins()
        run = "\n".join(
            line for line in pins.get("run", "").splitlines()
            if not line.lstrip().startswith("#")
        )
        read = re.search(
            r"""(?m)^\s*(\w+)=["']?\$\(\s*(?:cat\s+|<\s*)["']?"""
            r"""docker/GUARD-POLICY-SETS\.pin["']?\s*\)["']?\s*;?\s*$""", run,
        )
        self.assertIsNotNone(read, "pins must read docker/GUARD-POLICY-SETS.pin into a shell variable")
        variable = read.group(1)
        reference = r"\$(?:\{" + re.escape(variable) + r"\}|" + re.escape(variable) + r"\b)"
        bash_check = (
            r'\[\[\s*(?:!\s*)?"?' + reference
            + r'"?\s+=~\s+\^\[0-9a-f\]\{64\}\$\s*\]\]'
        )
        grep_check = (
            r"""(?:printf\s+['"]%s(?:\\n)?['"]\s+|echo\s+)"?""" + reference
            + r'"?\s*\|\s*grep\s+(?:-Eq|-qE|-E\s+-q|-q\s+-E)\s+'
            + r"""['"]\^\[0-9a-f\]\{64\}\$['"]"""
        )
        validation = [
            line for line in run.splitlines()
            if re.search(bash_check, line) or re.search(grep_check, line)
        ]
        self.assertTrue(
            validation, "pins must check the read value with [[ =~ ]] or printf | grep -Eq "
            "using ^[0-9a-f]{64}$",
        )
        export = re.search(
            r"""(?:echo\s+["']guard_policy_sets=""" + reference + r"""["']"""
            r"""|printf\s+['"]guard_policy_sets=%s\\n['"]\s+["']?"""
            + reference + r"""["']?).*?>>\s*["']?\$(?:\{GITHUB_OUTPUT\}|GITHUB_OUTPUT)""",
            run, re.S,
        )
        self.assertIsNotNone(export, "pins must export the same read value to GITHUB_OUTPUT")
        self.assertLess(read.start(), run.index(validation[0]))
        self.assertLess(run.index(validation[0]), export.start())

    def test_every_dockerfile_build_passes_the_same_pins_output(self):
        builders = [
            (job, step) for job in _workflow()["jobs"].values()
            for step in job.get("steps", [])
            if step.get("uses", "").split("@")[0] == "docker/build-push-action"
            and step.get("with", {}).get("file", "").removeprefix("./") == "docker/Dockerfile"
        ]
        self.assertTrue(builders, "must discover Dockerfile build actions, never assume their count")
        expressions = []
        for job, step in builders:
            with self.subTest(step=step.get("name", step.get("id"))):
                assignments = [
                    line.strip().split("=", 1)[1].strip()
                    for line in step.get("with", {}).get("build-args", "").splitlines()
                    if line.strip().startswith(ARG + "=")
                ]
                self.assertEqual(assignments, [PIN_EXPRESSION])
                expressions.extend(assignments)
                steps = job["steps"]
                pins = [row for row in steps if row.get("id") == "pins"]
                self.assertEqual(len(pins), 1, "pins output must be available in the build job")
                self.assertLess(steps.index(pins[0]), steps.index(step))
        self.assertEqual(set(expressions), {PIN_EXPRESSION})

    def test_metadata_action_does_not_override_guard_policy_label(self):
        metadata = [
            step for job in _workflow()["jobs"].values() for step in job.get("steps", [])
            if step.get("uses", "").split("@")[0] == "docker/metadata-action"
        ]
        self.assertTrue(metadata, "must inspect actual metadata action steps")
        for step in metadata:
            with self.subTest(step=step.get("name", step.get("id"))):
                self.assertNotIn(LABEL, step.get("with", {}).get("labels", ""))

    def test_other_builders_acknowledge_the_required_arg(self):
        allowlist = {
            "tests/test_release_hpo_image.py":
                "Historical Dockerfile fixtures quote build commands; they do not build images.",
            "tests/test_release_workflows.py":
                "Workflow and shell assertion fixtures inspect builds without invoking a builder.",
            "tools/release-dry-run/wfrun.py":
                "Stubs docker/build-push-action and records every non-empty build-args "
                "line of the workflow step verbatim; it never names a specific build arg, "
                "so the arg reaches it from publish.yml (checked above) without a mention here.",
        }
        # Both tools handle workflow build actions indirectly, without spelling
        # the Dockerfile path themselves; include them even if grep misses it.
        indirect = {"tools/release-dry-run/dry_run.py", "tools/release-dry-run/wfrun.py"}
        paths = _tracked_paths()
        self.assertTrue(indirect <= {path.as_posix() for path in paths})
        for relative in sorted(paths):
            name = relative.as_posix()
            if name == ".github/workflows/publish.yml":
                continue  # Every actual build action is checked individually above.
            path = REPO / relative
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
            commands = _logical(text)
            candidate = name in indirect or (
                "docker/Dockerfile" in text and (
                    "build-push-action" in text
                    or re.search(r"\b(?:docker\s+build(?:x\s+build)?|buildx\s+build)\b", commands)
                )
            )
            if candidate:
                with self.subTest(builder=name):
                    self.assertTrue(
                        ARG in text or bool(allowlist.get(name)),
                        f"{name} must mention {ARG} or have an explicit reason in the allowlist",
                    )
        self.assertIn(ARG, _read_bytes(REPO / "README.md").decode("utf-8"))


class GuardPolicyImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ.get("PINEFORGE_POLICY_TEST_IMAGE")
        if not cls.image:
            raise unittest.SkipTest(
                "layer 2 was skipped: set PINEFORGE_POLICY_TEST_IMAGE on a build host; "
                "a skipped layer 2 is NOT a pass of the PR"
            )
        if shutil.which("docker") is None:
            raise AssertionError("PINEFORGE_POLICY_TEST_IMAGE is set but docker is unavailable")

    def _docker(self, arguments):
        result = subprocess.run(
            ["docker", *arguments], stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def _run(self, entrypoint, *arguments):
        command = ["run", "--rm", "--pull=never", "--network=none"]
        platform = os.environ.get("PINEFORGE_POLICY_TEST_PLATFORM")
        if platform:
            command.extend(("--platform", platform))
        command.extend(("--entrypoint", entrypoint, self.image, *arguments))
        return self._docker(command)

    def test_image_file_label_and_committed_pin_agree(self):
        data = self._run("cat", IMAGE_FILE)
        digest = validate_digest_file(data)
        inspected = json.loads(self._docker(["image", "inspect", self.image]))
        self.assertEqual(len(inspected), 1)
        labels = inspected[0]["Config"].get("Labels") or {}
        self.assertIn(LABEL, labels, "image must carry the guard policy digest label")
        self.assertEqual(digest, labels[LABEL])
        self.assertEqual(digest, _pin())
        self.assertEqual(data, _read_bytes(PIN))

    def test_image_digest_file_is_readable_and_root_owned(self):
        self.assertEqual(self._run("stat", "-c", "%a %u:%g", IMAGE_FILE), b"644 0:0\n")


if __name__ == "__main__":
    unittest.main()
