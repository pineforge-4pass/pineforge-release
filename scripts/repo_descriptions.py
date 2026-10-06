#!/usr/bin/env python3
"""Render public repository descriptions and check them with read-only GETs."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = ROOT / "facts" / "repo-descriptions.json"
FACTS_SCHEMA = ROOT / "facts" / "facts.schema.json"
API_TIMEOUT_SECONDS = 60
MAX_INPUT_BYTES = 2 * 1024 * 1024
ROLES = {"engine", "codegen-oss", "corpus", "hpo", "release", "backtest-mcp"}
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
TOKEN = re.compile(r"\{\{(facts|license|word):([A-Za-z0-9_.-]+)(?:\|(int|grouped|decimal|text))?\}\}")


class InputError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(raw, name):
    def invalid_constant(value):
        raise InputError(f"non-finite JSON number: {value}")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise InputError(f"{name}: {error}") from error


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    return decode(raw, str(path)), hashlib.sha256(raw).hexdigest()


def validate_schema(value, schema, root, location="facts"):
    supported = {"$schema", "$id", "title", "$defs", "$ref", "type", "const", "anyOf",
                 "required", "properties", "additionalProperties", "propertyNames", "items",
                 "pattern", "minimum", "maximum", "format"}
    if set(schema) - supported:
        raise InputError("unsupported facts schema keyword; upgrade the validator explicitly")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise InputError("unsupported schema reference")
        validate_schema(value, root["$defs"][reference[8:]], root, location)
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                validate_schema(value, choice, root, location)
                break
            except InputError:
                continue
        else:
            raise InputError(f"{location}: does not match any allowed type")
    types = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float), "null": value is None}
    if "type" in schema and not types.get(schema["type"], False):
        raise InputError(f"{location}: expected {schema['type']}")
    if "const" in schema and value != schema["const"]:
        raise InputError(f"{location}: incorrect schema version")
    if type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise InputError(f"{location}: non-finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise InputError(f"{location}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise InputError(f"{location}: above maximum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value, re.ASCII):
            raise InputError(f"{location}: invalid spelling")
        if "format" in schema:
            try:
                if schema["format"] == "date":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, re.ASCII):
                        raise ValueError("invalid date spelling")
                    datetime.date.fromisoformat(value)
                elif schema["format"] == "date-time":
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value, re.ASCII):
                        raise ValueError("invalid timestamp spelling")
                    datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                else:
                    raise ValueError("unsupported format")
            except ValueError as error:
                raise InputError(f"{location}: {error}") from error
    if isinstance(value, dict):
        if set(schema.get("required", [])) - set(value):
            raise InputError(f"{location}: missing required fields")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if "propertyNames" in schema:
                validate_schema(key, schema["propertyNames"], root, location)
            rule = properties.get(key, schema.get("additionalProperties", True))
            if rule is False:
                raise InputError(f"{location}: unknown field {key}")
            if isinstance(rule, dict):
                validate_schema(child, rule, root, location + "." + key)
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            validate_schema(child, schema["items"], root, f"{location}[{index}]")


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        raise InputError("missing or unknown policy fields")


def clean_text(value, *, description=False):
    if not isinstance(value, str) or not value.strip() or any(
        unicodedata.category(character).startswith("C") or character in "\u2028\u2029"
        for character in value
    ):
        raise InputError("empty text or newline/control character")
    if description and len(value) > 350:
        raise InputError("GitHub descriptions must not exceed 350 characters")
    return value


def source(value):
    fields(value, {"repo", "commit", "url"})
    if not isinstance(value["repo"], str) or not REPOSITORY.fullmatch(value["repo"]):
        raise InputError("invalid public repository identity")
    if not isinstance(value["commit"], str) or not COMMIT.fullmatch(value["commit"]):
        raise InputError("source must pin a full public commit")
    expected = f"https://github.com/{value['repo']}/blob/{value['commit']}/LICENSE"
    if value["url"] != expected:
        raise InputError("LICENSE source URL does not match its repository/commit")


def flatten(value, prefix="", result=None):
    if result is None:
        result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(child, prefix + "." + key if prefix else key, result)
    elif not isinstance(value, list):
        if prefix in result:
            raise InputError("ambiguous facts token path")
        result[prefix] = value
    return result


def render_template(template, facts, policy):
    clean_text(template)
    names, licenses = set(), set()

    def replace(match):
        kind, name, formatting = match.groups()
        if kind == "facts":
            if name not in facts:
                raise InputError(f"unknown or missing facts token: {name}")
            value = facts[name]
            names.add(name)
            if formatting in {"int", "grouped"} and type(value) is int and value >= 0:
                return format(value, ",d") if formatting == "grouped" else str(value)
            if formatting == "decimal" and type(value) in (int, float) and value >= 0 and math.isfinite(value):
                return str(value)
            if formatting == "text" and isinstance(value, str):
                return clean_text(value)
            raise InputError(f"invalid token type or format: {name}")
        if formatting is not None:
            raise InputError("only facts tokens take a format")
        if kind == "license":
            if name not in policy["licenses"]:
                raise InputError(f"unknown license token: {name}")
            licenses.add(name)
            return policy["labels"][policy["licenses"][name]["label"]]
        if name not in policy["wording"]:
            raise InputError(f"unknown wording token: {name}")
        return policy["wording"][name]

    literals = TOKEN.sub("", template)
    if "{" in literals or "}" in literals or any(character.isdecimal() for character in literals):
        raise InputError("unresolved token or literal numeric claim in template")
    expected = clean_text(TOKEN.sub(replace, template), description=True)
    if "{{" in expected or "}}" in expected:
        raise InputError("unresolved token in description")
    sources = [dict(id=name, label=policy["labels"][policy["licenses"][name]["label"]],
                    **{key: policy["licenses"][name][key] for key in ("repo", "commit", "url")})
               for name in sorted(licenses)]
    return expected, sorted(names), sources


def render(facts_path, policy_path):
    facts, facts_hash = read_json(facts_path)
    schema, _ = read_json(FACTS_SCHEMA)
    validate_schema(facts, schema, schema)
    policy, policy_hash = read_json(policy_path)
    fields(policy, {"schema", "version", "labels", "licenses", "wording", "repositories"})
    if policy["schema"] != "pineforge/repo-descriptions/v1" or type(policy["version"]) is not int or policy["version"] < 1:
        raise InputError("invalid description policy version")
    for mapping in (policy["labels"], policy["licenses"], policy["wording"]):
        if not isinstance(mapping, dict):
            raise InputError("policy maps must be objects")
    for value in [*policy["labels"].values(), *policy["wording"].values()]:
        clean_text(value)
    for license_value in policy["licenses"].values():
        fields(license_value, {"label", "repo", "commit", "url"})
        source({key: license_value[key] for key in ("repo", "commit", "url")})
        if license_value["label"] not in policy["labels"]:
            raise InputError("unknown approved license label")
    if not isinstance(policy["repositories"], list):
        raise InputError("repository manifest must be an array")
    seen_roles, seen_repos, rows = set(), set(), []
    for row in policy["repositories"]:
        fields(row, {"role", "repo", "public", "disposition", "reason", "source"}, {"template", "approved_text"})
        source(row["source"])
        if row["repo"] != row["source"]["repo"] or row["public"] is not True:
            raise InputError("unverified or nonpublic repository")
        if row["role"] not in ROLES or row["role"] in seen_roles or row["repo"] in seen_repos:
            raise InputError("unknown or duplicate repository/role")
        seen_roles.add(row["role"])
        seen_repos.add(row["repo"])
        clean_text(row["reason"])
        disposition = row["disposition"]
        if (row["role"] == "hpo" or row["repo"].split("/")[1] == "pineforge-hpo") and disposition != "HOLD":
            raise InputError("HPO must remain held; no setting-change command is permitted")
        if disposition == "HOLD":
            if "template" in row or "approved_text" not in row:
                raise InputError("held rows require only approved_text")
            expected = row["approved_text"]
            if expected is not None:
                clean_text(expected, description=True)
            tokens, sources = [], []
        elif disposition in {"managed", "static"}:
            if "approved_text" in row or "template" not in row:
                raise InputError("managed/static rows require only a template")
            expected, tokens, sources = render_template(row["template"], flatten(facts), policy)
            if disposition == "static" and tokens:
                raise InputError("static rows cannot contain facts claims")
        else:
            raise InputError("unknown disposition")
        rows.append(dict(role=row["role"], repo=row["repo"], disposition=disposition,
                         reason=row["reason"], expected=expected, tokens=tokens,
                         license_sources=sources, public_source=row["source"]))
    if seen_roles != ROLES:
        raise InputError("incomplete confirmed-public manifest")
    return dict(schema="pineforge/repo-descriptions-render/v1", policy_version=policy["version"],
                facts_sha256=facts_hash, policy_sha256=policy_hash, repositories=rows)


def check_live(document, executable):
    resolved = shutil.which(executable)
    if not resolved or not os.access(resolved, os.X_OK):
        raise InputError("GitHub executable is missing or not executable")
    exit_code = 0
    for row in document["repositories"]:
        try:
            process = subprocess.run(
                [resolved, "api", "--method", "GET", "--hostname", "github.com", "repos/" + row["repo"]],
                capture_output=True, timeout=API_TIMEOUT_SECONDS, check=False,
            )
            if process.returncode:
                raise InputError(f"GitHub API/auth error (exit {process.returncode}): " +
                                 process.stderr.decode("utf-8", errors="replace")[:500])
            if len(process.stdout) > MAX_INPUT_BYTES:
                raise InputError("GitHub response exceeds size limit")
            response = decode(process.stdout, "GitHub response")
            if not isinstance(response, dict) or response.get("private") is not False or response.get("full_name") != row["repo"]:
                raise InputError("GitHub did not confirm the exact public repository")
            if "description" not in response or not (response["description"] is None or isinstance(response["description"], str)):
                raise InputError("GitHub description is missing or has the wrong type")
            row["actual"] = response["description"]
            row["status"] = "match" if row["actual"] == row["expected"] else "drift"
            if row["status"] == "drift":
                exit_code = max(exit_code, 1)
        except (InputError, OSError, subprocess.TimeoutExpired) as error:
            row["status"], row["error"] = "error", str(error)
            exit_code = 2
    document["exit_code"] = exit_code
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "check"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--facts", required=True, type=Path)
        command.add_argument("--policy", default=DEFAULT_POLICY, type=Path)
        if name == "render":
            command.add_argument("--format", choices=("json", "commands"), default="json")
        else:
            command.add_argument("--live", required=True, action="store_true")
            command.add_argument("--gh", default="gh")
    raw_arguments = sys.argv[1:] if argv is None else argv
    options = [value.split("=", 1)[0] for value in raw_arguments if value.startswith("--")]
    if len(options) != len(set(options)):
        parser.error("repeated options are ambiguous")
    arguments = parser.parse_args(raw_arguments)
    try:
        document = render(arguments.facts, arguments.policy)
        exit_code = check_live(document, arguments.gh) if arguments.command == "check" else 0
        if arguments.command == "render" and arguments.format == "commands":
            lines = ["# TOP review only; this program never applies descriptions.",
                     "# facts_sha256=" + document["facts_sha256"],
                     "# policy_sha256=" + document["policy_sha256"]]
            for row in document["repositories"]:
                lines.append("# " + row["disposition"] + " " + json.dumps(row, ensure_ascii=True, sort_keys=True))
                if row["disposition"] != "HOLD":
                    lines.append(shlex.join(["gh", "repo", "edit", row["repo"], "--description", row["expected"]]))
            print("\n".join(lines))
        else:
            print(json.dumps(document, indent=2, ensure_ascii=True, sort_keys=True))
        return exit_code
    except (InputError, OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as error:
        print(f"repo-descriptions: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
