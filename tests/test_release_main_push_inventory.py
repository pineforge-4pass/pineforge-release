#!/usr/bin/env python3
"""Every test workflow for pushes to main must run for every tracked input."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import unittest


REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"


def unevaluated_form(patterns: list[str]) -> str | None:
    return next(
        (pattern for pattern in patterns if any(char in pattern for char in "?+[]!")),
        None,
    )


def path_matches(pattern: str, path: str) -> bool:
    """Match exact characters, * and **; decline other filter forms."""
    if unevaluated_form([pattern]) is not None:
        return False
    parts = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern[index:index + 2] == "**":
            if (
                pattern[index:index + 3] == "**/"
                and (index == 0 or pattern[index - 1] == "/")
            ):
                parts.append("(?:.*/)?")
                index += 3
            else:
                parts.append(".*")
                index += 2
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(char))
            index += 1
    return re.fullmatch("".join(parts), path, flags=re.DOTALL) is not None


def push_applies_to_main(push: dict) -> bool:
    """Never skip a push because a branch form cannot be evaluated."""
    if "branches" in push:
        branches = push["branches"]
        return isinstance(branches, list) and any(
            unevaluated_form([branch]) is not None or path_matches(branch, "main")
            for branch in branches
        )
    ignored = push.get("branches-ignore", [])
    return isinstance(ignored, list) and not any(
        path_matches(branch, "main") for branch in ignored
    )


def matches_any(patterns: list[str], path: str) -> bool:
    return any(path_matches(pattern, path) for pattern in patterns)


def unittest_steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if "unittest discover" in step.get("run", "")
    ]


def check_main_push_inventory(
    workflows: dict[str, dict], tracked_files: list[str],
) -> tuple[set[str], list[str]]:
    """Return examined workflow names and exact path-filter violations."""
    examined = set()
    violations = []
    for name, workflow in sorted(workflows.items()):
        if not unittest_steps(workflow):
            continue
        triggers = workflow.get("on", {})
        if not isinstance(triggers, dict):
            continue
        push = triggers.get("push")
        if not isinstance(push, dict):
            continue
        if not push_applies_to_main(push):
            continue
        examined.add(name)
        for key in ("paths", "paths-ignore"):
            if key not in push:
                continue
            patterns = push[key] if isinstance(push[key], list) else []
            cause = f"{name}: push to main must not define {key}"
            pattern = unevaluated_form(patterns)
            if pattern is not None:
                details = (
                    f"pattern '{pattern}' uses a form this check does not evaluate; "
                    "omitted files are not listed"
                )
            else:
                omitted = [
                    path for path in sorted(set(tracked_files))
                    if matches_any(patterns, path) == (key == "paths-ignore")
                ][:5]
                if omitted:
                    details = "; ".join(
                        f"a push to main that changes only {path} would not run {name}"
                        for path in omitted
                    )
                else:
                    details = "path filters are forbidden even when no omitted tracked file is found"
            violations.append(f"{cause}; {details}")
    return examined, violations


def guard_problems(workflows: dict[str, dict], examined: set[str]) -> list[str]:
    problems = []
    for name in ("python-test.yml", "facts-validate.yml"):
        if name not in examined:
            problems.append(f"{name}: workflow was not examined")
    name = "facts-validate.yml"
    triggers = workflows.get(name, {}).get("on")
    if not isinstance(triggers, dict):
        problems.append(f"{name}: on must be a mapping")
        return problems
    push = triggers.get("push")
    if not isinstance(push, dict):
        problems.append(f"{name}: push must be a mapping")
        return problems
    branches = push.get("branches")
    if not isinstance(branches, list):
        problems.append(f"{name}: push.branches must be a list")
    elif "main" not in branches:
        problems.append(f"{name}: push.branches must include main")
    return problems


def load_workflows() -> dict[str, dict]:
    import yaml

    return {
        path.name: yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for path in sorted(WORKFLOWS.glob("*.yml"))
    }


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO)
    return sorted(os.fsdecode(path) for path in output.split(b"\0") if path)


class MainPushInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = load_workflows()
        cls.examined, cls.violations = check_main_push_inventory(
            cls.workflows, tracked_files(),
        )

    def test_main_push_test_workflows_have_no_path_filters(self):
        self.assertEqual(self.violations, [], "\n".join(self.violations))

    def test_path_filter_matching(self):
        cases = [
            (["**"], "docker/nested/input.py", True),
            (["**/*.py"], "input.py", True),
            (["**/*.py"], "docker/nested/input.py", True),
            (["docker/*"], "docker/input.py", True),
            (["docker/*"], "docker/nested/input.py", False),
            (["x/**"], "x/a/b.txt", True),
            (["x/**"], "other/x/a.txt", False),
            (["README.md"], "README.md", True),
            (["README.md"], "docs/README.md", False),
            (["README.md"], "READMEXmd", False),
            (["facts/**", "docs/*.md"], "docker/input.py", False),
            (["facts/**", "docs/*.md"], "docs/guide.md", True),
            ([], "facts/facts.json", False),
        ]
        for patterns, path, expected in cases:
            with self.subTest(patterns=patterns, path=path):
                self.assertEqual(matches_any(patterns, path), expected)
        form_cases = [
            (["x/?.py"], "x/?.py"),
            (["a+b"], "a+b"),
            (["a[1]"], "a[1]"),
            (["!x/**"], "!x/**"),
            (["a]"], "a]"),
            (["x/**"], None),
            (["**/*.py"], None),
            (["docs/*.md"], None),
            (["README.md"], None),
        ]
        for patterns, expected in form_cases:
            with self.subTest(patterns=patterns):
                self.assertEqual(unevaluated_form(patterns), expected)

    def test_expected_test_workflows_are_examined(self):
        problems = guard_problems(self.workflows, self.examined)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_guard_reports_exact_problems(self):
        cases = [
            (
                {"on": {"push": {"branches": ["main"]}}},
                {"python-test.yml", "facts-validate.yml"},
                [],
            ),
            (
                {},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: on must be a mapping"],
            ),
            (
                {"on": ["push"]},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: on must be a mapping"],
            ),
            (
                {"on": {}},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: push must be a mapping"],
            ),
            (
                {"on": {"push": "main"}},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: push must be a mapping"],
            ),
            (
                {"on": {"push": {}}},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: push.branches must be a list"],
            ),
            (
                {"on": {"push": {"branches": "main"}}},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: push.branches must be a list"],
            ),
            (
                {"on": {"push": {"branches": ["other"]}}},
                {"python-test.yml", "facts-validate.yml"},
                ["facts-validate.yml: push.branches must include main"],
            ),
            (
                {"on": {"push": {"branches": ["main"]}}},
                {"facts-validate.yml"},
                ["python-test.yml: workflow was not examined"],
            ),
            (
                {"on": {"push": {"branches": ["main"]}}},
                {"python-test.yml"},
                ["facts-validate.yml: workflow was not examined"],
            ),
        ]
        for facts, examined, expected in cases:
            with self.subTest(workflow=facts, examined=examined):
                workflows = {"python-test.yml": {}, "facts-validate.yml": facts}
                self.assertEqual(guard_problems(workflows, examined), expected)

    def test_checker_reports_exact_causes(self):
        files = ["src/input.py", "docs/guide.md", "docker/input.py"]
        cases = [
            (
                {"branches": ["main"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches": ["main"], "paths-ignore": ["docs/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths-ignore; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches": ["main"], "paths": ["**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "path filters are forbidden even when no omitted tracked file is found"
                ],
            ),
            (
                {"branches": ["main"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [],
            ),
            (
                {"branches": ["m[a]in"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches": ["m[a]in"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [],
            ),
            (
                {"branches": ["other"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                set(),
                [],
            ),
            (
                {"branches": ["main"], "paths": ["src/**"]},
                "python3 scripts/example.py",
                set(),
                [],
            ),
            (
                {"paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches": ["**"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches-ignore": ["main"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                set(),
                [],
            ),
            (
                {"branches-ignore": ["other"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches-ignore": ["m[a]in"], "paths": ["src/**"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "a push to main that changes only docker/input.py would not run example.yml; "
                    "a push to main that changes only docs/guide.md would not run example.yml"
                ],
            ),
            (
                {"branches": ["main"], "paths": ["x/?.py"]},
                "python3 -m unittest discover -s tests",
                {"example.yml"},
                [
                    "example.yml: push to main must not define paths; "
                    "pattern 'x/?.py' uses a form this check does not evaluate; "
                    "omitted files are not listed"
                ],
            ),
        ]
        for push, run, examined, violations in cases:
            with self.subTest(push=push, run=run):
                workflow = {
                    "on": {"push": push},
                    "jobs": {"tests": {"steps": [{"run": run}]}},
                }
                self.assertEqual(
                    check_main_push_inventory({"example.yml": workflow}, files),
                    (examined, violations),
                )


if __name__ == "__main__":
    unittest.main()
