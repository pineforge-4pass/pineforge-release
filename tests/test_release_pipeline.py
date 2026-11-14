"""Producer-content and send-boundary authorization controls for ordinary CI.

Only transport and storage are fixtures. The real CLI, authorizer, serializer,
artifact reader and aggregation rules execute in every control.
"""
from __future__ import annotations

import copy
import io
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_contract as contract  # noqa: E402
import release_pipeline as pipeline  # noqa: E402
from test_release_binding import (DELIVERY_SOURCES as SOURCE_FILES,  # noqa: E402
                                  MemoryStore, fixture_manifest)

TAG = "v1.6.0"
CLOCK = "2026-10-23T00:00:30Z"
COMMIT = "a" * 40


class DeliveryAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.manifest = fixture_manifest()
        self.store = MemoryStore(self.manifest)
        self.row = self.store.rows[TAG]
        self.row.update(draft=False, prerelease=False)
        self.raw = contract.canonical(self.manifest)
        self.store.put(self.row, contract.ASSET, self.raw)
        self.event = contract.payload(self.raw)
        self.hub = self.store
        self.hub.variables = {"RELEASE_READY_CONSUMERS": "application,compute",
                              "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}
        self.sent = []
        self.target = self
        self.token = "fixture-target-token"
        self.response_status = 204
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "delivery.json"
        env = {"GH_TOKEN": self.token, "HUB_TOKEN": self.hub.token,
               "GITHUB_RUN_ID": "901", "GITHUB_RUN_ATTEMPT": "2",
               "GITHUB_SHA": COMMIT, "GITHUB_ACTOR": "release-operator",
               "TARGET_REPOSITORY": "fixture-target",
               "RELEASE_READY_CONSUMERS": "application,compute",
               "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}
        self.start(patch.dict(os.environ, env, clear=True))
        self.start(patch.object(pipeline, "now", return_value=CLOCK))
        self.start(patch.object(pipeline, "age_minutes", return_value=1000))
        self.start(patch.object(pipeline, "Client", side_effect=self.client))
        self.start(patch.object(pipeline.urllib.request, "build_opener", return_value=self))

    def start(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def client(self, token=None):
        if token is None:
            return self.target
        self.assertIn(token, ("", self.hub.token))
        return self.hub

    def api(self, route, *, method="GET", data=None):
        # The old automatic sender uses Client.api; retain this baseline transport
        # so the red control fails on its assertion, rather than a fixture error.
        self.assertEqual((route, method), ("repos/pineforge-4pass/fixture-target/dispatches", "POST"))
        self.sent.append(contract.canonical(data))
        return None

    def open(self, request, timeout):
        self.assertEqual(request.full_url, "https://api.github.com/repos/pineforge-4pass/fixture-target/dispatches")
        self.sent.append(request.data)
        response = io.BytesIO()
        response.status = self.response_status
        return response

    def invoke(self, consumer, *, manual=False):
        if manual:
            return pipeline.redeliver(self.target, self.store, TAG, consumer, self.output)
        with patch.object(sys, "argv", [
                "release_pipeline.py", "dispatch", "--tag", TAG, "--consumer", consumer,
                "--output", str(self.output)]):
            return pipeline.main()

    def record(self, *, consumer="compute", manual=True, legacy=False):
        record = {"schema": "pineforge/release-redelivery/v2" if manual else "pineforge/release-delivery/v1",
                  "event_id": self.event["event_id"], "consumer": consumer, "attempt": 1,
                  "state": "dispatch_accepted", "run_id": 900, "timestamp": CLOCK}
        if manual:
            record.update(actor="release-operator", dispatch_status=204)
        if legacy:
            return record
        candidate = self.store.read(self.row, contract.CANDIDATE) if consumer == "compute" else None
        data = {"event_type": contract.EVENT_TYPES[consumer],
                "client_payload": contract.pair_payload(self.raw, candidate) if candidate else self.event}
        workflows = (".github/workflows/release-redeliver.yml",) if manual else (
            ".github/workflows/release-finalize.yml", ".github/workflows/publish.yml")
        record.update(
            schema="pineforge/release-redelivery/v3" if manual else "pineforge/release-delivery/v2",
            run_attempt=1, dispatch_status=204,
            context={"tag": TAG, "manifest_sha256": contract.sha256(self.raw),
                     "candidate_sha256": contract.sha256(candidate) if candidate else None,
                     "dispatch_sha256": contract.sha256(contract.canonical(data)),
                     "workflow_commit": COMMIT,
                     "source_sha256": {name: contract.sha256(self.hub.sources[name])
                                       for name in (*SOURCE_FILES, *workflows)}})
        return record

    def seed(self, record, *, manual=True):
        name = (f"delivery-redelivered-{record['consumer']}-{record['attempt']}.json"
                if manual else f"delivery-{record['consumer']}.json")
        self.store.put(self.row, name, contract.canonical(record))

    @contextmanager
    def transport_case(self):
        """Keep each control independent, including when a base assertion fails."""
        names = ("assets", "operations", "runs", "workflow_runs", "artifacts", "archives",
                 "artifact_runs", "variables", "sources", "reads", "run_reads")
        saved = copy.deepcopy({name: getattr(self.store, name) for name in names})
        listed = copy.deepcopy(self.row["assets"])
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(self.store, name, value)
            self.row["assets"] = listed
            self.hub.after_download = None
            self.sent.clear()
            self.output.unlink(missing_ok=True)

    def test_same_metadata_wrong_producing_content_cannot_complete_compute(self):
        forged = self.record(legacy=True)
        real = {**forged, "event_id": "sha256:" + "f" * 64}
        self.hub.publish_delivery(real)
        self.seed(forged)
        with self.assertRaises(contract.Invalid):
            pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))

    def test_three_field_automatic_acceptance_has_no_trusted_provenance(self):
        forged = {"event_id": self.event["event_id"], "consumer": "compute", "state": "dispatch_accepted"}
        self.seed(forged, manual=False)
        with self.assertRaises(contract.Invalid):
            pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        with self.assertRaises(contract.Invalid):
            pipeline.plan_delivery(self.store, self.row, self.event, "compute")
        self.assertEqual(self.invoke("compute"), 1)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.output.exists())

    def test_legacy_automatic_acceptance_needs_matching_producer_bytes(self):
        record = self.record(manual=False, legacy=True)
        self.hub.publish_delivery(record, manual=False, contents=contract.canonical({**record, "attempt": 2}))
        self.seed(record, manual=False)
        with self.assertRaises(contract.Invalid):
            pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        self.hub.artifacts.clear()
        self.hub.archives.clear()
        self.hub.publish_delivery(record, manual=False)
        result = pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        self.assertEqual(result["consumers"], {"compute": "delivered"})

    def test_revocation_after_planning_stops_both_send_paths(self):
        for manual in (False, True):
            for role in ("application", "compute"):
                with self.subTest(manual=manual, role=role), self.transport_case():
                    self.hub.variables["RELEASE_READY_CONSUMERS"] = "application,compute"
                    planned, failed = pipeline.plan_delivery(self.store, self.row, self.event, role)
                    self.assertFalse(failed)
                    self.assertIsNotNone(planned)
                    self.hub.after_download = lambda: self.hub.variables.update(RELEASE_READY_CONSUMERS="offline")
                    if manual:
                        with self.assertRaises(contract.Invalid):
                            self.invoke(role, manual=True)
                    else:
                        self.assertEqual(self.invoke(role), 1)
                    self.assertEqual(self.sent, [])
                    self.assertFalse(self.output.exists())

    def test_changed_compute_floor_and_unknown_authority_deny_at_send(self):
        denied = pipeline.artifacts.RequestError("artifact request failed (HTTP 403)")
        cases = [("compute", {"RELEASE_COMPUTE_FIRST_PAIR": floor})
                 for floor in ("1.6.1", "", "bad", None, denied)]
        cases += [(role, {"RELEASE_READY_CONSUMERS": value})
                  for role in ("application", "compute")
                  for value in (None, denied, "application,application",
                                {"name": "OTHER_VARIABLE", "value": "application,compute"},
                                {"name": "RELEASE_READY_CONSUMERS", "value": True})]
        for manual in (False, True):
            for role, changes in cases:
                with self.subTest(manual=manual, role=role, changes=changes), self.transport_case():
                    self.hub.variables = {"RELEASE_READY_CONSUMERS": "application,compute",
                                          "RELEASE_COMPUTE_FIRST_PAIR": "1.6.0"}
                    self.hub.after_download = lambda: self.hub.variables.update(changes)
                    if manual:
                        with self.assertRaises(contract.Invalid):
                            self.invoke(role, manual=True)
                    else:
                        self.assertEqual(self.invoke(role), 1)
                    self.assertEqual(self.sent, [])
                    self.assertFalse(self.output.exists())

    def test_authorized_controls_preserve_wire_bytes_and_record_exact_attempt(self):
        for manual in (False, True):
            for role in ("application", "compute"):
                with self.subTest(manual=manual, role=role), self.transport_case():
                    result = self.invoke(role, manual=manual)
                    if not manual:
                        self.assertEqual(result, 0)
                    written = contract.decode(self.output.read_bytes())
                    expected = self.record(consumer=role, manual=manual)
                    expected.update(run_id=901, run_attempt=2)
                    self.assertEqual(written, expected)
                    self.assertEqual(len(self.sent), 1)
                    self.assertEqual(contract.sha256(self.sent[0]), written["context"]["dispatch_sha256"])
                    self.assertIn(f"repos/{contract.HUB}/actions/variables/RELEASE_READY_CONSUMERS", self.hub.reads)
                    self.assertNotIn(b"fixture-target", self.output.read_bytes())

    def test_producing_attempt_survives_a_later_failed_rerun(self):
        self.check_rerun(legacy=True)

    def test_recorded_attempt_survives_a_later_failed_rerun(self):
        self.check_rerun(legacy=False)

    def check_rerun(self, *, legacy):
        for manual in (False, True):
            with self.subTest(manual=manual, legacy=legacy), self.transport_case():
                self.hub.artifacts.clear()
                self.hub.archives.clear()
                self.hub.artifact_runs.clear()
                self.hub.runs.clear()
                self.hub.workflow_runs.clear()
                record = self.record(manual=manual, legacy=legacy)
                self.hub.publish_delivery(record, manual=manual, tag=TAG)
                failed = {**self.hub.runs[900, 1], "run_attempt": 2, "conclusion": "failure"}
                self.hub.runs[900, 2] = failed
                self.hub.workflow_runs[900] = failed
                self.seed(record, manual=manual)
                try:
                    result = pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
                except contract.Invalid as error:
                    self.fail(f"successful producing attempt was invalidated by a later failed rerun: {error}")
                self.assertEqual(result["consumers"], {"compute": "delivered"})
                self.assertIn(f"repos/{contract.HUB}/actions/runs/900/attempts/1", self.hub.reads)
                self.assertNotIn(f"repos/{contract.HUB}/actions/runs/900", self.hub.reads)

    def test_replayed_attempt_or_missing_binding_cannot_complete(self):
        original = self.record()
        self.hub.publish_delivery(original)
        cases = ({**original, "run_attempt": 2},
                 {**original, "run_attempt": 0},
                 {**original, "run_attempt": True},
                 {**original, "attempt": 2},
                 *[{k: v for k, v in original.items() if k != field}
                   for field in ("run_id", "run_attempt", "context", "dispatch_status", "schema")])
        for record in cases:
            with self.subTest(record=record):
                self.store.assets = {key: raw for key, raw in self.store.assets.items()
                                     if not key[1].startswith("delivery-redelivered-")}
                self.row["assets"] = [a for a in self.row["assets"]
                                      if not a["name"].startswith("delivery-redelivered-")]
                self.seed(record)
                with self.assertRaises(contract.Invalid):
                    pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))

    def test_trusted_content_still_requires_exact_sources_and_run_context(self):
        for manual in (False, True):
            with self.subTest(manual=manual), self.transport_case():
                self.check_producing_context(manual=manual)

    def check_producing_context(self, *, manual):
        original = self.record(manual=manual)
        self.hub.publish_delivery(original, manual=manual)
        self.seed(original, manual=manual)
        self.assertEqual(pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))["consumers"],
                         {"compute": "delivered"})
        run = copy.deepcopy(self.hub.runs[900, 1])
        changes_to_run = [
            {"run_attempt": 2}, {"head_branch": "side"}, {"event": "pull_request"},
            {"repository": {"full_name": "other/repo"}},
            {"head_repository": {"full_name": "other/repo"}}, {"conclusion": "failure"},
            {"path": ".github/workflows/release-finalize.yml" if manual else
                     ".github/workflows/release-redeliver.yml"},
            {"run_started_at": "2026-10-23T00:00:31Z"}, {"updated_at": "2026-10-23T00:00:29Z"},
        ]
        if manual:
            changes_to_run += [{"actor": {"login": "other"}}, {"event": "push"}]
        else:
            for changes in ({"path": ".github/workflows/publish.yml"},
                            {"event": "schedule"}, {"event": "push"}):
                with self.subTest(allowed_run=changes):
                    self.hub.runs[900, 1] = {**run, **changes}
                    self.assertEqual(pipeline.aggregate(
                        self.store, self.row, self.event, enabled=("compute",))["consumers"],
                        {"compute": "delivered"})
        for changes in changes_to_run:
            with self.subTest(changes=changes):
                self.hub.runs[900, 1] = {**run, **changes}
                with self.assertRaises(contract.Invalid):
                    pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        self.hub.runs[900, 1] = run
        for name in original["context"]["source_sha256"]:
            with self.subTest(source=name):
                source = self.hub.sources[name]
                self.hub.sources[name] += b"\n# different producer source\n"
                try:
                    with self.assertRaises(contract.Invalid):
                        pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
                finally:
                    self.hub.sources[name] = source
        for field, changed in (("tag", "v1.6.1"), ("manifest_sha256", "b" * 64),
                               ("candidate_sha256", None), ("dispatch_sha256", "b" * 64),
                               ("workflow_commit", "b" * 40), ("source_sha256", {})):
            with self.subTest(context=field):
                record = {**original, "context": {**original["context"], field: changed}}
                self.hub.artifacts.clear()
                self.hub.archives.clear()
                self.hub.artifact_runs.clear()
                self.hub.publish_delivery(record, manual=manual, tag=TAG)
                name = "delivery-redelivered-compute-1.json" if manual else "delivery-compute.json"
                self.store.assets.pop((TAG, name))
                self.row["assets"] = [a for a in self.row["assets"] if a["name"] != name]
                self.seed(record, manual=manual)
                with self.assertRaises(contract.Invalid):
                    pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))

    def test_producing_artifact_integrity_and_uniqueness_are_required(self):
        record = self.record()
        self.hub.publish_delivery(record)
        self.seed(record)
        artifact = copy.deepcopy(self.hub.artifacts[0])
        for changes in ({"expired": True}, {"digest": "sha256:" + "b" * 64},
                        {"workflow_run": {"id": 901, "head_sha": COMMIT}},
                        {"name": artifact["name"].removesuffix("-1") + "-2"}):
            with self.subTest(changes=changes):
                self.hub.artifacts[:] = [{**artifact, **changes}]
                with self.assertRaises(contract.Invalid):
                    pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        self.hub.artifacts[:] = [artifact, copy.deepcopy(artifact)]
        with self.assertRaises(contract.Invalid):
            pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        archive = self.hub.archives[artifact["id"]]
        extra = io.BytesIO()
        with zipfile.ZipFile(extra, "w") as zipped:
            zipped.writestr("redelivery.json", contract.canonical(record))
            zipped.writestr("unrelated.json", b"{}\n")
        for raw in (b"not a ZIP", extra.getvalue()):
            with self.subTest(archive=raw):
                self.hub.archives[artifact["id"]] = raw
                self.hub.artifacts[:] = [{**artifact, "digest": "sha256:" + contract.sha256(raw)}]
                with self.assertRaises(contract.Invalid):
                    pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))
        self.hub.archives[artifact["id"]] = archive
        # The producer artifact can follow a full page of unrelated artifacts.
        unrelated = [{**artifact, "id": index, "name": f"unrelated-{index}"} for index in range(2, 102)]
        self.hub.artifact_runs.update({a["id"]: 900 for a in unrelated})
        self.hub.artifacts[:] = [*unrelated, artifact]
        self.assertEqual(pipeline.aggregate(self.store, self.row, self.event, enabled=("compute",))["consumers"],
                         {"compute": "delivered"})
        self.assertIn(f"repos/{contract.HUB}/actions/runs/900/artifacts?per_page=100&page=2", self.hub.reads)


if __name__ == "__main__":
    unittest.main()
