#!/usr/bin/env python3
"""Validate current canonical facts and signal one independently gated consumer."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from facts_descriptions import ApiError, GitHub, HUB
from facts_validate import (FACTS_SCHEMA, MAX_INPUT_BYTES, InputError, decode, digest, load_facts,
                            read_bytes, read_json, require, validate_facts)

OWNER = "pineforge-4pass"
EVENT = "facts-update"
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
TARGET = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\Z")
SOURCE_FILES = ("facts/facts.json", "facts/facts.schema.json")


class Failure(ValueError):
    """Only fixed public diagnostics and a numeric HTTP status cross this boundary."""
    def __init__(self, diagnostic, category, http_status=None):
        super().__init__(diagnostic)
        self.diagnostic = diagnostic
        self.category = category
        self.http_status = http_status


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats arbitrary option values, potentially a secret.
        raise InputError("invalid CLI arguments")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("github-redirect-refused", "http", code)


def payload_fields(payload):
    require(type(payload) is dict and set(payload) == {"commit", "sha256", "dry_run"},
            "invalid payload fields")
    require(isinstance(payload["commit"], str) and COMMIT.fullmatch(payload["commit"]),
            "invalid source commit")
    require(isinstance(payload["sha256"], str) and DIGEST.fullmatch(payload["sha256"]),
            "invalid source digest")
    require(type(payload["dry_run"]) is bool, "dry_run must be a JSON boolean")
    return payload


def target_name(value):
    # An ASCII short name is also safe as one create-github-app-token repository
    # input. No delimiters, URLs, controls, whitespace, owner, or encoded slashes.
    require(isinstance(value, str) and TARGET.fullmatch(value), "invalid consumer target")
    return value


def enabled():
    value = os.environ.get("FACTS_WEB_DISPATCH_ENABLED", "")
    require(value in ("", "false", "true"), "invalid rollout flag")
    return value == "true"


def authorized_context():
    require(os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_REF_PROTECTED") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") in {"push", "workflow_dispatch", "schedule"},
            "protected main wake-up required")


def public_receipt(command):
    return {"schema": "pineforge/facts-fanout-receipt/v1", "consumer": "web",
            "command": command, "status": "error", "category": "validation",
            "diagnostic": "invalid-input", "http_status": None, "mint_allowed": False,
            "permission_unavailable_proven": False, "installed": False,
            "alert": True, "exit_code": 2}


def outcome(receipt, status, diagnostic, *, code=0, category="plan", alert=False, http_status=None):
    receipt.update(status=status, diagnostic=diagnostic, exit_code=code,
                   category=category, alert=alert, http_status=http_status)
    return code


class ConsumerGitHub(GitHub):
    """Reuse immutable public-source reads; the only new write is a dispatch."""
    def __init__(self, test_api=None):
        super().__init__(test_api)
        self.dispatch_token = os.environ.get("FACTS_DISPATCH_TOKEN", "")
        if test_api is not None:
            require(not self.dispatch_token or self.dispatch_token.startswith("test-"),
                    "real credentials forbidden on test transport")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def dispatch(self, target, payload):
        target_name(target)
        payload_fields(payload)
        require(bool(self.dispatch_token), "consumer token missing")
        body = json.dumps({"event_type": EVENT, "client_payload": payload},
                          sort_keys=True, separators=(",", ":")).encode("ascii")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "pineforge-facts-fanout", "Content-Type": "application/json",
                   "Authorization": "Bearer " + self.dispatch_token}
        request = Request(self.base + f"/repos/{OWNER}/{target}/dispatches", data=body,
                          headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 204:
                    raise Failure("github-unexpected-status", "http", response.status)
        except HTTPError as error:
            # Do not read/print a private response body, request URL, or headers.
            raise Failure("github-post-failed", "http", error.code) from error
        except (URLError, OSError, TimeoutError, HTTPException) as error:
            raise Failure("github-post-network-failure", "network") from error


def immutable_source(api, commit):
    raw = {name: api.source_bytes(commit, name) for name in SOURCE_FILES}
    require(raw["facts/facts.schema.json"] == read_bytes(FACTS_SCHEMA),
            "source schema changed; retry current code")
    validate_facts(decode(raw["facts/facts.json"]))
    return raw


def mint_output(args, allowed):
    if args.github_output:
        with args.github_output.open("a") as stream:
            stream.write("mint_allowed=" + ("true" if allowed else "false") + "\n")


def prepare(args, receipt):
    gate = enabled()
    target = os.environ.get("FACTS_WEB_REPOSITORY", "")
    if target or gate:
        target_name(target)
    missing = [name for name in ("FACTS_WEB_REPOSITORY", "PINEFORGE_APP_ID", "PINEFORGE_APP_PRIVATE_KEY")
               if not os.environ.get(name)]
    if gate:
        authorized_context()
        if missing:
            raise Failure("missing-setup", "setup")
    # Validate before a source GET; nothing untrusted is copied into diagnostics.
    load_facts(args.facts)
    require(not args.output_dir.exists(), "source directory already exists")
    api = ConsumerGitHub(args.test_api)
    api.public_repository(HUB)
    commit = api.main_commit()  # Event SHA never selects the canonical snapshot.
    raw = immutable_source(api, commit)
    payload = payload_fields({"commit": commit, "sha256": digest(raw["facts/facts.json"]),
                              "dry_run": args.dry_run == "true"})
    receipt.update(payload, missing_setup=missing)
    if api.main_commit() != commit:
        mint_output(args, False)
        return outcome(receipt, "superseded", "source-moved")
    # No partial snapshot is published before the entire immutable source validates.
    args.output_dir.mkdir(parents=True)
    for name, contents in raw.items():
        (args.output_dir / Path(name).name).write_bytes(contents)
    (args.output_dir / "payload.json").write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    receipt["mint_allowed"] = gate
    mint_output(args, gate)
    if missing:
        return outcome(receipt, "setup-required", "rollout-disabled-missing-setup", alert=True)
    return outcome(receipt, "prepared" if gate else "planned",
                   "dispatch-authorized" if gate else "rollout-disabled")


def load_source(directory):
    payload, _ = read_json(directory / "payload.json")
    payload_fields(payload)
    raw = read_bytes(directory / "facts.json")
    require(digest(raw) == payload["sha256"], "source receipt digest differs")
    validate_facts(decode(raw))
    require(read_bytes(directory / "facts.schema.json") == read_bytes(FACTS_SCHEMA),
            "source schema differs")
    return payload


def dispatch(args, receipt):
    payload = load_source(args.source_dir)
    receipt.update(payload)
    gate = enabled()
    target = os.environ.get("FACTS_WEB_REPOSITORY", "")
    if target or gate:
        target_name(target)
    if not gate:
        require(args.mint_outcome == "skipped", "unexpected mint while disabled")
        return outcome(receipt, "planned", "rollout-disabled")
    authorized_context()
    if args.mint_outcome == "failure":
        raise Failure("mint-failure-unclassified", "mint")
    if args.mint_outcome != "success":
        raise Failure("mint-not-attempted", "mint")
    api = ConsumerGitHub(args.test_api)
    if not api.dispatch_token:
        raise Failure("mint-token-missing", "mint")
    api.public_repository(HUB)
    if api.main_commit() != payload["commit"]:
        return outcome(receipt, "superseded", "source-moved")
    raw = immutable_source(api, payload["commit"])
    require(digest(raw["facts/facts.json"]) == payload["sha256"], "immutable source differs")
    # A concurrent source move cannot be made atomic with GitHub's dispatch API.
    # Recheck immediately before POST; the next serialized wake-up repairs a race.
    if api.main_commit() != payload["commit"]:
        return outcome(receipt, "superseded", "source-moved")
    api.dispatch(target, payload)
    return outcome(receipt, "dispatched", "signal-accepted", category="dispatch", http_status=204)


def status(args, receipt):
    """Read one bounded public workflow-runs response, never logs or private URLs."""
    document = decode(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
    require(type(document) is dict and type(document.get("workflow_runs")) is list,
            "missing run telemetry")
    runs = document["workflow_runs"]
    require(len(runs) <= 20, "unbounded run telemetry")
    now = datetime.now(timezone.utc)
    completed, failed, stalled = [], [], []
    identities = set()
    for row in runs:
        require(type(row) is dict and type(row.get("id")) is int and row["id"] > 0,
                "invalid run identity")
        require(row["id"] not in identities, "duplicate run identity")
        identities.add(row["id"])
        require(row.get("head_branch") == "main"
                and row.get("path") == ".github/workflows/facts-fanout.yml", "wrong workflow telemetry")
        require(row.get("status") in {"completed", "queued", "in_progress", "waiting", "pending", "requested"},
                "invalid run status")
        require(row.get("conclusion") in {None, "success", "failure", "cancelled", "timed_out",
                                          "action_required", "neutral", "skipped", "stale", "startup_failure"},
                "invalid run conclusion")
        require(isinstance(row.get("created_at"), str), "missing run timestamp")
        started = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        require(started.tzinfo is not None, "run timestamp must include timezone")
        age = (now - started).total_seconds()
        require(age >= 0, "future run timestamp")
        if row["conclusion"] == "cancelled":
            continue
        if row["status"] == "completed":
            require(row["conclusion"] is not None, "missing completed conclusion")
            completed.append((started, row["id"], age))
            if row["conclusion"] != "success" and age <= 1800:
                failed.append(row["id"])
        elif age > 900:
            stalled.append(row["id"])
    latest = max(completed) if completed else None
    stale = latest is None or latest[2] > 1800
    alert = stale or bool(failed) or bool(stalled)
    receipt.update(latest_completed=latest[1] if latest else None, failed_runs=failed,
                   stalled_runs=stalled, telemetry_missing_or_stale=stale,
                   served_freshness="unverified")
    return outcome(receipt, "telemetry-alert" if alert else "workflow-observed",
                   "run-telemetry-only", code=1 if alert else 0, category="telemetry", alert=alert)


def parser():
    result = SafeParser(description=__doc__, allow_abbrev=False)
    commands = result.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", allow_abbrev=False)
    prep.add_argument("--facts", type=Path, required=True)
    prep.add_argument("--dry-run", choices=("true", "false"), default="true")
    prep.add_argument("--output-dir", type=Path, required=True)
    prep.add_argument("--github-output", type=Path)
    run = commands.add_parser("dispatch", allow_abbrev=False)
    run.add_argument("--source-dir", type=Path, required=True)
    run.add_argument("--mint-outcome", choices=("success", "failure", "skipped"), default="skipped")
    monitor = commands.add_parser("status", allow_abbrev=False)
    for command in (prep, run, monitor):
        command.add_argument("--receipt", type=Path)
    for command in (prep, run):
        command.add_argument("--test-api", help=argparse.SUPPRESS)
    return result


def main():
    receipt = public_receipt("invalid")
    args = None
    try:
        options = [item.split("=", 1)[0] for item in sys.argv[1:] if item.startswith("--")]
        require(len(options) == len(set(options)), "repeated options")
        args = parser().parse_args()
        receipt = public_receipt(args.command)
        code = {"prepare": prepare, "dispatch": dispatch, "status": status}[args.command](args, receipt)
    except Failure as error:
        code = outcome(receipt, "error", error.diagnostic, code=2, category=error.category,
                       http_status=error.http_status, alert=True)
    except ApiError as error:
        # The existing public GET helper emits fixed messages, never API bodies.
        match = re.fullmatch(r"GitHub GET failed \(HTTP ([0-9]{3})\)", str(error))
        category = "http" if match else "network" if "network failure" in str(error) else "source"
        code = outcome(receipt, "error", "github-source-failed", code=2, category=category,
                       http_status=int(match[1]) if match else None, alert=True)
    except (InputError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        code = outcome(receipt, "error", "invalid-input", code=2, category="validation", alert=True)
    except OSError:
        code = outcome(receipt, "error", "local-io-failed", code=2, category="io", alert=True)
    if code:
        receipt["mint_allowed"] = False
    output = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args and args.receipt:
        try:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(output)
        except OSError:
            print("facts-fanout: receipt unavailable; missing telemetry", file=sys.stderr)
            return 2
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
