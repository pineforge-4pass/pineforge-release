import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH_HEADING = "## Running the image: scratch space (engine 1.7.0)\n"


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

        docker = re.search(r"```sh\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(docker, "include a short Docker example")
        self.assertIn("docker run --read-only -v <host-dir>:/tmp", docker.group(1))
        self.assertNotIn("--tmpfs", docker.group(1))

        kubernetes = re.search(r"```yaml\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(kubernetes, "include a short Kubernetes snippet")
        self.assertIn("emptyDir: {}", kubernetes.group(1))
        self.assertIn("mountPath: /tmp", kubernetes.group(1))
        self.assertNotIn("medium: Memory", kubernetes.group(1))
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


if __name__ == "__main__":
    unittest.main()
