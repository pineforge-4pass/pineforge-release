#!/usr/bin/env python3
"""Render public repository descriptions and check them with read-only GETs."""
from __future__ import annotations

import argparse
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

from facts_validate import InputError, decode, read_json, validate_facts

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = ROOT / "facts" / "repo-descriptions.json"
API_TIMEOUT_SECONDS = 60
MAX_INPUT_BYTES = 2 * 1024 * 1024
ROLES = {"engine", "codegen-oss", "corpus", "hpo", "release", "backtest-mcp"}
PUBLIC_REPOSITORIES = {role: "pineforge-4pass/pineforge-" + role for role in ROLES}
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
TOKEN = re.compile(r"\{\{(facts|license|word):([A-Za-z0-9_.-]+)(?:\|(int|grouped|decimal|text))?\}\}")


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


def repository_identity(value):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value):
        raise InputError("invalid public repository identity")
    return value.casefold()


def source(value):
    fields(value, {"repo", "commit", "url"})
    identity = repository_identity(value["repo"])
    if identity not in PUBLIC_REPOSITORIES.values():
        raise InputError("source repository is not confirmed public")
    if not isinstance(value["commit"], str) or not COMMIT.fullmatch(value["commit"]):
        raise InputError("source must pin a full public commit")
    prefix, suffix = "https://github.com/", f"/blob/{value['commit']}/LICENSE"
    url = value["url"]
    if (not isinstance(url, str) or not url.startswith(prefix) or not url.endswith(suffix)
            or repository_identity(url[len(prefix):-len(suffix)]) != identity):
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
    policy, policy_hash = read_json(policy_path)
    return render_document(facts, policy, facts_hash, policy_hash)


def render_document(facts, policy, facts_hash, policy_hash):
    validate_facts(facts)
    scoreboard = facts["scoreboard"]
    if (scoreboard["belowStrong"] != 0
            or scoreboard["excellent"] + scoreboard["strong"] != scoreboard["graded"]):
        raise InputError("engine all-graded claim requires belowStrong == 0 and excellent + strong == graded")
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
        identity = repository_identity(row["repo"])
        if identity != repository_identity(row["source"]["repo"]) or row["public"] is not True:
            raise InputError("unverified or nonpublic repository")
        if row["role"] not in ROLES or row["role"] in seen_roles or identity in seen_repos:
            raise InputError("unknown or duplicate repository/role")
        seen_roles.add(row["role"])
        seen_repos.add(identity)
        clean_text(row["reason"])
        disposition = row["disposition"]
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
    for row in rows:
        if repository_identity(row["repo"]) != PUBLIC_REPOSITORIES[row["role"]]:
            raise InputError("role does not match its confirmed-public repository")
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
            if (not isinstance(response, dict) or response.get("private") is not False
                    or repository_identity(response.get("full_name")) != repository_identity(row["repo"])):
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
