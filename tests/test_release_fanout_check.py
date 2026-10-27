#!/usr/bin/env python3
"""Fake-client tests for release fanout startup evidence."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest.mock import patch

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_contract as contract  # noqa: E402
import release_fanout_check as fanout  # noqa: E402


EVENT_ID = "sha256:" + "a" * 64
DISPATCHED_AT = "2026-10-27T00:00:00Z"
TAG = "v1.7.0"


def receiver_run(*, conclusion=None, status="completed", event_id=None, workflow="pineforge-release"):
    title = workflow
    if event_id:
        title += " " + event_id
    return {
        "id": 7001,
        "name": workflow,
        "path": ".github/workflows/" + workflow + ".yml",
        "event": "repository_dispatch",
        "created_at": "2026-10-27T00:00:05Z",
        "status": status,
        "conclusion": conclusion,
        "display_title": title,
        "html_url": "https://github.com/pineforge-4pass/fixture/actions/runs/7001",
    }


class ReceiverClient:
    def __init__(self, runs=(), artifacts=()):
        self.runs = list(runs)
        self.artifacts = list(artifacts)
        self.routes = []

    def api(self, route):
        self.routes.append(route)
        if "/actions/runs?" in route:
            return {"total_count": len(self.runs), "workflow_runs": copy.deepcopy(self.runs)}
        if "/actions/runs/" in route and route.endswith("/artifacts?per_page=100&page=1"):
            return {"total_count": len(self.artifacts), "artifacts": copy.deepcopy(self.artifacts)}
        raise AssertionError("unexpected receiver route: " + route)


class HubClient:
    def __init__(self, release, assets):
        self.release = copy.deepcopy(release)
        self.assets = dict(assets)
        self.routes = []

    def api(self, route):
        self.routes.append(route)
        if route == f"repos/{contract.HUB}/releases/tags/{TAG}":
            return copy.deepcopy(self.release)
        raise AssertionError("unexpected hub route: " + route)

    def request(self, url, **kwargs):
        self.routes.append(url)
        asset_id = int(url.rsplit("/", 1)[-1])
        return self.assets[asset_id]


def release_with_delivery():
    ready = contract.canonical({"event_id": EVENT_ID, "timestamp": DISPATCHED_AT})
    hosted = contract.canonical({
        "schema": "pineforge/release-delivery/v1", "event_id": EVENT_ID,
        "consumer": "hosted", "state": "dispatch_accepted", "attempt": 1,
        "run_id": 41, "timestamp": DISPATCHED_AT,
    })
    release = {
        "tag_name": TAG, "draft": False,
        "assets": [
            {"id": 1, "name": "release-ready.json", "state": "uploaded"},
            {"id": 2, "name": "delivery-hosted.json", "state": "uploaded"},
        ],
    }
    return HubClient(release, {1: ready, 2: hosted})


class ReleaseFanoutCheckTests(unittest.TestCase):
    def test_configuration_preserves_explicit_dispatch_selection(self):
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "hosted",
                "RELEASE_READY_EXCLUDE": "",
                "FACTS_WEB_DISPATCH_ENABLED": "false",
                "FACTS_LICENSE_DISPATCH_ENABLED": "false",
        }, clear=True):
            self.assertEqual(fanout.configuration(), (["hosted"], []))

    def test_missing_target_repository_names_role_and_fails_before_dispatch(self):
        targets = {
            "offline": "offline-repo", "hosted": "hosted-repo",
            "application": "", "data": "data-repo",
        }
        with patch.dict(os.environ, {"RELEASE_READY_EXCLUDE": ""}, clear=True):
            with self.assertRaisesRegex(contract.Invalid, "missing target repository for role application"):
                fanout.validate_target_repositories(
                    ["offline", "hosted", "application", "data"], targets=targets)

    def test_receiver_that_never_starts_fails_after_the_bounded_poll_count(self):
        client = ReceiverClient()
        sleeps = []
        with self.assertRaisesRegex(contract.Invalid,
                                    r"receiver run missing: role=application repo=app tag=v1\.7\.0 event_id=sha256:"):
            fanout.wait_for_receiver(client, "application", "app", TAG, EVENT_ID, DISPATCHED_AT,
                                     sleep=sleeps.append)
        self.assertEqual(len(sleeps), len(fanout.RECEIVER_RUN_POLL_DELAYS_SECONDS) - 1)
        self.assertEqual(len(client.routes), len(fanout.RECEIVER_RUN_POLL_DELAYS_SECONDS))

    def test_failed_receiver_run_fails_and_successful_receiver_run_passes(self):
        failed = ReceiverClient([receiver_run(conclusion="failure")])
        with self.assertRaisesRegex(contract.Invalid, "conclusion=failure"):
            fanout.wait_for_receiver(failed, "application", "app", TAG, EVENT_ID, DISPATCHED_AT,
                                     sleep=lambda _: None)

        succeeded = ReceiverClient([receiver_run(conclusion="success", event_id=EVENT_ID)])
        result = fanout.wait_for_receiver(succeeded, "application", "app", TAG, EVENT_ID, DISPATCHED_AT,
                                          sleep=lambda _: None)
        self.assertEqual(result["id"], 7001)
        self.assertEqual(result["event_id_match"], "exact")

    def test_exclusion_is_visible_in_configuration_output(self):
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "offline,hosted,data",
                "RELEASE_READY_EXCLUDE": "application,web,license,compute",
                "FACTS_WEB_DISPATCH_ENABLED": "false",
                "FACTS_LICENSE_DISPATCH_ENABLED": "false",
        }, clear=True):
            enabled, excluded = fanout.configuration()
            self.assertEqual(enabled, ["offline", "hosted", "data"])
            self.assertEqual(excluded, ["application", "web", "license", "compute"])
            rendered = fanout.render_human({"excluded": excluded, "roles": []})
        self.assertIn("application", rendered)

    def test_compute_uses_its_release_pair_receiver_workflow_marker(self):
        succeeded = ReceiverClient([receiver_run(conclusion="success", event_id=EVENT_ID,
                                                 workflow="release-pair")])
        result = fanout.wait_for_receiver(succeeded, "compute", "compute-repo", TAG,
                                          EVENT_ID, DISPATCHED_AT, sleep=lambda _: None)
        self.assertEqual(result["event_id_match"], "exact")

    def test_event_id_can_be_found_in_receiver_artifact_name(self):
        run = receiver_run(conclusion="success")
        client = ReceiverClient([run], [{"name": EVENT_ID}])
        result = fanout.wait_for_receiver(client, "application", "app", TAG,
                                          EVENT_ID, DISPATCHED_AT, sleep=lambda _: None)
        self.assertEqual(result["event_id_match"], "exact")

    def test_protected_delivery_path_is_refused_before_resolving(self):
        with patch.object(fanout.Path, "resolve", side_effect=AssertionError("must not resolve")):
            with self.assertRaisesRegex(contract.Invalid, "^delivery path refused$"):
                fanout.safe_path("/Users/fixture/.ssh/key.json")

    def test_17_release_application_miss_is_a_failing_fanout_row(self):
        hub = release_with_delivery()
        receiver = ReceiverClient()
        targets = {
            "offline": "offline-repo", "hosted": "fixture",
            "application": "pineforge-app", "data": "data-repo",
        }
        with patch.dict(os.environ, {
                "RELEASE_READY_CONSUMERS": "hosted",
                "RELEASE_READY_EXCLUDE": "",
                "FACTS_WEB_DISPATCH_ENABLED": "false",
                "FACTS_LICENSE_DISPATCH_ENABLED": "false",
        }, clear=True):
            report = fanout.check_release(hub, receiver, TAG, targets=targets)
        application = next(row for row in report["roles"] if row["role"] == "application")
        self.assertFalse(application["dispatched"])
        self.assertFalse(application["receiver_run_found"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["event_id"], EVENT_ID)


if __name__ == "__main__":
    unittest.main()
