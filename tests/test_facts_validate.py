"""Facts CLI validation and committed schema compatibility."""
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

    def test_committed_facts_and_all_json_fixtures_keep_valid_with_the_repository_enum(self):
        from jsonschema import Draft202012Validator, FormatChecker, ValidationError
        sys.path.insert(0, str(ROOT / "scripts"))
        import release_contract as contract
        schema = json.loads((ROOT / "facts/facts.schema.json").read_bytes())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        repository_schema = schema["$defs"]["image"]["properties"]["repository"]
        repository_validator = Draft202012Validator(repository_schema)
        self.assertEqual(repository_schema["enum"], list(contract.IMAGES))
        for repository in contract.IMAGES:
            repository_validator.validate(repository)
        with self.assertRaises(ValidationError):
            repository_validator.validate("ghcr.io/unrelated/image")
        manifest_raw = (ROOT / "tests/fixtures/release-manifest.json").read_bytes()
        paths = [FACTS, *sorted((ROOT / "tests/fixtures").glob("*.json"))]
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                document = json.loads(raw)
                if document["schema"] == schema["properties"]["schema"]["const"]:
                    validator.validate(document)
                    result = self.invoke(path)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["sha256"], hashlib.sha256(raw).hexdigest())
                elif document["schema"] == contract.SCHEMA:
                    # Release fixtures keep their complete existing validators;
                    # their repository must also meet the changed facts constraint.
                    if "manifest" in document:
                        contract.validate_payload(document, manifest_raw)
                    else:
                        contract.payload(raw)
                    repository_validator.validate(document["image"]["repository"])
                else:
                    self.fail("committed fixture has no applicable schema validator")
                self.assertEqual(path.read_bytes(), raw)

    def test_optional_product_images_accept_either_repository_without_changing_legacy_facts(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from release_contract import IMAGES
        raw = FACTS.read_bytes()
        canonical = json.loads(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.json"
            for repository in IMAGES:
                for names in (None, list(IMAGES)):
                    obj = copy.deepcopy(canonical)
                    image = {"repository": repository, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
                    if names is not None:
                        image["repositories"] = names
                    obj["image"] = image
                    obj["releases"][next(iter(obj["releases"]))]["image"] = image
                    path.write_text(json.dumps(obj))
                    result = self.invoke(path)
                    self.assertEqual(result.returncode, 0, result.stderr)
            for image in ({"repository": "ghcr.io/unrelated/image", "digest": "sha256:" + "a" * 64},
                          {"repository": IMAGES[0], "repositories": [IMAGES[1]], "digest": "sha256:" + "a" * 64},
                          {"repository": IMAGES[0], "repositories": list(IMAGES) * 2, "digest": "sha256:" + "a" * 64}):
                obj = copy.deepcopy(canonical)
                obj["image"] = image
                path.write_text(json.dumps(obj))
                result = self.invoke(path)
                self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(FACTS.read_bytes(), raw)

    def test_compared_trade_count_is_one_paired_active_integer_and_optional(self):
        canonical = json.loads(FACTS.read_text())
        score = canonical["scoreboard"]
        self.assertIsInstance(score["tvTradesCompared"], int)
        self.assertNotIsInstance(score["tvTradesCompared"], bool)
        self.assertGreater(score["tvTradesCompared"], 0)
        self.assertNotIn("tvTradesCompared", canonical["inventory"])
        for release in canonical["releases"].values():
            self.assertNotIn("tvTradesCompared", release["scoreboard"])
        # Both fields absent is a valid file: the pair is optional, never half present.
        absent = copy.deepcopy(canonical)
        absent["scoreboard"].pop("tvTradesCompared")
        absent["scoreboard"].pop("tvTradesComparedSource")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts.json"
            path.write_text(json.dumps(absent))
            result = self.invoke(path)
            self.assertEqual(result.returncode, 0, result.stderr)

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
            "inventory-missing": lambda f: f.pop("inventory"),
            "compared-without-source": lambda f: f["scoreboard"].pop("tvTradesComparedSource"),
            "source-without-compared": lambda f: f["scoreboard"].pop("tvTradesCompared"),
            "compared-source-text": lambda f: f["scoreboard"].update(
                tvTradesComparedSource=f["scoreboard"]["tvTradesComparedSource"] + " Exported by a tool."),
            "compared-negative": lambda f: f["scoreboard"].update(tvTradesCompared=-1),
            "compared-float": lambda f: f["scoreboard"].update(tvTradesCompared=1.5),
            "compared-bool": lambda f: f["scoreboard"].update(tvTradesCompared=True),
            "compared-unsafe-integer": lambda f: f["scoreboard"].update(tvTradesCompared=2**53),
            "compared-in-release": lambda f: f["releases"]["1.0.1"]["scoreboard"].update(
                tvTradesCompared=f["scoreboard"]["tvTradesCompared"],
                tvTradesComparedSource=f["scoreboard"]["tvTradesComparedSource"]),
            "compared-per-lane": lambda f: f["scoreboard"]["pairs"][0].update(tvTradesCompared=1),
            "compared-per-scope": lambda f: f["scoreboard"]["scopes"]["corpus"].update(tvTradesCompared=1),
            "trade-rows-public": lambda f: f["scoreboard"].update(tvTradeRows=1),
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
