#!/usr/bin/env python3
"""Offline legacy predecessor identity regressions for the hub receipt validator."""
from __future__ import annotations

import copy
import unittest

import test_release_coordinator_receipts as coordinator


contract = coordinator.contract
example = coordinator.example
receipts = coordinator.receipts

FACTS = {"commit": "e" * 40, "sha256": "f" * 64}
LEGACY_PREVIOUS = {
    "legacy": True,
    "npm_version": "0.9.40",
    "npm_integrity": "sha512-o1hmSb6FwjkF8lIwLhENJV+YPbYqSwG1KPvHyViPj5Yan3aHoNRCpM5uINaYbjZKjGS95VDClmASOUSD6ZktVw==",
    "git_head": "b988217f2aa21b6b772fb723adbc89bd4130c3f5",
    # Synthetic OCI digest: the real 0.9.40 image digest is unavailable.
    "image_digest": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
}
LEGACY_PREVIOUS_BYTES = (
    b'{"git_head":"b988217f2aa21b6b772fb723adbc89bd4130c3f5",'
    b'"image_digest":"sha256:7777777777777777777777777777777777777777777777777777777777777777",'
    b'"legacy":true,'
    b'"npm_integrity":"sha512-o1hmSb6FwjkF8lIwLhENJV+YPbYqSwG1KPvHyViPj5Yan3aHoNRCpM5uINaYbjZKjGS95VDClmASOUSD6ZktVw==",'
    b'"npm_version":"0.9.40"}\n'
)


def offline_fixture():
    fixture = coordinator.ReceiptFixture("offline")
    fixture.receipt["previous"] = copy.deepcopy(LEGACY_PREVIOUS)
    return fixture


def facts_fixture(consumer):
    fixture = coordinator.ReceiptFixture(consumer)
    receipt = example.build_receipt(consumer=consumer, facts=FACTS)
    receipt["pr"] = copy.deepcopy(fixture.pr)
    for name in ("ci", "e2e", "deploy"):
        receipt[name] = copy.deepcopy(fixture.receipt[name])
    fixture.receipt = receipt
    return fixture


def verify_facts(fixture):
    return receipts.validate_receipt(fixture, fixture.repo, fixture.receipt, None,
                                     fixture.policy, fixture.reporting_run)


class LegacyPreviousReceiptTests(unittest.TestCase):
    def test_verified_offline_legacy_previous_is_accepted_and_serialized_unchanged(self):
        """Mutation: change identity()'s legacy consumer == "offline" to consumer == "data"."""
        fixture = offline_fixture()

        public = fixture.verify()
        self.assertEqual(public["previous"], LEGACY_PREVIOUS)
        self.assertEqual(contract.canonical(public["previous"]), LEGACY_PREVIOUS_BYTES)
        round_tripped = contract.decode(contract.canonical(public))
        self.assertEqual(round_tripped["previous"], LEGACY_PREVIOUS)

    def test_legacy_previous_refuses_each_extra_key(self):
        for extra in ("source", "event_id", "facts_commit", "unknown"):
            fixture = offline_fixture()
            # Mutation: remove identity()'s legacy keys() call (extra keys then pass).
            fixture.receipt["previous"][extra] = "unexpected"
            with self.subTest(extra=extra), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                fixture.verify()

    def test_legacy_previous_refuses_each_missing_key(self):
        for missing in ("legacy", "npm_version", "npm_integrity", "git_head", "image_digest"):
            fixture = offline_fixture()
            # Mutation: remove identity()'s legacy keys() call (missing npm_version then raises KeyError).
            del fixture.receipt["previous"][missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
                fixture.verify()

    def test_legacy_previous_requires_a_true_boolean_flag(self):
        for value in (False, 0, 1, "true", None):
            fixture = offline_fixture()
            # Mutation: change identity()'s obj["legacy"] is True to obj["legacy"] == True.
            fixture.receipt["previous"]["legacy"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity flag must be true"):
                fixture.verify()

    def test_legacy_identity_is_restricted_to_offline_previous(self):
        for slot in ("preview", "deployed"):
            fixture = offline_fixture()
            # Mutation: replace identity()'s legacy slot == "previous" with True.
            fixture.receipt[slot] = copy.deepcopy(LEGACY_PREVIOUS)
            # Call identity() too, before source lookups can mask a missing scope check.
            with self.subTest(slot=slot, call="identity"), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity is only valid for product offline.previous"):
                receipts.identity(fixture.receipt[slot], consumer="offline", slot=slot)
            with self.subTest(slot=slot, call="validate_receipt"), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity is only valid for product offline.previous"):
                fixture.verify()

        for consumer in ("hosted", "application", "web", "data", "license"):
            fixture = coordinator.ReceiptFixture(consumer)
            # Mutation: replace identity()'s legacy consumer == "offline" with True.
            fixture.receipt["previous"] = copy.deepcopy(LEGACY_PREVIOUS)
            with self.subTest(consumer=consumer), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity is only valid for product offline.previous"):
                fixture.verify()

    def test_legacy_scope_is_checked_before_identity_stage(self):
        """Mutation: remove or "legacy" in receipt[name] from the early identity guard."""
        for state in ("pending", "pr_open", "testing", "ready", "merged", "deploying",
                      "failed", "superseded", "skipped_by_policy"):
            for consumer, slot in (("offline", "preview"), ("offline", "deployed"),
                                   ("hosted", "previous"), ("data", "previous"), ("web", "previous")):
                with self.subTest(state=state, consumer=consumer, slot=slot):
                    fixture = coordinator.ReceiptFixture(consumer)
                    fixture.receipt["state"] = state
                    # Permit policy skipping so it cannot mask the missing legacy guard.
                    if state == "skipped_by_policy":
                        fixture.event["prerelease"] = True
                    fixture.receipt[slot] = copy.deepcopy(LEGACY_PREVIOUS)
                    with self.assertRaisesRegex(
                            contract.Invalid, "legacy identity is only valid for product offline.previous"):
                        fixture.verify()
                    # Even a later scope refusal must not stand in for the early guard.
                    self.assertEqual(fixture.queries, [])
                    self.assertEqual(fixture.downloads, [])

    def test_legacy_identity_refuses_facts_only_offline_previous(self):
        """Mutation: remove not facts_only and from identity()'s legacy scope condition."""
        with self.subTest(facts_only=True), self.assertRaisesRegex(
                contract.Invalid, "legacy identity is only valid for product offline.previous"):
            receipts.identity(LEGACY_PREVIOUS, facts_only=True, consumer="offline", slot="previous")

    def test_legacy_identity_needs_explicit_consumer_and_slot(self):
        """Mutation: replace identity()'s legacy scope condition with not facts_only."""
        for context in ({}, {"consumer": "offline"}, {"slot": "previous"}):
            with self.subTest(context=context), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity is only valid for product offline.previous"):
                receipts.identity(LEGACY_PREVIOUS, **context)

    def test_legacy_identity_accepts_product_offline_previous(self):
        """Positive control for a product offline.previous identity."""
        with self.subTest(consumer="offline", slot="previous"):
            self.assertIsNone(receipts.identity(LEGACY_PREVIOUS, consumer="offline", slot="previous"))

    def test_legacy_previous_is_rejected_in_each_facts_only_consumer(self):
        for consumer in ("web", "license"):
            fixture = facts_fixture(consumer)
            # Mutation: replace identity()'s legacy scope condition with slot == "previous".
            fixture.receipt["previous"] = copy.deepcopy(LEGACY_PREVIOUS)
            with self.subTest(consumer=consumer), self.assertRaisesRegex(
                    contract.Invalid, "legacy identity is only valid for product offline.previous"):
                verify_facts(fixture)

    def test_legacy_previous_rejects_noncanonical_npm_integrity(self):
        """Mutation: restore the decoded-length-only legacy npm integrity check."""
        fixture = offline_fixture()
        fixture.receipt["previous"]["npm_integrity"] = (
            "sha512-o1hmSb6FwjkF8lIwLhENJV+YPbYqSwG1KPvHyViPj5Yan3aHoNRCpM5uINaYbjZKjGS95VDClmASOUSD6ZktVx=="
        )
        with self.subTest(call="identity"), self.assertRaisesRegex(contract.Invalid, "invalid npm integrity"):
            receipts.identity(fixture.receipt["previous"], consumer="offline", slot="previous")
        with self.subTest(call="validate_receipt"), self.assertRaisesRegex(contract.Invalid, "invalid npm integrity"):
            fixture.verify()

    def test_legacy_previous_rejects_malformed_values(self):
        mutations = (
            # Mutation: replace identity()'s version(obj["npm_version"]) with pass.
            ("npm_version leading v", "npm_version", "v0.9.40"),
            ("npm_version short", "npm_version", "0.9"),
            ("npm_version leading zero", "npm_version", "01.2.3"),
            ("npm_version non-string", "npm_version", 9040),
            # Mutation: remove identity()'s match() call for obj["npm_integrity"].
            ("npm_integrity too short", "npm_integrity", "sha512-" + "A" * 85 + "=="),
            ("npm_integrity wrong prefix", "npm_integrity", "sha256-" + "A" * 86 + "=="),
            ("npm_integrity wrong padding", "npm_integrity", "sha512-" + "A" * 86 + "="),
            # Mutation: remove identity()'s match(COMMIT, obj["git_head"]).
            ("git_head 39 chars", "git_head", "a" * 39),
            ("git_head 41 chars", "git_head", "a" * 41),
            ("git_head uppercase", "git_head", "A" * 40),
            # Mutation: remove identity()'s match("sha256:" + SHA, obj["image_digest"]).
            ("image_digest null", "image_digest", None),
            ("image_digest 63 hex", "image_digest", "sha256:" + "7" * 63),
            ("image_digest uppercase", "image_digest", "sha256:" + "A" * 64),
            ("image_digest without prefix", "image_digest", "7" * 64),
        )
        for description, field, value in mutations:
            fixture = offline_fixture()
            fixture.receipt["previous"][field] = value
            with self.subTest(description=description), self.assertRaisesRegex(contract.Invalid, "malformed identity"):
                fixture.verify()

    def test_offline_previous_strict_five_key_shape_still_passes(self):
        """Mutation: replace identity()'s "legacy" in obj branch condition with True."""
        fixture = coordinator.ReceiptFixture("offline")

        public = fixture.verify()
        self.assertEqual(set(public["previous"]), {
            "source", "image_digest", "facts_commit", "facts_sha256", "deployment_id_sha256",
        })
        self.assertEqual(public["previous"], fixture.receipt["previous"])

    def test_strict_previous_with_legacy_key_is_refused(self):
        """Mutation: remove identity()'s legacy keys() call (npm_version then raises KeyError)."""
        fixture = coordinator.ReceiptFixture("offline")
        fixture.receipt["previous"]["legacy"] = True

        with self.assertRaisesRegex(contract.Invalid, "unexpected object fields"):
            fixture.verify()


if __name__ == "__main__":
    unittest.main()
