#!/usr/bin/env python3
"""Read consumer-owned Actions evidence and publish neutral completion receipts.

A callback is only a wakeup. Neither a callback nor a successful dispatch can
claim verified. Evidence must come from a configured workflow's artifact in the
configured repository, and its full CI/E2E inventory is checked against GitHub.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import os
import sys
import zipfile
from pathlib import Path

from release_artifacts import Client
from release_contract import (ASSET, COMMIT, HUB, RECEIPT_ROLES, ROLES, SHA, Invalid, Pending,
                              canonical, decode, keys, match, payload, positive,
                              require, sha256, version)
from release_pipeline import Store, receipt_roles, receipts, report_issue, run_link

STATES = ("pending", "pr_open", "testing", "ready", "merged", "deploying", "verified", "failed", "superseded", "skipped_by_policy")
RANK = {state: i for i, state in enumerate(STATES[:7])}
PRODUCT_SCHEMA = "pineforge/consumer-receipt/v1"
FACTS_SCHEMA = "pineforge/facts-consumer-receipt/v1"
COMMON_FIELDS = {"schema", "consumer", "attempt", "sequence", "state", "timestamp",
                 "pr", "ci", "e2e", "deploy", "preview", "deployed", "previous"}

# TEMPORARY first-adopter previous. Remove together with pineforge-data's BOOTSTRAP_PACKAGE exemption
# (scripts/release_chain/publishing.py), only after data 0.3.1 is verified AND every retained
# release-receipt-data artifact carrying this previous has expired (see docs/release-chain.md).
DATA_BOOTSTRAP_PREVIOUS = {
    "provenance": "pre-manifest-bootstrap",
    "package": "pineforge-data",
    "version": "0.3.0",
    "source": "fb5cec39b6c6d70af103df0df182d47bb5812191",
    "image_digest": "sha256:b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86",
    "distributions": {
        "pineforge_data-0.3.0-py3-none-any.whl": "609b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc",
        "pineforge_data-0.3.0.tar.gz": "f29a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc",
    },
}


def object_pages(client, route, key):
    rows = []
    for page in range(1, 1001):
        result = client.api(route + ("&" if "?" in route else "?") + f"per_page=100&page={page}")[key]
        rows.extend(result)
        if len(result) < 100:
            return rows
    raise Invalid("consumer API pagination limit exceeded")


def profile(role):
    require(role in RECEIPT_ROLES, "unexpected receipt role")
    profiles = decode(os.environ.get("RELEASE_RECEIPT_POLICY", "{}"))
    require(role in profiles, "consumer evidence policy not configured")
    policy = profiles[role]
    sections = ("ci", "e2e", "deploy")
    if type(policy) is dict and "post_merge" in policy:
        require(role == "application", "post-merge policy is only valid for application")
        sections += ("post_merge",)
    keys(policy, ("workflow", "artifact", "base", *sections))
    for item in (policy, *(policy[name] for name in sections)):
        match(r"[A-Za-z0-9_.-]+\.yml", item["workflow"])
    match(r"[A-Za-z0-9_.-]+", policy["artifact"])
    require(policy["base"] in ("main", "dev"), "unapproved consumer base")
    for name in sections:
        row = policy[name]
        keys(row, ("workflow", "expected", "approved_skips"))
        require(type(row["expected"]) is list and len(row["expected"]) > 0
                and len(row["expected"]) == len(set(row["expected"])), "empty or duplicate required inventory")
        require(type(row["approved_skips"]) is list and set(row["approved_skips"]) <= set(row["expected"]), "invalid skip policy")
        require(set(row["expected"]) - set(row["approved_skips"]), "entire required inventory cannot be skipped")
        if name == "post_merge":
            require(row["approved_skips"] == [], "post-merge checks cannot be skipped")
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
    run["checked_first_finished_at"] = min(finished)
    return run


def check_coordinator(client, repo, evidence, policy, pr, ci):
    """Hosted base-code E2E bound to an exact PR-head build attempt/artifact.

    The caller restricts this adapter to hosted. Keep check_run's exact SHA
    equality for every other workflow, including hosted CI and deployment.
    """
    require(policy["workflow"] == "release-coordinator.yml", "unapproved coordinator workflow")
    require(policy["expected"] == ["candidate", "release/ready"] and policy["approved_skips"] == [],
            "coordinator inventory differs")
    run = check_run(client, repo, evidence, policy, pr["base_sha"])
    require(run["id"] == evidence["run_id"] and run["run_attempt"] == evidence["run_attempt"],
            "coordinator run identity differs")
    metadata = client.api(f"repos/{repo}")
    require(metadata["full_name"].lower() == repo.lower()
            and run["repository"]["full_name"].lower() == repo.lower()
            and run["head_repository"]["full_name"].lower() == repo.lower(),
            "coordinator repository differs")
    require(run["event"] == "workflow_run" and pr["base"] == metadata["default_branch"] == "main"
            and run["head_branch"] == metadata["default_branch"], "untrusted coordinator source")
    artifacts = object_pages(client, f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts")
    name = f"coordinator-binding-{run['id']}-{run['run_attempt']}"
    found = [a for a in artifacts if a["name"] == name and not a["expired"]]
    require(len(found) == 1, "missing/ambiguous coordinator binding")
    positive(found[0]["id"])
    # GitHub 415s application/octet-stream on the artifact zip endpoint; release assets need it, artifacts must not use it (incident #61).
    archive = client.request(f"https://api.github.com/repos/{repo}/actions/artifacts/{found[0]['id']}/zip",
                             github=True)
    require(len(archive) <= 2 * 1024 * 1024, "binding archive too large")
    require(found[0].get("digest") == "sha256:" + sha256(archive), "binding archive digest missing or differs")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(zipped.namelist() == ["binding.json"], "unexpected binding archive inventory")
        require(zipped.getinfo("binding.json").file_size <= 2 * 1024 * 1024, "binding too large")
        binding = decode(zipped.read("binding.json"))
    keys(binding, ("schema", "pr", "head", "base", "source", "coordinator", "artifact"))
    require(binding["schema"] == "pineforge/mcp-coordinator-binding/v1", "unknown coordinator binding")
    positive(binding["pr"])
    keys(binding["source"], ("run_id", "attempt"))
    keys(binding["coordinator"], ("run_id", "attempt", "sha"))
    for bound_run in (binding["source"], binding["coordinator"]):
        positive(bound_run["run_id"])
        positive(bound_run["attempt"])
    require(binding["pr"] == pr["number"] and binding["head"] == pr["head"] and binding["base"] == pr["base_sha"],
            "coordinator verified another PR head/base")
    require(binding["source"] == {"run_id": ci["run_id"], "attempt": ci["run_attempt"]}, "coordinator verified another CI attempt")
    require(binding["coordinator"] == {"run_id": run["id"], "attempt": run["run_attempt"], "sha": pr["base_sha"]},
            "coordinator binding source differs")
    artifact = binding["artifact"]
    keys(artifact, ("id", "name", "digest", "manifest_sha256", "worker_sha256", "container_sha256"))
    positive(artifact["id"])
    require(artifact["name"] == f"candidate-{ci['run_id']}-{ci['run_attempt']}", "candidate attempt differs")
    match("sha256:" + SHA, artifact["digest"])
    for key in ("manifest_sha256", "worker_sha256", "container_sha256"):
        match(SHA, artifact[key])
    candidates = object_pages(client, f"repos/{repo}/actions/runs/{ci['run_id']}/artifacts", "artifacts")
    matched = [a for a in candidates if a["name"] == artifact["name"] and not a["expired"]]
    require(len(matched) == 1 and matched[0]["id"] == artifact["id"] and matched[0].get("digest") == artifact["digest"],
            "verified candidate artifact missing or replaced")
    return run


def check_postmerge(client, repo, evidence, policy, pr, actual, timestamp):
    """Hosted E2E on merged main; private evidence content stays consumer-side."""
    require(policy["e2e"]["expected"] == ["authz", "build", "verified"] and policy["e2e"]["approved_skips"] == [],
            "postmerge inventory differs")
    run = check_run(client, repo, evidence, policy["e2e"], pr["merge"])
    require(run["id"] == evidence["run_id"] and run["run_attempt"] == evidence["run_attempt"],
            "postmerge run identity differs")
    metadata = client.api(f"repos/{repo}")
    require(metadata["full_name"].lower() == repo.lower()
            and run["repository"]["full_name"].lower() == repo.lower()
            and run["head_repository"]["full_name"].lower() == repo.lower(),
            "postmerge repository differs")
    require(run["event"] == "workflow_dispatch" and run["head_branch"] == metadata["default_branch"] == "main" == pr["base"],
            "untrusted postmerge source")
    require(actual["merged"] is True and actual["merge_commit_sha"] == pr["merge"], "PR is not the recorded merge")
    require(run["checked_first_finished_at"] >= actual["merged_at"], "postmerge E2E completed before merge")
    require(timestamp >= run["checked_finished_at"], "receipt predates postmerge verification")
    # The released PR must not supply a different definition of its own gate.
    workflow_route = f"repos/{repo}/contents/.github/workflows/{policy['e2e']['workflow']}"
    blobs = []
    for ref in (pr["base_sha"], pr["merge"]):
        contents = client.api(f"{workflow_route}?ref={ref}")
        require(type(contents) is dict and contents.get("type") == "file", "postmerge workflow is not a file")
        blobs.append(match(COMMIT, contents.get("sha")))
    require(blobs[0] == blobs[1], "postmerge workflow differs from tested base")
    artifacts = object_pages(client, f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts")
    name = f"postmerge-evidence-{run['id']}-{run['run_attempt']}"
    found = [a for a in artifacts if a["name"] == name and not a["expired"]]
    require(len(found) == 1, "missing/ambiguous postmerge evidence")
    return run


def identity(obj, *, facts_only=False, consumer=None, slot=None):
    if type(obj) is dict and "legacy" in obj:
        require(not facts_only and consumer == "offline" and slot == "previous",
                "legacy identity is only valid for product offline.previous")
        keys(obj, ("legacy", "npm_version", "npm_integrity", "git_head", "image_digest"))
        require(obj["legacy"] is True, "legacy identity flag must be true")
        try:
            version(obj["npm_version"])
        except ValueError as error:
            raise Invalid("malformed identity") from error
        match(r"sha512-[A-Za-z0-9+/]{86}==", obj["npm_integrity"])
        require(len(base64.b64decode(obj["npm_integrity"][7:], validate=True)) == 64,
                "invalid npm integrity")
        match(COMMIT, obj["git_head"])
        match("sha256:" + SHA, obj["image_digest"])
        return
    if type(obj) is dict and "provenance" in obj:
        require(not facts_only and consumer == "data" and slot == "previous",
                "bootstrap identity is only valid for product data.previous")
        # Facts keys (including null values) and all other additions fail here.
        keys(obj, (*DATA_BOOTSTRAP_PREVIOUS, "deployment_id_sha256"))
        keys(obj["distributions"], DATA_BOOTSTRAP_PREVIOUS["distributions"])
        require(all(obj[name] == value for name, value in DATA_BOOTSTRAP_PREVIOUS.items()),
                "bootstrap previous identity differs")
        # Match pineforge-data scripts/release_chain/receipts.py served() byte for byte.
        deployment = {name: obj[name] for name in ("package", "version", "distributions")}
        require(obj["deployment_id_sha256"] == sha256(canonical(deployment)),
                "bootstrap deployment identity differs")
        return
    keys(obj, ("source", "image_digest", "facts_commit", "facts_sha256", "deployment_id_sha256"))
    match(COMMIT, obj["source"])
    match(COMMIT, obj["facts_commit"])
    for name in ("facts_sha256", "deployment_id_sha256"):
        match(SHA, obj[name])
    if not facts_only or obj["image_digest"] is not None:
        match("sha256:" + SHA, obj["image_digest"])


def validate_receipt(client, repo, receipt, event, policy, reporting_run):
    facts_only = receipt.get("schema") == FACTS_SCHEMA
    fields = {"facts"} if facts_only else {"event_id", "release_version"}
    if not facts_only and "post_merge" in policy:
        fields.add("post_merge")
    keys(receipt, COMMON_FIELDS | fields)
    require(receipt["schema"] in (PRODUCT_SCHEMA, FACTS_SCHEMA) and receipt["consumer"] in RECEIPT_ROLES, "invalid consumer receipt schema")
    if facts_only:
        require(event is None and receipt["consumer"] in ("web", "license"), "unexpected facts-only receipt")
        keys(receipt["facts"], ("commit", "sha256"))
        match(COMMIT, receipt["facts"]["commit"])
        match(SHA, receipt["facts"]["sha256"])
    else:
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
    # A provenance-bearing or legacy identity must obey its temporary rule even
    # before its evidence stage is required (including states that return early).
    for name in ("preview", "deployed", "previous"):
        if type(receipt[name]) is dict and ("provenance" in receipt[name] or "legacy" in receipt[name]):
            identity(receipt[name], facts_only=facts_only, consumer=receipt["consumer"], slot=name)
    # A public receipt never contains private URLs, names, job labels or raw deployment IDs.
    public = {key: receipt[key] for key in ("schema", "consumer", "attempt", "sequence", "state", "timestamp")}
    if facts_only:
        public["facts"] = receipt["facts"]
    else:
        public.update(event_id=receipt["event_id"], release_version=receipt["release_version"])
    public["evidence_sha256"] = sha256(canonical(receipt))
    public["reporting_run_id"] = reporting_run["id"]
    state = receipt["state"]
    postmerge = policy["e2e"]["workflow"] == "release-postmerge.yml"
    if postmerge:
        require(receipt["consumer"] == "hosted" and policy["ci"]["workflow"] == "release-pr.yml"
                and policy["deploy"]["workflow"] == "release.yml", "unexpected postmerge consumer")
        require((state == "verified") == (receipt["e2e"] is not None),
                "postmerge E2E is required exactly in verified state")
        require(state != "ready", "postmerge policy does not accept ready state")
    if state in ("pending", "failed", "superseded", "skipped_by_policy"):
        require(state != "skipped_by_policy" or (not facts_only and event["prerelease"]), "consumer cannot skip by policy")
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
        if postmerge:
            if state == "verified":
                e2e_run = check_postmerge(client, repo, receipt["e2e"], policy, pr, actual, receipt["timestamp"])
        elif policy["e2e"]["workflow"] == "release-coordinator.yml":
            require(receipt["consumer"] == "hosted" and policy["ci"]["workflow"] == "release-pr.yml", "unexpected coordinator consumer")
            e2e_run = check_coordinator(client, repo, receipt["e2e"], policy["e2e"], pr, receipt["ci"])
        else:
            e2e_run = check_run(client, repo, receipt["e2e"], policy["e2e"], pr["head"])
        identity(receipt["preview"], facts_only=facts_only, consumer=receipt["consumer"], slot="preview")
        if postmerge:
            require(receipt["preview"]["source"] == pr["merge"]
                    and (facts_only or receipt["preview"]["image_digest"] == event["image"]["digest"]),
                    "preview did not exercise the candidate source/image")
        else:
            require(receipt["preview"]["source"] == pr["head"]
                    and (facts_only or receipt["preview"]["image_digest"] == event["image"]["digest"]),
                    "preview did not exercise the candidate source/image")
        for name in ("ci", "e2e"):
            if postmerge and state != "verified" and name == "e2e":
                continue
            public[name] = {"run_id": receipt[name]["run_id"], "run_attempt": receipt[name]["run_attempt"],
                            "inventory_sha256": sha256(canonical(receipt[name]["results"]))}
        public["preview"] = receipt["preview"]
    if state in ("merged", "deploying", "verified"):
        match(COMMIT, pr["merge"])
        require(actual["merged"] is True and actual["merge_commit_sha"] == pr["merge"], "PR is not the recorded merge")
        if postmerge:
            require(ci_run["checked_finished_at"] <= actual["merged_at"], "CI completed after merge")
        else:
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
        identity(receipt["deployed"], facts_only=facts_only, consumer=receipt["consumer"], slot="deployed")
        identity(receipt["previous"], facts_only=facts_only, consumer=receipt["consumer"], slot="previous")
        require(receipt["deployed"]["source"] == pr["merge"]
                and (facts_only or receipt["deployed"]["image_digest"] == event["image"]["digest"]),
                "actual served source/image differs")
        # A retained newer facts snapshot is allowed only with verified bytes
        # and the same immutable release entry (checked below by the adapter).
        public["deployed"] = receipt["deployed"]
        public["previous"] = receipt["previous"]
        public["deploy"] = {"run_id": receipt["deploy"]["run_id"], "run_attempt": receipt["deploy"]["run_attempt"],
                            "inventory_sha256": sha256(canonical(receipt["deploy"]["results"]))}
        if "post_merge" in policy:
            post_run = check_run(client, repo, receipt["post_merge"], policy["post_merge"], pr["merge"])
            require(post_run["id"] == receipt["post_merge"]["run_id"] and post_run["run_attempt"] == receipt["post_merge"]["run_attempt"],
                    "post-merge run identity differs")
            require(post_run["event"] == "push", "post-merge checks must use push")
            require(post_run["head_branch"] == policy["base"], "post-merge checks branch differs from policy")
            require(receipt["timestamp"] >= post_run["checked_finished_at"], "served receipt predates post-merge verification")
            public["post_merge"] = {"run_id": receipt["post_merge"]["run_id"], "run_attempt": receipt["post_merge"]["run_attempt"],
                                    "inventory_sha256": sha256(canonical(receipt["post_merge"]["results"]))}
    if facts_only:
        for name in ("preview", "deployed"):
            if name in public:
                require(public[name]["facts_commit"] == receipt["facts"]["commit"]
                        and public[name]["facts_sha256"] == receipt["facts"]["sha256"],
                        "facts-only target differs from consumed identity")
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
    elif previous["attempt"] == current["attempt"] and previous["state"] in ("failed", "superseded", "skipped_by_policy"):
        require(current["state"] == previous["state"], "terminal consumer state requires a new attempt")
    elif previous["attempt"] == current["attempt"] and previous["state"] in RANK and current["state"] in RANK:
        require(RANK[current["state"]] >= RANK[previous["state"]], "consumer state regressed")
    return True


def fetch_artifact(client, repo, run, policy, artifacts=None):
    if artifacts is None:
        artifacts = object_pages(client, f"repos/{repo}/actions/runs/{run['id']}/artifacts", "artifacts")
    found = [row for row in artifacts if row["name"] == policy["artifact"] and not row["expired"]]
    if not found:
        return None
    require(len(found) == 1, "ambiguous consumer receipt artifact")
    # GitHub 415s application/octet-stream on the artifact zip endpoint; release assets need it, artifacts must not use it (incident #61).
    archive = client.request(f"https://api.github.com/repos/{repo}/actions/artifacts/{found[0]['id']}/zip",
                             github=True)
    if found[0].get("digest"):
        require(found[0]["digest"] == "sha256:" + sha256(archive), "receipt archive digest differs")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(zipped.namelist() == ["release-receipt.json"], "unexpected receipt archive inventory")
        require(zipped.getinfo("release-receipt.json").file_size <= 2 * 1024 * 1024, "receipt exceeds size limit")
        return decode(zipped.read("release-receipt.json"))


def validate_facts_target(client, target):
    """Facts-only identity is canonical hub bytes on main, never a product event."""
    from facts_validate import validate_facts
    raw = client.file(HUB, target["commit"], "facts/facts.json")
    require(sha256(raw) == target["sha256"], "facts-only canonical digest differs")
    validate_facts(decode(raw))
    main = client.api(f"repos/{HUB}/git/ref/heads/main")["object"]["sha"]
    match(COMMIT, main)
    comparison = client.api(f"repos/{HUB}/compare/{target['commit']}...{main}")
    require(comparison["status"] in ("ahead", "identical"), "facts-only target is not canonical main history")


def collect(client, hub_client, store, role, run_id=0):
    # DELIVERY_ONLY roles have no consumer policy or evidence workflow to poll.
    require(role in RECEIPT_ROLES, "unexpected receipt role")
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
        artifacts_by_run = {}
        for artifact in object_pages(client, f"repos/{repo}/actions/artifacts?name={policy['artifact']}", "artifacts"):
            if artifact["name"] == policy["artifact"] and not artifact["expired"]:
                artifacts_by_run.setdefault(artifact["workflow_run"]["id"], {})[artifact["id"]] = artifact
        runs = [client.api(f"repos/{repo}/actions/runs/{candidate_id}")
                for candidate_id in sorted(artifacts_by_run)]
    def collect_run(run):
        if run["path"] != ".github/workflows/" + policy["workflow"]:
            # Never in the old workflow-run scope; only a callback naming such a run fails closed.
            require(not run_id, "unexpected reporting workflow")
            return
        if run_id:
            receipt = fetch_artifact(client, repo, run, policy)
        else:
            if run["status"] != "completed":
                return
            receipt = fetch_artifact(client, repo, run, policy, list(artifacts_by_run[run["id"]].values()))
        if receipt is None:
            require(not run_id, "callback has no receipt artifact")
            return
        require(receipt["consumer"] == role, "receipt role differs from scoped target")
        facts_only = receipt["schema"] == FACTS_SCHEMA
        event = None
        if not facts_only:
            row = store.get("v" + str(version(receipt["release_version"])))
            raw = store.read(row, ASSET)
            require(raw is not None and not row["draft"], "receipt has no ready release")
            event = payload(raw)
            if not run_id:
                positive(receipt["attempt"])
                positive(receipt["sequence"])
                previous = receipts(store, row, event["event_id"]).get(role)
                if previous is not None:
                    old = (previous["attempt"], previous["sequence"])
                    new = (receipt["attempt"], receipt["sequence"])
                    # Only omit work that cannot advance the stored receipt.
                    if old > new or (old == new and previous.get("evidence_sha256") == sha256(canonical(receipt))):
                        return
        public = validate_receipt(client, repo, receipt, event, policy, run)
        if facts_only:
            facts_target = receipt["facts"]
            validate_facts_target(hub_client, facts_target)
            row = store.facts()
        for identity_name in ("preview", "deployed"):
            observed = public.get(identity_name)
            if observed is None or facts_only:
                continue
            original = hub_client.file(HUB, event["facts"]["commit"], event["facts"]["path"])
            consumed = hub_client.file(HUB, observed["facts_commit"], event["facts"]["path"])
            comparison = hub_client.api(f"repos/{HUB}/compare/{event['facts']['commit']}...{observed['facts_commit']}")
            require(comparison["status"] in ("ahead", "identical"), "consumed canonical facts are older or divergent")
            from release_contract import retain_facts
            require(retain_facts(event, original, consumed) == observed["facts_sha256"], "consumed facts digest differs")
        prefix = "facts-consumer-" if facts_only else "consumer-"
        if facts_only:
            previous = None
            for item in sorted(row["assets"], key=lambda a: a["id"]):
                if item["name"].startswith(prefix + role + "-" + facts_target["commit"] + "-" + facts_target["sha256"] + "-"):
                    obj = decode(store.read(row, item["name"]))
                    if previous is None or (obj["attempt"], obj["sequence"]) > (previous["attempt"], previous["sequence"]):
                        previous = obj
        elif run_id:
            previous = receipts(store, row, event["event_id"]).get(role)
        if transition(previous, public):
            stem = prefix + role + "-" + (facts_target["commit"] + "-" + facts_target["sha256"] + "-" if facts_only else "")
            name = f"{stem}{public['attempt']}-{public['sequence']}.json"
            receipt_raw = canonical(public)
            if run_id:
                store.put(row, name, receipt_raw)
            if public["state"] == "failed":
                incident = "facts-" + facts_target["sha256"] if facts_only else row["tag_name"]
                title = "Facts consumer failed" if facts_only else f"Release {event['release_version']}: consumer failed"
                report_issue(hub_client, incident, title,
                             f"Consumer role: {role}; failed stage evidence retained in the consumer's private report.\n\n" + run_link())
            if not run_id:
                # Keep the sweep's only receipt write last, after every other fallible step.
                store.put(row, name, receipt_raw)

    skipped = []
    for run in sorted(runs, key=lambda row: row["id"]):
        if run_id:
            # Callbacks retain their fail-closed behavior outside the sweep handler.
            collect_run(run)
        else:
            try:
                collect_run(run)
            except (Invalid, Pending, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
                # The text can carry consumer-controlled bytes: no markup, mention, marker or line break reaches an issue.
                safe_characters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _.,;()'=-"
                reason = "".join(char if char in safe_characters else "?" for char in str(error))[:200]
                skipped.append((run["id"], reason))
                print(f"::warning::receipt artifact skipped: role={role} run={run['id']}: {reason}", file=sys.stderr)
    if skipped:
        skipped_lines = [f"run={candidate_id}: {reason}" for candidate_id, reason in sorted(skipped)[:50]]
        if len(skipped) > 50:
            skipped_lines.append(f"and {len(skipped) - 50} more skipped")
        report_issue(hub_client, "receipt-skipped-" + role,
                     f"Release chain: receipt artifacts skipped ({role})",
                     "\n".join(skipped_lines))


def signal_targets(signal, enabled):
    keys(signal, ("schema", "consumer", "release_version", "run_id"))
    require(signal["schema"] == "pineforge/consumer-signal/v1"
            and signal["consumer"] in RECEIPT_ROLES and signal["consumer"] in enabled, "invalid consumer signal")
    if signal["release_version"] is None:
        require(signal["consumer"] in ("web", "license"), "unexpected facts-only signal role")
    else:
        version(signal["release_version"])
    positive(signal["run_id"])
    return [{"consumer": signal["consumer"], "run_id": str(signal["run_id"])}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("collect")
    p.add_argument("--consumer", choices=RECEIPT_ROLES, required=True)
    p.add_argument("--run-id", default="0")
    p = sub.add_parser("delivery")
    for name in ("tag", "consumer", "file"):
        p.add_argument("--" + name, required=True)
    p = sub.add_parser("signal")
    p.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "signal":
            enabled = receipt_roles()
            event = decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
            if os.environ["GITHUB_EVENT_NAME"] == "repository_dispatch":
                signal = event["client_payload"]
                targets = signal_targets(signal, enabled)
            else:
                targets = [{"consumer": role, "run_id": "0"} for role in enabled]
            # This workflow is serialized. Create the audit bucket before the
            # parallel web/license collectors can race to create two drafts.
            if any(target["consumer"] in ("web", "license") for target in targets):
                Store(Client()).facts()
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
                if obj["state"] == "dispatch_stopped":
                    require(obj["consumer"] == args.consumer and store.read(row, f"delivery-failed-{args.consumer}.json") is not None,
                            "delivery stop has no exhausted retry history")
                    return 0
                require(obj["consumer"] == args.consumer and obj["state"] == "dispatch_accepted", "invalid delivery receipt")
                event = payload(store.read(row, ASSET))
                require(obj["event_id"] == event["event_id"], "delivery event differs")
                name = f"delivery-{args.consumer}.json"
                if store.read(row, name) is None:
                    store.put(row, name, canonical(obj))
    except (Invalid, Pending, ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"::error::consumer evidence invalid or unavailable; no verified state recorded: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
