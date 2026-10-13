#!/usr/bin/env python3
"""Source mirror refusal cases, authored before the guard; run on the spot box.

set-02.tsv is the real 84-source input. Binary rows and source archive bytes
below are explicitly test fixtures, never evidence of a published mirror.
Expected hashes are computed here from those bytes, independently of the CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_release_hpo_image import (
    _build_arguments, _dockerfile_stages, _reachable_stages,
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_sources.py"
FIXTURES = REPO / "tests" / "fixtures"
REGISTRY_HEADER = "set-id\trelease-tag\tn-pairs\tsources-list-sha256\tlist-asset\n"
NOTICE_PATH = "/usr/share/doc/pineforge/SOURCES.txt"
RELEASES_URL = "https://github.com/pineforge-4pass/pineforge-release/releases"


def _cli(*arguments: str):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *arguments], cwd=REPO,
        env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10,
    )


def _fixture_pairs() -> list[tuple[str, str]]:
    rows = (FIXTURES / "set-02.tsv").read_text(encoding="utf-8").splitlines()
    return sorted({tuple(line.split("\t")[:2]) for line in rows
                   if line and not line.startswith("#")})


def _inventory(pairs: list[tuple[str, str]], arch: str = "amd64") -> str:
    # One binary per source, plus 66 separate binary outputs from the same
    # sources: a 150-row dpkg inventory when all 84 real pairs are supplied.
    rows = []
    for source, version in pairs:
        # gcc's binary version differs from gcc-defaults' source version.
        # This prevents a parser using column 2 instead of column 4 from
        # accidentally passing the real-pair fixture checks.
        binary, binary_version = (("gcc", "4:12.2.0-3") if source == "gcc-defaults"
                                  else (source + "-bin", version))
        rows.append(f"{binary}:{arch}\t{binary_version}\t{source}\t{version}\n")
    rows += [f"{source}-data\t{version}\t{source}\t{version}\n"
             for source, version in pairs[:max(0, 150 - len(rows))]]
    return "".join(rows)


def _set_id(pairs: list[tuple[str, str]]) -> str:
    canonical = "".join(f"{source}\t{version}\n" for source, version in sorted(set(pairs)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SourceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pairs = _fixture_pairs()
        self.assertEqual(len(self.pairs), 84)
        self.assertEqual(len({source for source, _ in self.pairs}), 84)
        self.amd64 = self.root / "amd64.tsv"
        self.arm64 = self.root / "arm64.tsv"
        self.amd64.write_text(_inventory(self.pairs), encoding="utf-8")
        self.arm64.write_text(_inventory(self.pairs, "arm64"), encoding="utf-8")
        self.assertEqual(len(self.amd64.read_text().splitlines()), 150)
        self.registry = self.root / "SOURCE-SETS.tsv"
        self.sources_list = self.root / "set-02.SOURCES.list"
        self.assets_file = self.root / "assets.json"
        self.assets = []
        self.list_lines = []
        for source, version in self.pairs:
            name = f"set-02.{source}.fixture.tar"
            data = f"TEST FIXTURE ONLY: {source}\t{version}\n".encode("utf-8")
            (self.root / name).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.list_lines.append(f"{digest}  {len(data)}  {name}  {source}  {version}\n")
            self.assets.append({"name": name, "size": len(data), "digest": f"sha256:{digest}"})
        self.sources_list.write_text("".join(self.list_lines), encoding="utf-8")
        self.refresh_list_metadata()
        self.write_registry()

    def refresh_list_metadata(self):
        data = self.sources_list.read_bytes()
        self.assets = [item for item in self.assets if item["name"] != self.sources_list.name]
        self.assets.append({"name": self.sources_list.name, "size": len(data),
                            "digest": "sha256:" + hashlib.sha256(data).hexdigest()})

    def write_registry(self, pairs=None, count=None, digest=None):
        pairs = self.pairs if pairs is None else pairs
        count = len(pairs) if count is None else count
        digest = hashlib.sha256(self.sources_list.read_bytes()).hexdigest() if digest is None else digest
        self.registry.write_text(
            REGISTRY_HEADER + f"{_set_id(pairs)}\tv1.5.0\t{count}\t{digest}\t{self.sources_list.name}\n",
            encoding="utf-8",
        )

    def check(self, assets=None):
        self.assets_file.write_text(json.dumps({"assets": self.assets if assets is None else assets}),
                                    encoding="utf-8")
        return _cli("check", "--registry", str(self.registry),
                    "--amd64", str(self.amd64), "--arm64", str(self.arm64),
                    "--release-assets", str(self.assets_file),
                    "--sources-list", str(self.sources_list))

    def refused(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "", "a refusal must never emit a successful set-id")
        self.assertIn(message, result.stderr)

    def test_01_no_registered_set_is_refused(self):
        self.registry.write_text(REGISTRY_HEADER + "# no mirrors yet\n", encoding="utf-8")
        self.refused(self.check(), f"source set {_set_id(self.pairs)} is not in registry")

    def test_02_registered_set_without_list_asset_is_refused(self):
        assets = [item for item in self.assets if item["name"] != self.sources_list.name]
        self.refused(self.check(assets), "missing release asset: set-02.SOURCES.list")
        self.refused(self.check([]), "missing release asset: set-02.SOURCES.list")

    def test_03_one_named_source_asset_missing_is_refused(self):
        name = self.assets[0]["name"]
        self.refused(self.check(self.assets[1:]), f"missing release asset: {name}")

    def test_04_wrong_source_asset_digest_is_refused(self):
        item = self.assets[0]
        item["digest"] = "sha256:" + hashlib.sha256(b"a different fixture").hexdigest()
        self.refused(self.check(), f"digest mismatch for {item['name']}")

    def test_05_wrong_source_asset_size_is_refused(self):
        item = self.assets[0]
        item["size"] += 1
        self.refused(self.check(), f"size mismatch for {item['name']}")

    def test_06_image_pair_absent_from_list_is_refused(self):
        pairs = self.pairs + [("fixture-extra", "1.0-1")]
        self.amd64.write_text(_inventory(pairs), encoding="utf-8")
        self.arm64.write_text(_inventory(pairs, "arm64"), encoding="utf-8")
        # Register the NEW image id: this must fail at list coverage, not lookup.
        self.write_registry(pairs)
        self.refused(self.check(), "only in image (first 5): fixture-extra\t1.0-1")

    def test_07_architecture_source_version_difference_is_refused(self):
        pairs = self.pairs.copy()
        pairs[0] = (pairs[0][0], "99.0-1")
        self.arm64.write_text(_inventory(pairs, "arm64"), encoding="utf-8")
        result = self.check()
        self.refused(result, "amd64 and arm64 source pair sets differ")
        self.assertIn("only in amd64 (first 5): acl\t2.3.1-3", result.stderr)
        self.assertIn("only in arm64 (first 5): acl\t99.0-1", result.stderr)

    def test_08_inventory_with_ten_sources_is_refused(self):
        self.amd64.write_text(_inventory(self.pairs[:10]), encoding="utf-8")
        self.refused(self.check(), "has 10 distinct source packages; need at least 50")

    def test_09_list_bytes_not_matching_registered_hash_are_refused(self):
        self.sources_list.write_bytes(self.sources_list.read_bytes() + b"# altered list\n")
        self.refused(self.check(), "SOURCES.list SHA-256 does not match registry")

    def test_10_list_asset_digest_or_size_mismatch_is_refused(self):
        item = self.assets[-1]
        original = item.copy()
        item["digest"] = "sha256:" + hashlib.sha256(b"wrong list metadata").hexdigest()
        self.refused(self.check(), f"digest mismatch for {item['name']}")
        item.update(original)
        item["size"] += 1
        self.refused(self.check(), f"size mismatch for {item['name']}")

    def test_11_wrong_registry_pair_count_is_refused(self):
        self.write_registry(count=83)
        self.refused(self.check(), "registry pair count 83 does not match image pair count 84")

    def test_12_duplicate_release_asset_name_is_refused(self):
        self.assets.append(self.assets[0].copy())
        self.refused(self.check(), f"duplicate release asset: {self.assets[0]['name']}")

    def test_13_extra_list_pair_is_refused_even_when_all_assets_exist(self):
        data = b"TEST FIXTURE ONLY: extra source\n"
        digest = hashlib.sha256(data).hexdigest()
        self.sources_list.write_text(
            "".join(self.list_lines) + f"{digest}  {len(data)}  extra.fixture.tar  fixture-extra  1.0-1\n",
            encoding="utf-8",
        )
        self.assets.append({"name": "extra.fixture.tar", "size": len(data), "digest": "sha256:" + digest})
        self.refresh_list_metadata()
        self.write_registry()
        self.refused(self.check(), "only in list (first 5): fixture-extra\t1.0-1")

    def test_14_missing_or_non_sha256_asset_digest_is_refused(self):
        for digest in (None, "", "md5:invalid"):
            with self.subTest(digest=digest):
                self.assets[0]["digest"] = digest
                self.refused(self.check(), f"digest mismatch for {self.assets[0]['name']}")

    def test_15_architecture_difference_lists_only_first_five_per_side(self):
        pairs = [(source, "99.0-1") if index < 6 else (source, version)
                 for index, (source, version) in enumerate(self.pairs)]
        self.arm64.write_text(_inventory(pairs, "arm64"), encoding="utf-8")
        result = self.check()
        self.refused(result, "amd64 and arm64 source pair sets differ")
        for source, version in self.pairs[:5]:
            self.assertIn(f"{source}\t{version}", result.stderr)
            self.assertIn(f"{source}\t99.0-1", result.stderr)
        self.assertNotIn("base-files\t", result.stderr)

    def test_16_many_versions_of_only_ten_sources_are_still_refused(self):
        pairs = [(source, f"{index}.0-1") for source, _ in self.pairs[:10] for index in range(6)]
        self.amd64.write_text(_inventory(pairs), encoding="utf-8")
        self.refused(self.check(), "has 10 distinct source packages; need at least 50")

    def test_90_complete_mirror_passes_and_prints_the_independent_set_id(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"set-id={_set_id(self.pairs)}\n")
        self.assertEqual(result.stderr, "")
        # gh's assets object and a bare asset array must both work.
        self.assets_file.write_text(json.dumps(self.assets), encoding="utf-8")
        result = _cli("check", "--registry", str(self.registry),
                      "--amd64", str(self.amd64), "--arm64", str(self.arm64),
                      "--release-assets", str(self.assets_file),
                      "--sources-list", str(self.sources_list))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"set-id={_set_id(self.pairs)}\n")

    def test_91_changing_one_digest_byte_flips_the_verdict(self):
        before = self.check()
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        item = self.assets[0]
        digest = item["digest"]
        item["digest"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        self.refused(self.check(), f"digest mismatch for {item['name']}")

    def test_92_pairs_are_unique_sorted_and_hashed_with_a_final_newline(self):
        self.assertIn("gcc:amd64\t4:12.2.0-3\tgcc-defaults\t1.203\n",
                      self.amd64.read_text(encoding="utf-8"))
        self.amd64.write_text("".join(reversed(self.amd64.read_text().splitlines(keepends=True))),
                              encoding="utf-8")
        result = _cli("pairs", str(self.amd64))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = "".join(f"{source}\t{version}\n" for source, version in self.pairs)
        self.assertEqual(result.stdout, expected + f"set-id={_set_id(self.pairs)}\n")

    def test_93_pairs_refuses_malformed_inventory_rows(self):
        for row in ("binary\t1\tsource\n", "binary\t1\tsource\t\n",
                    "binary\t1\tsource\t1\textra\n", "\n"):
            with self.subTest(row=row):
                self.amd64.write_text(row, encoding="utf-8")
                self.refused(_cli("pairs", str(self.amd64)), "inventory line 1: expected 4 non-empty columns")

    def test_94_locate_emits_the_registered_download_coordinates(self):
        result = _cli("locate", "--registry", str(self.registry),
                      "--amd64", str(self.amd64), "--arm64", str(self.arm64))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(fields["set-id"], _set_id(self.pairs))
        self.assertEqual(fields["release-tag"], "v1.5.0")
        self.assertEqual(fields["list-asset"], "set-02.SOURCES.list")

    def test_95_repository_registry_has_the_documented_header(self):
        lines = (REPO / "docker" / "SOURCE-SETS.tsv").read_text(encoding="utf-8").splitlines()
        data = [line for line in lines if line and not line.startswith("#")]
        self.assertTrue(data, "the registry must retain its header when the box adds real rows")
        self.assertEqual(data[0], REGISTRY_HEADER.rstrip("\n"))

    def test_96_multiple_assets_for_one_source_pair_keep_the_same_set_id(self):
        source, version = next(pair for pair in self.pairs if ":" in pair[1])
        name = f"set-02.{source}_{version}.orig.fixture.tar"
        data = b"TEST FIXTURE ONLY: a second source component\n"
        (self.root / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.sources_list.write_text(
            "".join(self.list_lines) + f"{digest}  {len(data)}  {name}  {source}  {version}\n",
            encoding="utf-8",
        )
        self.assets.append({"name": name, "size": len(data), "digest": "sha256:" + digest})
        self.refresh_list_metadata()
        self.write_registry()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"set-id={_set_id(self.pairs)}\n")


class ToolchainCliTest(unittest.TestCase):
    def test_real_file_output_exactly_matches_real_hpo_declarations_and_guards(self):
        path = REPO / "docker" / "hpo-toolchain.args"
        result = _cli("toolchain-args", str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected_lines = [line for line in path.read_text().splitlines()
                          if line and not line.startswith("#")]
        self.assertEqual(result.stdout, "\n".join(expected_lines) + "\n")
        names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}
        stages = _dockerfile_stages((REPO / "docker" / "Dockerfile").read_text())
        hpo = _reachable_stages(stages, "hpo")
        ordinary = _reachable_stages(stages)
        for required in (False, True):
            required_names = (_build_arguments(stages, hpo, required=required)
                              - _build_arguments(stages, ordinary, required=required)
                              - {"HPO_VERSION", "HPO_WHEEL_SHA256"})
            self.assertEqual(names, required_names)

    def test_mutated_argument_files_are_refused_with_line_numbers(self):
        original = (REPO / "docker" / "hpo-toolchain.args").read_text()
        lines = original.splitlines()
        number = next(index for index, line in enumerate(lines, 1) if line.startswith("HPO_"))
        line = lines[number - 1]
        name = line.split("=", 1)[0]
        cases = [(original + "HPO_EXTRA_VERSION=1\n", "unknown toolchain argument", len(lines) + 1),
                 ("\n".join(ln for ln in lines if ln != line) + "\n",
                  "missing toolchain arguments", len(lines)),
                 (original.replace(line, name + "=", 1), "empty value", number),
                 (original + line + "\n", "duplicate toolchain argument", len(lines) + 1)]
        for value in ("1;command", "1 2", '"1"', "'1'", "$value", "`command`", "1\t2"):
            cases.append((original.replace(line, name + "=" + value, 1), "plain NAME=VALUE", number))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "args"
            for text, message, expected_line in cases:
                with self.subTest(message=message, text=text):
                    path.write_text(text, encoding="utf-8")
                    result = _cli("toolchain-args", str(path))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(message, result.stderr)
                    self.assertIn(f"toolchain args line {expected_line}:", result.stderr)


class SourceNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stages = {stage.name: stage for stage in
                      _dockerfile_stages((REPO / "docker" / "Dockerfile").read_text())}
        cls.template = (REPO / "docker" / "SOURCES.txt.in").read_text(encoding="utf-8")

    def test_template_preserves_biz_notice_verbatim_except_version_token(self):
        approved = (FIXTURES / "GPL-NOTICE-v1.txt").read_text(encoding="utf-8")
        self.assertEqual(self.template, approved.replace("<this version>", "@RELEASE_VERSION@"))
        self.assertEqual(re.findall(r"@[A-Za-z0-9_]+@", self.template), ["@RELEASE_VERSION@"])
        self.assertNotIn("<this version>", self.template)

    def notice_run(self, name):
        runs = [(index, body) for index, (keyword, body) in enumerate(self.stages[name].instructions)
                if keyword == "RUN" and "sed " in body and "SOURCES.txt.in" in body]
        self.assertEqual(len(runs), 1, name)
        return runs[0]

    def test_each_final_runtime_writes_notice_after_every_doc_cleanup_and_label(self):
        for name in ("release", "hpo"):
            with self.subTest(stage=name):
                stage = self.stages[name]
                notice_index, run = self.notice_run(name)
                cleanups = [index for index, (keyword, body) in enumerate(stage.instructions)
                            if keyword == "RUN" and "rm -rf" in body and "/usr/share/doc " in body]
                self.assertTrue(cleanups, "test must see the real doc-clearing apt instruction")
                self.assertGreater(notice_index, max(cleanups))
                other_doc_mentions = [index for index, (_, body) in enumerate(stage.instructions)
                                      if "/usr/share/doc" in body and index != notice_index]
                self.assertGreater(notice_index, max(other_doc_mentions))
                self.assertIn("mkdir -p /usr/share/doc/pineforge", run)
                self.assertIn(NOTICE_PATH, run)
                labels = " ".join(body for keyword, body in stage.instructions if keyword == "LABEL")
                self.assertIn(f'io.pineforge.sources.notice="{NOTICE_PATH}"', labels)
                self.assertIn(f'io.pineforge.sources.url="{RELEASES_URL}/tag/${{RELEASE_VERSION}}"', labels)
                copies = [body for keyword, body in stage.instructions if keyword == "COPY"]
                self.assertIn("docker/SOURCES.txt.in /tmp/pineforge-SOURCES.txt.in", copies)

    def test_real_sed_substitutions_render_tag_and_local_urls(self):
        # Extract the actual sed expressions from BOTH real RUN instructions;
        # a separate invented renderer cannot make a broken Dockerfile pass.
        for name in ("release", "hpo"):
            _, run = self.notice_run(name)
            self.assertIn('if [ "${RELEASE_VERSION}" = unknown ]; then', run)
            expressions = re.findall(r"sed\s+(['\"])(s\|[^\n]*?)\1\s+/tmp/pineforge-SOURCES\.txt\.in", run)
            self.assertEqual(len(expressions), 2, run)
            local_expression, release_expression = [expression for _, expression in expressions]
            for version, expression, expected_url in (
                    ("v1.6.0", release_expression, RELEASES_URL + "/tag/v1.6.0"),
                    ("v1.6.0-rc.1", release_expression, RELEASES_URL + "/tag/v1.6.0-rc.1"),
                    ("unknown", local_expression, RELEASES_URL)):
                with self.subTest(stage=name, version=version):
                    operation, old, new, flags = expression.split("|")
                    self.assertEqual((operation, flags), ("s", "g"))
                    rendered = self.template.replace(old, new.replace("${RELEASE_VERSION}", version))
                    if version == "unknown":
                        local_line = re.search(r"printf '%s\\n' '([^']+)' >> " + re.escape(NOTICE_PATH), run)
                        self.assertIsNotNone(local_line, "unknown builds must add one local-build line")
                        self.assertIn("Local build", local_line[1])
                        rendered += local_line[1] + "\n"
                        self.assertNotIn(RELEASES_URL + "/tag/", rendered)
                    self.assertIn("  " + expected_url + "\n", rendered)
                    self.assertNotIn("@RELEASE_VERSION@", rendered)

    def test_every_stage_that_expands_release_version_declares_the_argument(self):
        # A build ARG is local to its stage: `FROM release AS hpo` does not inherit the value
        # (only ENV is inherited). Without its own ARG the hpo image would name an empty tag.
        for name in ("release", "hpo"):
            with self.subTest(stage=name):
                declared = [body for keyword, body in self.stages[name].instructions
                            if keyword == "ARG" and body.split("=", 1)[0] == "RELEASE_VERSION"]
                self.assertEqual(declared, ["RELEASE_VERSION=unknown"], name)
        expanding = [name for name, stage in self.stages.items()
                     if any("${RELEASE_VERSION}" in body for keyword, body in stage.instructions
                            if keyword != "ARG")]
        for name in expanding:
            with self.subTest(expanding_stage=name):
                self.assertTrue(any(keyword == "ARG" and body.split("=", 1)[0] == "RELEASE_VERSION"
                                    for keyword, body in self.stages[name].instructions),
                                name + " expands ${RELEASE_VERSION} without declaring the ARG")

    def test_default_remains_a_no_op_release_alias(self):
        self.assertEqual(self.stages["default"].instructions, (("FROM", "release AS default"),))


def _deletion_commands(text: str) -> list[str]:
    """Scan logical command lines, including shell continuations and YAML uses.

    Return the offending commands, rather than accepting a grep that happens
    to miss every input. A gh API delete can place its method before its path.
    """
    logical = re.sub(r"\\\r?\n\s*", " ", text)
    patterns = (
        r"\bgh\s+release\s+(?:delete-asset|delete)\b",
        r"\bgh\s+api\b[^\n]*(?:-X\s*['\"]?DELETE\b|--method(?:=|\s+)['\"]?DELETE\b)",
        r"\bactions/delete-package-versions(?:@|\b)",
        r"\bgit\s+push\b[^\n]*--delete\b",
        r"\bgit\s+push\b[^\n]*\borigin\s+['\"]?:",
    )
    return [line.strip() for line in logical.splitlines()
            if not line.lstrip().startswith("#") and any(re.search(pattern, line) for pattern in patterns)]


class SourceRetentionTest(unittest.TestCase):
    def test_scanner_finds_deletion_fixture_commands(self):
        fixtures = (
            "gh release delete v1.0.0 --yes\ngh release delete-asset v1.0.0 set-02.source.tar\n",
            "gh api repos/org/repo/releases/assets/123 " + "\\" + "\n"
            "  --method DELETE\nuses: actions/delete-package-versions@v5\n"
            "gh api -X DELETE user/packages/container/image/versions/123\n",
            "git push --delete origin v1.0.0\ngit push origin :refs/tags/v1.0.0\n",
        )
        for text in fixtures:
            with self.subTest(text=text):
                commands = _deletion_commands(text)
                self.assertGreaterEqual(len(commands), 2)
                for line in re.sub(r"\\\n\s*", " ", text).splitlines():
                    self.assertIn(line.strip(), commands)
        self.assertEqual(_deletion_commands("gh release view v1.0.0 --json assets\n"
                                            "gh api --method GET repos/org/repo/releases\n"), [])

    def test_no_source_deletion_command_in_real_repository_automation(self):
        paths = list((REPO / ".github" / "workflows").glob("*"))
        paths += list((REPO / "scripts").glob("*"))
        paths += list((REPO / "tools").rglob("*"))
        found = []
        for path in paths:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            found += [f"{path.relative_to(REPO)}: {command}" for command in _deletion_commands(text)]
        self.assertEqual(found, [])

    def test_readme_has_the_retention_sentence_and_notice_path(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Debian source packages\n", readme)
        self.assertIn("A release's source assets are never deleted while its image remains on ghcr.", readme)
        self.assertIn(NOTICE_PATH, readme)
        self.assertIn("[BIZ licence statement placeholder:", readme)


if __name__ == "__main__":
    unittest.main()
