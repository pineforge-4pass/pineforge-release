#!/usr/bin/env python3
"""Read consumer-owned Actions evidence and publish neutral completion receipts.

A callback is only a wakeup. Neither a callback nor a successful dispatch can
claim verified. Evidence must come from a configured workflow's artifact in the
configured repository, and its full CI/E2E inventory is checked against GitHub.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import zipfile
from pathlib import Path

from release_artifacts import Client
from release_contract import (ASSET, COMMIT, HUB, ROLES, SHA, Invalid, Pending,
                              canonical, decode, keys, match, payload, positive,
                              require, sha256, version)
from release_pipeline import Store, receipts, report_issue, run_link

STATES = ("pending", "pr_open", "testing", "ready", "merged", "deploying", "verified", "failed", "superseded", "skipped_by_policy")
RANK = {state: i for i, state in enumerate(STATES[:7])}


def object_pages(client, route, key):
    rows = []
    for page in range(1, 1001):
        result = client.api(route + ("&" if "?" in route else "?") + f"per_page=100&page={page}")[key]
        rows.extend(result)
        if len(result) < 100:
            return rows
    raise Invalid("consumer API pagination limit exceeded")


def profile(role):
    profiles = decode(os.environ.get("RELEASE_RECEIPT_POLICY", "{}"))
    require(role in profiles, "consumer evidence policy not configured")
    policy = profiles[role]
    keys(policy, ("workflow", "artifact", "base", "ci", "e2e", "deploy"))
    for item in (policy, policy["ci"], policy["e2e"], policy["deploy"]):
        match(r"[A-Za-z0-9_.-]+\.yml", item["workflow"])
    match(r"[A-Za-z0-9_.-]+", policy["artifact"])
    require(policy["base"] in ("main", "dev"), "unapproved consumer base")
    for name in ("ci", "e2e", "deploy"):
        row = policy[name]
        keys(row, ("workflow", "expected", "approved_skips"))
        require(type(row["expected"]) is list and len(row["expected"]) > 0
                and len(row["expected"]) == len(set(row["expected"])), "empty or duplicate required inventory")
        require(type(row["approved_skips"]) is list and set(row["approved_skips"]) <= set(row["expected"]), "invalid skip policy")
        require(set(row["expected"]) - set(row["approved_skips"]), "entire required inventory cannot be skipped")
    return policy


def check_run(client, repo, evidence, policy, expected_sha):
    keys(evidence, ("run_id", "run_attempt", "sha", "expected", "approved_skips", "results"))
    positive(evidence["run_id"])
    positive(evidence["run_attempt"])
    require(evidence["sha"] == expected_sha, "checks ran against a different source")
    require(evidence["expected"] == policy["expected"] and evidence["approved_skips"] == policy["approved_skips"],
            "required check inventory differs from owner policy")
    run = client.api(f"repos/{repo}/actions/runs/{evidence['run_id']}/attempts/{evidence['run_attempt']}")
    require(run["path"] == ".github/workflows/" + policy["workflow"] and run["head_sha"] == expected_sha,
            "unexpected checks workflow or head")
    require(run["status"] == "completed" and run["conclusion"] == "success", "required check run not successful")
    jobs = object_pages(client, f"repos/{repo}/actions/runs/{evidence['run_id']}/attempts/{evidence['run_attempt']}/jobs", "jobs")
    checked, finished = {}, []
    for name in policy["expected"]:
        found = [row for row in jobs if row["name"] == name]
        require(len(found) == 1 and found[0]["status"] == "completed", "required check missing or incomplete")
        result = found[0]["conclusion"]
        require(result == "success" or (result == "skipped" and name in policy["approved_skips"]), "required check failed/cancelled/skipped")
        if result == "success":
            require(isinstance(found[0].get("completed_at"), str), "required check completion time missing")
            finished.append(found[0]["completed_at"])
        checked[name] = result
    require(evidence["results"] == checked, "reported checks differ from GitHub")
    require(bool(finished), "required inventory contains no successful checks")
    run["checked_finished_at"] = max(finished)
    return run


def identity(obj):
    keys(obj, ("source", "image_digest", "facts_commit", "facts_sha256", "deployment_id_sha256"))
    match(COMMIT, obj["source"])
    match(COMMIT, obj["facts_commit"])
    for name in ("facts_sha256", "deployment_id_sha256"):
        match(SHA, obj[name])
    match("sha256:" + SHA, obj["image_digest"])


def validate_receipt(client, repo, receipt, event, policy, reporting_run):
    keys(receipt, ("schema", "event_id", "consumer", "release_version", "attempt", "sequence", "state", "timestamp",
                   "pr", "ci", "e2e", "deploy", "preview", "deployed", "previous"))
    require(receipt["schema"] == "pineforge/consumer-receipt/v1" and receipt["consumer"] in ROLES, "invalid consumer receipt schema")
    require(receipt["event_id"] == event["event_id"] and receipt["release_version"] == event["release_version"], "receipt event differs")
    positive(receipt["attempt"])
    positive(receipt["sequence"])
    require(receipt["state"] in STATES, "invalid receipt state")
    match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", receipt["timestamp"])
    timestamp = dt.datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))
    require(timestamp <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5), "receipt timestamp is in future")
    require(reporting_run["path"] == ".github/workflows/" + policy["workflow"], "untrusted receipt workflow")
    require(reporting_run["event"] in ("push", "workflow_dispatch", "workflow_run", "repository_dispatch"),
            "receipt must come from the trusted consumer coordinator")
    # A public receipt never contains private URLs, names, job labels or raw deployment IDs.
    public = {key: receipt[key] for key in ("schema", "event_id", "consumer", "release_version", "attempt", "sequence", "state", "timestamp")}
    public["evidence_sha256"] = sha256(canonical(receipt))
    public["reporting_run_id"] = reporting_run["id"]
    state = receipt["state"]
    if state in ("pending", "failed", "superseded", "skipped_by_policy"):
        require(state != "skipped_by_policy" or event["prerelease"], "stable consumer cannot skip by policy")
        return public
    pr = receipt["pr"]
    keys(pr, ("number", "base", "base_sha", "head", "merge"))
    positive(pr["number"])
    match(COMMIT, pr["base_sha"])
    match(COMMIT, pr["head"])
    require(pr["base"] == policy["base"], "PR base differs from policy")
    actual = client.api(f"repos/{repo}/pulls/{pr['number']}")
    require(actual["base"]["ref"] == pr["base"] and actual["head"]["sha"] == pr["head"], "PR head or base moved")
    require(actual["head"]["repo"]["full_name"].lower() == repo.lower(), "receipt refers to fork PR")
    if not actual["merged"]:
        require(actual["base"]["sha"] == pr["base_sha"], "PR base moved after candidate verification")
    public["pr"] = pr
    if state in ("ready", "merged", "deploying", "verified"):
        ci_run = check_run(client, repo, receipt["ci"], policy["ci"], pr["head"])
        e2e_run = check_run(client, repo, receipt["e2e"], policy["e2e"], pr["head"])
        identity(receipt["preview"])
        require(receipt["preview"]["source"] == pr["head"] and receipt["preview"]["image_digest"] == event["image"]["digest"],
                "preview did not exercise the candidate source/image")
        for name in ("ci", "e2e"):
            public[name] = {"run_id": receipt[name]["run_id"], "run_attempt": receipt[name]["run_attempt"],
                            "inventory_sha256": sha256(canonical(receipt[name]["results"]))}
        public["preview"] = receipt["preview"]
    if state in ("merged", "deploying", "verified"):
        match(COMMIT, pr["merge"])
        require(actual["merged"] is True and actual["merge_commit_sha"] == pr["merge"], "PR is not the recorded merge")
        # Required E2E must precede the merge; a production smoke cannot stand
        # in for the consumer's candidate E2E gate.
        require(ci_run["checked_finished_at"] <= actual["merged_at"] and e2e_run["checked_finished_at"] <= actual["merged_at"], "CI/E2E completed after merge")
        merged_commit = client.api(f"repos/{repo}/commits/{pr['merge']}")
        require(merged_commit["parents"] and merged_commit["parents"][0]["sha"] == pr["base_sha"],
                "merge inputs differ from the tested base; require updated merge/squash candidate")
    if state == "verified":
        require(reporting_run["head_sha"] == pr["merge"] and reporting_run["event"] != "pull_request", "served receipt source differs")
        deployed_run = check_run(client, repo, receipt["deploy"], policy["deploy"], pr["merge"])
        require(receipt["timestamp"] >= deployed_run["checked_finished_at"], "served receipt predates verification")
        identity(receipt["deployed"])
        identity(receipt["previous"])
        require(receipt["deployed"]["source"] == pr["merge"] and receipt["deployed"]["image_digest"] == event["image"]["digest"],
                "actual served source/image differs")
        # A retained newer facts snapshot is allowed only with verified bytes
        # and the same immutable release entry (checked below by the adapter).
        public["deployed"] = receipt["deployed"]
        public["previous"] = receipt["previous"]
        public["deploy"] = {"run_id": receipt["deploy"]["run_id"], "run_attempt": receipt["deploy"]["run_attempt"],
                            "inventory_sha256": sha256(canonical(receipt["deploy"]["results"]))}
    return public


def transition(previous, current):
    if previous is None:
        return True
    old, new = (previous["attempt"], previous["sequence"]), (current["attempt"], current["sequence"])
    if new < old:
        return False
    if new == old:
        require(previous == current, "receipt identity reused with different evidence")
        return False
    if previous["state"] == "verified":
        require(current["state"] == "verified", "verified release cannot regress to a delivery/intermediate state")
    elif previous["attempt"] == current["attempt"] and previous["state"] in RANK and current["state"] in RANK:
        require(RANK[current["state"]] >= RANK[previous["state"]], "consumer state regressed")
    return True


def fetch_artifact(client, repo, run, policy):
    artifacts = object_pages(client, f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts")
    found = [row for row in artifacts if row["name"] == policy["artifact"] and not row["expired"]]
    if not found:
        return None
    require(len(found) == 1, "ambiguous consumer receipt artifact")
    archive = client.request(f"https://api.github.com/repos/{repo}/actions/artifacts/{found[0]['id']}/zip",
                             github=True, accept="application/octet-stream")
    if found[0].get("digest"):
        require(found[0]["digest"] == "sha256:" + sha256(archive), "receipt archive digest differs")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(zipped.namelist() == ["release-receipt.json"], "unexpected receipt archive inventory")
        require(zipped.getinfo("release-receipt.json").file_size <= 2 * 1024 * 1024, "receipt exceeds size limit")
        return decode(zipped.read("release-receipt.json"))


def collect(client, hub_client, store, role, run_id=0):
    target = os.environ.get("TARGET_REPOSITORY", "")
    match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target)
    repo = "pineforge-4pass/" + target
    policy = profile(role)
    # Target identity is configuration; never accept it from a callback.
    metadata = client.api(f"repos/{repo}")
    require(metadata["full_name"].lower() == repo.lower(), "consumer repository identity differs")
    if run_id:
        runs = [client.api(f"repos/{repo}/actions/runs/{run_id}")]
    else:
        runs = object_pages(client, f"repos/{repo}/actions/workflows/{policy['workflow']}/runs", "workflow_runs")
    for run in sorted(runs, key=lambda row: row["id"]):
        if run["path"] != ".github/workflows/" + policy["workflow"]:
            raise Invalid("unexpected reporting workflow")
        receipt = fetch_artifact(client, repo, run, policy)
        if receipt is None:
            require(not run_id, "callback has no receipt artifact")
            continue
        require(receipt["consumer"] == role, "receipt role differs from scoped target")
        row = store.get("v" + str(version(receipt["release_version"])))
        raw = store.read(row, ASSET)
        require(raw is not None and not row["draft"], "receipt has no ready release")
        event = payload(raw)
        facts_only = receipt["schema"] == "pineforge/facts-consumer-receipt/v1"
        if facts_only:
            require(role in ("web", "license"), "unexpected facts-only receipt role")
            facts_target = receipt["facts"]
            keys(facts_target, ("commit", "sha256"))
            match(COMMIT, facts_target["commit"])
            match(SHA, facts_target["sha256"])
            normalized = {key: value for key, value in receipt.items() if key != "facts"}
            normalized["schema"] = "pineforge/consumer-receipt/v1"
        else:
            normalized = receipt
        public = validate_receipt(client, repo, normalized, event, policy, run)
        if facts_only:
            public.update(schema="pineforge/facts-consumer-receipt/v1", facts=facts_target,
                          evidence_sha256=sha256(canonical(receipt)))
        for identity_name in ("preview", "deployed"):
            observed = public.get(identity_name)
            if observed is None:
                continue
            if facts_only:
                require(observed["facts_commit"] == facts_target["commit"] and observed["facts_sha256"] == facts_target["sha256"],
                        "facts-only target differs from consumed identity")
            original = hub_client.file(HUB, event["facts"]["commit"], event["facts"]["path"])
            consumed = hub_client.file(HUB, observed["facts_commit"], event["facts"]["path"])
            comparison = hub_client.api(f"repos/{HUB}/compare/{event['facts']['commit']}...{observed['facts_commit']}")
            require(comparison["status"] in ("ahead", "identical"), "consumed canonical facts are older or divergent")
            from release_contract import retain_facts
            require(retain_facts(event, original, consumed) == observed["facts_sha256"], "consumed facts digest differs")
        prefix = "facts-consumer-" if facts_only else "consumer-"
        if facts_only:
            previous = None
            for item in sorted(store.get(row["tag_name"])["assets"], key=lambda a: a["id"]):
                if item["name"].startswith(prefix + role + "-" + facts_target["sha256"] + "-"):
                    previous = decode(store.read(row, item["name"]))
        else:
            previous = receipts(store, row, event["event_id"]).get(role)
        if transition(previous, public):
            stem = prefix + role + "-" + (facts_target["sha256"] + "-" if facts_only else "")
            store.put(row, f"{stem}{public['attempt']}-{public['sequence']}.json", canonical(public))
            if public["state"] == "failed":
                incident = "facts-" + facts_target["sha256"] if facts_only else row["tag_name"]
                title = "Facts consumer failed" if facts_only else f"Release {event['release_version']}: consumer failed"
                report_issue(hub_client, incident, title,
                             f"Consumer role: {role}; failed stage evidence retained in the consumer's private report.\n\n" + run_link())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--consumer", choices=ROLES, required=True)
    p.add_argument("--run-id", default="0")
    p = sub.add_parser("delivery")
    for name in ("tag", "consumer", "file"):
        p.add_argument("--" + name, required=True)
    p = sub.add_parser("signal")
    p.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "signal":
            from release_pipeline import configured_roles
            enabled = configured_roles(os.environ.get("RELEASE_READY_CONSUMERS", ""))
            event = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
            if os.environ["GITHUB_EVENT_NAME"] == "repository_dispatch":
                signal = event["client_payload"]
                keys(signal, ("schema", "consumer", "release_version", "run_id"))
                require(signal["schema"] == "pineforge/consumer-signal/v1" and signal["consumer"] in enabled, "invalid consumer signal")
                version(signal["release_version"])
                positive(signal["run_id"])
                targets = [{"consumer": signal["consumer"], "run_id": str(signal["run_id"])}]
            else:
                targets = [{"consumer": role, "run_id": "0"} for role in enabled]
            Path(args.output).write_bytes(canonical(targets))
        else:
            hub_client = Client(token=os.environ.get("HUB_TOKEN", os.environ.get("GH_TOKEN", "")))
            store = Store(hub_client)
            if args.cmd == "collect":
                match(r"[0-9]+", args.run_id)
                collect(Client(), hub_client, store, args.consumer, int(args.run_id))
            else:
                require(args.consumer in ROLES, "unexpected delivery role")
                row = store.get(args.tag)
                obj = decode(Path(args.file).read_bytes())
                require(obj["consumer"] == args.consumer and obj["state"] == "dispatch_accepted", "invalid delivery receipt")
                event = payload(store.read(row, ASSET))
                require(obj["event_id"] == event["event_id"], "delivery event differs")
                name = f"delivery-{args.consumer}.json"
                if store.read(row, name) is None:
                    store.put(row, name, canonical(obj))
    except (Invalid, Pending, ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
        print("::error::consumer evidence invalid or unavailable; no verified state recorded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
