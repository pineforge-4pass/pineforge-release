#!/usr/bin/env python3
"""Build synthetic consumer receipt v1 examples; never send a callback.

Replace the synthetic PR, workflow and served identities with observed evidence
in a real consumer. CI validates both examples with the hub's actual validator.
These examples use the ordinary five-key served identity, including previous.
The temporary data.previous bootstrap is not synthesized: it describes only
the pinned pre-manifest pineforge-data 0.3.0 release in docs/release-chain.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from release_contract import ROLES, canonical, payload, require


def example_policy(consumer="offline"):
    if consumer == "hosted":
        return {
            "workflow": "release-report.yml", "artifact": "release-receipt-hosted", "base": "main",
            "ci": {"workflow": "release-pr.yml", "expected": ["authz", "validate", "checks", "container"], "approved_skips": []},
            "e2e": {"workflow": "release-postmerge.yml", "expected": ["authz", "build", "verified"], "approved_skips": []},
            "deploy": {"workflow": "release.yml", "expected": ["decide", "deploy"], "approved_skips": []},
        }
    if consumer == "application":
        # Only the base, artifact, CI job names and post-merge gate are known.
        # Replace example workflow names and E2E/deploy inventories from APP.
        return {
            "workflow": "example-application-report.yml", "artifact": "release-receipt-application", "base": "dev",
            "ci": {"workflow": "example-application-ci.yml",
                   "expected": ["check", "image-build-contexts", "studio-responsive (1)", "studio-responsive (2)",
                                "studio-responsive (3)", "studio-paths", "studio-perf-build", "studio-perf"],
                   "approved_skips": []},
            "e2e": {"workflow": "example-application-e2e.yml", "expected": ["example-candidate-e2e"], "approved_skips": []},
            "deploy": {"workflow": "example-application-deploy.yml",
                       "expected": ["example-served-identity", "example-rollback-identity"], "approved_skips": []},
            "post_merge": {"workflow": "ci-post-merge.yml", "expected": ["test", "int"], "approved_skips": []},
        }
    policy = {"workflow": "release.yml", "artifact": "release-receipt-" + consumer, "base": "main"}
    for name in ("ci", "e2e", "deploy"):
        policy[name] = {"workflow": name + ".yml", "expected": [name + "/one", name + "/two"], "approved_skips": []}
    return policy


def build_receipt(event=None, *, consumer="offline", facts=None):
    """A complete verified snapshot; facts-only has no product identity fields."""
    require(consumer in ROLES, "unexpected consumer")
    require((facts is not None and consumer in ("web", "license")) or event is not None, "receipt target missing")
    head, merge = "1" * 40, "2" * 40
    policy = example_policy(consumer)
    receipt = {"schema": "pineforge/facts-consumer-receipt/v1" if facts is not None else "pineforge/consumer-receipt/v1",
               "consumer": consumer, "attempt": 90, "sequence": 7, "state": "verified", "timestamp": "2020-01-01T00:07:00Z",
               "pr": {"number": 9, "base": policy["base"], "base_sha": "3" * 40, "head": head, "merge": merge}}
    if facts is not None:
        receipt["facts"] = dict(facts)
        image_digest = None
        facts_commit, facts_sha = facts["commit"], facts["sha256"]
    else:
        receipt.update(event_id=event["event_id"], release_version=event["release_version"])
        image_digest = event["image"]["digest"]
        facts_commit, facts_sha = event["facts"]["commit"], event["facts"]["sha256"]
    sections = ("ci", "e2e", "deploy") + (("post_merge",) if consumer == "application" else ())
    for run_id, name in enumerate(sections, 1):
        expected = policy[name]["expected"]
        sha = merge if name in ("deploy", "post_merge") else head
        if consumer == "hosted" and name == "e2e":
            sha = merge
        receipt[name] = {"run_id": run_id, "run_attempt": 1, "sha": sha,
                         "expected": expected, "approved_skips": [], "results": {item: "success" for item in expected}}
    identity = {"source": head, "image_digest": image_digest, "facts_commit": facts_commit,
                "facts_sha256": facts_sha, "deployment_id_sha256": "d" * 64}
    preview = {**identity, "source": merge} if consumer == "hosted" else identity
    receipt.update(preview=preview, deployed={**identity, "source": merge}, previous={**identity, "source": "4" * 40})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/release-manifest.json")
    parser.add_argument("--consumer", choices=ROLES)
    parser.add_argument("--facts-only", action="store_true")
    parser.add_argument("--facts-commit", default="e" * 40)
    parser.add_argument("--facts-sha256", default="f" * 64)
    args = parser.parse_args()
    if args.facts_only:
        receipt = build_receipt(consumer=args.consumer or "license", facts={"commit": args.facts_commit, "sha256": args.facts_sha256})
    else:
        receipt = build_receipt(payload(args.manifest.read_bytes()), consumer=args.consumer or "offline")
    sys.stdout.buffer.write(canonical(receipt))


if __name__ == "__main__":
    main()
