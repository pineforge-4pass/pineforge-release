#!/usr/bin/env python3
"""Validate all public facts before rendering or contacting any consumer."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource

ROOT = Path(__file__).resolve().parents[1]
FACTS_SCHEMA = ROOT / "facts/facts.schema.json"
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
COUNTS = ("graded", "excellent", "strong", "belowStrong", "engineErrors")
TIERS = ("excellent", "strong", "moderate", "weak", "minimal", "no-trades")


class InputError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InputError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(raw, name="input"):
    require(len(raw) <= MAX_INPUT_BYTES, "input exceeds size limit")

    def invalid_constant(value):
        raise InputError("non-finite JSON number")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        # Never repeat untrusted bytes, API bodies, or local paths in diagnostics.
        raise InputError("invalid JSON document") from error


def read_bytes(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    require(len(raw) <= MAX_INPUT_BYTES, "input exceeds size limit")
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    raw = read_bytes(path)
    return decode(raw), digest(raw)


def scalar_safety(value, key=""):
    if isinstance(value, dict):
        for name, child in value.items():
            scalar_safety(name)
            scalar_safety(child, name)
    elif isinstance(value, list):
        for child in value:
            scalar_safety(child)
    elif isinstance(value, str):
        require(bool(value.strip()) and not any(unicodedata.category(c).startswith("C")
                or c in "\u2028\u2029" for c in value), "empty or unsafe facts text")
        require(not re.search(r"(?:gs|s3)://|/(?:Users|home)/|\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
                              r"|\b[a-z0-9-]+-(?:workflow|scrapper)\b|gen-lang-client", value),
                "nonpublic locator in facts text")
    elif type(value) in (int, float):
        require(math.isfinite(value) and 0 <= value <= MAX_SAFE_INTEGER, "unsafe facts number")
        require(key.endswith("Pct") or type(value) is int, "counts must be JSON integers")


def tally(group):
    require(group["excellent"] + group["strong"] + group["belowStrong"] == group["graded"],
            "all-graded tier sum differs")
    require(sum(group["tiers"].values()) == group["graded"], "tier sum differs from graded")
    require(group["excellent"] == group["tiers"]["excellent"]
            and group["strong"] == group["tiers"]["strong"]
            and group["belowStrong"] == sum(group["tiers"][tier] for tier in TIERS[2:]),
            "tier summaries differ")


def scoreboard_invariants(score):
    provenance = score["provenance"]
    for field in ("engineCommit", "codegenCommit", "snapshotSha256", "populationSha256"):
        require(score[field] == provenance[field], "scoreboard provenance differs")
    require(score["id"] == provenance["baselineId"] and
            score["date"] == provenance["promotionDate"][:10], "baseline or promotion date differs")
    require(provenance["promotionDate"].endswith(("Z", "+00:00")), "promotion date must be UTC")
    require(score["source"] == (
        f'Registry baseline {provenance["baselineId"]}; digest-verified snapshot '
        f'{provenance["snapshotSha256"]}; private evidence sha256:{provenance["privateEvidenceSha256"]}.'),
        "scoreboard source does not bind its provenance")
    tally(score)
    require(score["graded"] + score["engineErrors"] + score["anomaliesExcluded"] == score["population"],
            "population sum differs")
    pairs = score["pairs"]
    require(len({(p["symbol"], p["timeframe"]) for p in pairs}) == len(pairs), "duplicate lane identity")
    require(score["lanes"] == len(pairs) and score["markets"] == len({p["symbol"] for p in pairs})
            and score["timeframes"] == len({p["timeframe"] for p in pairs}), "lane dimensions differ")
    for pair in pairs:
        require(re.fullmatch(r"[1-9][0-9]*[DSWM]?", pair["timeframe"]), "invalid lane timeframe")
        tally(pair)
        require(pair["corpusProbes"] + pair["closedProbes"] == pair["graded"] + pair["engineErrors"],
                "lane scope sum differs")
        require(pair["hardProbes"] <= pair["graded"] + pair["engineErrors"], "hard probes exceed lane")
    for field in COUNTS:
        require(sum(p[field] for p in pairs) == score[field], "lane count sum differs")
        require(sum(scope[field] for scope in score["scopes"].values()) == score[field],
                "scope count sum differs")
    for tier in TIERS:
        require(sum(p["tiers"][tier] for p in pairs) == score["tiers"][tier], "lane tier sum differs")
        require(sum(s["tiers"][tier] for s in score["scopes"].values()) == score["tiers"][tier],
                "scope tier sum differs")
    for name, scope in score["scopes"].items():
        tally(scope)
        require(score[name + "Probes"] == scope["graded"] + scope["engineErrors"]
                == sum(p[name + "Probes"] for p in pairs), "scope probes differ")
    hard_pairs = [pair for pair in pairs if pair["hardProbes"]]
    require(score["hardLane"] == (hard_pairs[0] if hard_pairs else None),
            "hard lane differs from lane evidence")
    for field, count in (("excellentPct", score["excellent"]), ("strongPct", score["strong"]),
                         ("excellentOrStrongPct", score["excellent"] + score["strong"])):
        expected = ((Decimal(count) * 100 / score["graded"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if score["graded"] else Decimal(0))
        require(Decimal(str(score[field])) == expected, "score percentage differs")


def validate_facts(facts):
    schema, _ = read_json(FACTS_SCHEMA)
    Draft202012Validator.check_schema(schema)

    def no_network(uri):
        raise NoSuchResource(ref=uri)

    # Explicitly prohibit remote $ref retrieval. The committed schema is self-contained.
    validator = Draft202012Validator(schema, format_checker=FormatChecker(),
                                    registry=Registry(retrieve=no_network))
    error = next(validator.iter_errors(facts), None)
    if error is not None:
        raise InputError(f"facts schema validation failed ({error.validator})")
    scalar_safety(facts)
    scoreboard_invariants(facts["scoreboard"])
    for version, release in facts["releases"].items():
        require(release["source"] == f"https://github.com/pineforge-4pass/pineforge-engine/blob/v{version}/README.md#validation-scoreboard",
                "release source identity differs")
        scoreboard_invariants(release["scoreboard"])
    if "inventory" in facts:
        inventory = facts["inventory"]
        release = facts["releases"].get(inventory["sourceRelease"])
        require(release is not None, "inventory source release is missing")
        historic = release["scoreboard"]
        require(inventory["populationSha256"] == historic["populationSha256"], "inventory population differs from release")
        require(inventory["closedScripts"] == inventory["communityScripts"] + inventory["probeScripts"],
                "historical script sum differs")
        require(inventory["closedProbes"] == inventory["communityProbes"] + inventory["probeScriptProbes"]
                == historic["closedProbes"], "historical probe sum differs")
        require(inventory["corpusScripts"] == historic["corpusProbes"], "historical corpus inventory differs")
        prefix = ("https://raw.githubusercontent.com/pineforge-4pass/pineforge-engine/"
                  f'{inventory["sourceCommit"]}/README.md#validation-scoreboard; historical release '
                  f'{inventory["sourceRelease"]} ')
        require(inventory["source"] == prefix + "authored-script and closed-trade inventory, independent of the active population; not registry row or slug totals.",
                "inventory source identity differs")
    return facts


def load_facts(path):
    facts, sha = read_json(path)
    return validate_facts(facts), sha


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--facts", required=True, type=Path)
    args = parser.parse_args()
    try:
        facts, sha = load_facts(args.facts)
        print(json.dumps({"sha256": sha, "schema": facts["schema"], "status": "valid"}, sort_keys=True))
        return 0
    except (InputError, OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        print("facts-validate: invalid facts or unavailable input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
