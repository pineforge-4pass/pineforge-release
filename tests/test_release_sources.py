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
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_release_hpo_image import (
    _build_arguments, _dockerfile_instructions, _dockerfile_stages, _reachable_stages,
)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import release_sources as sources  # noqa: E402

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

    def source_asset_positions(self):
        # The SOURCES.list metadata is last in self.assets; the last SOURCE
        # archive is at len(self.pairs) - 1, and must be tested separately.
        return (("first", 0), ("middle", len(self.pairs) // 2), ("last", len(self.pairs) - 1))

    def test_01_no_registered_set_is_refused(self):
        self.registry.write_text(REGISTRY_HEADER + "# no mirrors yet\n", encoding="utf-8")
        self.refused(self.check(), f"source set {_set_id(self.pairs)} is not in registry")

    def test_02_registered_set_without_list_asset_is_refused(self):
        assets = [item for item in self.assets if item["name"] != self.sources_list.name]
        self.refused(self.check(assets), "missing release asset: set-02.SOURCES.list")
        self.refused(self.check([]), "missing release asset: set-02.SOURCES.list")

    def test_03_one_named_source_asset_missing_is_refused(self):
        for position, index in self.source_asset_positions():
            with self.subTest(position=position):
                name = self.assets[index]["name"]
                assets = [item.copy() for item in self.assets if item["name"] != name]
                self.refused(self.check(assets), f"missing release asset: {name}")

    def test_04_wrong_source_asset_digest_is_refused(self):
        for position, index in self.source_asset_positions():
            with self.subTest(position=position):
                assets = [item.copy() for item in self.assets]
                item = assets[index]
                item["digest"] = "sha256:" + hashlib.sha256(b"a different fixture").hexdigest()
                self.refused(self.check(assets), f"digest mismatch for {item['name']}")

    def test_05_wrong_source_asset_size_is_refused(self):
        for position, index in self.source_asset_positions():
            with self.subTest(position=position):
                assets = [item.copy() for item in self.assets]
                item = assets[index]
                item["size"] += 1
                self.refused(self.check(assets), f"size mismatch for {item['name']}")

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
        for position, index in self.source_asset_positions():
            with self.subTest(position=position):
                assets = [item.copy() for item in self.assets]
                assets.append(assets[index].copy())
                self.refused(self.check(assets), f"duplicate release asset: {assets[index]['name']}")

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
        for position, index in self.source_asset_positions():
            for digest in (None, "", "md5:invalid"):
                with self.subTest(position=position, digest=digest):
                    assets = [item.copy() for item in self.assets]
                    assets[index]["digest"] = digest
                    self.refused(self.check(assets), f"digest mismatch for {assets[index]['name']}")

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

    def test_17_sources_list_filename_must_match_the_registered_asset(self):
        renamed = self.root / "set-03.SOURCES.list"
        renamed.write_bytes(self.sources_list.read_bytes())
        self.sources_list = renamed
        self.refused(self.check(),
                     "SOURCES.list filename does not match registry list-asset: set-02.SOURCES.list")

    def test_18_registry_list_asset_must_have_a_set_numbered_filename(self):
        original = self.registry.read_text(encoding="utf-8")
        for name in ("SOURCES.list", "set-2.SOURCES.list", "../set-02.SOURCES.list",
                     "set-02.SOURCES.list.bak"):
            with self.subTest(filename=name):
                self.registry.write_text(original.replace("\tset-02.SOURCES.list\n", f"\t{name}\n", 1),
                                         encoding="utf-8")
                self.refused(self.check(),
                             "registry line 2: list-asset must name a set-NN.SOURCES.list asset")

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
        for position, index in self.source_asset_positions():
            with self.subTest(position=position):
                assets = [item.copy() for item in self.assets]
                item = assets[index]
                digest = item["digest"]
                item["digest"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")
                self.refused(self.check(assets), f"digest mismatch for {item['name']}")

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

    def test_95_repository_registry_has_the_documented_header_and_real_source_sets(self):
        lines = (REPO / "docker" / "SOURCE-SETS.tsv").read_text(encoding="utf-8").splitlines()
        data = [line for line in lines if line and not line.startswith("#")]
        self.assertTrue(data, "the registry must retain its documented header")
        self.assertEqual(data[0], REGISTRY_HEADER.rstrip("\n"))
        rows = [line.split("\t") for line in data[1:]]
        self.assertEqual(len(rows), 12, "the registry must contain the 12 real source sets")
        expected_tags = ("v0.1.0", "v1.4.0", "v0.1.21", "v0.1.15", "v1.0.1", "v0.1.11",
                         "v0.1.13", "v1.0.0-rc.1", "v0.1.19", "v0.1.25", "v1.3.0", "v1.4.0")
        identities = []
        for number, columns in enumerate(rows, 1):
            with self.subTest(source_set=f"set-{number:02d}"):
                self.assertEqual(len(columns), 5, "registry rows must have five tab-separated fields")
                identity, tag, count, digest, asset = columns
                self.assertRegex(identity, r"^[0-9a-f]{64}$")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
                self.assertTrue(tag.startswith("v"), "holding releases must use v-prefixed tags")
                self.assertIn(tag, expected_tags)
                self.assertEqual(tag, expected_tags[number - 1], "each set must use its confirmed release")
                self.assertEqual(count, "84")
                self.assertEqual(asset, f"set-{number:02d}.SOURCES.list")
                identities.append(identity)
        self.assertEqual(len(set(identities)), 12, "source set-ids must be unique")

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
        artifact_names = set(sources.HPO_ARTIFACT_ARGS)
        self.assertLessEqual(artifact_names, _build_arguments(stages, hpo))
        for required in (False, True):
            required_names = (_build_arguments(stages, hpo, required=required)
                              - _build_arguments(stages, ordinary, required=required)
                              - {"HPO_VERSION"} - artifact_names)
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


class HpoArtifactArgumentsTest(unittest.TestCase):
    def test_verified_binding_fields_are_required_for_both_artifact_arguments(self):
        self.assertEqual(sources.HPO_ARTIFACT_ARGS, {
            "HPO_WHEEL_SHA256": ("hpo_wheel_arg", "wheel_sha256"),
            "HPO_SDIST_SHA256": ("hpo_sdist_arg", "sdist_sha256"),
        })
        pin = {"wheel_sha256": "a" * 64, "sdist_sha256": "b" * 64}
        self.assertEqual(sources.hpo_artifact_args(pin), {
            "hpo_wheel_arg": "HPO_WHEEL_SHA256=" + "a" * 64,
            "hpo_sdist_arg": "HPO_SDIST_SHA256=" + "b" * 64,
        })
        for field in pin:
            for invalid in (None, "", 0, "a" * 63, "A" * 64, "sha256:" + "a" * 64,
                            "a" * 64 + "\n", "$(command)"):
                with self.subTest(field=field, invalid=invalid):
                    mutated = {**pin, field: invalid}
                    with self.assertRaises(sources.SourcesError):
                        sources.hpo_artifact_args(mutated)
            with self.subTest(missing=field), self.assertRaises(sources.SourcesError):
                sources.hpo_artifact_args({key: value for key, value in pin.items() if key != field})

    def test_each_declared_digest_guard_rejects_changed_bytes_before_consumption(self):
        stages = {stage.name: stage for stage in
                  _dockerfile_stages((REPO / "docker" / "Dockerfile").read_text())}
        shell = shutil.which("sh", path=os.defpath)
        self.assertIsNotNone(shell, "sh is required to exercise the real checksum guards")
        guards = {
            "HPO_WHEEL_SHA256": ("hpo-wheel", "hpo-wheel.whl", "pip3 install"),
            "HPO_SDIST_SHA256": ("hpo-native", "hpo-source.tar.gz", "tar -xzf"),
        }
        self.assertEqual(set(guards), set(sources.HPO_ARTIFACT_ARGS))
        for argument, (stage, filename, consume) in guards.items():
            with self.subTest(argument=argument), tempfile.TemporaryDirectory() as tmp:
                text = stages[stage].text
                self.assertIn("ARG " + argument + "\n", text)
                start = text.index('if [ -n "${' + argument + '}" ]; then')
                end = text.index("fi;", start) + len("fi;")
                guard = text[start:end]
                self.assertIn("sha256sum -c --strict", guard)
                self.assertLess(text.index("curl -fsSL"), start)
                self.assertLess(end, text.index(consume))
                # Run only the real checksum guard, on explicit fixture bytes;
                # downloads, extraction and installation remain outside it.
                script = "set -eu; " + guard.replace("\\\n", "").replace("/tmp/", "")
                root = Path(tmp)
                artifact = root / filename
                data = b"TEST FIXTURE ONLY: bound artifact bytes\n"
                artifact.write_bytes(data)
                env = {"PATH": os.environ.get("PATH", os.defpath), "wheel": filename,
                       argument: hashlib.sha256(data).hexdigest()}

                def run_guard():
                    return subprocess.run([shell, "-c", script], cwd=root, env=env,
                                          stdin=subprocess.DEVNULL, capture_output=True,
                                          text=True, timeout=10)

                good = run_guard()
                self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
                artifact.write_bytes(data + b"tampered\n")
                bad = run_guard()
                self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
                self.assertIn("FAILED", bad.stdout + bad.stderr)


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

    def cleanup_run(self, name):
        runs = [(index, body) for index, (keyword, body) in enumerate(self.stages[name].instructions)
                if keyword == "RUN" and "find /usr/share/doc" in body and "-delete" in body]
        self.assertEqual(len(runs), 1,
                         name + ": expected one copyright-preserving doc cleanup RUN")
        return runs[0]

    def run_shell(self, script, root, version="unknown"):
        shell = shutil.which("sh", path=os.defpath)
        self.assertIsNotNone(shell, "sh is required to exercise the real Dockerfile RUN")
        return subprocess.run([shell, "-c", script], cwd=root,
                              env={"PATH": os.defpath, "RELEASE_VERSION": version},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_each_final_runtime_keeps_copyright_after_apt_and_before_its_notice(self):
        for name in ("release", "hpo"):
            with self.subTest(stage=name):
                stage = self.stages[name]
                notice_index, run = self.notice_run(name)
                cleanup_index, cleanup = self.cleanup_run(name)
                apt_runs = [(index, body) for index, (keyword, body) in enumerate(stage.instructions)
                            if keyword == "RUN" and "apt-get install" in body]
                self.assertEqual(len(apt_runs), 1, "test must see the final stage's real apt RUN")
                apt_index, apt = apt_runs[0]
                self.assertLess(apt_index, cleanup_index, "copyright cleanup must follow apt")
                self.assertLess(cleanup_index, notice_index, "copyright cleanup must precede the notice")
                self.assertEqual(apt.count("rm -rf"), 1)
                self.assertEqual(shlex.split(apt.split("rm -rf", 1)[1]),
                                 ["/var/lib/apt/lists/*", "/usr/share/man",
                                  "/usr/share/locale", "/usr/share/info"],
                                 "apt cleanup must remove only the /usr/share/doc token")
                self.assertTrue(cleanup.startswith("set -eu;"))
                self.assertEqual(
                    " ".join(cleanup.split()),
                    "set -eu; if [ -d /usr/share/doc ]; then "
                    "find /usr/share/doc -type f ! -name copyright -delete; "
                    "find /usr/share/doc -xtype l -delete; "
                    "find /usr/share/doc -depth -type d -empty -delete; fi",
                    "doc cleanup must remove non-copyright files, then only broken links, then empty directories",
                )
                self.assertEqual(stage.text.count("# --- sources notice (begin)\n"), 1)
                self.assertEqual(stage.text.count("# --- sources notice (end)\n"), 1)
                block = stage.text.split("# --- sources notice (begin)\n", 1)[1].split(
                    "# --- sources notice (end)\n", 1)[0]
                instructions = _dockerfile_instructions(block)
                self.assertIn(("RUN", cleanup), instructions)
                self.assertIn(("RUN", run), instructions)
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

    def test_real_cleanup_removes_dangling_links_and_preserves_working_symlinks(self):
        for name in ("release", "hpo"):
            with self.subTest(stage=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                docs = root / "usr share doc"
                keep = {"gcc-12-base/copyright": b"gcc copyright fixture\n",
                        "gcc-12/copyright": b"gcc compiler copyright fixture\n",
                        "python3.11/copyright": b"python runtime copyright fixture\n",
                        "python3/copyright": b"python defaults copyright fixture\n",
                        "fixture-package/copyright": b"another copyright fixture\n"}
                for relative, data in keep.items():
                    target = docs / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                (docs / "libgcc-s1").symlink_to("gcc-12-base", target_is_directory=True)
                (docs / "copyright-link").mkdir()
                linked = docs / "copyright-link" / "copyright"
                linked.symlink_to("../gcc-12-base/copyright")
                for relative in ("gcc-12-base/changelog.gz", "fixture-package/examples/demo.txt",
                                 "gcc-12/README.Bugs", "python3.11/NEWS.gz",
                                 "python3/python-policy.txt.gz",
                                 "only-changelogs/changelog", "pineforge/SOURCES.txt"):
                    target = docs / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"discard this documentation\n")
                dangling = {"cpp/README.Bugs": "../gcc-12/README.Bugs",
                            "python3.11/changelog.gz": "NEWS.gz",
                            "python3.11/python-policy.txt.gz": "../python3/python-policy.txt.gz",
                            "only-links/changelog.gz": "../only-changelogs/changelog"}
                for relative, destination in dangling.items():
                    link = docs / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(destination)
                    self.assertTrue(link.exists(), "fixture link must resolve before file cleanup")
                (docs / "empty-directory").mkdir()
                _, run = self.cleanup_run(name)
                script = run.replace("/usr/share/doc", shlex.quote(str(docs)))
                done = self.run_shell(script, root)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                for relative in dangling:
                    self.assertFalse((docs / relative).is_symlink(),
                                     "broken doc symlink must be removed: " + relative)
                for relative, data in keep.items():
                    self.assertEqual((docs / relative).read_bytes(), data)
                self.assertTrue((docs / "libgcc-s1").is_symlink(), "package doc symlink must survive")
                self.assertEqual((docs / "libgcc-s1").readlink(), Path("gcc-12-base"))
                self.assertEqual((docs / "libgcc-s1" / "copyright").read_bytes(), keep["gcc-12-base/copyright"])
                self.assertTrue(linked.is_symlink(), "copyright file symlink must survive")
                self.assertEqual(linked.readlink(), Path("../gcc-12-base/copyright"))
                self.assertEqual(linked.read_bytes(), keep["gcc-12-base/copyright"])
                remaining = {str(path.relative_to(docs)) for path in docs.rglob("*")
                             if path.is_file() and not path.is_symlink()}
                self.assertEqual(remaining, set(keep), "non-copyright regular files must be removed")
                for relative in ("fixture-package/examples", "only-changelogs", "cpp", "only-links",
                                 "pineforge", "empty-directory"):
                    self.assertFalse((docs / relative).exists(), "empty doc directories must be removed")

    def test_real_cleanup_succeeds_with_absent_or_empty_doc_paths(self):
        for name in ("release", "hpo"):
            for present in (False, True):
                with self.subTest(stage=name, doc_path_present=present), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    docs = root / "docs"
                    if present:
                        docs.mkdir()
                    _, run = self.cleanup_run(name)
                    done = self.run_shell(run.replace("/usr/share/doc", shlex.quote(str(docs))), root)
                    self.assertEqual(done.returncode, 0,
                                     "absent or empty doc paths must not fail the build: " + done.stderr)

    def assert_notice_rendered(self, run, version, expected_url):
        # Execute the actual RUN body; only the two image paths are relocated.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notice_dir = root / "usr share doc pineforge"
            template = root / "pineforge SOURCES.txt.in"
            template.write_text(self.template, encoding="utf-8")
            script = run.replace("/usr/share/doc/pineforge", shlex.quote(str(notice_dir))).replace(
                "/tmp/pineforge-SOURCES.txt.in", shlex.quote(str(template)))
            done = self.run_shell(script, root, version)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            notice = notice_dir / "SOURCES.txt"
            self.assertTrue(notice.is_file(), "notice RUN must write the declared SOURCES.txt path")
            rendered = notice.read_text(encoding="utf-8")
            self.assertIn("  " + expected_url + "\n", rendered,
                          "notice RUN must write the expected URL to SOURCES.txt")
            self.assertNotIn("@RELEASE_VERSION@", rendered)
            self.assertFalse(template.exists(), "notice RUN must remove its temporary template")
            if version == "unknown":
                self.assertNotIn(RELEASES_URL + "/tag/", rendered)
                self.assertIn("Local build: use the releases page to locate mirrored Debian sources.\n", rendered)
            else:
                self.assertNotIn("Local build:", rendered)

    def test_real_notice_run_writes_tagged_prerelease_and_local_urls(self):
        for name in ("release", "hpo"):
            _, run = self.notice_run(name)
            for version, expected_url in (("v1.6.0", RELEASES_URL + "/tag/v1.6.0"),
                                          ("v1.6.0-rc.1", RELEASES_URL + "/tag/v1.6.0-rc.1"),
                                          ("unknown", RELEASES_URL)):
                with self.subTest(stage=name, version=version):
                    self.assert_notice_rendered(run, version, expected_url)

    def test_a_typo_in_either_real_notice_redirect_is_detected(self):
        for name in ("release", "hpo"):
            _, run = self.notice_run(name)
            redirects = list(re.finditer(r"sed [^;]+ > (" + re.escape(NOTICE_PATH) + r")(?=;)", run))
            self.assertEqual(len(redirects), 2, "test must find both real sed output redirects")
            for index, version, url in ((0, "unknown", RELEASES_URL),
                                        (1, "v1.6.0", RELEASES_URL + "/tag/v1.6.0")):
                with self.subTest(stage=name, version=version):
                    redirect = redirects[index]
                    altered = run[:redirect.start(1)] + NOTICE_PATH + ".wrong" + run[redirect.end(1):]
                    self.assertNotEqual(altered, run)
                    with self.assertRaisesRegex(AssertionError, "notice RUN must write"):
                        self.assert_notice_rendered(altered, version, url)

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

    def test_readme_documents_bootstrapping_new_source_sets_and_kept_copyright_files(self):
        readme = " ".join((REPO / "README.md").read_text(encoding="utf-8").split())
        self.assertIn(
            "A new source set (an image package set not yet in docker/SOURCE-SETS.tsv) needs its "
            "bundle attached to an earlier or dedicated release before its first publish, because "
            "the guard looks up the holding release by tag before the new tag's GitHub Release exists.",
            readme,
            "README must explain how to make a new source set available before its first publish",
        )
        self.assertIn(
            "The default image differs from 1.5.0 only by the source notice, the two source "
            "labels, the licences label and the retained Debian package copyright files.",
            readme,
            "README must state the notice and copyright differences from the 1.5.0 default image",
        )


if __name__ == "__main__":
    unittest.main()
