#!/usr/bin/env python3
"""Hub reconciliation, append-only release assets, finalization and ready delivery.

Only Actions calls the mutating commands. Unit/replay tests inject a fake client,
store and registry; there is deliberately no fake-success production switch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from urllib.parse import quote

import release_pair as pair
from release_artifacts import Client, PRODUCERS, command, npm, producer
from release_contract import (ASSET, COMMIT, HUB, IMAGE, ROLES, SCHEMA, SHA, Invalid,
                              Pending, bind_facts, canonical, decode, keys, match,
                              payload, positive, require, sha256, validate, version)

CANDIDATE = "release-candidate.json"
FACTS_RECEIPTS_TAG = "facts-receipts"
# Delay after each reserved attempt: two retries, then an acknowledgement grace
# period before a terminal failure. Reservations survive failed/cancelled jobs.
DELIVERY_BACKOFF_MINUTES = (15, 60, 15)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_minutes(timestamp):
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds() / 60


class Store:
    def __init__(self, client):
        self.client = client

    def get(self, tag):
        version(tag.removeprefix("v"))
        # GitHub's tags endpoint exposes only published releases. The caller
        # needs Contents: write so the paginated list includes candidate drafts.
        found = [row for row in self.releases() if row["tag_name"] == tag]
        require(len(found) <= 1, "ambiguous hub release tag")
        if not found:
            raise Pending("hub release unavailable")
        return found[0]

    def releases(self):
        return self.client.pages(f"repos/{HUB}/releases")

    def facts(self):
        """Private audit bucket, independent of any product version or event."""
        found = [row for row in self.releases() if row["tag_name"] == FACTS_RECEIPTS_TAG]
        require(len(found) <= 1, "ambiguous facts receipt storage")
        if found:
            require(found[0]["draft"], "facts receipt storage must remain draft")
            return found[0]
        return self.client.api(f"repos/{HUB}/releases", method="POST", data={
            "tag_name": FACTS_RECEIPTS_TAG, "target_commitish": "main", "name": "Facts consumer receipts",
            "body": "Append-only facts consumer audit storage; never publish as a product release.",
            "draft": True, "prerelease": False, "make_latest": "false"})

    def read(self, release, name):
        # Refresh every read; another job may have appended an asset.
        release = self.client.api(f"repos/{HUB}/releases/{release['id']}")
        found = [item for item in release["assets"] if item["name"] == name]
        if not found:
            return None
        require(len(found) == 1 and found[0]["state"] == "uploaded", "invalid hub asset metadata")
        raw = self.client.request(f"https://api.github.com/repos/{HUB}/releases/assets/{found[0]['id']}",
                                  github=True, accept="application/octet-stream")
        if found[0].get("digest"):
            require(found[0]["digest"] == "sha256:" + sha256(raw), "hub asset digest differs")
        return raw

    def put(self, release, name, raw):
        """Never overwrite/delete an asset. Replay must preserve its exact bytes."""
        match(r"[A-Za-z0-9_.-]+\.json", name)
        previous = self.read(release, name)
        if previous is not None:
            require(previous == raw, "immutable hub asset already exists with different bytes")
            return
        self.client.request(f"https://uploads.github.com/repos/{HUB}/releases/{release['id']}/assets?name={quote(name)}",
                            method="POST", data=raw, github=True, binary=True)
        require(self.read(release, name) == raw, "hub asset upload readback differs")

    def ensure_candidate_release(self, tag, notes):
        try:
            return self.get(tag)
        except Pending:
            self.client.tag_commit(HUB, tag)  # never silently create a tag
            return self.client.api(f"repos/{HUB}/releases", method="POST", data={
                "tag_name": tag, "name": tag, "body": notes, "draft": True,
                "prerelease": pair.parse(tag).prerelease, "make_latest": "false"})

    def publish(self, release, latest):
        return self.client.api(f"repos/{HUB}/releases/{release['id']}", method="PATCH", data={
            "draft": False, "prerelease": pair.parse(release["tag_name"]).prerelease,
            "make_latest": "true" if latest else "false"})


class Registry:
    def raw(self, ref):
        match(r"(?:sha256:[0-9a-f]{64}|[A-Za-z0-9_.-]+)", ref)
        # Public pull token, never a GitHub credential. Reading the actual OCI
        # response bytes avoids CLI pretty-printing/newline changes to digests.
        client = Client(token="")
        token = decode(client.request("https://ghcr.io/token?service=ghcr.io&scope=repository:" + HUB + ":pull"))["token"]
        return client.request("https://ghcr.io/v2/" + HUB + "/manifests/" + ref,
                              accept="application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json",
                              extra_headers={"Authorization": "Bearer " + token})

    def digest(self, tag):
        match(r"[A-Za-z0-9_.-]+", tag)
        try:
            raw = self.raw(tag)
        except Pending:
            return None
        decode(raw)
        return "sha256:" + sha256(raw)

    def point(self, tag, digest, *, fixed=False):
        match("sha256:" + SHA, digest)
        current = self.digest(tag)
        if current == digest:
            return
        require(not fixed or current is None, "fixed image identity already exists with a different digest")
        command("docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", IMAGE + ":" + tag,
                IMAGE + "@" + digest)
        require(self.digest(tag) == digest, "image alias readback differs")


def bindings(client, release_version, engine, codegen, hpo, hub_commit):
    pair.check(release_version, engine, codegen, hpo=hpo)
    match(COMMIT, hub_commit)
    versions, inventories = {}, {}
    for role, pin in (("engine", engine), ("codegen", codegen), ("hpo", hpo)):
        if pin:
            versions[role], inventory = producer(client, role, pin)
            if inventory:
                inventories[role] = inventory
        else:
            versions[role] = None
    return {"schema": "pineforge/release-candidate/v1", "release_version": release_version,
            "hub_commit": hub_commit, "versions": versions, "inventories": inventories}


def validate_candidate(obj):
    keys(obj, ("schema", "release_version", "hub_commit", "versions", "inventories", "image", "verification", "sources", "run_id"))
    require(obj["schema"] == "pineforge/release-candidate/v1", "invalid candidate schema")
    v = version(obj["release_version"])
    match(COMMIT, obj["hub_commit"])
    positive(obj["run_id"])
    pins = obj["versions"]
    pair.check(str(v), pins["engine"]["version"], pins["codegen"]["version"], hpo=pins["hpo"]["version"] if pins["hpo"] else "")
    keys(obj["verification"], ("amd64", "arm64"))
    for result in obj["verification"].values():
        match(SHA, result["backtest_sha256"])
        positive(result["trades"])
        if pins["hpo"]:
            require(type(result["hpo_trials"]) is int and result["hpo_trials"] == 9, "HPO smoke incomplete")
            match(SHA, result["hpo_sha256"])
    keys(obj["sources"], ("release_tag", "list_asset", "list_sha256", "amd64_sha256", "arm64_sha256"))
    for name in ("list_sha256", "amd64_sha256", "arm64_sha256"):
        match(SHA, obj["sources"][name])
    return obj


def report_issue(client, key, title, body, *, complete=False):
    if os.environ.get("RELEASE_REPORT_TOKEN"):
        client = Client(token=os.environ["RELEASE_REPORT_TOKEN"])
    marker = "<!-- pineforge-release-chain:" + key + " -->"
    owner = os.environ.get("RELEASE_REPORT_OWNER", "")
    if owner:
        match(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        body = "@" + owner + "\n\n" + body
    body = marker + "\n\n" + body
    issues = client.pages(f"repos/{HUB}/issues?state=all")
    found = [row for row in issues if marker in (row.get("body") or "") and "pull_request" not in row]
    require(len(found) <= 1, "duplicate release incident markers")
    if found:
        state = "closed" if complete else "open"
        if found[0]["body"] != body or found[0]["state"] != state:
            client.api(f"repos/{HUB}/issues/{found[0]['number']}", method="PATCH",
                       data={"title": title, "body": body, "state": state})
    else:
        data = {"title": title, "body": body}
        if owner:
            data["assignees"] = [owner]
        client.api(f"repos/{HUB}/issues", method="POST", data=data)


def finalized_version(store):
    ready = []
    for row in store.releases():
        try:
            v = pair.parse(row["tag_name"])
        except ValueError:
            continue
        if not v.prerelease and not row["draft"] and any(a["name"] == ASSET for a in row["assets"]):
            if store.read(row, "release-rejected.json") is None:
                ready.append(v)
    return max(ready) if ready else None


def finalize(client, store, registry, row):
    """No image rebuild and no alias or fan-out until every ready prerequisite exists."""
    if store.read(row, "release-rejected.json") is not None:
        return None
    candidate_raw = store.read(row, CANDIDATE)
    if candidate_raw is None:
        return None  # historical release; never invent readiness for it
    candidate = validate_candidate(decode(candidate_raw))
    v = version(candidate["release_version"])
    require(row["tag_name"] == "v" + str(v), "candidate release tag differs")
    require(client.tag_commit(HUB, row["tag_name"]) == candidate["hub_commit"], "hub tag source changed")
    raw = store.read(row, ASSET)
    if raw is None:
        pins = dict(candidate["versions"])
        pins["codegen"] = npm(client, pins["codegen"], candidate["inventories"]["codegen"])
        facts_commit = client.api(f"repos/{HUB}/git/ref/heads/main")["object"]["sha"]
        facts_raw = client.file(HUB, facts_commit, "facts/facts.json")
        facts = bind_facts(facts_raw, facts_commit, str(v), pins)
        manifest = validate({"schema": SCHEMA, "release_version": str(v), "prerelease": v.prerelease,
                             "hub_commit": candidate["hub_commit"], "versions": pins, "image": candidate["image"],
                             "facts": facts, "run_id": candidate["run_id"]})
        raw = canonical(manifest)
        store.put(row, ASSET, raw)
    event = payload(raw)
    require(event["image"] == candidate["image"] and event["hub_commit"] == candidate["hub_commit"],
            "manifest does not bind this candidate")
    for role in ("engine", "codegen", "hpo"):
        expected = candidate["versions"][role]
        actual = event["versions"][role]
        if role == "codegen":
            actual = {key: value for key, value in actual.items() if key not in ("npm_version", "npm_integrity")}
        require(actual == expected, "manifest producer identity differs from candidate")
    # An immutable manifest is the commit point. Every step below can replay.
    registry.point(str(v), event["image"]["digest"], fixed=True)
    # Preserve the existing fixed alias formats, but only after readiness.
    component_tag = "engine" + event["versions"]["engine"]["version"] + "-codegen" + event["versions"]["codegen"]["version"]
    registry.point(component_tag, event["image"]["digest"], fixed=True)
    registry.point("sha-" + event["hub_commit"][:7], event["image"]["digest"], fixed=True)
    newest = finalized_version(store)
    # Freshly finalized drafts participate before they become public. A later
    # ready release must never be displaced by completion of an older draft.
    moving = not v.prerelease and (newest is None or v >= newest)
    aliases = ("latest", f"{v.major}.{v.minor}") if moving else ()
    before = store.read(row, "release-aliases-before.json")
    if before is None:
        store.put(row, "release-aliases-before.json", canonical({name: registry.digest(name) for name in aliases}))
    for name in aliases:
        registry.point(name, event["image"]["digest"])
    store.publish(row, moving)
    if store.read(row, "release-ready.json") is None:
        store.put(row, "release-ready.json", canonical({"event_id": event["event_id"], "timestamp": now()}))
    return event


def configured_roles(raw):
    roles = raw.split(",") if raw else []
    require(len(roles) == len(set(roles)) and set(roles) <= set(ROLES), "invalid release consumer role configuration")
    return roles


def receipt_roles():
    roles = configured_roles(os.environ.get("RELEASE_READY_CONSUMERS", ""))
    for role, flag in (("web", "FACTS_WEB_DISPATCH_ENABLED"), ("license", "FACTS_LICENSE_DISPATCH_ENABLED")):
        if os.environ.get(flag) == "true" and role not in roles:
            roles.append(role)
    return roles


def candidate_exists(store, tag):
    try:
        return store.read(store.get(tag), CANDIDATE) is not None
    except Pending:
        return False


def receipts(store, row, event_id):
    result = {}
    fresh = store.get(row["tag_name"])
    for item in sorted(fresh["assets"], key=lambda a: a["id"]):
        if item["name"].startswith("consumer-"):
            obj = decode(store.read(fresh, item["name"]))
            require(obj["event_id"] == event_id and obj["consumer"] in ROLES, "consumer receipt identity differs")
            old = result.get(obj["consumer"])
            if old is None or (obj["attempt"], obj["sequence"]) > (old["attempt"], old["sequence"]):
                result[obj["consumer"]] = obj
    return result


def aggregate(store, row, event, *, enabled=(), newest=None):
    by_role = receipts(store, row, event["event_id"])
    states = {}
    for role in ROLES:
        if role not in enabled:
            continue
        obj = by_role.get(role)
        if newest is not None and version(event["release_version"]) < newest and (not obj or obj["state"] != "verified"):
            states[role] = "superseded"
        elif obj:
            states[role] = obj["state"]
        elif event["prerelease"] and role in ("application", "web", "data", "license"):
            states[role] = "skipped_by_policy"
        else:
            states[role] = "pending"
    return {"schema": "pineforge/release-status/v1", "event_id": event["event_id"],
            "release_version": event["release_version"], "consumers": states,
            "complete": all(value in ("verified", "skipped_by_policy", "superseded") for value in states.values())}


def plan_delivery(store, row, event, role):
    """Reserve at most three deliveries; accepted transport never retries."""
    if store.read(row, f"delivery-{role}.json") is not None:
        return None, False
    failed_name = f"delivery-failed-{role}.json"
    if store.read(row, failed_name) is not None:
        return None, True
    attempts = []
    prefix = f"delivery-attempt-{role}-"
    for asset in store.get(row["tag_name"])["assets"]:
        if asset["name"].startswith(prefix):
            obj = decode(store.read(row, asset["name"]))
            require(obj["event_id"] == event["event_id"] and obj["consumer"] == role, "delivery attempt identity differs")
            positive(obj["attempt"])
            attempts.append(obj)
    attempts.sort(key=lambda obj: obj["attempt"])
    require([obj["attempt"] for obj in attempts] == list(range(1, len(attempts) + 1))
            and len(attempts) <= len(DELIVERY_BACKOFF_MINUTES), "invalid delivery attempt history")
    if attempts and age_minutes(attempts[-1]["timestamp"]) < DELIVERY_BACKOFF_MINUTES[len(attempts) - 1]:
        return None, False
    if len(attempts) == len(DELIVERY_BACKOFF_MINUTES):
        store.put(row, failed_name, canonical({"event_id": event["event_id"], "consumer": role,
                                             "state": "failed", "attempts": len(attempts), "timestamp": now()}))
        return None, True
    attempt = len(attempts) + 1
    store.put(row, f"{prefix}{attempt}.json", canonical({"event_id": event["event_id"], "consumer": role,
              "attempt": attempt, "run_id": int(os.environ["GITHUB_RUN_ID"]), "timestamp": now()}))
    return {"consumer": role, "tag": row["tag_name"], "attempt": attempt}, False


def redeliver(client, store, tag, consumer, output):
    """Manually resend one public event; append one audit only after acceptance."""
    require(consumer in configured_roles(os.environ.get("RELEASE_READY_CONSUMERS", "")),
            "redelivery consumer is not configured")
    require(tag == "v" + str(version(tag.removeprefix("v"))), "redelivery tag must be canonical vX.Y.Z")
    # The dispatch client has a target-only App token. Release checks and exact
    # manifest reads use a public client; the injected store appends audit.
    public = Client(token="")
    row = public.api(f"repos/{HUB}/releases/tags/{tag}")
    require(row["tag_name"] == tag and not row["draft"], "cannot redeliver a draft or different release")
    require(not any(a["name"] == "release-rejected.json" for a in row["assets"]), "release rejected")
    raw = public.download(f"https://github.com/{HUB}/releases/download/{tag}/{ASSET}", {"github.com"})
    event = payload(raw)
    require(tag == "v" + event["release_version"], "redelivery release differs")
    # Keep dispatch's channel-aware newer-release rule, including its exclusions.
    newer = [pair.parse(r["tag_name"]) for r in public.pages(f"repos/{HUB}/releases")
             if not r["draft"] and (event["prerelease"] or not r["prerelease"])
             and any(a["name"] == ASSET for a in r["assets"])
             and not any(a["name"] == "release-rejected.json" for a in r["assets"])]
    require(not newer or version(event["release_version"]) >= max(newer), "release superseded before redelivery")
    public_store = Store(public)
    accepted = public_store.read(row, f"delivery-{consumer}.json")
    if accepted is not None:
        obj = decode(accepted)
        require(obj["event_id"] == event["event_id"] and obj["consumer"] == consumer
                and obj["state"] == "dispatch_accepted", "accepted delivery identity differs")
        by_role = receipts(public_store, row, event["event_id"])
        require(by_role.get(consumer, {}).get("state") != "verified", "consumer already verified")

    prefix = f"delivery-redelivered-{consumer}-"
    actor_pattern = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?"
    attempts = []
    for asset in public_store.get(tag)["assets"]:
        if not asset["name"].startswith(prefix):
            continue
        previous = public_store.read(row, asset["name"])
        obj = decode(previous)
        keys(obj, ("schema", "event_id", "consumer", "attempt", "state", "run_id", "timestamp", "actor"))
        require(obj["schema"] == "pineforge/release-redelivery/v1"
                and obj["event_id"] == event["event_id"] and obj["consumer"] == consumer
                and obj["state"] == "dispatch_accepted", "redelivery history identity differs")
        positive(obj["attempt"])
        positive(obj["run_id"])
        require(asset["name"] == f"{prefix}{obj['attempt']}.json" and canonical(obj) == previous,
                "redelivery history asset differs")
        match(actor_pattern, obj["actor"])
        match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", obj["timestamp"])
        dt.datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
        attempts.append(obj["attempt"])
    attempts.sort()
    require(attempts == list(range(1, len(attempts) + 1)), "invalid redelivery attempt history")
    attempt = len(attempts) + 1
    positive(attempt)
    run_id = int(os.environ["GITHUB_RUN_ID"])
    positive(run_id)
    actor = match(actor_pattern, os.environ["GITHUB_ACTOR"])
    target = os.environ.get("TARGET_REPOSITORY", "")
    match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target)
    client.api(f"repos/pineforge-4pass/{target}/dispatches", method="POST",
               data={"event_type": "pineforge-release", "client_payload": event})
    audit = canonical({"schema": "pineforge/release-redelivery/v1", "event_id": event["event_id"],
                       "consumer": consumer, "attempt": attempt, "state": "dispatch_accepted",
                       "run_id": run_id, "timestamp": now(), "actor": actor})
    # No reservation or planner/receipt updates. An unsuccessful append fails
    # this run; absent an audit asset, the next run sends the same event again.
    store.put(row, f"{prefix}{attempt}.json", audit)
    Path(output).write_bytes(audit)


def facts_freshness(client, store, roles):
    """30 minutes from the actual facts file change, independent of main's age."""
    roles = [role for role in roles if role in ("web", "license")]
    if not roles:
        return None
    changes = client.api(f"repos/{HUB}/commits?path=facts/facts.json&sha=main&per_page=1")
    require(type(changes) is list and len(changes) == 1, "missing facts change clock")
    change = changes[0]
    match(COMMIT, change["sha"])
    changed_at = change["commit"]["committer"]["date"]
    elapsed = age_minutes(changed_at)
    require(elapsed >= 0, "future facts change clock")
    facts_sha = sha256(client.file(HUB, change["sha"], "facts/facts.json"))
    latest = {}
    for row in store.releases():
        for item in row["assets"]:
            if item["name"].startswith(("consumer-", "facts-consumer-")):
                obj = decode(store.read(row, item["name"]))
                role = obj["consumer"]
                if role in roles and obj["state"] in ("verified", "failed", "deploying"):
                    previous = latest.get(role)
                    if previous is None or obj["timestamp"] > previous["timestamp"]:
                        latest[role] = obj
    states = {role: "verified" if latest.get(role, {}).get("state") == "verified"
              and latest.get(role, {}).get("deployed", {}).get("facts_sha256") == facts_sha
              else "overdue" if elapsed > 30 else "pending" for role in roles}
    report = {"schema": "pineforge/facts-freshness/v1", "commit": change["sha"], "sha256": facts_sha,
              "changed_at": changed_at, "consumers": states}
    report_issue(client, "facts-" + facts_sha, "Facts consumers: " + ("verified" if set(states.values()) == {"verified"} else "pending/overdue"),
                 "Canonical facts changed at " + changed_at + "; freshness threshold: 30 minutes.\n\n"
                 + "\n".join(f"- {role}: {state}" for role, state in states.items())
                 + "\n\nOnly verified served-state receipts count. Unrelated main commits and accepted dispatches do not reset this clock.",
                 complete=set(states.values()) == {"verified"})
    return report


def finalize_all(client, store, registry, tag, output):
    enabled = configured_roles(os.environ.get("RELEASE_READY_CONSUMERS", ""))
    rows = [store.get(tag)] if tag else store.releases()
    deliveries, failures = [], []
    # Highest semver first: old completion cannot briefly acquire latest.
    rows = [row for row in rows if any(a["name"] == CANDIDATE for a in row["assets"])]
    rows.sort(key=lambda r: pair.parse(r["tag_name"]), reverse=True)
    for row in rows:
        try:
            event = finalize(client, store, registry, row)
            if event is None:
                continue
            newest = finalized_version(store)
            status = aggregate(store, row, event, enabled=enabled, newest=newest)
            for role, state in status["consumers"].items():
                if state == "pending":
                    delivery, failed = plan_delivery(store, row, event, role)
                    if delivery:
                        deliveries.append(delivery)
                    if failed:
                        status["consumers"][role] = "failed"
            status["complete"] = all(state in ("verified", "skipped_by_policy", "superseded")
                                     for state in status["consumers"].values())
            pending = [role for role, state in status["consumers"].items()
                       if state in ("pending", "pr_open", "testing", "ready", "merged", "deploying")]
            by_role = receipts(store, row, event["event_id"])
            ready_at = decode(store.read(row, "release-ready.json"))["timestamp"]
            stale = [role for role in pending if age_minutes(by_role.get(role, {}).get("timestamp", ready_at)) > int(os.environ.get(
                "RELEASE_APPLICATION_MAX_AGE_MINUTES" if role == "application" else "RELEASE_CONSUMER_MAX_AGE_MINUTES",
                "1440" if role == "application" else "180"))]
            status["stale"] = stale
            body = "Event: `" + event["event_id"] + "`\n\n" + "\n".join(
                f"- {role}: {state}" + (" (completion overdue)" if role in stale else "") for role, state in status["consumers"].items())
            body += "\n\nDispatch acceptance is transport only. Verified means checked consumer receipt and served-state evidence."
            previous_status = None
            for asset in sorted(store.get(row["tag_name"])["assets"], key=lambda item: item["id"]):
                if asset["name"].startswith("release-status-"):
                    previous_status = store.read(row, asset["name"])
            if previous_status != canonical(status):
                report_issue(client, row["tag_name"], f"Release {event['release_version']}: " + ("complete" if status["complete"] else "pending/failed"),
                             body, complete=status["complete"])
                store.put(row, f"release-status-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}.json", canonical(status))
        except Pending as error:
            limit = int(os.environ.get("RELEASE_READY_MAX_AGE_MINUTES", "60"))
            overdue = age_minutes(row["created_at"]) > limit
            report_issue(client, row["tag_name"], f"Release {row['tag_name']}: readiness pending",
                         str(error) + (" (readiness overdue)" if overdue else "")
                         + "\n\nThe scheduled finalizer retries without rebuilding the candidate.\n\n" + run_link())
        except (Invalid, ValueError, KeyError, TypeError):
            failures.append(row["tag_name"])
            report_issue(client, row["tag_name"], f"Release {row['tag_name']}: readiness failed",
                         "Readiness verification failed. No new aliases or consumer delivery authorized.\n\n" + run_link())
    Path(output).write_bytes(canonical(deliveries))
    facts_freshness(client, store, receipt_roles())
    require(not failures, "release finalization failed; inspect release incidents")


def run_link():
    return f"https://github.com/{HUB}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"


def reconcile(client, store):
    """Replay stable public releases; never execute producer version-bump logic."""
    floor = version(Path("VERSION").read_text().strip())
    failures, candidates = [], []
    for component, repo in PRODUCERS.items():
        for row in client.pages(f"repos/{repo}/releases"):
            if row["draft"] or row["prerelease"]:
                continue
            try:
                v = pair.parse(pair.hpo_version(row["tag_name"], to_semver=True) if component == "hpo" else row["tag_name"])
            except ValueError:
                continue  # source bundles and other non-product releases
            if v.prerelease or v.line != "pair" or v < floor or (component == "hpo" and not pair.hpo_pairing(v)):
                continue
            candidates.append((v, component))
    # Reconcile the desired stable version once per source. Replaying older
    # releases after a newer dispatch would manufacture downgrade incidents;
    # the handler still owns lockstep, mismatch and duplicate decisions.
    desired = max((v for v, _ in candidates), default=None)
    if desired is None:
        return
    try:
        client.tag_commit(HUB, "v" + str(desired))
    except Pending:
        pass
    else:
        # A hub tag hands responsibility to publish/finalize, including drafts
        # awaiting facts. Do not re-download producer artifacts or re-dispatch.
        return
    for v, component in sorted(set(candidates), key=lambda item: (item[0], item[1])):
        if v != desired:
            continue
        try:
            producer(client, component, str(v))
            client.api(f"repos/{HUB}/dispatches", method="POST", data={"event_type": component + "-release",
                "client_payload": {"version": v.pep440 if component == "hpo" else str(v), "prerelease": False,
                                   "run_id": int(os.environ["GITHUB_RUN_ID"])}})
        except Pending:
            report_issue(client, f"producer-{component}-{v}", f"Producer {component} {v}: artifacts pending", run_link())
        except (Invalid, ValueError, KeyError, TypeError):
            failures.append(component)
            report_issue(client, f"producer-{component}-{v}", f"Producer {component} {v}: verification failed", run_link())
    require(not failures, "producer reconciliation failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("producer")
    p.add_argument("component", choices=PRODUCERS)
    p.add_argument("version")
    p = sub.add_parser("bindings")
    for field in ("release-version", "engine", "codegen", "hub-commit", "output"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--hpo", default="")
    p = sub.add_parser("candidate-exists")
    p.add_argument("tag")
    p = sub.add_parser("candidate")
    for field in ("file", "tag", "notes-file", "source-tag", "source-list", "amd64", "arm64"):
        p.add_argument("--" + field, required=True)
    p = sub.add_parser("finalize")
    p.add_argument("--tag", default="")
    p.add_argument("--output", required=True)
    sub.add_parser("reconcile")
    p = sub.add_parser("dispatch")
    p.add_argument("--tag", required=True)
    p.add_argument("--consumer", choices=ROLES, required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--attempt", type=int, default=1)
    p = sub.add_parser("redeliver")
    p.add_argument("--tag", required=True)
    p.add_argument("--consumer", choices=ROLES, required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("alert")
    p.add_argument("--stage", required=True, choices=("publish", "producer", "finalize", "dispatch", "receipt"))
    args = parser.parse_args()
    client = Client()
    store = Store(client)
    try:
        if args.cmd == "producer":
            producer(client, args.component, args.version)
        elif args.cmd == "bindings":
            Path(args.output).write_bytes(canonical(bindings(client, args.release_version, args.engine,
                                                          args.codegen, args.hpo, args.hub_commit)))
        elif args.cmd == "candidate-exists":
            print("exists=" + ("true" if candidate_exists(store, args.tag) else "false"))
        elif args.cmd == "candidate":
            obj = decode(Path(args.file).read_bytes())
            obj.update(sources={"release_tag": args.source_tag, "list_asset": Path(args.source_list).name,
                                "list_sha256": sha256(Path(args.source_list).read_bytes()),
                                "amd64_sha256": sha256(Path(args.amd64).read_bytes()),
                                "arm64_sha256": sha256(Path(args.arm64).read_bytes())}, run_id=int(os.environ["GITHUB_RUN_ID"]))
            validate_candidate(obj)
            require(args.tag == "v" + obj["release_version"], "candidate tag differs")
            row = store.ensure_candidate_release(args.tag, Path(args.notes_file).read_text())
            store.put(row, CANDIDATE, canonical(obj))
        elif args.cmd == "finalize":
            if args.tag:
                version(args.tag.removeprefix("v"))
            finalize_all(client, store, Registry(), args.tag, args.output)
        elif args.cmd == "reconcile":
            reconcile(client, store)
        elif args.cmd == "redeliver":
            # App token: dispatch to one consumer. Hub token: append audit only.
            redeliver(client, Store(Client(token=os.environ["HUB_TOKEN"])),
                      args.tag, args.consumer, args.output)
        elif args.cmd == "dispatch":
            require(1 <= args.attempt <= len(DELIVERY_BACKOFF_MINUTES), "invalid delivery attempt")
            # The App token here is scoped ONLY to the target. Read the public
            # immutable hub via an uncredentialed client/gh environment.
            public = Client(token="")
            row = public.api(f"repos/{HUB}/releases/tags/{args.tag}")
            require(not row["draft"], "cannot dispatch a draft candidate")
            require(not any(a["name"] == "release-rejected.json" for a in row["assets"]), "release rejected")
            accepted = Store(public).read(row, f"delivery-{args.consumer}.json")
            if accepted is not None:
                Path(args.output).write_bytes(accepted)
                return 0
            exhausted = Store(public).read(row, f"delivery-failed-{args.consumer}.json")
            if exhausted is not None:
                # A previously queued job can start after the planner exhausted
                # the bound. Preserve an audit outcome without sending again.
                Path(args.output).write_bytes(canonical({**decode(exhausted), "state": "dispatch_stopped"}))
                return 0
            raw = public.download(f"https://github.com/{HUB}/releases/download/{args.tag}/{ASSET}", {"github.com"})
            event = payload(raw)
            require(args.tag == "v" + event["release_version"], "dispatch release differs")
            newer = [pair.parse(r["tag_name"]) for r in public.pages(f"repos/{HUB}/releases")
                     if not r["draft"] and (event["prerelease"] or not r["prerelease"])
                     and any(a["name"] == ASSET for a in r["assets"])
                     and not any(a["name"] == "release-rejected.json" for a in r["assets"])]
            require(not newer or version(event["release_version"]) >= max(newer), "release superseded before dispatch")
            target = os.environ.get("TARGET_REPOSITORY", "")
            match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target)
            client.api(f"repos/pineforge-4pass/{target}/dispatches", method="POST",
                       data={"event_type": "pineforge-release", "client_payload": event})
            Path(args.output).write_bytes(canonical({"schema": "pineforge/release-delivery/v1", "event_id": event["event_id"],
                "consumer": args.consumer, "state": "dispatch_accepted", "attempt": args.attempt,
                "run_id": int(os.environ["GITHUB_RUN_ID"]), "timestamp": now()}))
        else:
            report_issue(client, args.stage + "-failure", "Release chain: " + args.stage + " failed", run_link())
    except Pending as error:
        print("::notice::" + str(error), file=sys.stderr)
        return 3
    except (Invalid, ValueError, OSError, KeyError, TypeError, pair.RuleError):
        # No untrusted API bodies, private target names, paths or credentials.
        print("::error::release chain operation failed; inspect the neutral incident and workflow run", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
