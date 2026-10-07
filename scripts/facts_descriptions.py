#!/usr/bin/env python3
"""Resolve immutable current-main facts, reconcile public descriptions, emit receipts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from facts_validate import (FACTS_SCHEMA, MAX_INPUT_BYTES, InputError, decode, digest,
                            read_bytes, read_json, require)
from repo_descriptions import (COMMIT, DEFAULT_POLICY, PUBLIC_REPOSITORIES, clean_text,
                               fields, render, render_document, repository_identity)

HUB = "pineforge-4pass/pineforge-release"
SOURCE_FILES = ("facts/facts.json", "facts/repo-descriptions.json", "facts/facts.schema.json")
MANAGED_ROLES = ("engine", "codegen-oss", "corpus")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class ApiError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("GitHub redirect refused")


class GitHub:
    def __init__(self, test_api=None):
        self.base = "https://api.github.com"
        self.read_token = os.environ.get("FACTS_READ_TOKEN", "")
        self.write_token = os.environ.get("FACTS_WRITE_TOKEN", "")
        if test_api is not None:
            parsed = urlsplit(test_api)
            require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                    and not parsed.username and not parsed.password and parsed.path == ""
                    and not parsed.query and not parsed.fragment, "test API must be an explicit loopback origin")
            require(all(not token or token.startswith("test-") for token in (self.read_token, self.write_token)),
                    "real credentials are forbidden on the test transport")
            self.base = test_api
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, *, description=None):
        method = "PATCH" if description is not None else "GET"
        token = self.write_token if method == "PATCH" else self.read_token
        payload = None
        if method == "PATCH":
            require(path in {"/repos/" + PUBLIC_REPOSITORIES[role] for role in MANAGED_ROLES},
                    "write target is not managed")
            clean_text(description, description=True)
            require(bool(token), "a descriptions App token is required")
            payload = json.dumps({"description": description}, ensure_ascii=True, separators=(",", ":")).encode()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "pineforge-facts-descriptions", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(self.base + path, data=payload, headers=headers, method=method),
                                  timeout=30) as response:
                raw = response.read(MAX_INPUT_BYTES + 1)
            return decode(raw)
        except HTTPError as error:
            # A PATCH 403 does not establish missing permission. Do not echo response bodies.
            raise ApiError(f"GitHub {method} failed (HTTP {error.code})") from error
        except (URLError, OSError, TimeoutError, HTTPException) as error:
            raise ApiError(f"GitHub {method} network failure") from error
        except InputError as error:
            raise ApiError(f"GitHub {method} returned invalid data") from error

    def main_commit(self):
        response = self.request(f"/repos/{HUB}/commits/main")
        require(isinstance(response, dict) and isinstance(response.get("sha"), str)
                and COMMIT.fullmatch(response["sha"]), "GitHub returned an invalid main commit")
        return response["sha"]

    def source_bytes(self, commit, name):
        require(COMMIT.fullmatch(commit) and name in SOURCE_FILES, "invalid immutable source")
        response = self.request(f"/repos/{HUB}/contents/{name}?ref={commit}")
        require(isinstance(response, dict) and response.get("type") == "file"
                and response.get("path") == name and response.get("encoding") == "base64"
                and isinstance(response.get("content"), str), "invalid source content envelope")
        try:
            raw = base64.b64decode(response["content"].replace("\n", ""), validate=True)
        except ValueError as error:
            raise InputError("invalid source encoding") from error
        require(len(raw) <= MAX_INPUT_BYTES, "source exceeds size limit")
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        require(response.get("sha") == blob, "source Git blob digest mismatch")
        return raw

    def public_repository(self, identity):
        identity = repository_identity(identity)
        require(identity in PUBLIC_REPOSITORIES.values(), "repository is not confirmed public")
        response = self.request("/repos/" + identity)
        if (not isinstance(response, dict) or response.get("private") is not False
                or repository_identity(response.get("full_name")) != identity):
            raise ApiError("GitHub did not confirm the exact public repository")
        return response

    def description(self, row):
        identity = repository_identity(row["repo"])
        require(identity == PUBLIC_REPOSITORIES[row["role"]], "public role identity differs")
        response = self.public_repository(identity)
        if "description" not in response or not (response["description"] is None
                                                  or isinstance(response["description"], str)):
            raise ApiError("GitHub description has invalid shape")
        return response["description"]


def apply_policy(document):
    for row in document["repositories"]:
        expected = "managed" if row["role"] in MANAGED_ROLES else "HOLD" if row["role"] == "hpo" else "static"
        require(row["disposition"] == expected, "apply policy cannot promote held or static rows")
    return document


def prepare(args):
    # Validate the complete local facts AND policy before even a source GET.
    apply_policy(render(args.facts, args.policy))
    require(not args.output_dir.exists(), "source output directory already exists")
    api = GitHub(args.test_api)
    api.public_repository(HUB)
    commit = api.main_commit()  # The event SHA is deliberately only a wake-up.
    raw = {name: api.source_bytes(commit, name) for name in SOURCE_FILES}
    require(raw["facts/facts.schema.json"] == read_bytes(FACTS_SCHEMA), "source schema changed; retry current code")
    document = apply_policy(render_document(decode(raw["facts/facts.json"]),
                                            decode(raw["facts/repo-descriptions.json"]),
                                            digest(raw["facts/facts.json"]), digest(raw["facts/repo-descriptions.json"])))
    source = {"schema": "pineforge/facts-source/v1", "source_commit": commit,
              "facts_sha256": document["facts_sha256"], "policy_sha256": document["policy_sha256"]}
    # No output directory, artifact, or stdout exists until the whole source validates.
    args.output_dir.mkdir(parents=True)
    for name, contents in raw.items():
        (args.output_dir / Path(name).name).write_bytes(contents)
    (args.output_dir / "source.json").write_text(json.dumps(source, indent=2, sort_keys=True) + "\n")
    if args.github_output:
        targets = ",".join(PUBLIC_REPOSITORIES[role].split("/", 1)[1] for role in MANAGED_ROLES)
        with args.github_output.open("a") as output:
            output.write(f"source_commit={commit}\nfacts_sha256={source['facts_sha256']}\nrepositories={targets}\n")
    return source, 0


def load_source(directory):
    document = apply_policy(render(directory / "facts.json", directory / "repo-descriptions.json"))
    source, _ = read_json(directory / "source.json")
    fields(source, {"schema", "source_commit", "facts_sha256", "policy_sha256"})
    require(source["schema"] == "pineforge/facts-source/v1" and COMMIT.fullmatch(source["source_commit"]),
            "invalid source receipt")
    for field in ("facts_sha256", "policy_sha256"):
        require(DIGEST.fullmatch(source[field]) and source[field] == document[field], "source receipt digest differs")
    require(read_bytes(directory / "facts.schema.json") == read_bytes(FACTS_SCHEMA), "source schema differs")
    return source, document


def mode(args, api):
    # No reliable structured mint error is exposed by create-github-app-token.
    # Fail visibly without claiming a permission diagnosis or using another App.
    if args.mint_outcome == "failure":
        return False, "mint-failure-unclassified", 2
    if args.setup == "missing":
        return False, "missing-setup", 1
    if args.dry_run == "true":
        return False, "dry-run", 0
    if (os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REF_PROTECTED") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch", "schedule"}):
        return False, "unauthorized-event-or-ref", 2
    if os.environ.get("FACTS_DESCRIPTIONS_APPLY_ENABLED") != "true":
        return False, "rollout-disabled", 0
    if args.mint_outcome != "success":
        return False, "mint-not-attempted", 2
    if not api.write_token:
        return False, "mint-token-missing", 2
    return True, "apply-authorized", 0


def reconcile(args):
    source, document = load_source(args.source_dir)
    api = GitHub(args.test_api)
    write, diagnostic, code = mode(args, api)
    receipt = dict(source, schema="pineforge/facts-descriptions-receipt/v1", consumer="descriptions",
                   dry_run=args.dry_run == "true", diagnostic=diagnostic, permission_unavailable_proven=False,
                   status="report-only", repositories=document["repositories"])
    rows = receipt["repositories"]
    try:
        # Bind the receipt to the actual immutable source, not caller-asserted hashes.
        api.public_repository(HUB)
        if api.main_commit() != source["source_commit"]:
            if code == 0:
                return dict(receipt, status="superseded", alert=False, exit_code=0), 0
            return dict(receipt, alert=True, exit_code=code), code
        for name, field in (("facts/facts.json", "facts_sha256"), ("facts/repo-descriptions.json", "policy_sha256")):
            require(digest(api.source_bytes(source["source_commit"], name)) == source[field], "immutable source differs")
        # Check every identity before the first write. No private API record is copied to the receipt.
        for row in rows:
            try:
                row["actual"] = api.description(row)
                row["status"] = "match" if row["actual"] == row["expected"] else "drift"
            except (ApiError, InputError) as error:
                row["status"], row["error"] = "error", str(error)
                code = 2
        if write and code < 2:
            ordered = [next(row for row in rows if row["role"] == role) for role in MANAGED_ROLES]
            for row in ordered:
                if row["status"] == "match":
                    continue
                # Re-read this public identity, then source immediately before each PATCH.
                # This includes the first write after all comparisons; source movement is a clean hold.
                actual = api.description(row)
                if api.main_commit() != source["source_commit"]:
                    return dict(receipt, status="superseded", alert=False, exit_code=0), 0
                if actual == row["expected"]:
                    row.update(actual=actual, status="match")
                    continue
                try:
                    api.request("/repos/" + repository_identity(row["repo"]), description=row["expected"])
                    actual = api.description(row)
                    row["actual"] = actual
                    if actual != row["expected"]:
                        raise ApiError("description readback mismatch")
                    row.update(actual=actual, status="applied")
                except (ApiError, InputError) as error:
                    row["status"], row["error"] = "error", str(error)
                    code = 2
                    break  # Engine first; a refused write never falls back or proceeds silently.
            receipt["status"] = ("applied" if any(row["status"] == "applied" for row in rows)
                                 else "drift" if any(row["status"] == "drift" for row in rows) else "match")
        code = max(code, int(any(row.get("status") == "drift" for row in rows)))
    except (ApiError, InputError) as error:
        receipt["error"] = str(error)
        code = 2
    if code == 2:
        receipt["status"] = "error"
    receipt.update(alert=code != 0, exit_code=code)
    return receipt, code


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", allow_abbrev=False)
    prep.add_argument("--facts", required=True, type=Path)
    prep.add_argument("--policy", default=DEFAULT_POLICY, type=Path)
    prep.add_argument("--output-dir", required=True, type=Path)
    prep.add_argument("--github-output", type=Path)
    run = commands.add_parser("reconcile", allow_abbrev=False)
    run.add_argument("--source-dir", required=True, type=Path)
    run.add_argument("--dry-run", choices=("true", "false"), default="true")
    run.add_argument("--setup", choices=("ready", "missing"), default="missing")
    run.add_argument("--mint-outcome", choices=("success", "failure", "skipped"), default="skipped")
    run.add_argument("--receipt", type=Path)
    for command in (prep, run):
        command.add_argument("--test-api", help=argparse.SUPPRESS)
    options = [argument.split("=", 1)[0] for argument in sys.argv[1:] if argument.startswith("--")]
    if len(options) != len(set(options)):
        parser.error("repeated options are ambiguous")
    args = parser.parse_args()
    try:
        result, code = prepare(args) if args.command == "prepare" else reconcile(args)
        output = json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
        if args.command == "reconcile" and args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(output)
        sys.stdout.write(output)
        return code
    except (InputError, ApiError, OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        print("facts-descriptions: invalid input or source unavailable; no receipt emitted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
