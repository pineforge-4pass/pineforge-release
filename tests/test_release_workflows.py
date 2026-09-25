#!/usr/bin/env python3
"""Wiring checks: the release workflows run scripts/release_pair.py where its
rules apply and never fall back to an rc-unsafe version sort. Stdlib only."""
from __future__ import annotations

import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _step(text: str, name: str) -> str:
    """The body of the workflow step whose `- name:` line starts with name."""
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


class HandleUpstreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WORKFLOWS / "handle-upstream.yml").read_text(encoding="utf-8")

    def test_no_rc_unsafe_version_sort_left(self):
        self.assertNotIn("sort -V", self.text)
        self.assertNotIn("--sort=-v:refname", self.text)

    def test_validates_the_prerelease_flag_and_the_version(self):
        body = _step(self.text, "Validate payload")
        self.assertIn("RAW_PRERELEASE: ${{ github.event.client_payload.prerelease }}", body)
        self.assertIn("python3 scripts/release_pair.py info", body)

    def test_landed_pins_come_from_the_semver_latest_tag(self):
        body = _step(self.text, "Resolve the landed")
        self.assertIn("git tag -l 'v*' | python3 scripts/release_pair.py latest-tag", body)

    def test_partner_is_probed_only_on_the_pair_line(self):
        body = _step(self.text, "Probe the partner")
        self.assertIn("if: steps.in.outputs.line == 'pair'", body)
        self.assertIn("pypi.org/pypi/pineforge-codegen/${PYPI}/json", body)
        self.assertIn("pineforge-v${VER}-linux-aarch64.tar.gz", body)

    def test_decision_is_the_script(self):
        body = _step(self.text, "Decide action")
        self.assertIn("python3 scripts/release_pair.py decide", body)
        self.assertIn('--other-published="${PUBLISHED:-false}"', body)

    def test_first_event_of_a_pair_waits_without_building(self):
        body = _step(self.text, "Wait for the partner")
        self.assertIn("if: steps.decide.outputs.mode == 'wait'", body)
        self.assertNotIn("git push", body)

    def test_pair_release_version_is_the_pair_version(self):
        body = _step(self.text, "Bump VERSION")
        self.assertIn("if: steps.decide.outputs.mode == 'bump'", body)
        self.assertIn('if [ "$LINE" = pair ]; then', body)
        self.assertIn('next="$RELEASE"', body)


class PublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")

    def test_no_rc_unsafe_version_sort_left(self):
        self.assertNotIn("sort -V", self.text)
        self.assertNotIn("sort -uV", self.text)

    def test_pins_fallback_on_the_pair_line_is_the_release_version(self):
        body = _step(self.text, "Read pins")
        self.assertIn('python3 scripts/release_pair.py info "$REF"', body)
        self.assertIn('e="${REF#v}"; c="${REF#v}"', body)

    def test_pair_is_checked_before_anything_is_built(self):
        body = _step(self.text, "Pairing rule")
        self.assertIn("id: pair", body)
        self.assertIn("python3 scripts/release_pair.py check", body)
        self.assertLess(self.text.index("- name: Pairing rule"),
                        self.text.index("docker/build-push-action"))

    def test_waits_for_codegen_under_its_pypi_spelling(self):
        body = _step(self.text, "Wait for upstream artifacts")
        self.assertIn("CP: ${{ steps.pair.outputs.codegen_pypi }}", body)
        self.assertIn("pypi.org/pypi/pineforge-codegen/${CP}/json", body)

    def test_prerelease_never_gets_latest_or_the_moving_minor_tag(self):
        stable = "enable=${{ steps.pair.outputs.prerelease == 'false' }}"
        body = _step(self.text, "Image metadata")
        self.assertIn("latest=false", body)
        self.assertIn(f"type=raw,value=latest,{stable}", body)
        self.assertIn("type=semver,pattern={{major}}.{{minor}},value=${{ github.ref_name }},"
                      + stable, body)
        self.assertEqual(body.count("value=latest"), 1)

    def test_github_release_channel(self):
        body = _step(self.text, "GitHub Release")
        self.assertIn("PRERELEASE: ${{ steps.pair.outputs.prerelease }}", body)
        self.assertIn("--prerelease --latest=false", body)
        self.assertIn("python3 scripts/release_pair.py latest-tag --stable", body)

    def test_prerelease_flag_reaches_every_consumer(self):
        self.assertIn("prerelease: ${{ steps.pair.outputs.prerelease }}", self.text)
        body = _step(self.text, "Dispatch pineforge-release")
        self.assertIn("PRERELEASE: ${{ needs.publish.outputs.prerelease }}", body)
        self.assertIn('-F "client_payload[prerelease]=${PRERELEASE}"', body)
        self.assertIn("repo: [pineforge-backtest-mcp, pineforge-mcp-public, pineforge-app]",
                      self.text)


if __name__ == "__main__":
    unittest.main()
