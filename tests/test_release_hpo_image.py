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
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HPO_WHEEL_SHA256 = "ee09f5fd72fd43c26027c294d0e871ab6f3d856fbc5d28c53910a3bbcfa968a6"
HPO_REJECTED_WHEEL_SHA256 = "57fff4c49cba794a9657e7882376eee294366843cf0442306195b8390dd0d073"


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
            self.assertEqual(hashlib.sha256(red_wheel.read_bytes()).hexdigest(),
                             HPO_REJECTED_WHEEL_SHA256, "the rejected wheel is not the released asset")
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


@dataclass(frozen=True)
class DockerStage:
    name: str
    base: str
    text: str
    instructions: tuple[tuple[str, str], ...]


def _dockerfile_instructions(text: str) -> tuple[tuple[str, str], ...]:
    """Read logical instructions, including continued RUN and COPY lines."""
    instructions = []
    pending = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.strip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical = pending + line
        pending = ""
        match = re.fullmatch(r"([A-Za-z]+)\s+(.*)", logical)
        if match is None:
            raise ValueError("malformed Dockerfile instruction")
        instructions.append((match[1].upper(), match[2]))
    if pending:
        raise ValueError("unterminated Dockerfile continuation")
    return tuple(instructions)


def _dockerfile_stages(text: str) -> tuple[DockerStage, ...]:
    """Keep each stage's original bytes as well as its parsed instructions."""
    starts = list(re.finditer(r"^[ \t]*FROM[ \t]+([^\r\n]+)", text, re.M | re.I))
    if not starts:
        raise ValueError("Dockerfile has no stages")
    stages = []
    names = set()
    for index, start in enumerate(starts):
        words = shlex.split(start[1])
        while words and words[0].startswith("--"):
            words.pop(0)
        if len(words) == 1:
            base, name = words[0], str(index)
        elif len(words) == 3 and words[1].lower() == "as":
            base, name = words[0], words[2].lower()
        else:
            raise ValueError("malformed Dockerfile FROM instruction")
        if name in names:
            raise ValueError("duplicate Dockerfile stage: " + name)
        names.add(name)
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        lines = text[start.start():end].splitlines(keepends=True)
        # Inter-stage headings are outside the preceding stage's last
        # instruction. Internal comments and whitespace remain byte-identical.
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
            lines.pop()
        source = "".join(lines)
        stages.append(DockerStage(name, base, source, _dockerfile_instructions(source)))
    return tuple(stages)


def _stage_references(stages: tuple[DockerStage, ...]) -> dict[str, DockerStage]:
    references = {str(index): stage for index, stage in enumerate(stages)}
    references.update((stage.name, stage) for stage in stages)
    return references


def _reachable_stages(stages: tuple[DockerStage, ...], target: str | None = None) -> set[str]:
    """Follow actual FROM and COPY --from edges, by alias or numeric index.

    External images do not name a stage in this Dockerfile. No target names or
    expected dependency sets are built into the traversal.
    """
    references = _stage_references(stages)
    start = stages[-1] if target is None else references[target.lower()]
    pending = [start]
    reached = set()
    while pending:
        stage = pending.pop()
        if stage.name in reached:
            continue
        reached.add(stage.name)
        sources = [stage.base]
        for instruction, body in stage.instructions:
            if instruction == "COPY":
                words = shlex.split(body)
                for index, word in enumerate(words):
                    if word.startswith("--from="):
                        sources.append(word.split("=", 1)[1])
                    elif word == "--from" and index + 1 < len(words):
                        sources.append(words[index + 1])
        pending.extend(references[source.lower()] for source in sources
                       if source.lower() in references)
    return reached


def _build_arguments(stages: tuple[DockerStage, ...], reached: set[str], *,
                     required: bool = False) -> set[str]:
    arguments = set()
    for stage in stages:
        if stage.name not in reached:
            continue
        for instruction, body in stage.instructions:
            if required and instruction == "RUN":
                arguments.update(re.findall(
                    r"\btest\s+-n\s+[\"']?\$\{?([A-Z_][A-Z0-9_]*)\}?[\"']?", body))
            elif not required and instruction == "ARG":
                arguments.add(body.split("=", 1)[0].strip())
    return arguments


def _hpo_arguments(stages: tuple[DockerStage, ...], reached: set[str], *,
                   required: bool = False) -> set[str]:
    return {name for name in _build_arguments(stages, reached, required=required)
            if name.startswith("HPO_")}


def _inherited_instruction(stages: tuple[DockerStage, ...], target: str,
                           instruction: str) -> str | None:
    references = _stage_references(stages)
    stage = references[target.lower()]
    seen = set()
    while stage.name not in seen:
        seen.add(stage.name)
        for keyword, body in reversed(stage.instructions):
            if keyword == instruction:
                return body
        if stage.base.lower() not in references:
            break
        stage = references[stage.base.lower()]
    return None


def _without_stage_alias(stage: DockerStage) -> str:
    return re.sub(r"(^[ \t]*FROM[^\r\n]*?)[ \t]+AS[ \t]+\S+(?=\r?$)",
                  r"\1", stage.text, count=1, flags=re.M | re.I)


def _without_sources_notice(text: str) -> str:
    begin = "# --- sources notice (begin)\n"
    end = "# --- sources notice (end)\n"
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError("expected exactly one marked sources notice block")
    start = text.index(begin)
    stop = text.index(end, start) + len(end)
    return text[:start] + text[stop:]


def _restore_original_doc_cleanup(text: str) -> str:
    """Undo only the single approved apt cleanup token change for the old pin."""
    new = "    && rm -rf /var/lib/apt/lists/* /usr/share/man /usr/share/locale /usr/share/info\n"
    old = "    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info\n"
    lines = text.splitlines(keepends=True)
    if lines.count(new) != 1:
        raise ValueError("expected exactly one apt cleanup line without /usr/share/doc")
    lines[lines.index(new)] = old
    return "".join(lines)


def _restore_original_harness_manifest(text: str) -> str:
    """Undo exactly CR-150's approved harness list changes for the old runtime pin."""
    changes = (
        ("# The harness is the engine's runtime files for the pinned engine tag, all ten and nothing\n",
         "# The harness is the engine's runtime files for the pinned engine tag, all eight and nothing\n"),
        ("COPY docker/pf_bars.py                  /opt/pineforge/bin/pf_bars.py\n", ""),
        ("COPY docker/pf_sink.py                  /opt/pineforge/bin/pf_sink.py\n", ""),
        ("# must name exactly the ten files above, in sorted order, and nothing else may sit in bin/;\n",
         "# must name exactly the eight files above, in this order, and nothing else may sit in bin/;\n"),
        ('    want="bind_compiled_inventory.py entrypoint.sh pf_bars.py pf_sink.py request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py selected_window_plan.py selected_window_report.py"; \\\n',
         '    want="bind_compiled_inventory.py entrypoint.sh request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py selected_window_plan.py selected_window_report.py"; \\\n'),
        ('    echo "harness: the ten files match HARNESS-SHA256SUMS"\n',
         '    echo "harness: the eight files match HARNESS-SHA256SUMS"\n'),
    )
    for current, original in changes:
        if text.count(current) != 1:
            raise ValueError("expected exactly one approved CR-150 harness manifest fragment")
        text = text.replace(current, original, 1)
    return text


def _pinned_packages(stage: DockerStage) -> dict[str, str]:
    packages = {}
    for instruction, body in stage.instructions:
        if instruction == "RUN":
            packages.update(re.findall(
                r"([a-z0-9][a-z0-9.+-]*)=\$\{([A-Z_][A-Z0-9_]*)\}", body))
    return packages


def _read_toolchain_arguments(text: str) -> dict[str, str]:
    """Plain NAME=VALUE data only; never evaluate this file as shell code."""
    arguments = {}
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#") or not line.strip():
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([A-Za-z0-9.+:~_-]*)", line)
        if match is None:
            raise ValueError(f"toolchain args line {number}: expected plain NAME=VALUE "
                             "with no spaces, quotes or shell syntax")
        name, value = match.groups()
        if name in arguments:
            raise ValueError("duplicate toolchain argument: " + name)
        arguments[name] = value
    return arguments


def _toolchain_argument_problems(dockerfile: str, args_text: str) -> list[str]:
    stages = _dockerfile_stages(dockerfile)
    reached = _reachable_stages(stages, "hpo")
    default = _reachable_stages(stages)
    wheel_arguments = {"HPO_VERSION", "HPO_WHEEL_SHA256", "HPO_SDIST_SHA256"}
    declared = (_build_arguments(stages, reached) - _build_arguments(stages, default)
                - wheel_arguments)
    required = (_build_arguments(stages, reached, required=True)
                - _build_arguments(stages, default, required=True) - wheel_arguments)
    listed = set(_read_toolchain_arguments(args_text))
    problems = []
    if listed != declared:
        problems.append("toolchain argument names differ from HPO-only ARG declarations: "
                        + ", ".join(sorted(listed ^ declared)))
    if listed != required:
        problems.append("toolchain argument names differ from HPO-only required guards: "
                        + ", ".join(sorted(listed ^ required)))
    return problems


def _require_filled_toolchain_arguments(arguments: dict[str, str]) -> None:
    unfilled = sorted(name for name, value in arguments.items() if not value)
    if unfilled:
        raise unittest.SkipTest("unfilled toolchain versions: " + ", ".join(unfilled)
                                + "; TO BE FILLED BY THE BOX RUN")


class DockerfileTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (REPO / "docker" / "Dockerfile").read_bytes().decode("utf-8")
        cls.stages = _dockerfile_stages(cls.dockerfile)
        cls.named = {stage.name: stage for stage in cls.stages}

    def test_last_stage_is_a_no_op_release_alias_with_the_original_entrypoint(self):
        last = self.stages[-1]
        self.assertEqual(last.name, "default")
        self.assertEqual(last.base, "release")
        self.assertEqual(last.instructions, (("FROM", "release AS default"),))
        self.assertNotIn("ARG HPO_", last.text)
        self.assertNotIn("entrypoint-dispatch.sh", last.text)
        original = 'ENTRYPOINT ["/opt/pineforge/bin/entrypoint.sh"]'
        self.assertIn(original, self.named["release"].text)
        self.assertEqual(_inherited_instruction(self.stages, last.name, "ENTRYPOINT"),
                         original[len("ENTRYPOINT "):])
        self.assertEqual(_inherited_instruction(self.stages, last.name, "USER"), "10001")

    def test_default_reachability_has_no_declared_or_required_hpo_argument(self):
        reached = _reachable_stages(self.stages)
        self.assertEqual(reached, {"default", "release", "engine", "codegen"})
        self.assertEqual(_hpo_arguments(self.stages, reached), set())
        self.assertEqual(_hpo_arguments(self.stages, reached, required=True), set())

    def test_hpo_reaches_its_build_stages_and_requires_all_seven_arguments(self):
        reached = _reachable_stages(self.stages, "hpo")
        self.assertEqual(reached, {"hpo", "hpo-wheel", "hpo-toolchain", "hpo-native",
                                   "release", "engine", "codegen"})
        required = {"HPO_VERSION", "HPO_GCC_DEFAULTS_VERSION", "HPO_GCC_VERSION",
                    "HPO_BINUTILS_VERSION", "HPO_EIGEN_VERSION", "HPO_CMAKE_VERSION",
                    "HPO_MAKE_VERSION"}
        self.assertEqual(_hpo_arguments(self.stages, reached, required=True), required)
        self.assertEqual(_hpo_arguments(self.stages, reached), required | {"HPO_WHEEL_SHA256", "HPO_SDIST_SHA256"})
        for stage in self.stages:
            for instruction, body in stage.instructions:
                if instruction == "ARG" and body.startswith("HPO_"):
                    with self.subTest(stage=stage.name, argument=body.split("=", 1)[0]):
                        self.assertNotIn("=", body, "HPO arguments must have no defaults")
                        self.assertIn(stage.name, reached)

    # sha256 of the final stage of docker/Dockerfile as it was before the hpo stages were added (what a
    # plain `docker build` built then), kept the way _dockerfile_stages keeps a stage: from its FROM line
    # to the line before the next heading, bytes unchanged. It is pinned here, not read from git history,
    # because a shallow CI checkout and a main reached by a squash merge do not contain that commit.
    # Restore exactly the approved CR-150 harness list changes before checking this historical pin.
    ORIGINAL_RUNTIME_STAGE_SHA256 = "a632a13892deaeb350733b8ca8917b9889db69d0894a611498c3ab6da8c333a2"

    def test_release_stage_matches_original_runtime_after_notice_and_harness_changes(self):
        text = _without_sources_notice(_without_stage_alias(self.named["release"]))
        text = _restore_original_doc_cleanup(text)
        text = _restore_original_harness_manifest(text)
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         self.ORIGINAL_RUNTIME_STAGE_SHA256,
                         "after restoring the one /usr/share/doc token and removing the marked "
                         "notice and approved CR-150 harness changes, release must remain "
                         "byte for byte the original runtime stage")

    def test_original_harness_normalization_requires_both_new_modules(self):
        text = _without_sources_notice(_without_stage_alias(self.named["release"]))
        for name, copy_line in (
                ("pf_bars.py", "COPY docker/pf_bars.py                  /opt/pineforge/bin/pf_bars.py\n"),
                ("pf_sink.py", "COPY docker/pf_sink.py                  /opt/pineforge/bin/pf_sink.py\n")):
            self.assertEqual(text.count(copy_line), 1)
            self.assertEqual(text.count(" " + name + " "), 1)
            for scenario, altered in (
                    ("missing COPY", text.replace(copy_line, "", 1)),
                    ("duplicate COPY", text.replace(copy_line, copy_line + copy_line, 1)),
                    ("wrong destination", text.replace("/bin/" + name, "/bin/unexpected.py", 1)),
                    ("missing checksum name", text.replace(" " + name + " ", " ", 1))):
                with self.subTest(module=name, scenario=scenario):
                    self.assertNotEqual(altered, text)
                    with self.assertRaisesRegex(ValueError, "approved CR-150 harness manifest fragment"):
                        _restore_original_harness_manifest(altered)

    def test_original_doc_cleanup_normalization_requires_one_exact_line(self):
        text = _without_sources_notice(_without_stage_alias(self.named["release"]))
        line = "    && rm -rf /var/lib/apt/lists/* /usr/share/man /usr/share/locale /usr/share/info\n"
        self.assertEqual(text.count(line), 1)
        restored = _restore_original_doc_cleanup(text)
        self.assertEqual(restored, text.replace(" /usr/share/man", " /usr/share/doc /usr/share/man", 1))
        for scenario, altered in (
                ("missing", text.replace(line, "", 1)),
                ("duplicate", text + line),
                ("adjacent duplicate", text.replace(line, line + line, 1)),
                ("different leading whitespace", text.replace(line, " " + line, 1)),
                ("one other character changed", text.replace(line, line.replace("rm -rf", "rm  -rf"), 1))):
            with self.subTest(scenario=scenario):
                with self.assertRaisesRegex(ValueError, "expected exactly one apt cleanup line"):
                    _restore_original_doc_cleanup(altered)

    def test_notice_and_harness_changes_cannot_hide_other_instructions(self):
        text = _without_stage_alias(self.named["release"])
        stripped = _without_sources_notice(text)
        self.assertNotEqual(text, stripped)
        stripped = _restore_original_doc_cleanup(stripped)
        stripped = _restore_original_harness_manifest(stripped)
        self.assertNotEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                            self.ORIGINAL_RUNTIME_STAGE_SHA256)
        self.assertEqual(hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
                         self.ORIGINAL_RUNTIME_STAGE_SHA256)
        for name in ("release", "hpo"):
            stage = self.named[name].text
            _without_sources_notice(stage)  # also rejects missing/duplicate markers
            block = stage.split("# --- sources notice (begin)\n", 1)[1].split(
                "# --- sources notice (end)\n", 1)[0]
            instructions = _dockerfile_instructions(block)
            self.assertEqual([keyword for keyword, _ in instructions], ["RUN", "COPY", "LABEL", "RUN"])
            self.assertEqual(
                " ".join(instructions[0][1].split()),
                "set -eu; if [ -d /usr/share/doc ]; then "
                "find /usr/share/doc -type f ! -name copyright -delete; "
                "find /usr/share/doc -xtype l -delete; "
                "find /usr/share/doc -depth -type d -empty -delete; fi",
                name + ": doc cleanup must remove non-copyright files, then only broken links, then empty directories",
            )
            self.assertEqual(instructions[1][1], "docker/SOURCES.txt.in /tmp/pineforge-SOURCES.txt.in")
            labels = shlex.split(instructions[2][1])
            self.assertEqual({label.split("=", 1)[0] for label in labels},
                             {"io.pineforge.sources.notice", "io.pineforge.sources.url"})
            self.assertNotIn("/opt/pineforge/bin", block)
            self.assertNotIn("apt-get", block)
        # A change outside the block must still break the old immutable pin.
        altered = text.replace("RUN mkdir -p /opt/pineforge/bin\n",
                               "RUN mkdir -p /opt/pineforge/bin /unexpected\n", 1)
        self.assertNotEqual(altered, text)
        normalized = _restore_original_harness_manifest(
            _restore_original_doc_cleanup(_without_sources_notice(altered)))
        self.assertNotEqual(hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                            self.ORIGINAL_RUNTIME_STAGE_SHA256)

    def test_hpo_inherits_release_and_retains_its_runtime_contract(self):
        hpo = self.named["hpo"]
        self.assertEqual(hpo.base, "release")
        self.assertEqual([body for keyword, body in hpo.instructions if keyword == "USER"],
                         ["root", "10001"])
        self.assertEqual(_inherited_instruction(self.stages, "hpo", "ENTRYPOINT"),
                         '["/opt/pineforge/entrypoint-dispatch.sh"]')
        for text in ("PYTHONPATH=/opt/pineforge/pycodegen:/opt/pineforge/hpo",
                     "PINEFORGE_HPO_VERSION=${HPO_VERSION}",
                     "PINEFORGE_ENGINE_ROOT=/opt/pineforge",
                     "PINEFORGE_HPO_NATIVE=/opt/pineforge/pineforge-hpo-native",
                     'io.pineforge.hpo.version="${HPO_VERSION}"',
                     "COPY --from=hpo-wheel /opt/hpo",
                     "COPY --from=hpo-wheel /opt/hpo-licenses",
                     "COPY --from=hpo-toolchain /opt/pineforge/licenses",
                     "COPY --from=hpo-native /tmp/hpo-build/bin/pineforge-hpo-native",
                     "COPY --from=hpo-native /opt/hpo-native-licenses",
                     "sha256sum -c --strict LICENSE-SHA256SUMS",
                     "install -d -o 10001 /home/pineforge/.cache/pineforge/hpo"):
            with self.subTest(contract=text):
                self.assertIn(text, hpo.text)
        self.assertEqual(_pinned_packages(hpo), _pinned_packages(self.named["hpo-toolchain"]))
        self.assertEqual(set(_pinned_packages(self.named["hpo-native"])), {"cmake", "make"})
        wheel = self.named["hpo-wheel"].text
        self.assertIn('if [ -n "${HPO_WHEEL_SHA256}" ]', wheel)
        self.assertIn("sha256sum -c --strict /tmp/hpo-wheel.sha256", wheel)
        self.assertIn("python3 -I /tmp/check_hpo_resolution.py", wheel)
        self.assertIn('--target /opt/hpo "$wheel"', wheel)
        self.assertIn("--no-deps", wheel)

    def test_reachability_follows_added_copy_edges_in_the_real_dockerfile(self):
        last_header = self.stages[-1].text.splitlines()[0] + "\n"
        native_index = next(index for index, stage in enumerate(self.stages)
                            if stage.name == "hpo-native")
        for source, expected in (("hpo-wheel", {"hpo-wheel", "codegen"}),
                                 (str(native_index), {"hpo-native", "hpo-toolchain", "engine"})):
            with self.subTest(copy_source=source):
                mutated = self.dockerfile.replace(
                    last_header, last_header + f"COPY --from={source} /opt /probe\n", 1)
                stages = _dockerfile_stages(mutated)
                reached = _reachable_stages(stages)
                self.assertTrue(expected <= reached)
                self.assertIn("HPO_VERSION", _hpo_arguments(stages, reached, required=True))

    def test_reachability_follows_a_changed_from_edge_in_the_real_dockerfile(self):
        last_header = self.stages[-1].text.splitlines()[0]
        mutated = self.dockerfile.replace(last_header, "FROM hpo AS default", 1)
        stages = _dockerfile_stages(mutated)
        self.assertEqual(_reachable_stages(stages), _reachable_stages(stages, "hpo") | {"default"})
        self.assertIn("HPO_VERSION", _hpo_arguments(stages, _reachable_stages(stages), required=True))


class ToolchainArgumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        cls.args_text = (REPO / "docker" / "hpo-toolchain.args").read_text(encoding="utf-8")

    def test_plain_argument_file_matches_both_declared_args_and_required_guards(self):
        self.assertEqual(_toolchain_argument_problems(self.dockerfile, self.args_text), [])
        stages = _dockerfile_stages(self.dockerfile)
        for stage in stages:
            self.assertNotIn("hpo-toolchain.args", stage.text,
                             "the workflow reads this data file; Dockerfile stays build-arg driven")

    def test_argument_comments_name_the_packages_pinned_by_the_real_stages(self):
        stages = _dockerfile_stages(self.dockerfile)
        reached = _reachable_stages(stages, "hpo")
        packages = {}
        for stage in stages:
            if stage.name in reached:
                for package, argument in _pinned_packages(stage).items():
                    packages.setdefault(argument, set()).add(package)
        lines = self.args_text.splitlines()
        prefixes = ("# TO BE FILLED BY THE BOX RUN: Debian packages ", "# Debian packages ")
        for index, line in enumerate(lines):
            if line.startswith("#") or not line.strip():
                continue
            argument = line.split("=", 1)[0]
            with self.subTest(argument=argument):
                self.assertGreater(index, 0)
                prefix = next((p for p in prefixes if lines[index - 1].startswith(p)), None)
                self.assertIsNotNone(prefix, lines[index - 1])
                covered = set(lines[index - 1][len(prefix):].split(", "))
                self.assertEqual(covered, packages[argument])

    def test_adding_an_arg_only_to_the_real_dockerfile_is_rejected(self):
        self.assertEqual(_toolchain_argument_problems(self.dockerfile, self.args_text), [])
        hpo = next(stage for stage in _dockerfile_stages(self.dockerfile) if stage.name == "hpo")
        header = hpo.text.splitlines()[0] + "\n"
        # Check a bare declaration as well as one with a required guard: the
        # data file must track new ARGs even before their RUN is added.
        for argument in ("HPO_TEST_EXTRA_VERSION", "TOOLCHAIN_TEST_EXTRA_VERSION"):
            for guarded in (False, True):
                with self.subTest(argument=argument, guarded=guarded):
                    guard = 'RUN test -n "${' + argument + '}"\n' if guarded else ""
                    mutated = self.dockerfile.replace(
                        header, header + "ARG " + argument + "\n" + guard, 1)
                    problems = _toolchain_argument_problems(mutated, self.args_text)
                    self.assertTrue(problems)
                    self.assertIn(argument, "\n".join(problems))

    def test_adding_or_removing_an_arg_only_in_the_real_args_file_is_rejected(self):
        self.assertEqual(_toolchain_argument_problems(self.dockerfile, self.args_text), [])
        argument = next(iter(_read_toolchain_arguments(self.args_text)))
        missing = "\n".join(line for line in self.args_text.splitlines()
                            if not line.startswith(argument + "=")) + "\n"
        for mutated, changed in ((self.args_text + "HPO_TEST_EXTRA_VERSION=\n",
                                  "HPO_TEST_EXTRA_VERSION"), (missing, argument)):
            with self.subTest(argument=changed):
                problems = _toolchain_argument_problems(self.dockerfile, mutated)
                self.assertTrue(problems)
                self.assertIn(changed, "\n".join(problems))

    def test_spaces_quotes_shell_syntax_and_duplicate_names_are_rejected(self):
        argument = next(iter(_read_toolchain_arguments(self.args_text)))
        line = next(line for line in self.args_text.splitlines() if line.startswith(argument + "="))
        for invalid in (argument + " =", argument + "= ", argument + '=""',
                        argument + "=''", "export " + argument + "=",
                        argument + "=$(command)", argument + "=value;command",
                        argument, argument + "=value=other"):
            with self.subTest(line=invalid):
                mutated = self.args_text.replace(line + "\n", invalid + "\n", 1)
                with self.assertRaisesRegex(ValueError, "plain NAME=VALUE"):
                    _read_toolchain_arguments(mutated)
        with self.assertRaisesRegex(ValueError, "duplicate toolchain argument"):
            _read_toolchain_arguments(self.args_text + line + "\n")

    def test_empty_value_is_detected_as_an_unfilled_skip(self):
        arguments = _read_toolchain_arguments(self.args_text)
        argument = next(iter(arguments))
        arguments[argument] = ""
        with self.assertRaisesRegex(unittest.SkipTest, "unfilled toolchain versions") as skipped:
            _require_filled_toolchain_arguments(arguments)
        self.assertIn(argument, str(skipped.exception))

    def test_unfilled_toolchain_versions_wait_for_the_signed_box_run(self):
        # Shape and set errors must fail before an unfilled-value skip, so an
        # empty or malformed file can never make these schema checks green.
        self.assertEqual(_toolchain_argument_problems(self.dockerfile, self.args_text), [])
        _require_filled_toolchain_arguments(_read_toolchain_arguments(self.args_text))


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
                                 ["python3", "-m", "pineforge_hpo.cli", *arguments])

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
                    expected = (["python3", "-m", "pineforge_hpo.cli", "run"]
                                if arguments else ["entrypoint.sh"])
                    self.assertEqual(self._dispatch(arguments, status), expected)


class DockerfileHarnessTest(unittest.TestCase):
    HARNESS = ("bind_compiled_inventory.py", "entrypoint.sh", "pf_bars.py", "pf_sink.py", "request_feed_inventory.py",
               "run_execution_observer.py", "run_json.py", "run_phase_transport.py",
               "selected_window_plan.py", "selected_window_report.py")
    # The complete byte-check RUN is the contract, including all ten names
    # and the check for an extra bin/ file.
    CHECK = r'''RUN set -eu; \
    cd /opt/pineforge/bin; \
    want="bind_compiled_inventory.py entrypoint.sh pf_bars.py pf_sink.py request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py selected_window_plan.py selected_window_report.py"; \
    listed="$(sed 's/^[0-9a-f]\{64\}  //' HARNESS-SHA256SUMS | tr '\n' ' ' | sed 's/ $//')"; \
    present="$(LC_ALL=C ls -A | grep -v '^HARNESS-SHA256SUMS$' | tr '\n' ' ' | sed 's/ $//')"; \
    if [ "$listed" != "$want" ] || [ "$present" != "$want" ]; then \
      echo "ERROR: HARNESS-SHA256SUMS lists [${listed}] and bin/ holds [${present}]; both must be exactly [${want}]" >&2; exit 1; \
    fi; \
    sha256sum -c --strict HARNESS-SHA256SUMS; \
    echo "harness: the ten files match HARNESS-SHA256SUMS"'''

    def test_harness_copy_list_and_full_build_check_are_unchanged(self):
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        stages = _dockerfile_stages(dockerfile)
        release = next(stage for stage in stages if stage.name == "release")
        self.assertIn(release.name, _reachable_stages(stages, "hpo"))
        self.assertIn(release.name, _reachable_stages(stages))
        copies = re.findall(r"^COPY docker/(\S+)\s+/opt/pineforge/bin/(\S+)\s*$",
                            dockerfile, re.M)
        self.assertEqual(sorted(copies),
                         sorted((name, name) for name in self.HARNESS + ("HARNESS-SHA256SUMS",)))
        self.assertEqual(_run_block(release.text, "sha256sum -c --strict HARNESS-SHA256SUMS"),
                         self.CHECK)
        sums = (REPO / "docker" / "HARNESS-SHA256SUMS").read_text(encoding="utf-8")
        self.assertEqual([line.split("  ", 1)[1] for line in sums.splitlines()], list(self.HARNESS))
        self.assertIn('ENTRYPOINT ["/opt/pineforge/entrypoint-dispatch.sh"]', dockerfile)
        self.assertIn("PF_ABI_VERSION", _run_block(release.text, 'echo "harness ABI'))


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


class DispatcherModuleTest(unittest.TestCase):
    """The module the dispatcher runs must exist and be runnable in the real published wheel."""

    def test_dispatcher_module_is_runnable_in_the_real_wheel(self):
        wheel = os.environ.get("PINEFORGE_HPO_ACCEPTED_WHEEL")
        if not wheel or not Path(wheel).is_file():
            self.skipTest("supply PINEFORGE_HPO_ACCEPTED_WHEEL (a real hpo wheel file)")
        script = (REPO / "docker" / "entrypoint-dispatch.sh").read_text(encoding="utf-8")
        modules = re.findall(r"exec python3 -m (\S+)", script)
        self.assertEqual(len(modules), 1, modules)
        relative = modules[0].replace(".", "/")
        import zipfile
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            candidates = [relative + ".py", relative + "/__main__.py"]
            found = [name for name in candidates if name in names]
            self.assertEqual(len(found), 1, "the dispatcher's module " + modules[0] + " is not runnable in the wheel: " + str(candidates))
            self.assertIn('if __name__ == "__main__"', archive.read(found[0]).decode("utf-8"))


class ReadmePairRuleTest(unittest.TestCase):
    # SHA-256 of README.md lines 196-225 (the pair rule) as published on the hub's main when this change was made.
    # A checkout in CI has no origin/main, so the rule is pinned by digest; a deliberate change of the rule changes
    # this value in the same commit.
    PAIR_RULE_SHA256 = "35b72f7f5c7466cfc212a71eaf05df98f45e92411fd34a67bfff761556c61601"

    def test_lines_196_to_225_are_byte_identical_to_the_published_rule(self):
        lines = (REPO / "README.md").read_bytes().splitlines(keepends=True)
        self.assertGreaterEqual(len(lines), 225)
        self.assertEqual(hashlib.sha256(b"".join(lines[195:225])).hexdigest(), self.PAIR_RULE_SHA256)


if __name__ == "__main__":
    unittest.main()
