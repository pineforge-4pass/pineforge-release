#!/usr/bin/env python3
"""Run the named parity assertion controls on isolated source copies."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    relative: str
    old: str
    new: str
    test_file: str
    test_name: str
    row: str
    assertion_message: str
    subtest_row: str | None = None

    @property
    def test_id(self):
        return Path(self.test_file).stem + "." + self.test_name


MUTANTS = (
    Mutation(
        "w2-commit-pinned-source-read",
        "scripts/parity_bundle.py",
        """    payload = {row["destination"]: read_content_blob(client, commit, row["source"])
               for row in manifest["files"]}
""",
        """    payload = {row["destination"]: read_content_blob(client, "b" * 40, row["source"])
               for row in manifest["files"]}
""",
        "tests/test_parity_bundle.py",
        "ParityBundleTests.test_source_bundle_reads_are_bound_to_resolved_commit",
        "commit-pinned source read",
        "False is not true : commit-pinned source read",
    ),
    Mutation(
        "w2-tagged-tree-blob-identity",
        "scripts/parity_bundle.py",
        '        require(git_blob(files[path]) == row["sha"], "parity payload blob digest differs")',
        '        require(True, "parity payload blob digest differs")',
        "tests/test_parity_bundle.py",
        "ParityBundleTests.test_negative_rows_have_a_green_control_and_fail_closed",
        "bad_tree_hash",
        "Invalid not raised : bad_tree_hash",
        "bad_tree_hash",
    ),
    Mutation(
        "w2-local-payload-identity",
        "scripts/parity_bundle.py",
        """    require(all(local[name] == raw for name, raw in source.payload.items()),
            "parity source bytes differ")
""",
        '    require(True, "parity source bytes differ")\n',
        "tests/test_parity_source_proof.py",
        "ParitySourceProofTests.test_negative_rows_have_a_green_control_and_refuse",
        "local_payload_mismatch",
        "Invalid not raised : local_payload_mismatch",
        "local_payload_mismatch",
    ),
    Mutation(
        "w3-descriptor-engine-commit",
        "scripts/parity_bundle.py",
        """    require(descriptor["engine_commit"] == source.engine_commit,
            "parity descriptor commit differs from engine tag")
""",
        '    require(True, "parity descriptor commit differs from engine tag")\n',
        "tests/test_parity_source_proof.py",
        "ParitySourceProofTests.test_negative_rows_have_a_green_control_and_refuse",
        "wrong_source_commit",
        "Invalid not raised : wrong_source_commit",
        "wrong_source_commit",
    ),
    Mutation(
        "w4-pair-parity-identity",
        "scripts/release_contract.py",
        """        require(manifest["versions"]["engine"]["parity"] == candidate["versions"]["engine"]["parity"],
                "parity identity differs from manifest")
""",
        '        require(True, "parity identity differs from manifest")\n',
        "tests/test_release_parity_wire.py",
        "ParityWireTests.test_manifest_identity_mismatch_refuses_before_aliases",
        "pair_payload parity identity",
        "Invalid not raised : pair_payload parity identity",
    ),
    Mutation(
        "w4-finalize-parity-identity",
        "scripts/release_pipeline.py",
        """        require(event["versions"]["engine"]["parity"] == candidate["versions"]["engine"]["parity"],
                "parity identity differs from manifest")
""",
        '        require(True, "parity identity differs from manifest")\n',
        "tests/test_release_parity_wire.py",
        "ParityWireTests.test_manifest_identity_mismatch_refuses_before_aliases",
        "finalize parity identity",
        "Invalid not raised : finalize parity identity",
    ),
)


class AssertionResult(unittest.TextTestResult):
    """Record unittest's exception classification, not rendered output matches."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = []
        self.successes = []
        self.assertion_failures = []

    def startTest(self, test):
        self.started.append(test.id())
        super().startTest(test)

    def addSuccess(self, test):
        self.successes.append(test.id())
        super().addSuccess(test)

    def record_assertion(self, test, error, params):
        frames = []
        for frame in traceback.extract_tb(error[2]):
            try:
                relative = Path(frame.filename).relative_to(ROOT).as_posix()
            except ValueError:
                continue
            frames.append({"file": relative, "function": frame.name})
        self.assertion_failures.append({
            "test_id": test.id(),
            "params": params,
            "exception_type": error[0].__module__ + "." + error[0].__qualname__,
            "assertion": issubclass(error[0], test.failureException),
            "message": str(error[1]),
            "frames": frames,
        })

    def addFailure(self, test, error):
        self.record_assertion(test, error, {})
        super().addFailure(test, error)

    def addSubTest(self, test, subtest, error):
        if error is not None and issubclass(error[0], test.failureException):
            self.record_assertion(test, error, dict(subtest.params))
        super().addSubTest(test, subtest, error)

    def report(self):
        return {
            "tests_run": self.testsRun,
            "started": self.started,
            "successes": self.successes,
            "failures": self.assertion_failures,
            "failure_count": len(self.failures),
            "errors": [test.id() for test, _ in self.errors],
            "skips": [test.id() for test, _ in self.skipped],
            "expected_failures": [test.id() for test, _ in self.expectedFailures],
            "unexpected_successes": [test.id() for test in self.unexpectedSuccesses],
        }


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_case(test_id, result_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "tests"))
    suite = unittest.TestLoader().loadTestsFromName(test_id)
    result = unittest.TextTestRunner(verbosity=2, resultclass=AssertionResult).run(suite)
    result_path.write_text(json.dumps(result.report(), sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def source_copy(destination):
    shutil.copytree(
        ROOT, destination,
        ignore=shutil.ignore_patterns(
            ".git", ".agents", ".codex", ".aws", "__pycache__", "*.pyc", "build*", "corpus"))


def child_result(copy_root, spec, result_path, phase):
    done = subprocess.run(
        [sys.executable, str(copy_root / "tests/run_parity_mutations.py"),
         "--case", spec.test_id, "--result", str(result_path)],
        cwd=copy_root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True,
    )
    print(f"=== {spec.name}: {phase}, row {spec.row} ===", flush=True)
    if done.stdout:
        print(done.stdout, end="", flush=True)
    if done.stderr:
        print(done.stderr, end="", file=sys.stderr, flush=True)
    try:
        report = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AssertionError(f"{spec.name}: no complete unittest result; no kill credited") from error
    require(type(report) is dict
            and type(report.get("tests_run")) is int and report["tests_run"] == 1
            and report.get("started") == [spec.test_id],
            f"{spec.name}: the named test did not run exactly once; no kill credited")
    for field in ("errors", "skips", "expected_failures", "unexpected_successes"):
        require(report.get(field) == [],
                f"{spec.name}: {phase} has {field}; no kill credited")
    return done.returncode, report


def run_mutant(spec):
    with tempfile.TemporaryDirectory(prefix="pineforge-parity-mutant-") as temporary:
        copy_root = Path(temporary) / "repo"
        source_copy(copy_root)
        path = copy_root / spec.relative
        source = path.read_text(encoding="utf-8")
        require(source.count(spec.old) == 1,
                f"{spec.name}: mutation anchor occurs {source.count(spec.old)} times")
        mutant = source.replace(spec.old, spec.new, 1)
        require(mutant != source, f"{spec.name}: replacement did not change the source")

        # Run the actual named test on unchanged source first. Errors, skips or
        # an empty suite cannot establish this control, regardless of exit code.
        status, control = child_result(
            copy_root, spec, Path(temporary) / "positive.json", "positive control")
        require(status == 0 and control.get("successes") == [spec.test_id]
                and control.get("failures") == [] and control.get("failure_count") == 0,
                f"{spec.name}: positive control did not pass; no kill credited")

        path.write_text(mutant, encoding="utf-8")
        status, result = child_result(
            copy_root, spec, Path(temporary) / "mutant.json", "mutated source")
        failures = result.get("failures")
        require(status == 1 and result.get("successes") == []
                and result.get("failure_count") == 1
                and type(failures) is list and len(failures) == 1,
                f"{spec.name}: expected one named assertion failure; no kill credited")
        failure = failures[0]
        params = {"row": spec.subtest_row} if spec.subtest_row is not None else {}
        frame = {"file": spec.test_file, "function": spec.test_name.split(".")[-1]}
        require(type(failure) is dict and failure.get("test_id") == spec.test_id
                and failure.get("params") == params
                and failure.get("exception_type") == "builtins.AssertionError"
                and failure.get("assertion") is True
                and failure.get("message") == spec.assertion_message
                and frame in failure.get("frames", []),
                f"{spec.name}: failure is not the named actual assertion; no kill credited")
        print(f"{spec.name}: named assertion killed the mutant after a passing control", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--case")
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    if args.case is not None:
        if args.result is None:
            parser.error("--result is required with --case")
        return run_case(args.case, args.result)
    if args.result is not None:
        parser.error("--result requires --case")
    for spec in MUTANTS:
        run_mutant(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
