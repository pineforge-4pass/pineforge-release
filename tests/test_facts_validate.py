"""Canonical validation exercised only through its public CLI."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "facts/facts.json"


class FactsValidationCLI(unittest.TestCase):
    def invoke(self, path):
        return subprocess.run([sys.executable, str(ROOT / "scripts/facts_validate.py"),
                               "--facts", str(path)], capture_output=True, text=True)

    def test_current_and_historical_facts_validate(self):
        result = self.invoke(FACTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sha256"], hashlib.sha256(FACTS.read_bytes()).hexdigest())

    def test_complete_schema_and_semantic_negatives_have_no_output(self):
        canonical = json.loads(FACTS.read_text())
        changes = {
            "root-extra": lambda f: f.update(extra=True),
            "missing": lambda f: f["scoreboard"].pop("pairs"),
            "bool-count": lambda f: f["scoreboard"].update(graded=True),
            "float-count": lambda f: f["scoreboard"].update(graded=7989.0),
            "unsafe-integer": lambda f: f["inventory"].update(tvTrades=2**53),
            "date": lambda f: f["scoreboard"].update(date="2026-02-30"),
            "timestamp": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-10-06"),
            "invalid-timestamp-day": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-02-30T12:00:00Z"),
            "non-utc-timestamp": lambda f: f["scoreboard"]["provenance"].update(promotionDate="2026-10-06T12:00:00+01:00"),
            "digest": lambda f: f["scoreboard"].update(snapshotSha256="a" * 64 + "\n"),
            "provenance": lambda f: f["scoreboard"]["provenance"].update(engineCommit="a" * 40),
            "source": lambda f: f["scoreboard"].update(source="unverified source"),
            "population": lambda f: f["scoreboard"].update(population=1),
            "pairs-sum": lambda f: f["scoreboard"]["pairs"][1].update(strong=7),
            "duplicate-pair": lambda f: f["scoreboard"]["pairs"].append(copy.deepcopy(f["scoreboard"]["pairs"][0])),
            "tiers": lambda f: f["scoreboard"]["tiers"].update(moderate=1),
            "scope": lambda f: f["scoreboard"]["scopes"]["corpus"].update(excellent=1),
            "hard-lane": lambda f: f["scoreboard"]["hardLane"].update(hardProbes=1),
            "lanes": lambda f: f["scoreboard"].update(lanes=1),
            "percentage": lambda f: f["scoreboard"].update(excellentPct=50),
            "symbol": lambda f: f["scoreboard"]["pairs"][0].update(symbol="bad/path"),
            "timeframe": lambda f: f["scoreboard"]["pairs"][0].update(timeframe="0"),
            "release-nested": lambda f: f["releases"]["1.0.1"]["scoreboard"]["pairs"][0].update(unknown=1),
            "release-provenance": lambda f: f["releases"]["1.0.1"]["scoreboard"].update(id="wrong"),
            "release-version": lambda f: f["releases"].update({"v1.0.0": f["releases"]["1.0.1"]}),
            "release-missing-source": lambda f: f["releases"]["1.0.1"].pop("source"),
            "release-identity": lambda f: f["releases"]["1.0.1"].update(source="https://github.com/pineforge-4pass/other/blob/v1.0.1/README.md"),
            "inventory-sum": lambda f: f["inventory"].update(closedScripts=1),
            "inventory-release": lambda f: f["inventory"].update(sourceRelease="99.0.0"),
            "inventory-digest": lambda f: f["inventory"].update(populationSha256="a" * 64),
            "inventory-source": lambda f: f["inventory"].update(source=f["inventory"]["source"] + " additional locator"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.json"
            for name, change in changes.items():
                with self.subTest(name=name):
                    value = copy.deepcopy(canonical)
                    change(value)
                    path.write_text(json.dumps(value))
                    result = self.invoke(path)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(result.stderr)


if __name__ == "__main__":
    unittest.main()
