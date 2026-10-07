"""Real SVG and endpoint bytes, parsed outside the renderer process."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "facts/facts.json"


class FactsCardsCLI(unittest.TestCase):
    def invoke(self, variant="full", format="svg", facts=FACTS, theme="light"):
        return subprocess.run([sys.executable, str(ROOT / "scripts/facts_cards.py"),
                               "--facts", str(facts), "--variant", variant,
                               "--format", format, "--theme", theme], capture_output=True, text=True)

    def test_actual_full_and_corpus_svg_and_endpoint_are_deterministic(self):
        facts = json.loads(FACTS.read_text())
        for variant in ("full", "corpus"):
            score = facts["scoreboard"] if variant == "full" else facts["scoreboard"]["scopes"]["corpus"]
            for theme in ("light", "dark"):
                with self.subTest(variant=variant, theme=theme):
                    result = self.invoke(variant, theme=theme)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, self.invoke(variant, theme=theme).stdout)
                    svg = ET.fromstring(result.stdout)
                    ns = {"s": "http://www.w3.org/2000/svg"}
                    self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
                    self.assertEqual(svg.attrib["role"], "img")
                    self.assertTrue(svg.find("s:title", ns).text)
                    desc = svg.find("s:desc", ns).text
                    visible = " ".join(node.text or "" for node in svg.findall("s:text", ns))
                    for text in (f'{score["graded"]:,}', f'{score["excellent"]:,}',
                                 facts["scoreboard"]["date"], facts["scoreboard"]["engineCommit"][:8]):
                        self.assertIn(text, desc)
                        self.assertIn(text, visible)
                    self.assertNotIn("<script", result.stdout)
                    self.assertNotIn("http://", result.stdout.replace("http://www.w3.org/2000/svg", ""))
            endpoint = self.invoke(variant, "json")
            self.assertEqual(endpoint.returncode, 0, endpoint.stderr)
            data = json.loads(endpoint.stdout)
            self.assertEqual(data["schemaVersion"], 1)
            self.assertIn(f'{score["excellent"]:,}', data["message"])
            self.assertIn(f'{score["graded"]:,}', data["message"])
            self.assertEqual(data["factsSha256"], hashlib.sha256(FACTS.read_bytes()).hexdigest())

    def test_malformed_history_never_produces_partial_svg_or_json(self):
        with tempfile.TemporaryDirectory() as directory:
            facts = json.loads(FACTS.read_text())
            facts["releases"]["1.0.1"]["scoreboard"]["graded"] += 1
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(facts))
            for format in ("svg", "json"):
                result = self.invoke(facts=path, format=format)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
