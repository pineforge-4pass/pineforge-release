#!/usr/bin/env python3
"""Reject a ready release and restore only its recorded moving aliases. Delete nothing."""
import argparse
import os
import sys

from release_artifacts import Client
from release_contract import ASSET, Invalid, Pending, canonical, decode, payload, require
from release_pipeline import Registry, Store, now, report_issue, run_link


def rollback(client, store, registry, tag):
    row = store.get(tag)
    raw = store.read(row, ASSET)
    require(raw is not None, "rollback requires a finalized manifest")
    event = payload(raw)
    before = store.read(row, "release-aliases-before.json")
    require(before is not None, "rollback has no prior alias evidence")
    rejected = store.read(row, "release-rejected.json")
    if rejected is None:
        store.put(row, "release-rejected.json", canonical({"event_id": event["event_id"], "timestamp": now(),
                                                          "reason": "rollback", "run_id": int(os.environ["GITHUB_RUN_ID"])}))
    failures = []
    allowed = ("latest", ".".join(event["release_version"].split(".")[:2]))
    for alias, good_digest in decode(before).items():
        label = alias if alias in allowed else "unrecognized alias"
        try:
            require(alias in allowed, "unexpected rollback alias")
            if not registry.restore(alias, event["image"]["digest"], good_digest):
                failures.append(label + ": no prior good identity; operator repair required (deletion is forbidden)")
        except (Invalid, Pending) as error:
            failures.append(label + ": " + str(error))
        except (ValueError, KeyError, TypeError, OSError):
            failures.append(label + ": alias restoration failed; inspect workflow evidence")
    report_issue(client, row["tag_name"], f"Tunaxis {event['release_version']}: rejected / rollback",
                 "Fan-out stopped. Restoration of recorded moving aliases was attempted on both image names; "
                 "newer aliases were preserved. Fixed releases, manifests, candidates, source assets and tags were preserved."
                 + ("\n\nAlias failures:\n" + "\n".join("- " + failure for failure in failures) if failures else "")
                 + "\n\n" + run_link())
    require(not failures, "rollback incomplete; inspect the incident's alias failures")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    client = Client()
    try:
        rollback(client, Store(client), Registry(), args.tag)
    except (Invalid, Pending, ValueError, KeyError, OSError):
        print("::error::Tunaxis rollback incomplete; identities preserved; inspect the incident", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
