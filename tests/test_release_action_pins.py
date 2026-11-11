#!/usr/bin/env python3
"""Keep every action and reusable workflow under .github pinned to a commit."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

import yaml
from yaml.tokens import KeyToken, ScalarToken, ValueToken

ROOT = Path(__file__).resolve().parents[1]
MIN_USES = 63  # 62 remote actions plus one local reusable workflow after retirement.
VALID_SHA = "0123456789abcdef" * 2 + "01234567"
VALID_DIGEST = "0123456789abcdef" * 4

_REMOTE_PIN = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^/\s@]+)*@[0-9a-f]{40}"
)
_LOCAL = re.compile(r"\./[^\s@]+")
_DOCKER_PIN = re.compile(r"docker://[^\s@]+@sha256:[0-9a-fA-F]{64}")


def uses_entries(text: str) -> list[tuple[int, str]]:
    """Extract scalar uses fields and their physical source lines."""
    # Token marks preserve line numbers in block and flow mappings. The YAML
    # scanner also keeps punctuation inside plain scalars and decodes quoting,
    # without mistaking comments, names or shell block scalars for uses keys.
    try:
        tokens = list(yaml.scan(text, Loader=yaml.BaseLoader))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return [(mark.line + 1 if mark is not None else 1, f"invalid YAML: {exc}")]
    entries = []
    for index, token in enumerate(tokens[:-3]):
        name, separator, value = tokens[index + 1:index + 4]
        if (isinstance(token, KeyToken) and isinstance(name, ScalarToken)
                and name.value == "uses" and isinstance(separator, ValueToken)):
            # An empty, aliased or collection value is not one of the allowed
            # literal references and must still count as a finding.
            reference = value.value if isinstance(value, ScalarToken) else ""
            entries.append((name.start_mark.line + 1, reference))
    return entries


def validate_uses(text: str) -> list[tuple[int, str]]:
    """Return (line, reference) findings, with no filesystem or other effects."""
    findings = []
    for line_number, value in uses_entries(text):
        if value.startswith("./"):
            pinned = _LOCAL.fullmatch(value) is not None
        elif value.startswith("docker://"):
            pinned = _DOCKER_PIN.fullmatch(value) is not None
        else:
            pinned = _REMOTE_PIN.fullmatch(value) is not None
        if not pinned:
            findings.append((line_number, value))
    return findings


class ActionPinValidatorTest(unittest.TestCase):
    def test_rejects_unpinned_uses_at_their_actual_lines(self):
        rows = {
            "tag": ("  - uses: owner/repo@v4", "owner/repo@v4"),
            "branch": ("    uses: owner/repo@main", "owner/repo@main"),
            "short sha": ("  - uses: owner/repo@abc1234", "owner/repo@abc1234"),
            "uppercase sha": (f"  - uses: owner/repo@{VALID_SHA.upper()}",
                              f"owner/repo@{VALID_SHA.upper()}"),
            "39 hex": (f"  - uses: owner/repo@{VALID_SHA[:-1]}",
                       f"owner/repo@{VALID_SHA[:-1]}"),
            "41 hex": (f"  - uses: owner/repo@{VALID_SHA}0",
                       f"owner/repo@{VALID_SHA}0"),
            "non-hex": (f"  - uses: owner/repo@{'g' * 40}",
                        f"owner/repo@{'g' * 40}"),
            "single quoted tag": ("  - uses: 'owner/repo@v4'", "owner/repo@v4"),
            "double quoted tag": ('  - uses: "owner/repo@v4"', "owner/repo@v4"),
            "quoted tag with comment": ('  - uses: "owner/repo@v4" # v4',
                                        "owner/repo@v4"),
            "flow tag": ("  - {uses: owner/repo@v4}", "owner/repo@v4"),
            "later flow field": ('  - {name: Checkout, uses: "owner/repo@v4"}',
                                 "owner/repo@v4"),
            "quoted flow key": ('  - {"uses": "owner/repo@v4"}', "owner/repo@v4"),
            "docker tag": ("  - uses: docker://x:latest", "docker://x:latest"),
            "docker without digest": ("  - uses: docker://x", "docker://x"),
            "short docker digest": (f"  - uses: docker://x@sha256:{VALID_DIGEST[:-1]}",
                                    f"docker://x@sha256:{VALID_DIGEST[:-1]}"),
            "reusable workflow tag": (
                "    uses: owner/repo/.github/workflows/x.yml@v4",
                "owner/repo/.github/workflows/x.yml@v4",
            ),
            "missing ref": ("  - uses: owner/repo", "owner/repo"),
            "missing repository": (f"  - uses: owner@{VALID_SHA}",
                                   f"owner@{VALID_SHA}"),
            "local with ref": (f"  - uses: ./local@{VALID_SHA}",
                               f"./local@{VALID_SHA}"),
            "empty": ("  - uses: # missing value", ""),
            "attached hash": (f"  - uses: owner/repo@{VALID_SHA}#v4",
                              f"owner/repo@{VALID_SHA}#v4"),
            "plain scalar punctuation": (f"  - uses: owner/repo@{VALID_SHA},extra",
                                         f"owner/repo@{VALID_SHA},extra"),
            "hash inside quotes": (f'  - uses: "owner/repo@{VALID_SHA} # v4"',
                                   f"owner/repo@{VALID_SHA} # v4"),
        }
        for name, (row, reference) in rows.items():
            with self.subTest(format=name):
                text = "steps:\n  # Keep the physical line in findings.\n" + row + "\n"
                self.assertEqual(uses_entries(text), [(3, reference)])
                self.assertEqual(validate_uses(text), [(3, reference)])

    def test_accepts_only_full_pins_and_the_two_exemptions(self):
        rows = {
            "lowercase sha": f"  - uses: owner/repo@{VALID_SHA}",
            "commented sha": f"  - uses: owner/repo@{VALID_SHA} # v4.1.0",
            "single quoted sha": f"  - uses: 'owner/repo@{VALID_SHA}'",
            "double quoted sha": f'  - uses: "owner/repo@{VALID_SHA}"',
            "quoted sha with comment": f'  - uses: "owner/repo@{VALID_SHA}" # v4.1.0',
            "flow sha": f"  - {{uses: owner/repo@{VALID_SHA}, name: Checkout}}",
            "quoted flow sha": f"  - {{uses: 'owner/repo@{VALID_SHA}'}} # v4.1.0",
            "local": "  - uses: ./local",
            "quoted local": '  - uses: "./.github/actions/local"',
            "docker digest": f"  - uses: docker://x@sha256:{VALID_DIGEST}",
            "uppercase docker digest": f"  - uses: docker://x@sha256:{VALID_DIGEST.upper()}",
            "quoted docker digest": f'  - uses: "docker://x@sha256:{VALID_DIGEST}"',
            "reusable workflow sha": (
                f"    uses: owner/repo/.github/workflows/x.yml@{VALID_SHA}"
            ),
            "action subpath": f"  - uses: owner/repo/path/to/action@{VALID_SHA}",
        }
        for name, row in rows.items():
            with self.subTest(format=name):
                text = "steps:\n" + row + "\n"
                self.assertEqual(len(uses_entries(text)), 1)
                self.assertEqual(validate_uses(text), [])

    def test_comments_and_other_quoted_fields_are_not_uses(self):
        text = (
            "# uses: owner/repo@v4\n"
            "steps:\n"
            '  - name: "example, uses: owner/repo@main"\n'
            "  - {name: 'example, uses: owner/repo@v4'} # uses: owner/repo@main\n"
            "  - run: |\n"
            "      uses: owner/repo@v4\n"
        )
        self.assertEqual(uses_entries(text), [])
        self.assertEqual(validate_uses(text), [])

    def test_reports_every_flow_entry_on_its_line(self):
        text = (
            "steps:\n"
            "  [{uses: owner/first@v4}, {uses: owner/second@main}, "
            f"{{uses: owner/third@{VALID_SHA}}}]\n"
        )
        self.assertEqual(len(uses_entries(text)), 3)
        self.assertEqual(validate_uses(text), [
            (2, "owner/first@v4"), (2, "owner/second@main"),
        ])


class ReleaseActionPinsTest(unittest.TestCase):
    def test_all_github_yaml_uses_are_pinned(self):
        paths = sorted(
            path for path in (ROOT / ".github").rglob("*")
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        )
        count = 0
        findings = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            count += len(uses_entries(text))
            findings.extend(
                f"{path.relative_to(ROOT).as_posix()}:{line}: uses: {value!r} "
                "requires a lowercase 40-hex commit SHA, a local path without "
                "a ref, or a sha256-pinned Docker image"
                for line, value in validate_uses(text)
            )
        self.assertGreaterEqual(
            count, MIN_USES,
            f"saw only {count} uses entries across {len(paths)} .github YAML files; "
            f"expected at least {MIN_USES}",
        )
        self.assertEqual(findings, [], "\n".join(findings))

    def test_real_workflow_tag_mutation_is_reported(self):
        path = ROOT / ".github" / "workflows" / "publish.yml"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(validate_uses(text), [], "mutation needs a clean baseline")
        candidates = [
            (line, value) for line, value in uses_entries(text)
            if _REMOTE_PIN.fullmatch(value)
        ]
        self.assertTrue(candidates, "mutation needs a real remote commit pin")
        line_number, original = candidates[0]
        replacement = original.rsplit("@", 1)[0] + "@v4"
        lines = text.splitlines(keepends=True)
        self.assertEqual(lines[line_number - 1].count(original), 1)
        lines[line_number - 1] = lines[line_number - 1].replace(original, replacement, 1)
        mutated = "".join(lines)
        self.assertNotEqual(mutated, text, "the mutation must change the real workflow")
        self.assertEqual(len(uses_entries(mutated)), len(uses_entries(text)))
        self.assertEqual(validate_uses(mutated), [(line_number, replacement)])


if __name__ == "__main__":
    unittest.main()
