#!/usr/bin/env python3
"""Check release delivery assets and receiver workflow startup evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from release_artifacts import Client
from release_contract import (ASSET, EVENT_TYPES, HUB, Invalid, Pending, ROLES, SHA,
                              canonical, decode, match, payload, require)
import release_pipeline as pipeline


# Eight polls and 650 seconds of sleeps allow GitHub Actions to enqueue a
# repository dispatch before a missing receiver run fails notification.
RECEIVER_RUN_POLL_DELAYS_SECONDS = (0, 5, 15, 30, 60, 120, 180, 240)
RECEIVER_API_CALL_BUDGET = 64
RECEIVER_CHECK_API_CALL_BUDGET = 64
RECEIVER_MAX_API_PAGES = 8
RECEIVER_WORKFLOW_MARKERS = {
    role: "release-pair" if role == "compute" else "pineforge-release"
    for role in ROLES
}
TARGET_ENVIRONMENT = {
    role: "RELEASE_TARGET_" + role.upper() + "_REPOSITORY"
    for role in ROLES
}
PROTECTED_PATH_MARKERS = (
    (".config", "zsh", "keys"),
    (".config", "pineforge"),
    (".ssh",),
    (".aws",),
    (".codex",),
)


def timestamp(value):
    match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value)
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Invalid("malformed dispatch timestamp") from error


def protected_path(raw):
    for candidate in (raw, os.path.abspath(raw)):
        parts = Path(candidate).parts
        if parts and parts[-1].endswith(".env"):
            return True
        for marker in PROTECTED_PATH_MARKERS:
            if any(parts[index:index + len(marker)] == marker
                   for index in range(len(parts) - len(marker) + 1)):
                return True
    return False


def safe_path(raw):
    require(isinstance(raw, str) and raw and not raw.startswith("~"), "delivery path refused")
    require(not protected_path(raw), "delivery path refused")
    path = Path(raw).resolve(strict=False)
    require(not protected_path(str(path)), "delivery path refused")
    return path


def read_delivery(raw_path):
    path = safe_path(raw_path)
    # Diagnostic copies and test tools must identify the file before opening it.
    print("following file: " + str(path), file=sys.stderr)
    obj = decode(path.read_bytes())
    require(type(obj) is dict and obj.get("state") == "dispatch_accepted",
            "delivery is not accepted")
    match("sha256:" + SHA, obj.get("event_id"))
    match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", obj.get("consumer"))
    timestamp(obj.get("timestamp"))
    return obj


class ApiBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def take(self):
        if self.used >= self.limit:
            raise Invalid("receiver API call budget exceeded")
        self.used += 1


def object_pages(client, route, key, budget=None):
    budget = budget or ApiBudget(RECEIVER_API_CALL_BUDGET)
    rows = []
    for page in range(1, RECEIVER_MAX_API_PAGES + 1):
        budget.take()
        result = client.api(route + ("&" if "?" in route else "?") + f"per_page=100&page={page}")
        require(type(result) is dict and type(result.get(key)) is list,
                "unexpected receiver API response")
        page_rows = result[key]
        rows.extend(page_rows)
        if len(page_rows) < 100:
            return rows
    raise Invalid("receiver API pagination limit exceeded")


def repository_name(repository):
    match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
    return "pineforge-4pass/" + repository


def receiver_runs(client, role, repository, dispatched_at, budget):
    encoded = quote(dispatched_at, safe=":")
    route = (f"repos/{repository_name(repository)}/actions/runs"
             f"?event=repository_dispatch&created=%3E%3D{encoded}")
    return object_pages(client, route, "workflow_runs", budget)


def receiver_workflow(run, role):
    marker = RECEIVER_WORKFLOW_MARKERS[role]
    values = [run.get(name, "") for name in ("path", "name", "display_title")]
    return any(marker in str(value).lower().replace(" ", "-")
               for value in values)


def event_id_marker(values, event_id):
    texts = [str(value) for value in values if value is not None]
    if any(event_id in value for value in texts):
        return True
    if any(re.search(r"sha256:[0-9a-f]{64}", value) for value in texts):
        return False
    return None


def run_event_match(client, repository, run, event_id, budget):
    state = event_id_marker(
        [run.get("display_title"), run.get("name"), run.get("path")], event_id)
    if state is not None:
        return state
    run_id = run.get("id")
    require(type(run_id) is int and run_id > 0, "receiver run identity differs")
    try:
        artifacts = object_pages(
            client, f"repos/{repository_name(repository)}/actions/runs/{run_id}/artifacts",
            "artifacts", budget)
    except Pending:
        return None
    return event_id_marker([artifact.get("name") for artifact in artifacts], event_id)


def find_receiver_run(client, role, repository, event_id, dispatched_at, budget=None):
    timestamp(dispatched_at)
    require(role in EVENT_TYPES, "unknown receiver role")
    budget = budget or ApiBudget(RECEIVER_API_CALL_BUDGET)
    exact, unknown = [], []
    dispatched_time = timestamp(dispatched_at)
    for run in receiver_runs(client, role, repository, dispatched_at, budget):
        require(type(run) is dict, "unexpected receiver run")
        if run.get("event") != "repository_dispatch" or not receiver_workflow(run, role):
            continue
        created = run.get("created_at")
        if not isinstance(created, str):
            raise Invalid("receiver run timestamp differs")
        if timestamp(created) < dispatched_time:
            continue
        state = run_event_match(client, repository, run, event_id, budget)
        if state is True:
            exact.append(run)
        elif state is None:
            unknown.append(run)
    candidates = exact or unknown
    if not candidates:
        return None
    for candidate in candidates:
        require(type(candidate.get("id")) is int and candidate["id"] > 0,
                "receiver run identity differs")
    chosen = max(candidates, key=lambda row: row["id"])
    return {
        "run": chosen,
        "event_id_match": "exact" if exact else "unavailable",
    }


def run_result(repository, found):
    run = found["run"]
    run_id = run["id"]
    conclusion = run.get("conclusion")
    status = run.get("status")
    failed = status == "completed" and conclusion != "success"
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "url": run.get("html_url") or f"https://github.com/{repository_name(repository)}/actions/runs/{run_id}",
        "event_id_match": found["event_id_match"],
        "failed": failed,
    }


def wait_for_receiver(client, role, repository, tag, event_id, dispatched_at, *,
                      sleep=time.sleep, poll_delays=RECEIVER_RUN_POLL_DELAYS_SECONDS):
    repository_name(repository)
    require(role in EVENT_TYPES, "unknown receiver role")
    match(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?", tag)
    match("sha256:" + SHA, event_id)
    timestamp(dispatched_at)
    budget = ApiBudget(RECEIVER_API_CALL_BUDGET)
    for index, delay in enumerate(poll_delays):
        if index:
            sleep(delay)
        try:
            found = find_receiver_run(client, role, repository, event_id, dispatched_at, budget)
        except (Invalid, Pending) as error:
            raise Invalid("receiver run check failed: role=" + role + " repo=" + repository
                          + " tag=" + tag + " event_id=" + event_id + ": " + str(error)) from error
        if found is None:
            continue
        result = run_result(repository, found)
        if result["failed"]:
            raise Invalid(
                "receiver run failed: role=" + role + " repo=" + repository
                + " tag=" + tag + " event_id=" + event_id
                + " run_id=" + str(result["id"])
                + " conclusion=" + str(result["conclusion"]))
        return result
    raise Invalid("receiver run missing: role=" + role + " repo=" + repository
                  + " tag=" + tag + " event_id=" + event_id)


def configuration():
    return pipeline.release_roles(), pipeline.excluded_roles()


def validate_target_repositories(roles, *, targets=None):
    provided = {} if targets is None else dict(targets)
    for role in roles:
        target = provided.get(role, os.environ.get(TARGET_ENVIRONMENT[role], ""))
        if not target:
            raise Invalid("missing target repository for role " + role)
        try:
            repository_name(target)
        except Invalid as error:
            raise Invalid("malformed target repository for role " + role) from error
    return {role: provided.get(role, os.environ.get(TARGET_ENVIRONMENT[role], ""))
            for role in roles}


def release_asset(client, release, name):
    found = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    require(len(found) <= 1, "ambiguous release delivery asset")
    if not found:
        return None
    require(found[0].get("state") == "uploaded", "invalid release delivery asset metadata")
    raw = client.request(
        f"https://api.github.com/repos/{HUB}/releases/assets/{found[0]['id']}",
        github=True, accept="application/octet-stream")
    return decode(raw)


def release_event_id(client, release):
    ready = release_asset(client, release, "release-ready.json")
    if ready is not None and type(ready) is dict and "event_id" in ready:
        return match("sha256:" + SHA, ready["event_id"])
    manifest = release_asset(client, release, ASSET)
    require(manifest is not None, "release manifest unavailable")
    return payload(canonical(manifest))["event_id"]


def check_release(hub_client, receiver_client, tag, *, roles=None, excluded=None, targets=None):
    match(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?", tag)
    release = hub_client.api(f"repos/{HUB}/releases/tags/{tag}")
    event_id = release_event_id(hub_client, release)
    if roles is None:
        roles, configured_excluded = configuration()
        if excluded is None:
            excluded = configured_excluded
    elif excluded is None:
        excluded = pipeline.excluded_roles()
    roles = list(roles)
    excluded = list(excluded)
    enabled = set(roles)
    excluded_set = set(excluded)
    targets = {} if targets is None else dict(targets)
    budget = ApiBudget(RECEIVER_CHECK_API_CALL_BUDGET)
    rows = []
    for role in ROLES:
        target = targets.get(role, os.environ.get(TARGET_ENVIRONMENT[role], ""))
        accounted = role in enabled or role in excluded_set
        if role not in enabled:
            rows.append({
                "role": role,
                "enabled": False,
                "excluded": role in excluded_set,
                "accounted": accounted,
                "repository": None,
                "dispatched": False,
                "receiver_run_found": False,
                "run_id": None,
                "status": None,
                "conclusion": None,
                "url": None,
                "event_id_match": None,
                "error": "excluded by configuration" if role in excluded_set else "role is not accounted for",
            })
            continue
        delivery = release_asset(hub_client, release, f"delivery-{role}.json")
        dispatched = (delivery is not None
                      and delivery.get("event_id") == event_id
                      and delivery.get("consumer") == role
                      and delivery.get("state") == "dispatch_accepted")
        result = None
        error = None
        if delivery is None:
            error = "delivery asset missing"
        elif not dispatched:
            error = "delivery asset identity differs"
        elif not target:
            error = "missing target repository for role " + role
        else:
            try:
                validate_target_repositories([role], targets={role: target})
                found = find_receiver_run(receiver_client, role, target, event_id,
                                          delivery["timestamp"], budget)
                if found is None:
                    error = "receiver run missing"
                else:
                    result = run_result(target, found)
                    if result["failed"]:
                        error = "receiver run concluded " + str(result["conclusion"])
            except (Invalid, KeyError, TypeError, ValueError) as caught:
                error = str(caught)
        rows.append({
            "role": role,
            "enabled": True,
            "excluded": False,
            "accounted": True,
            "repository": target,
            "dispatched": dispatched,
            "receiver_run_found": result is not None,
            "run_id": None if result is None else result["id"],
            "status": None if result is None else result["status"],
            "conclusion": None if result is None else result["conclusion"],
            "url": None if result is None else result["url"],
            "event_id_match": None if result is None else result["event_id_match"],
            "error": error,
        })
    return {
        "schema": "pineforge/release-fanout/v1",
        "tag": tag,
        "event_id": event_id,
        "excluded": excluded,
        "roles": rows,
        "ok": all(row["excluded"] or
                  (row["enabled"] and row["dispatched"]
                   and row["receiver_run_found"] and not row["error"])
                  for row in rows),
    }


def render_human(report):
    lines = [
        "tag=" + str(report.get("tag", "")) + " event_id=" + str(report.get("event_id", "")),
        "excluded=" + ",".join(report.get("excluded", [])),
        "role | state | repository | dispatched | receiver_run | conclusion | url | error",
    ]
    for row in report.get("roles", []):
        state = "enabled" if row.get("enabled") else "excluded" if row.get("excluded") else "unaccounted"
        lines.append("{} | {} | {} | {} | {} | {} | {}".format(
            row.get("role", ""), state, row.get("repository") or "-",
            str(row.get("dispatched", False)).lower(),
            str(row.get("run_id") or "-"),
            row.get("conclusion") or "-",
            row.get("url") or "-",
            row.get("error") or "-"))
    return "\n".join(lines) + "\n"


def target_map(roles):
    return {role: os.environ.get(TARGET_ENVIRONMENT[role], "") for role in roles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--tag", required=True)
    wait = sub.add_parser("wait")
    wait.add_argument("--tag", required=True)
    wait.add_argument("--role", choices=pipeline.ROLES, required=True)
    wait.add_argument("--repository", required=True)
    wait.add_argument("--delivery", required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "check":
            hub_token = os.environ.get("GH_HUB_TOKEN", os.environ.get("GH_TOKEN", ""))
            receiver_token = os.environ.get("GH_RECEIVER_TOKEN", os.environ.get("GH_TOKEN", ""))
            roles, excluded = configuration()
            report = check_release(Client(token=hub_token), Client(token=receiver_token), args.tag,
                                   roles=roles, excluded=excluded, targets=target_map(roles))
            print(canonical(report).decode("utf-8"), end="")
            print(render_human(report), end="")
            return 0 if report["ok"] else 1
        delivery = read_delivery(args.delivery)
        require(delivery["consumer"] == args.role, "delivery role differs")
        client = Client()
        result = wait_for_receiver(
            client, args.role, args.repository, args.tag,
            delivery["event_id"], delivery["timestamp"])
        print(canonical({
            "schema": "pineforge/release-fanout/v1", "role": args.role,
            "repository": args.repository, "tag": args.tag,
            "event_id": delivery["event_id"], "receiver": result,
        }).decode("utf-8"), end="")
        return 0
    except (Invalid, Pending, ValueError, OSError, KeyError, TypeError) as error:
        print("::error::" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
