import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH_HEADING = "## Running the image: scratch space (engine 1.7.0)\n"
HPO_CACHE_PATH = "/home/pineforge/.cache/pineforge/hpo"


class RuntimeScratchDocumentationTest(unittest.TestCase):
    def setUp(self):
        self.doc = (ROOT / "docs" / "release-chain.md").read_text(encoding="utf-8")

    def assert_scratch_guidance(self, text):
        self.assertIn(SCRATCH_HEADING, text)
        section = text.split(SCRATCH_HEADING, 1)[1].split("\n## ", 1)[0]
        for required in (
            "--read-only",
            "--tmpfs",
            "emptyDir",
            "medium: Memory",
            "report_sink_failed",
            "scratch_io",
            "TMPDIR",
            "closes a trade",
            "defaults to `/tmp`",
            "$TMPDIR/pf-work.*",
            "ramfs/tmpfs",
            "disk-backed",
            "-e TMPDIR=/scratch -v <host-dir>:/scratch",
            "without `medium: Memory`",
            "PersistentVolume",
        ):
            self.assertIn(required, section)

        prose = re.sub(r"```.*?```", "", section, flags=re.DOTALL)
        cache_sentence = next(
            (paragraph for paragraph in prose.split("\n\n")
             if "HPO subcommands" in paragraph),
            "",
        )
        for required in ("read-only", "writable", "disk-backed", HPO_CACHE_PATH, "--cache-dir"):
            self.assertIn(required, cache_sentence, "explain HPO cache requirements")

        docker = re.search(r"```sh\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(docker, "include a short Docker example")
        self.assertIn("docker run --read-only -v <host-dir>:/tmp", docker.group(1))
        self.assertNotIn("--tmpfs", docker.group(1))
        hpo = next(
            (command for command in docker.group(1).replace("\\\n", "").splitlines()
             if "hpo run /in/study.json" in command),
            "",
        )
        for required in (
            "docker run --read-only -v <host-dir>:/tmp",
            f"-v <cache-dir>:{HPO_CACHE_PATH}",
            '-v "$PWD/tests/fixtures/release-smoke:/in:ro"',
            "--engine-root /opt/pineforge",
        ):
            self.assertIn(required, hpo, "include the HPO run with writable scratch and cache")

        kubernetes = re.search(r"```yaml\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(kubernetes, "include a short Kubernetes snippet")
        self.assertIn("emptyDir: {}", kubernetes.group(1))
        self.assertIn("mountPath: /tmp", kubernetes.group(1))
        self.assertNotIn("medium: Memory", kubernetes.group(1))
        self.assertIn(
            f"- name: hpo-cache\n          mountPath: {HPO_CACHE_PATH}",
            kubernetes.group(1),
        )
        self.assertIn("- name: hpo-cache\n      emptyDir: {}", kubernetes.group(1))
        return section

    def test_runtime_scratch_guidance(self):
        self.assert_scratch_guidance(self.doc)

    def test_missing_scratch_section_is_rejected(self):
        section = self.assert_scratch_guidance(self.doc)
        without_section = self.doc.replace(SCRATCH_HEADING + section, "", 1)
        self.assertNotEqual(without_section, self.doc)
        with self.assertRaisesRegex(AssertionError, "Running the image"):
            self.assert_scratch_guidance(without_section)

    def test_missing_tmpfs_warning_is_rejected(self):
        self.assert_scratch_guidance(self.doc)
        without_tmpfs = self.doc.replace("--tmpfs", "--mount")
        self.assertNotEqual(without_tmpfs, self.doc)
        with self.assertRaisesRegex(AssertionError, "--tmpfs"):
            self.assert_scratch_guidance(without_tmpfs)

    def test_missing_hpo_cache_sentence_is_rejected(self):
        section = self.assert_scratch_guidance(self.doc)
        cache_sentence = next(
            paragraph for paragraph in section.split("\n\n")
            if "HPO subcommands" in paragraph
        )
        without_cache_sentence = self.doc.replace(cache_sentence, "", 1)
        self.assertNotEqual(without_cache_sentence, self.doc)
        with self.assertRaisesRegex(AssertionError, "HPO cache"):
            self.assert_scratch_guidance(without_cache_sentence)


class TrixieRuntimeDocumentationTest(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def assert_trixie_guidance(self, text):
        prose = " ".join(text.split())
        for required in (
            "Debian 13 (trixie), with the glibc 2.41 family and GCC 14",
            "`__isoc23_strtol`",
            "bookworm's glibc 2.36 cannot load that library",
            "Engine, codegen and HPO remain at 1.10.0, with unchanged producer artifact SHA-256 pins",
            "`linux-x86_64` and `linux-aarch64`",
            "trixie image's new source set was derived from both native image inventories",
            "equal amd64/arm64 source/version sets",
            "verified published list and asset mirror before adding the registry row",
            "published 1.10.0 image passed `publish.yml`'s source coverage checks and runtime smoke on both architectures",
            "CMake and make remain build-only",
            "Both native image builds completed, and `publish.yml` verified the published image's runtime on amd64 and arm64",
            "unfilled skip, which is never proof",
        ):
            self.assertIn(required, prose)

    def test_runtime_floor_unchanged_producers_and_published_mirror_are_documented(self):
        self.assert_trixie_guidance(self.readme)

    def test_wrong_runtime_floor_or_missing_producer_or_mirror_disclosure_is_rejected(self):
        for fragment in ("Debian 13 (trixie)", "glibc 2.41", "remain at 1.10.0",
                         "unchanged producer artifact SHA-256 pins",
                         "verified published list and asset mirror",
                         "Both native image\nbuilds completed",
                         "source coverage checks and runtime smoke on both architectures",
                         "unfilled skip, which is never proof"):
            with self.subTest(fragment=fragment):
                mutated = self.readme.replace(fragment, "omitted", 1)
                self.assertNotEqual(mutated, self.readme)
                with self.assertRaises(AssertionError):
                    self.assert_trixie_guidance(mutated)


if __name__ == "__main__":
    unittest.main()
