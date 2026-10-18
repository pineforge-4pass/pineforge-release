#!/usr/bin/env python3
"""Exercise the actual entrypoint heredocs and Dockerfile install/license code.

Run on CI/lab only. Temporary codegen packages expose which import was selected;
the selection logic itself is extracted unchanged from the shipped entrypoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_release_hpo_image import _dockerfile_stages
from test_release_workflows import _run_block


class CodegenSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
        cls.dockerfile = (ROOT / "docker/Dockerfile").read_text()
        cls.programs = re.findall(r"python3 - [^\n]+ <<'PY'\n(.*?)\nPY", cls.entrypoint, re.S)
        if len(cls.programs) != 2:
            raise AssertionError("expected both transpile programs from the real entrypoint")

    def package(self, root, name, *, broken=False):
        package = root / name
        package.mkdir()
        (package / "errors.py").write_text("class CompileError(Exception):\n    pass\n")
        source = (
            "import hashlib\n"
            "def transpile(source, *, filename):\n"
            "    return __name__ + ':' + filename + ':' + source\n"
            "def transpile_with_request_inventory(source, *, filename):\n"
            "    return {'cpp': transpile(source, filename=filename),\n"
            "            'request_feed_inventory': {'source_sha256': hashlib.sha256(source.encode()).hexdigest()}}\n"
        )
        (package / "__init__.py").write_text("raise RuntimeError('broken preferred package')\n" if broken else source)

    def invoke(self, root, program, *arguments):
        return subprocess.run([sys.executable, "-S", "-c", program, *map(str, arguments)],
                              cwd=root, env={"PATH": os.defpath, "PYTHONPATH": str(root),
                                             "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, text=True, timeout=30)

    def test_both_real_transpile_paths_select_the_available_import_prefer_tunaxis(self):
        for packages in (("pineforge_codegen",), ("tunaxis_codegen",),
                         ("pineforge_codegen", "tunaxis_codegen")):
            with self.subTest(packages=packages), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in packages:
                    self.package(root, name)
                pine = root / "strategy.pine"
                text = "strategy('input-specific test')\n"
                pine.write_text(text)
                preferred = "tunaxis_codegen" if "tunaxis_codegen" in packages else "pineforge_codegen"
                expected = preferred + ":strategy.pine:" + text
                plain = self.invoke(root, self.programs[0], pine, "-")
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertEqual(plain.stdout, expected)
                cpp, inventory = root / "strategy.cpp", root / "inventory.json"
                selected = self.invoke(root, self.programs[1], pine, cpp, inventory)
                self.assertEqual(selected.returncode, 0, selected.stderr)
                self.assertEqual(cpp.read_bytes(), expected.encode())
                self.assertEqual(json.loads(inventory.read_text())["source_sha256"],
                                 hashlib.sha256(text.encode()).hexdigest())

    def test_a_broken_preferred_package_is_not_silently_replaced_by_the_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.package(root, "pineforge_codegen")
            self.package(root, "tunaxis_codegen", broken=True)
            pine = root / "strategy.pine"
            pine.write_text("source which must not reach the legacy import")
            for program in self.programs:
                result = self.invoke(root, program, pine, "-", root / "inventory.json")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("broken preferred package", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_real_docker_install_selects_one_distribution_and_keeps_exact_version_pin(self):
        global_args = self.dockerfile.split("\nFROM ", 1)[0]
        default = re.search(r"^ARG CODEGEN_DIST=(\S+)$", global_args, re.M)
        self.assertIsNotNone(default)
        self.assertEqual(default[1], "pineforge-codegen")
        stage = next(stage for stage in _dockerfile_stages(self.dockerfile) if stage.name == "codegen")
        install = next(body for keyword, body in stage.instructions if keyword == "RUN" and "pip3 install" in body)
        guard = next(body for keyword, body in stage.instructions if keyword == "RUN" and "unsupported CODEGEN_DIST" in body)
        for distribution in (default[1], "tunaxis-codegen"):
            with self.subTest(distribution=distribution):
                env = {"PATH": os.defpath, "CODEGEN_DIST": distribution, "CODEGEN_VERSION": "1.6.1-rc.2"}
                result = subprocess.run(["bash", "--noprofile", "--norc", "-euc",
                                         "pip3() { printf '%s\\0' \"$@\"; }\n" + guard + "\n" + install],
                                        env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = result.stdout.decode().rstrip("\0").split("\0")
                self.assertEqual(arguments[-1], distribution + "==" + env["CODEGEN_VERSION"])
                self.assertEqual(arguments[arguments.index("--target") + 1], "/opt/codegen")
        rejected = subprocess.run(["bash", "--noprofile", "--norc", "-euc", guard],
                                  env={"PATH": os.defpath, "CODEGEN_DIST": "unapproved-distribution"},
                                  capture_output=True, text=True, timeout=30)
        self.assertNotEqual(rejected.returncode, 0)

    def test_real_docker_license_copy_uses_the_selected_distribution_only(self):
        block = _run_block(self.dockerfile, 'codegen_dist="$(printf')
        selection = block[block.index('codegen_dist="$(printf'):].replace("\\\n", "\n")
        for distribution in ("pineforge-codegen", "tunaxis-codegen"):
            with self.subTest(distribution=distribution), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, destination = root / "codegen", root / "licences"
                destination.mkdir()
                normalized = distribution.replace("-", "_")
                directory = source / (normalized + "-1.6.0.dist-info") / "licenses"
                directory.mkdir(parents=True)
                content = (distribution + " licence bytes\n").encode()
                (directory / "LICENSE").write_bytes(content)
                script = selection.replace("/opt/codegen", shlex.quote(str(source))).replace(
                    "/opt/hpo-licenses/pineforge-codegen/", shlex.quote(str(destination)) + "/")
                result = subprocess.run(["bash", "--noprofile", "--norc", "-euc", script],
                                        env={"PATH": os.defpath, "CODEGEN_DIST": distribution},
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((destination / "LICENSE").read_bytes(), content)
                # A missing selected distribution cannot borrow the other one's licence.
                other = "tunaxis-codegen" if distribution == "pineforge-codegen" else "pineforge-codegen"
                missing = subprocess.run(["bash", "--noprofile", "--norc", "-euc", script],
                                         env={"PATH": os.defpath, "CODEGEN_DIST": other},
                                         capture_output=True, text=True, timeout=30)
                self.assertNotEqual(missing.returncode, 0)

    def test_entrypoint_retains_configuration_names_and_brands_stderr(self):
        self.assertNotRegex(self.entrypoint, r"\bTUNAXIS_[A-Z_]+")
        for variable in ("PINEFORGE_TRANSPILE_ONLY", "PINEFORGE_INPUTS", "PINEFORGE_OVERRIDES",
                         "PINEFORGE_REPORT_POLICY", "PINEFORGE_REQUEST_FEED_INVENTORY"):
            self.assertIn(variable, self.entrypoint)
        self.assertNotIn("[pineforge]", self.entrypoint)
        self.assertIn('echo "[Tunaxis] formerly PineForge" >&2', self.entrypoint)


if __name__ == "__main__":
    unittest.main()
