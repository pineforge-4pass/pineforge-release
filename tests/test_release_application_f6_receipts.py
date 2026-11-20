#!/usr/bin/env python3
"""Application protected-main reporter: signed App records bound to a distinct reporter SHA.

What this proves, and what it cannot:

* Every signed record below has the members the PINNED App coordinator writes (App blob 0c96484d for the
  coordinator, blob 260912eb for the signed state; each fixture field's source is listed in
  docs/application-receipt-reporter.md). dry_run and receipt_only are inputs of the coordinator's workflow
  dispatch and are members of no signed record. No authenticated record of them exists, so a fully valid row
  is refused LAST, for GAP. That refusal is the baseline control: every other check passed before it.
* Each negative changes exactly ONE field (or one signature byte, key or binding) of the valid row and must be
  refused by its own check, with its own exact reason. A reason that is the blanket legacy refusal or GAP
  would prove nothing, and the tables refuse both as expected reasons.
* The test signer uses labelled keys and signs the fixture bytes independently of the product verifier.
  The tests invoke the real verification path and never patch the signature result.
"""
from __future__ import annotations

import base64
import copy
import functools
import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import consumer_receipt_example as example  # noqa: E402
import release_contract as contract  # noqa: E402
import release_receipts as receipts  # noqa: E402
import test_release_coordinator_receipts as coordinator  # noqa: E402

# Nothing at module level may need code that only exists after the change: on a base checkout these tests
# must reach their assertions and fail there, not stop at import. The policy key is therefore transcribed.
PROFILE = "application_reporter"
GAP = "application reporter dispatch inputs are not authenticated for this run"
BLANKET = "served receipt source differs"

# SYNTHETIC values: reporter identity, IDs, times and the opaque `pair`.
# The retained manual-run test reads its provider inputs from the operator's record.
REPORTER_COMMIT = "8c" * 20
WORKFLOW_ID = 410001
REPORTER_WORKFLOW = "example-app-reporter.yml"
RUN_PATH = f".github/workflows/{REPORTER_WORKFLOW}"
OTHER_COMMIT = "6" * 40
RUN_ID = 44441477500
OWNER_RUN = 44441470001
SNAPSHOT_ID = 9100001
ROW_ID = 9100002
REQUEST_ID = 9000001
SEQUENCE = 7
TAG_UUID = "0f1e2d3c-4b5a-4968-8776-655443322110"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"
SNAPSHOT_SIGNED_AT = "2020-01-01T00:06:20Z"
RUN_CREATED_AT = "2020-01-01T00:06:30Z"
ROW_SIGNED_AT = "2020-01-01T00:06:31Z"
ENVIRONMENT, TASK, SCHEMA = "dev-release-evidence", "dev-release-observation", "pineforge/dev-observation/v1"
DELETE = object()


# --- Labelled Ed25519 test keys -------------------------------------------------------------------------
# Written from the curve's definition (RFC 8032 section 5.1): affine twisted-Edwards arithmetic over
# 2**255 - 19, a square root by Atkin's method, and the published key and signing steps. It only produces
# signatures, and is checked below against the RFC's own published vectors.
FIELD_PRIME = 2**255 - 19
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493


def invert(value):
    return pow(value % FIELD_PRIME, FIELD_PRIME - 2, FIELD_PRIME)


EDWARDS_D = -121665 * invert(121666) % FIELD_PRIME


def square_root(value):
    """Atkin's method: the prime is 5 modulo 8 and `value` must be a quadratic residue."""
    doubled = 2 * value % FIELD_PRIME
    power = pow(doubled, (FIELD_PRIME - 5) // 8, FIELD_PRIME)
    root_of_minus_one = doubled * power * power % FIELD_PRIME
    return value * power * (root_of_minus_one - 1) % FIELD_PRIME


def edwards_sum(first, second):
    """The complete addition law of -x*x + y*y = 1 + d*x*x*y*y, in affine coordinates."""
    x1, y1 = first
    x2, y2 = second
    cross = EDWARDS_D * x1 * x2 % FIELD_PRIME * y1 % FIELD_PRIME * y2 % FIELD_PRIME
    return ((x1 * y2 + x2 * y1) * invert(1 + cross) % FIELD_PRIME,
            (y1 * y2 + x1 * x2) * invert(1 - cross) % FIELD_PRIME)


def edwards_times(scalar, point):
    """scalar * point, taking the scalar's bits from the most significant down."""
    result = (0, 1)
    for bit in bin(scalar)[2:]:
        result = edwards_sum(result, result)
        if bit == "1":
            result = edwards_sum(result, point)
    return result


def pack_point(point):
    x, y = point
    return (y | (x % 2) << 255).to_bytes(32, "little")


GENERATOR_Y = 4 * invert(5) % FIELD_PRIME
GENERATOR_X = square_root((GENERATOR_Y * GENERATOR_Y - 1) * invert(EDWARDS_D * GENERATOR_Y * GENERATOR_Y + 1))
GENERATOR = (GENERATOR_X if GENERATOR_X % 2 == 0 else FIELD_PRIME - GENERATOR_X, GENERATOR_Y)


class LabelledKey:
    """An Ed25519 key for tests: a 32-byte seed, its clamped secret scalar and the signing steps."""

    def __init__(self, seed):
        digest = hashlib.sha512(seed).digest()
        self.scalar = (int.from_bytes(digest[:32], "little") & ~7 & ~(1 << 255)) | (1 << 254)
        self.nonce_prefix = digest[32:]
        self.public = pack_point(edwards_times(self.scalar, GENERATOR))

    def sign(self, message):
        r = int.from_bytes(hashlib.sha512(self.nonce_prefix + message).digest(), "little") % GROUP_ORDER
        commitment = pack_point(edwards_times(r, GENERATOR))
        k = int.from_bytes(hashlib.sha512(commitment + self.public + message).digest(), "little") % GROUP_ORDER
        return commitment + ((r + k * self.scalar) % GROUP_ORDER).to_bytes(32, "little")


@functools.lru_cache(maxsize=None)
def labelled_key(label):
    return LabelledKey(hashlib.sha256(b"pineforge-release labelled test key: " + label.encode("ascii")).digest())


# Ed25519 is deterministic, so a signature of the same bytes is the same signature: caching only saves time.
@functools.lru_cache(maxsize=None)
def sign(label, message):
    return labelled_key(label).sign(message)


ED25519_OID = bytes([0x06, 0x03, 0x2B, 0x65, 0x70])  # 1.3.101.112 (RFC 8410)
ALGORITHM_IDENTIFIER = bytes([0x30, len(ED25519_OID)]) + ED25519_OID


def subject_public_key_info(raw_key):
    bit_string = bytes([0x03, len(raw_key) + 1, 0x00]) + raw_key
    body = ALGORITHM_IDENTIFIER + bit_string
    return bytes([0x30, len(body)]) + body


@functools.lru_cache(maxsize=None)
def key_pem_and_pin(label):
    """The PEM of a labelled key and the SHA-256 of its DER, as a policy pins them."""
    der = subject_public_key_info(labelled_key(label).public)
    return ("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode("ascii") + "\n-----END PUBLIC KEY-----\n",
            hashlib.sha256(der).hexdigest())


def canonical_bytes(record):
    """The App's canonical form of a record of text, integers, booleans, null and objects without integer-like
    member names: keys sorted, compact, one final LF. (canonical() is imported by the pinned signed state and is
    not itself among the pinned inputs.)"""
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def envelope(record, label):
    return {"record": copy.deepcopy(record),
            "signature": base64.b64encode(sign(label, canonical_bytes(record))).decode("ascii")}


def reporter_pin(commit=REPORTER_COMMIT, actor="github-actions[bot]", workflow_id=WORKFLOW_ID):
    """The policy pin of the reporter profile; it holds only the coordinator's PUBLIC key."""
    pem, spki = key_pem_and_pin("coordinator")
    return {"commit": commit, "workflow_id": workflow_id, "reusable": "release-report.yml", "actor": actor,
            "coordinator_public_key": pem, "coordinator_spki_sha256": spki}


# --- Fixture ---------------------------------------------------------------------------------------------
class ReporterFixture(coordinator.ReceiptFixture):
    """The coordinator application fixture, a distinct protected-main reporter and the two signed records.

    The coordinator writes the snapshot and then the receipt-run row in one call, from the same values, and
    its commit is the reporter's commit (the pinned coordinator refuses a reporter at any other head); the
    fixture follows that.
    """

    def __init__(self, *, repo=None, run=None, commit=REPORTER_COMMIT, run_id=RUN_ID, owner=OWNER_RUN,
                 snapshot_id=SNAPSHOT_ID, row_id=ROW_ID, request=REQUEST_ID, actor="github-actions[bot]",
                 workflow_id=WORKFLOW_ID, workflow=REPORTER_WORKFLOW, signer="coordinator"):
        if repo is not None:
            self.repo = repo
        policy = copy.deepcopy(coordinator.APPLICATION_POLICY)
        policy["workflow"] = workflow
        workflow_path = f".github/workflows/{workflow}"
        policy[PROFILE] = reporter_pin(commit, actor, workflow_id)
        super().__init__("application", policy)
        repo = self.repo
        self.signer, self.commit, self.row_id, self.snapshot_id = signer, commit, row_id, snapshot_id
        # The consumer receipt names the coordinator run as its attempt and the snapshot's sequence as its own.
        self.receipt["attempt"], self.receipt["sequence"] = owner, SEQUENCE
        self.reporting_run = copy.deepcopy(run) if run is not None else {
            "id": run_id, "run_attempt": 1, "event": "workflow_dispatch", "workflow_id": workflow_id,
            "path": workflow_path, "head_branch": "main", "head_sha": commit, "status": "completed",
            "conclusion": "success", "created_at": RUN_CREATED_AT,
            "actor": {"login": actor}, "triggering_actor": {"login": actor},
            "repository": {"full_name": repo}, "head_repository": {"full_name": repo},
            "referenced_workflows": [{"path": f"{repo}/.github/workflows/release-report.yml@{commit}",
                                      "ref": "refs/heads/main", "sha": commit}],
        }
        self.main_head = commit
        self.owner = {"id": owner, "run_attempt": 1, "head_sha": commit, "repository": {"full_name": repo}}
        self.tag = f"dev-release/{owner}/receipt-{SEQUENCE}-{TAG_UUID}"
        pair = {"pr": self.pr["number"], "head": self.pr["head"]}
        self.snapshot = {
            "schema": SCHEMA, "kind": "snapshot", "owner": owner, "owner_sha": commit, "ref": self.tag,
            "source": self.pr["merge"], "request": request, "payload": copy.deepcopy(self.event),
            "sequence": SEQUENCE, "observation": {"state": "verified", "pr": copy.deepcopy(self.pr)},
            "before": None, "after": None, "previousGood": None, "deployedGood": None, "landed": None,
            "postMerge": None, "pair": copy.deepcopy(pair), "deploy_ref": None, "landed_ref": None,
            "owner_attempt": 1, "created_at": SNAPSHOT_SIGNED_AT,
        }
        self.row = {
            "schema": SCHEMA, "kind": "receipt-run", "owner": owner, "owner_sha": commit,
            "snapshot_id": snapshot_id, "request": request, "repository": repo, "workflow": workflow_path,
            "dispatch_ref": "main", "workflow_run_id": run_id, "workflow_sha": commit, "ref": self.tag,
            "source": self.pr["merge"], "pair": copy.deepcopy(pair), "sequence": SEQUENCE, "attempt": 1,
            "owner_attempt": 1, "created_at": ROW_SIGNED_AT,
        }
        self.extra, self.pending = [], set()
        self.seal()

    def deployment(self, identifier, record, label=None, **override):
        # The deployment's sha is the owner run's head (publishRecord: ref is run.head_sha), whatever a record
        # claims, so a record's own owner_sha can change on its own.
        item = {"id": identifier, "sha": self.commit, "ref": self.commit, "task": TASK,
                "environment": ENVIRONMENT, "payload": envelope(record, label or self.signer)}
        item.update(override)
        return item

    def seal(self, row_label=None, snapshot_label=None):
        """Sign both records and publish them, and their coordinator run, through the provider double."""
        self.snapshot_deployment = self.deployment(self.snapshot_id, self.snapshot, snapshot_label)
        self.row_deployment = self.deployment(self.row_id, self.row, row_label)
        self.publish()

    def publish(self):
        repo = self.repo
        listing = [self.row_deployment, self.snapshot_deployment, *self.extra]
        self.listing_route = (f"repos/{repo}/deployments?environment={ENVIRONMENT}&task={TASK}"
                              "&per_page=100&page=1")
        self.responses[self.listing_route] = listing
        for item in listing:
            self.responses[f"repos/{repo}/deployments/{item['id']}"] = item
        self.branch_route = f"repos/{repo}/branches/main"
        self.responses[self.branch_route] = {"name": "main", "protected": True,
                                             "commit": {"sha": self.main_head}}
        self.compare_route = f"repos/{repo}/compare/{self.commit}...{self.main_head}"
        self.responses[self.compare_route] = {"status": "identical" if self.main_head == self.commit else "ahead"}
        self.owner_route = f"repos/{repo}/actions/runs/{self.owner['id']}"
        self.responses[self.owner_route] = self.owner

    def api(self, route):
        self.queries.append(route)
        if route in self.pending:
            raise contract.Pending("fixture 404")
        if route in self.responses:
            return copy.deepcopy(self.responses[route])
        if re.fullmatch(rf"repos/{re.escape(self.repo)}/(?:deployments|actions/runs)/[0-9]+", route):
            raise contract.Pending("fixture 404")
        raise AssertionError("unexpected fixture API route: " + route)

    def target(self, name):
        """The fixture object a change is aimed at."""
        return {
            "run": self.reporting_run, "owner": self.owner, "row": self.row, "snapshot": self.snapshot,
            "receipt": self.receipt, "event": self.event, "pin": self.policy[PROFILE],
            "branch": self.responses[self.branch_route], "compare": self.responses[self.compare_route],
            "row_deployment": self.row_deployment, "snapshot_deployment": self.snapshot_deployment,
            "row_envelope": self.row_deployment["payload"],
            "snapshot_envelope": self.snapshot_deployment["payload"],
            "row_signed_copy": self.row_deployment["payload"]["record"],
            "snapshot_signed_copy": self.snapshot_deployment["payload"]["record"],
        }[name]


class Set:
    """One field of one fixture object changed. DELETE removes the member; a callable maps the old value.

    Changing a field of a signed record signs the changed record again with the fixture's key, because the
    coordinator would have; the *_signed_copy and *_envelope targets change the stored envelope instead,
    which is how a record altered after signing, or a signature byte, is expressed.
    """

    def __init__(self, target, path, value, note=None):
        self.target, self.path, self.value, self.note = target, path, value, note

    @property
    def name(self):
        """`target.path -> new value`: unique within a table, because no table repeats a change."""
        shown = "removed" if self.value is DELETE else (self.note or repr(self.value))
        return self.target + "." + ".".join(str(part) for part in self.path) + " -> " + shown

    def __call__(self, f):
        node = f.target(self.target)
        for part in self.path[:-1]:
            node = node[part]
        last = self.path[-1]
        if self.value is DELETE:
            del node[last]
        else:
            node[last] = self.value(node[last]) if callable(self.value) else self.value
        if self.target in ("row", "snapshot"):
            f.seal()


class Op:
    """One binding, provider or key change that is not a single field."""

    def __init__(self, name, operation):
        self.name, self.operation = name, operation

    def __call__(self, f):
        self.operation(f)


def flip_one_byte(text):
    raw = bytearray(base64.b64decode(text))
    raw[10] ^= 1
    return base64.b64encode(bytes(raw)).decode("ascii")


def another_authentic_row(**changes):
    """A second row the coordinator signed for this run, differing from the first in the given field."""
    def operation(f):
        other = copy.deepcopy(f.row)
        other.update(changes)
        f.extra.append(f.deployment(ROW_ID + 10, other))
        f.publish()
    return operation


def row_of_another_run_for_this_snapshot(f):
    other = copy.deepcopy(f.row)
    other["workflow_run_id"] = RUN_ID + 1
    f.extra.append(f.deployment(ROW_ID + 10, other))
    f.publish()


def row_for_this_run_signed_by(label):
    def operation(f):
        f.seal(row_label=label)
    return operation


def snapshot_signed_by(label):
    def operation(f):
        f.seal(snapshot_label=label)
    return operation


OTHER_TAG = f"dev-release/{OWNER_RUN}/receipt-{SEQUENCE}-{OTHER_UUID}"
ATTACKER_PEM, ATTACKER_PIN = key_pem_and_pin("attacker")
UNPINNED_PEM = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


# --- Tables: field changed -> exact reason ---------------------------------------------------------------
# Policy pin: refused when the profile is loaded or first used, before any provider read.
POLICY_ROWS = (
    (Set("pin", ("coordinator_public_key",), ATTACKER_PEM),
     "application reporter coordinator public key is not the pinned key"),
    (Set("pin", ("coordinator_spki_sha256",), ATTACKER_PIN),
     "application reporter coordinator public key is not the pinned key"),
    (Set("pin", ("coordinator_public_key",), UNPINNED_PEM), "application reporter coordinator public key is malformed"),
    (Set("pin", ("coordinator_public_key",), None), "application reporter coordinator public key is malformed"),
    (Set("pin", ("coordinator_spki_sha256",), lambda old: old.upper(), "uppercased"),
     "application reporter coordinator key pin is malformed"),
    (Set("pin", ("coordinator_spki_sha256",), 7), "application reporter coordinator key pin is malformed"),
    (Set("pin", ("commit",), "4a94290"), "application reporter policy member commit is malformed"),
    (Set("pin", ("workflow_id",), 0), "application reporter policy member workflow_id is malformed"),
    (Set("pin", ("workflow_id",), str(WORKFLOW_ID)), "application reporter policy member workflow_id is malformed"),
    (Set("pin", ("workflow_id",), True), "application reporter policy member workflow_id is malformed"),
    (Set("pin", ("workflow_id",), 2**53), "application reporter policy member workflow_id is malformed"),
    (Set("pin", ("reusable",), "dir/release-report.yml"), "application reporter policy member reusable is malformed"),
    (Set("pin", ("reusable",), "release-report.yaml"), "application reporter policy member reusable is malformed"),
    (Set("pin", ("reusable",), REPORTER_WORKFLOW),
     "application reporter reusable workflow must differ from the reporting workflow"),
    (Set("pin", ("actor",), ""), "application reporter policy member actor is malformed"),
    (Set("pin", ("actor",), "github actions"), "application reporter policy member actor is malformed"),
    (Set("pin", ("actor",), None), "application reporter policy member actor is malformed"),
    (Set("pin", ("extra",), 1), "application reporter policy fields differ from the schema"),
    (Set("pin", ("commit",), DELETE), "application reporter policy fields differ from the schema"),
)

# Policy pin values the run is checked against: a pin that changes must change what is refused.
PIN_USED_ROWS = (
    (Set("pin", ("commit",), OTHER_COMMIT), "application reporter head differs from the pinned commit"),
    (Set("pin", ("workflow_id",), WORKFLOW_ID + 1), "application reporter workflow differs from policy"),
    (Set("pin", ("actor",), "pineforge-app[bot]"), "application reporter actor differs from policy"),
    (Set("pin", ("reusable",), "other-report.yml"), "application reporter reusable workflow path differs from the pin"),
)

# Provider run: checked before any other provider read.
RUN_ROWS = (
    (Set("run", ("event",), "push"), "application reporter requires workflow_dispatch"),
    (Set("run", ("event",), "repository_dispatch"), "application reporter requires workflow_dispatch"),
    (Set("run", ("event",), "workflow_run"), "application reporter requires workflow_dispatch"),
    (Set("run", ("event",), "pull_request"), "receipt must come from the trusted consumer coordinator"),
    (Set("run", ("event",), "schedule"), "receipt must come from the trusted consumer coordinator"),
    (Set("run", ("workflow_id",), WORKFLOW_ID + 1), "application reporter workflow differs from policy"),
    (Set("run", ("workflow_id",), str(WORKFLOW_ID)), "application reporter workflow differs from policy"),
    (Set("run", ("workflow_id",), True), "application reporter workflow differs from policy"),
    (Set("run", ("workflow_id",), DELETE), "application reporter run lacks a required member"),
    (Set("run", ("path",), ".github/workflows/other.yml"), "untrusted receipt workflow"),
    (Set("run", ("path",), RUN_PATH + "@refs/heads/main"), "untrusted receipt workflow"),
    (Set("run", ("run_attempt",), 2), "application reporter attempt differs"),
    (Set("run", ("run_attempt",), "1"), "application reporter attempt differs"),
    (Set("run", ("head_branch",), "dev"), "application reporter branch differs from main"),
    (Set("run", ("head_branch",), "feature"), "application reporter branch differs from main"),
    (Set("run", ("head_sha",), OTHER_COMMIT), "application reporter head differs from the pinned commit"),
    (Set("run", ("head_sha",), "2" * 40), "application reporter head differs from the pinned commit"),
    (Set("run", ("actor", "login"), "a-human-login"), "application reporter actor differs from policy"),
    (Set("run", ("actor", "login"), "pineforge-app[bot]"), "application reporter actor differs from policy"),
    (Set("run", ("triggering_actor", "login"), "octocat"), "application reporter actor differs from policy"),
    (Set("run", ("actor",), None), "application reporter actor differs from policy"),
    (Set("run", ("triggering_actor",), {}), "application reporter actor differs from policy"),
    (Set("run", ("repository", "full_name"), "pineforge-4pass/other"), "application reporter repository differs"),
    (Set("run", ("head_repository", "full_name"), "someone/fixture"), "application reporter repository differs"),
    (Set("run", ("created_at",), "2020-01-01T00:05:00Z"), "application reporter was not created after the PR merge"),
    (Set("run", ("created_at",), "2020-01-01T00:04:59Z"), "application reporter was not created after the PR merge"),
    (Set("run", ("created_at",), "2020-01-01 00:06:30"), "application reporter timestamp is malformed"),
    (Set("run", ("referenced_workflows",), DELETE), "application reporter run lacks a required member"),
    (Set("run", ("referenced_workflows",), None), "application reporter must reference exactly one reusable workflow"),
    (Set("run", ("referenced_workflows",), []), "application reporter must reference exactly one reusable workflow"),
    (Op("run.referenced_workflows gains a second entry",
        lambda f: f.reporting_run["referenced_workflows"].append(dict(f.reporting_run["referenced_workflows"][0]))),
     "application reporter must reference exactly one reusable workflow"),
    (Set("run", ("referenced_workflows",), ["release-report.yml"]), "application reporter reusable reference members differ"),
    (Set("run", ("referenced_workflows", 0, "extra"), "x"), "application reporter reusable reference members differ"),
    (Set("run", ("referenced_workflows", 0, "ref"), DELETE), "application reporter reusable reference members differ"),
    (Set("run", ("referenced_workflows", 0, "path"),
         "pineforge-4pass/fixture/.github/workflows/release-report.yml@main"),
     "application reporter reusable workflow path differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "path"),
         "pineforge-4pass/fixture/.github/workflows/release-report.yml@refs/heads/main"),
     "application reporter reusable workflow path differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "path"),
         f"pineforge-4pass/foreign/.github/workflows/release-report.yml@{REPORTER_COMMIT}"),
     "application reporter reusable workflow path differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "path"),
         f"pineforge-4pass/fixture/.github/workflows/other.yml@{REPORTER_COMMIT}"),
     "application reporter reusable workflow path differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "path"),
         f"pineforge-4pass/fixture/.github/workflows/release-report.yml@{OTHER_COMMIT}"),
     "application reporter reusable workflow path differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "sha"), OTHER_COMMIT),
     "application reporter reusable workflow commit differs from the pin"),
    (Set("run", ("referenced_workflows", 0, "ref"), "refs/tags/v1"),
     "application reporter reusable workflow ref is not refs/heads/main"),
    (Set("run", ("referenced_workflows", 0, "ref"), "refs/heads/dev"),
     "application reporter reusable workflow ref is not refs/heads/main"),
    (Set("run", ("referenced_workflows", 0, "ref"), "refs/pull/9/merge"),
     "application reporter reusable workflow ref is not refs/heads/main"),
    (Set("run", ("referenced_workflows", 0, "ref"), "main"),
     "application reporter reusable workflow ref is not refs/heads/main"),
)

# Protected main: the pinned commit is on the protected branch.
BRANCH_ROWS = (
    (Set("branch", ("protected",), False), "application reporter branch is not protected main"),
    (Set("branch", ("protected",), DELETE), "application reporter branch is not protected main"),
    (Set("branch", ("name",), "dev"), "application reporter branch is not protected main"),
    (Op("branches/main answers 404", lambda f: f.pending.add(f.branch_route)),
     "application reporter branch is not protected main"),
    (Set("compare", ("status",), "behind"), "application reporter commit is not on protected main"),
    (Set("compare", ("status",), "diverged"), "application reporter commit is not on protected main"),
    (Op("compare answers 404", lambda f: f.pending.add(f.compare_route)),
     "application reporter commit is not on protected main"),
)

# Finding and authenticating the receipt-run row: by the run it names, signed by the pinned key as a receipt-run.
LOOKUP_ROWS = (
    (Set("row", ("workflow_run_id",), RUN_ID + 1), "missing signed application receipt-run binding"),
    (Set("row_deployment", ("environment",), "dev-release-queue"), "missing signed application receipt-run binding"),
    (Set("row_deployment", ("task",), "dev-release-request"), "missing signed application receipt-run binding"),
    (Set("row_deployment", ("payload",), "not json"), "missing signed application receipt-run binding"),
    (Op("the deployment store lists nothing", lambda f: f.responses[f.listing_route].clear()),
     "missing signed application receipt-run binding"),
    (Set("row_envelope", ("signature",), DELETE), "application receipt-run record is not signed"),
    (Set("row_envelope", ("signature",), ""), "application receipt-run record is not signed"),
    (Set("row_envelope", ("signature",), None), "application receipt-run record is not signed"),
    (Set("row_envelope", ("signature",), 7), "application receipt-run signature is malformed"),
    (Set("row_envelope", ("signature",), "***"), "application receipt-run signature is malformed"),
    (Set("row_envelope", ("signature",), lambda old: old[:40], "truncated"),
     "application receipt-run signature is malformed"),
    (Set("row_envelope", ("signature",), flip_one_byte, "one byte flipped"),
     "application receipt-run signature does not verify against the pinned key"),
    (Set("row_signed_copy", ("sequence",), lambda old: old + 1, "+1 after signing"),
     "application receipt-run signature does not verify against the pinned key"),
    (Op("the row is signed by another key", row_for_this_run_signed_by("attacker")),
     "application receipt-run signature does not verify against the pinned key"),
    (Set("row_envelope", ("public_key",), ATTACKER_PEM), "application receipt-run envelope is malformed"),
    (Set("row", ("kind",), "snapshot"), "application receipt-run record is not a receipt-run binder"),
    (Set("row", ("kind",), "f6-key-correspondence-proof"), "application receipt-run record is not a receipt-run binder"),
    (Set("row", ("kind",), "lease"), "application receipt-run record is not a receipt-run binder"),
    (Op("a second authentic row for this run differs in request", another_authentic_row(request=REQUEST_ID + 1)),
     "application receipt run is bound more than once"),
    (Op("a second authentic row for this run names another snapshot",
        another_authentic_row(snapshot_id=SNAPSHOT_ID + 1)),
     "application receipt run is bound more than once"),
    (Op("an authentic row of another run names this snapshot", row_of_another_run_for_this_snapshot),
     "application snapshot is bound to another receipt run"),
)

# The row's own members: exactly the pinned coordinator's, each of its form.
ROW_SHAPE_ROWS = (
    (Set("row", ("schema",), "pineforge/dev-observation/v2"), "application receipt-run record schema differs"),
    (Set("row", ("pair",), DELETE), "application receipt-run record fields differ from the schema"),
    (Set("row", ("extra",), 1), "application receipt-run record fields differ from the schema"),
    (Set("row", ("dry_run",), "false"), "application receipt-run record fields differ from the schema"),
    (Set("row", ("receipt_only",), "true"), "application receipt-run record fields differ from the schema"),
    (Set("row", ("owner",), "x"), "application receipt-run member owner is malformed"),
    (Set("row", ("owner_sha",), "abc"), "application receipt-run member owner_sha is malformed"),
    (Set("row", ("snapshot_id",), 0), "application receipt-run member snapshot_id is malformed"),
    (Set("row", ("request",), "7"), "application receipt-run member request is malformed"),
    (Set("row", ("repository",), ""), "application receipt-run member repository is malformed"),
    (Set("row", ("workflow",), 5), "application receipt-run member workflow is malformed"),
    (Set("row", ("dispatch_ref",), ""), "application receipt-run member dispatch_ref is malformed"),
    (Set("row", ("workflow_sha",), "abc"), "application receipt-run member workflow_sha is malformed"),
    (Set("row", ("ref",), ""), "application receipt-run member ref is malformed"),
    (Set("row", ("source",), "main"), "application receipt-run member source is malformed"),
    (Set("row", ("sequence",), "7"), "application receipt-run member sequence is malformed"),
    (Set("row", ("attempt",), "1"), "application receipt-run member attempt is malformed"),
    (Set("row", ("owner_attempt",), True), "application receipt-run member owner_attempt is malformed"),
    (Set("row", ("created_at",), "2020-01-01"), "application receipt-run member created_at is malformed"),
    (Set("row", ("created_at",), "2020-01-01T00:06:31.000Z"), "application receipt-run member created_at is malformed"),
)

# The row against the provider run.
ROW_RUN_ROWS = (
    (Set("row", ("repository",), "pineforge-4pass/other"), "application receipt-run repository differs from the provider run"),
    (Set("row", ("workflow",), ".github/workflows/other.yml"), "application receipt-run workflow differs from the provider run"),
    (Set("row", ("dispatch_ref",), "dev"), "application receipt-run dispatch ref differs from the provider run"),
    (Set("row", ("workflow_sha",), OTHER_COMMIT), "application receipt-run workflow SHA differs from the provider run"),
    (Set("row", ("attempt",), 2), "application receipt-run attempt differs from the provider run"),
)

# Reading and authenticating the snapshot the row names.
SNAPSHOT_FETCH_ROWS = (
    (Set("row", ("snapshot_id",), SNAPSHOT_ID + 7), "application snapshot deployment is missing"),
    (Set("row", ("snapshot_id",), ROW_ID), "application snapshot record is not a snapshot binder"),
    (Set("snapshot_deployment", ("id",), SNAPSHOT_ID + 1), "application snapshot deployment differs from the receipt-run"),
    (Set("snapshot_deployment", ("environment",), "dev-release-queue"),
     "application snapshot deployment is not a coordinator observation"),
    (Set("snapshot_deployment", ("task",), "dev-release-request"),
     "application snapshot deployment is not a coordinator observation"),
    (Set("snapshot_deployment", ("payload",), "not json"), "application snapshot deployment is not a coordinator observation"),
    (Set("snapshot_envelope", ("signature",), DELETE), "application snapshot record is not signed"),
    (Set("snapshot_envelope", ("signature",), ""), "application snapshot record is not signed"),
    (Set("snapshot_envelope", ("signature",), 7), "application snapshot signature is malformed"),
    (Set("snapshot_envelope", ("signature",), "***"), "application snapshot signature is malformed"),
    (Set("snapshot_envelope", ("signature",), lambda old: old[:40], "truncated"),
     "application snapshot signature is malformed"),
    (Set("snapshot_envelope", ("signature",), flip_one_byte, "one byte flipped"),
     "application snapshot signature does not verify against the pinned key"),
    (Set("snapshot_signed_copy", ("sequence",), lambda old: old + 1, "+1 after signing"),
     "application snapshot signature does not verify against the pinned key"),
    (Op("the snapshot is signed by another key", snapshot_signed_by("attacker")),
     "application snapshot signature does not verify against the pinned key"),
    (Set("snapshot_envelope", ("public_key",), ATTACKER_PEM), "application snapshot envelope is malformed"),
    (Set("snapshot", ("kind",), "receipt-run"), "application snapshot record is not a snapshot binder"),
    (Set("snapshot", ("kind",), "f6-key-correspondence-proof"), "application snapshot record is not a snapshot binder"),
)

# The snapshot's own members: those this binding reads, and nothing the coordinator does not write.
SNAPSHOT_SHAPE_ROWS = (
    (Set("snapshot", ("schema",), "pineforge/dev-observation/v0"), "application snapshot record schema differs"),
    (Set("snapshot", ("pair",), DELETE), "application snapshot record fields differ from the schema"),
    (Set("snapshot", ("extra",), 1), "application snapshot record fields differ from the schema"),
    (Set("snapshot", ("owner",), "x"), "application snapshot member owner is malformed"),
    (Set("snapshot", ("owner_sha",), "abc"), "application snapshot member owner_sha is malformed"),
    (Set("snapshot", ("ref",), ""), "application snapshot member ref is malformed"),
    (Set("snapshot", ("source",), "main"), "application snapshot member source is malformed"),
    (Set("snapshot", ("request",), 0), "application snapshot member request is malformed"),
    (Set("snapshot", ("sequence",), "7"), "application snapshot member sequence is malformed"),
    (Set("snapshot", ("owner_attempt",), True), "application snapshot member owner_attempt is malformed"),
    (Set("snapshot", ("created_at",), "yesterday"), "application snapshot member created_at is malformed"),
    (Set("snapshot", ("payload",), None), "application snapshot member payload is malformed"),
    (Set("snapshot", ("payload", "manifest", "sha256"), "x"), "application snapshot member payload is malformed"),
    (Set("snapshot", ("observation", "pr"), None), "application snapshot member observation is malformed"),
    (Set("snapshot", ("owner",), OWNER_RUN + 1), "application snapshot ref is not the receipt tag of its owner and sequence"),
    (Set("snapshot", ("sequence",), SEQUENCE + 1), "application snapshot ref is not the receipt tag of its owner and sequence"),
    (Set("snapshot", ("ref",), "refs/heads/main"), "application snapshot ref is not the receipt tag of its owner and sequence"),
)

# The snapshot against this release, the served source, and the row and the receipt against the snapshot.
RELEASE_ROWS = (
    (Set("snapshot", ("payload", "release_version"), "9.9.9"), "application snapshot release differs from the receipt"),
    (Set("snapshot", ("payload", "manifest", "sha256"), "e" * 64), "application snapshot manifest differs from the release"),
    (Set("event", ("manifest", "sha256"), "e" * 64), "application snapshot manifest differs from the release"),
    (Set("snapshot", ("observation", "pr", "number"), 10), "application snapshot PR differs from the receipt"),
    (Set("snapshot", ("observation", "pr", "merge"), OTHER_COMMIT), "application snapshot merge differs from the receipt"),
    (Set("snapshot", ("source",), REPORTER_COMMIT), "application snapshot source differs from the merge commit"),
    (Set("row", ("owner",), OWNER_RUN + 1), "application receipt-run owner differs from the signed snapshot"),
    (Set("row", ("owner_sha",), OTHER_COMMIT), "application receipt-run owner_sha differs from the signed snapshot"),
    (Set("snapshot", ("owner_sha",), OTHER_COMMIT), "application receipt-run owner_sha differs from the signed snapshot"),
    (Set("row", ("owner_attempt",), 2), "application receipt-run owner_attempt differs from the signed snapshot"),
    (Set("snapshot", ("owner_attempt",), 2), "application receipt-run owner_attempt differs from the signed snapshot"),
    (Set("row", ("request",), REQUEST_ID + 1), "application receipt-run request differs from the signed snapshot"),
    (Set("snapshot", ("request",), REQUEST_ID + 1), "application receipt-run request differs from the signed snapshot"),
    (Set("row", ("source",), REPORTER_COMMIT), "application receipt-run source differs from the signed snapshot"),
    (Set("row", ("sequence",), SEQUENCE + 1), "application receipt-run sequence differs from the signed snapshot"),
    (Set("row", ("ref",), OTHER_TAG), "application receipt-run ref differs from the signed snapshot"),
    (Set("snapshot", ("ref",), OTHER_TAG), "application receipt-run ref differs from the signed snapshot"),
    (Set("row", ("pair",), None), "application receipt-run pair differs from the signed snapshot"),
    (Set("snapshot", ("pair",), {"pr": 10}), "application receipt-run pair differs from the signed snapshot"),
    (Set("receipt", ("attempt",), OWNER_RUN + 1), "application receipt attempt differs from the signed snapshot owner"),
    (Set("receipt", ("sequence",), SEQUENCE + 1), "application receipt sequence differs from the signed snapshot"),
)

# The coordinator run both records name.
COORDINATOR_ROWS = (
    (Set("row_deployment", ("sha",), OTHER_COMMIT), "application deployment SHA differs from the coordinator owner SHA"),
    (Set("snapshot_deployment", ("sha",), OTHER_COMMIT), "application deployment SHA differs from the coordinator owner SHA"),
    (Set("owner", ("head_sha",), OTHER_COMMIT), "application coordinator owner run differs from the signed owner"),
    (Set("owner", ("id",), OWNER_RUN + 1), "application coordinator owner run differs from the signed owner"),
    (Set("owner", ("repository", "full_name"), "pineforge-4pass/other"),
     "application coordinator owner run differs from the signed owner"),
    (Op("the coordinator run answers 404", lambda f: f.pending.add(f.owner_route)),
     "application coordinator owner run is missing"),
    (Set("owner", ("run_attempt",), 2), "application coordinator owner attempt differs from the receipt-run"),
)

# The checks that follow the reporter's, and are reached only because the dispatch-input refusal is last.
LATER_ROWS = (
    (Set("receipt", ("deployed", "source"), REPORTER_COMMIT), "actual served source/image differs"),
    (Set("receipt", ("deployed", "image_digest"), "sha256:" + "9" * 64), "actual served source/image differs"),
    (Set("receipt", ("deploy", "sha"), OTHER_COMMIT), "checks ran against a different source"),
)

TABLES = (("policy pin", POLICY_ROWS), ("pin values", PIN_USED_ROWS), ("provider run", RUN_ROWS),
          ("protected main", BRANCH_ROWS), ("row lookup and signature", LOOKUP_ROWS),
          ("row members", ROW_SHAPE_ROWS), ("row against run", ROW_RUN_ROWS),
          ("snapshot fetch and signature", SNAPSHOT_FETCH_ROWS), ("snapshot members", SNAPSHOT_SHAPE_ROWS),
          ("release and cross bindings", RELEASE_ROWS), ("coordinator", COORDINATOR_ROWS),
          ("later checks", LATER_ROWS))


class SignerTests(unittest.TestCase):
    def test_signer_reproduces_the_published_rfc_8032_vectors(self):
        for seed, public, message, signature in (
                ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
                 "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
                 "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
                 "d25bf5f0595bbe24655141438e7a100b"),
                ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
                 "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
                 "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
                 "387b2eaeb4302aeeb00d291612bb0c00")):
            key = LabelledKey(bytes.fromhex(seed))
            with self.subTest(public=public[:8]):
                self.assertEqual(key.public.hex(), public)
                self.assertEqual(key.sign(bytes.fromhex(message)).hex(), signature)

    def test_the_generator_lies_on_the_curve_and_has_the_published_y(self):
        x, y = GENERATOR
        self.assertEqual((-x * x + y * y - 1 - EDWARDS_D * x * x * y * y) % FIELD_PRIME, 0)
        self.assertEqual(y * 5 % FIELD_PRIME, 4)
        self.assertEqual(x % 2, 0)

    def test_the_product_reads_a_labelled_key_as_a_pinned_key(self):
        pem, pin = key_pem_and_pin("coordinator")
        self.assertEqual(hashlib.sha256(receipts.pinned_key_der(pem)).hexdigest(), pin)
        self.assertNotEqual(key_pem_and_pin("attacker")[1], pin)


class BaselineTests(unittest.TestCase):
    def only_the_gap(self, f):
        with self.assertRaises(contract.Invalid) as caught:
            f.verify()
        self.assertEqual(str(caught.exception), GAP)

    def test_a_valid_row_passes_every_check_and_is_refused_only_for_the_dispatch_inputs(self):
        f = ReporterFixture()
        self.assertNotEqual(f.reporting_run["head_sha"], f.pr["merge"])
        self.assertEqual(f.row["source"], f.pr["merge"])
        self.only_the_gap(f)
        queried = set(f.queries)
        # Everything before the refusal was reached: the store, the snapshot by its ID, the coordinator run,
        # protected main, and the checks that follow the reporter's (the application post-merge run).
        for route in (f.listing_route, f"repos/{f.repo}/deployments/{f.row['snapshot_id']}", f.owner_route,
                      f.branch_route, f.compare_route, f"repos/{f.repo}/actions/runs/74/attempts/5/jobs?per_page=100&page=1"):
            self.assertIn(route, queried)

    def test_the_refusal_is_the_documented_gap_and_never_the_blanket_one(self):
        self.assertNotEqual(GAP, BLANKET)
        self.assertNotIn(BLANKET, [reason for _, rows in TABLES for _, reason in rows])
        self.assertNotIn(GAP, [reason for _, rows in TABLES for _, reason in rows])

    def test_a_run_member_called_inputs_is_never_read(self):
        for inputs in ({"dry_run": "false", "receipt_only": "true"}, {"dry_run": "true", "receipt_only": "false"}, {}):
            f = ReporterFixture()
            f.reporting_run["inputs"] = inputs
            with self.subTest(inputs=inputs):
                self.only_the_gap(f)

    def test_variants_that_are_not_negatives_also_reach_only_the_gap(self):
        def text_payloads(f):
            f.row_deployment["payload"] = json.dumps(f.row_deployment["payload"])
            f.snapshot_deployment["payload"] = json.dumps(f.snapshot_deployment["payload"])

        def exact_replay(f):
            f.extra.append(f.deployment(ROW_ID + 10, copy.deepcopy(f.row)))
            f.publish()

        def noise(f):
            forged = copy.deepcopy(f.row)
            forged["request"] = 5
            f.extra.append(f.deployment(ROW_ID + 11, forged, "attacker"))
            f.extra.append(f.deployment(ROW_ID + 12, forged, "attacker", environment="dev-release-queue"))
            f.extra.append({"id": ROW_ID + 13, "task": TASK, "environment": ENVIRONMENT, "sha": f.commit,
                            "payload": {"record": forged}})
            f.extra.append({"id": ROW_ID + 14, "task": TASK, "environment": ENVIRONMENT, "sha": f.commit,
                            "payload": "not json"})
            other_run = dict(forged, workflow_run_id=RUN_ID + 5)
            f.extra.append(f.deployment(ROW_ID + 15, other_run, "attacker"))
            f.publish()

        def main_advanced(f):
            f.main_head = OTHER_COMMIT
            f.publish()

        def without_optional_members(f):
            for name in receipts.SNAPSHOT_OPTIONAL:
                del f.snapshot[name]
            f.seal()

        def optional_members_present(f):
            for name in receipts.SNAPSHOT_OPTIONAL:
                f.snapshot[name] = {"kept": "by the coordinator"}
            f.seal()

        def null_pair(f):
            f.row["pair"] = f.snapshot["pair"] = None
            f.seal()

        for name, change in (("payloads as JSON text", text_payloads), ("exact replay of the row", exact_replay),
                             ("unauthentic claims", noise), ("main advanced", main_advanced),
                             ("snapshot without its optional members", without_optional_members),
                             ("snapshot with its optional members", optional_members_present),
                             ("a null pair in both records", null_pair)):
            f = ReporterFixture()
            change(f)
            with self.subTest(case=name):
                self.only_the_gap(f)

    def test_the_actor_migration_is_a_policy_change_only(self):
        f = ReporterFixture(actor="pineforge-app[bot]")
        self.only_the_gap(f)
        for event in ("repository_dispatch", "push", "workflow_run"):
            g = ReporterFixture(actor="pineforge-app[bot]")
            g.reporting_run["event"] = event
            with self.subTest(event=event):
                with self.assertRaises(contract.Invalid) as caught:
                    g.verify()
                self.assertEqual(str(caught.exception), "application reporter requires workflow_dispatch")

    def test_every_supplied_id_is_what_the_validator_queries(self):
        other = dict(run_id=RUN_ID + 17, owner=OWNER_RUN + 23, snapshot_id=SNAPSHOT_ID + 31, row_id=ROW_ID + 37,
                     request=REQUEST_ID + 41, commit="7" * 40)
        seen = []
        for ids in ({}, other):
            f = ReporterFixture(**ids)
            self.only_the_gap(f)
            queried = set(f.queries)
            for route in (f.listing_route, f"repos/{f.repo}/deployments/{f.row['snapshot_id']}",
                          f"repos/{f.repo}/actions/runs/{f.row['owner']}", f.branch_route, f.compare_route):
                self.assertIn(route, queried)
            seen.append(queried)
        defaults, moved = seen
        self.assertFalse({route for route in defaults if str(SNAPSHOT_ID) in route or str(OWNER_RUN) in route}
                         & moved)


class NonVerifiedStatesTests(unittest.TestCase):
    def test_only_the_verified_state_is_affected_by_the_profile(self):
        for state in ("ready", "merged", "deploying"):
            f = ReporterFixture()
            f.receipt["state"] = state
            with self.subTest(state=state):
                self.assertEqual(f.verify()["state"], state)
                self.assertFalse([route for route in f.queries
                                  if "deployments" in route or "/branches/" in route or "/compare/" in route])


class OneFieldRefusalTests(unittest.TestCase):
    def refused(self, rows):
        names = [change.name for change, _ in rows]
        self.assertEqual(len(names), len(set(names)), "a repeated name would hide a case")
        for change, reason in rows:
            with self.subTest(changed=change.name):
                f = ReporterFixture()
                change(f)
                with self.assertRaises(contract.Invalid) as caught:
                    f.verify()
                self.assertEqual(str(caught.exception), reason)

    def test_policy_pin_fields(self):
        self.refused(POLICY_ROWS)

    def test_pin_values_are_what_the_run_is_checked_against(self):
        self.refused(PIN_USED_ROWS)

    def test_provider_run_fields(self):
        self.refused(RUN_ROWS)

    def test_protected_main(self):
        self.refused(BRANCH_ROWS)

    def test_row_lookup_and_signature(self):
        self.refused(LOOKUP_ROWS)

    def test_row_members(self):
        self.refused(ROW_SHAPE_ROWS)

    def test_row_against_the_provider_run(self):
        self.refused(ROW_RUN_ROWS)

    def test_snapshot_fetch_and_signature(self):
        self.refused(SNAPSHOT_FETCH_ROWS)

    def test_snapshot_members(self):
        self.refused(SNAPSHOT_SHAPE_ROWS)

    def test_release_and_cross_bindings(self):
        self.refused(RELEASE_ROWS)

    def test_coordinator_run(self):
        self.refused(COORDINATOR_ROWS)

    def test_checks_after_the_reporter_are_reached(self):
        self.refused(LATER_ROWS)

    def test_no_row_expects_a_reason_that_would_prove_nothing(self):
        for table, rows in TABLES:
            for change, reason in rows:
                with self.subTest(table=table, changed=change.name):
                    self.assertTrue(reason)
                    self.assertNotIn(reason, (BLANKET, GAP))


class ReporterPolicyTests(unittest.TestCase):
    def load(self, policy, role="application"):
        with patch.dict(os.environ, {"RELEASE_RECEIPT_POLICY": json.dumps({role: policy})}):
            return receipts.profile(role)

    def test_a_valid_profile_loads_and_is_returned_unchanged(self):
        f = ReporterFixture()
        self.assertEqual(self.load(f.policy), f.policy)

    def test_the_loader_refuses_each_policy_field_for_its_own_reason(self):
        for change, reason in POLICY_ROWS:
            with self.subTest(changed=change.name):
                f = ReporterFixture()
                change(f)
                with self.assertRaises(contract.Invalid) as caught:
                    self.load(f.policy)
                self.assertEqual(str(caught.exception), reason)

    def test_profile_is_application_only(self):
        for role in ("offline", "hosted", "web", "data", "license"):
            policy = example.example_policy(role)
            policy[PROFILE] = reporter_pin()
            with self.subTest(role=role):
                with self.assertRaises(contract.Invalid) as caught:
                    self.load(policy, role)
                self.assertEqual(str(caught.exception), "application reporter policy is only valid for application")

    def test_profile_is_application_only_in_the_validator_too(self):
        for role in ("offline", "hosted", "data"):
            policy = example.example_policy("offline")
            policy["artifact"] = "release-receipt-" + role
            policy[PROFILE] = reporter_pin()
            f = coordinator.ReceiptFixture(role, policy)
            with self.subTest(role=role):
                with self.assertRaises(contract.Invalid) as caught:
                    f.verify()
                self.assertEqual(str(caught.exception), "application reporter policy is only valid for application")
        for role in ("web", "license"):
            policy = example.example_policy("offline")
            policy["artifact"] = "release-receipt-" + role
            f = coordinator.ReceiptFixture(role, policy)
            f.receipt.update(schema="pineforge/facts-consumer-receipt/v1",
                             facts={key: f.event["facts"][key] for key in ("commit", "sha256")})
            f.receipt.pop("event_id")
            f.receipt.pop("release_version")
            f.event = None
            f.policy[PROFILE] = reporter_pin()
            with self.subTest(role=role):
                with self.assertRaises(contract.Invalid) as caught:
                    f.verify()
                self.assertEqual(str(caught.exception), "application reporter policy is only valid for application")

    def test_a_pin_that_is_not_an_object_is_missing(self):
        f = ReporterFixture()
        f.policy[PROFILE] = [1]
        with self.assertRaises(contract.Invalid) as caught:
            self.load(f.policy)
        self.assertEqual(str(caught.exception), "application reporter policy missing")

    def test_the_validator_checks_the_pin_before_any_provider_read(self):
        f = ReporterFixture()
        f.policy[PROFILE]["extra"] = 1
        with self.assertRaises(contract.Invalid):
            f.verify()
        self.assertEqual([route for route in f.queries if "deployments" in route], [])


class LegacyPathTests(unittest.TestCase):
    """The profile changes nothing for any policy that does not carry it."""

    def application(self):
        return coordinator.ReceiptFixture("application", coordinator.APPLICATION_POLICY)

    def test_application_without_the_profile_keeps_the_merge_equality(self):
        f = self.application()
        self.assertEqual(f.reporting_run["head_sha"], f.pr["merge"])
        self.assertEqual(f.verify()["state"], "verified")
        f.reporting_run["head_sha"] = REPORTER_COMMIT
        with self.assertRaises(contract.Invalid) as caught:
            f.verify()
        self.assertEqual(str(caught.exception), BLANKET)

    def test_application_without_the_profile_reads_no_signed_record(self):
        f = self.application()
        f.verify()
        self.assertFalse([route for route in f.queries if "deployments" in route or "/branches/" in route
                          or "/compare/" in route])

    def test_every_other_role_still_needs_the_reporting_run_at_the_merge(self):
        for role in ("offline", "hosted", "web", "data", "license"):
            policy = example.example_policy("offline")
            policy["artifact"] = "release-receipt-" + role
            f = coordinator.ReceiptFixture(role, policy)
            self.assertEqual(f.verify()["state"], "verified")
            for head in (REPORTER_COMMIT, f.pr["head"]):
                f.reporting_run["head_sha"] = head
                with self.subTest(role=role, head=head[:7]):
                    with self.assertRaises(contract.Invalid) as caught:
                        f.verify()
                    self.assertEqual(str(caught.exception), BLANKET)

    def test_selecting_the_profile_never_falls_back_to_the_merge_equality(self):
        f = ReporterFixture()
        # Exactly what the legacy rule accepts: a non-PR reporter whose head is the PR merge.
        f.reporting_run["head_sha"] = f.pr["merge"]
        with self.assertRaises(contract.Invalid) as caught:
            f.verify()
        self.assertEqual(str(caught.exception), "application reporter head differs from the pinned commit")
        # The very same receipt and run are accepted once the profile is absent.
        del f.policy[PROFILE]
        self.assertEqual(f.verify()["state"], "verified")

    def test_a_distinct_reporter_is_refused_without_the_profile(self):
        f = ReporterFixture()
        del f.policy[PROFILE]
        with self.assertRaises(contract.Invalid) as caught:
            f.verify()
        self.assertEqual(str(caught.exception), BLANKET)


class RetainedManualRunTests(unittest.TestCase):
    """The retained manual-dispatch run record is a NEGATIVE: not a coordinator run, not a live positive.

    The record is an operator input and is not part of the integration. Point PINEFORGE_F6_MANUAL_RUN_RECORD at
    its JSON to run this test; without it the test is SKIPPED, and a skip is not a pass.
    """

    def test_the_manual_run_is_refused_for_its_actor_and_has_no_inputs(self):
        path = os.environ.get("PINEFORGE_F6_MANUAL_RUN_RECORD")
        if not path:
            self.skipTest("PINEFORGE_F6_MANUAL_RUN_RECORD is not set: the retained manual run was NOT judged")
        capture = json.loads(Path(path).read_bytes())
        self.assertNotIn("inputs", capture)
        self.assertEqual(capture["event"], "workflow_dispatch")
        f = ReporterFixture(repo=capture["repository"]["full_name"], run=capture, commit=capture["head_sha"],
                            workflow_id=capture["workflow_id"], workflow=Path(capture["path"]).name)
        with self.assertRaises(contract.Invalid) as caught:
            f.verify()
        self.assertEqual(str(caught.exception), "application reporter actor differs from policy")


if __name__ == "__main__":
    unittest.main()
