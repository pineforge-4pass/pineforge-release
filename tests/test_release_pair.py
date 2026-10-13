#!/usr/bin/env python3
"""Unit tests for scripts/release_pair.py, the release rules the workflows run.

Covers semver order (a prerelease sorts below its release, unlike git's and
coreutils' version sort), the codegen PEP 440 spelling, the handle-upstream
decision (0.x keeps the coupled bump; from 1.0.0 on engine and codegen must
share one version, the first event of a pair waits) and the publish-time
check. Stdlib only: `python3 -m unittest discover -s tests -p 'test_release_*.py'`.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from itertools import permutations
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_pair.py"
sys.path.insert(0, str(SCRIPT.parent))

import release_pair as rp  # noqa: E402


def run(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin, capture_output=True, text=True, check=False,
    )


def outputs(proc: subprocess.CompletedProcess) -> dict:
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def decide(component: str, version: str, prev: tuple, *, current: str = "0.1.25",
           flag: str = "", other: str = "false", image: str = "true",
           force: str = "false", partner: str = "") -> subprocess.CompletedProcess:
    return run(
        "decide", "--component", component, "--version", version,
        "--prerelease-flag", flag, "--prev-engine", prev[0], "--prev-codegen", prev[1],
        "--current-release", current, "--other-published", other,
        "--image-exists", image, "--force", force, "--partner-latest", partner,
    )


def check(release: str, engine: str, codegen: str, newest: str = "") -> subprocess.CompletedProcess:
    return run("check", "--release", release, "--engine", engine, "--codegen", codegen,
               "--newest-stable", newest)


class VersionTest(unittest.TestCase):
    def test_prerelease_sorts_below_its_release(self):
        ordered = ["0.1.25", "0.13.1", "1.0.0-alpha.1", "1.0.0-beta.1", "1.0.0-rc.1",
                   "1.0.0-rc.2", "1.0.0-rc.10", "1.0.0", "1.0.1-rc.1", "1.0.1", "1.1.0"]
        scrambled = ordered[1::2] + ordered[::-2]
        self.assertEqual(sorted(scrambled, key=rp.parse), ordered)
        self.assertLess(rp.parse("1.0.0-rc.1"), rp.parse("1.0.0"))
        self.assertGreater(rp.parse("1.0.0-rc.1"), rp.parse("0.13.1"))

    def test_leading_v_is_the_same_version(self):
        self.assertEqual(rp.parse("v1.0.0-rc.1"), rp.parse("1.0.0-rc.1"))
        self.assertEqual(str(rp.parse("v1.0.0-rc.1")), "1.0.0-rc.1")

    def test_rejects_spellings_without_one_mapping(self):
        for bad in ["1.0.0rc1", "1.0.0-rc1", "1.0.0-rc", "1.0.0-foo.1", "1.0.0-RC.1",
                    "1.0", "01.0.0", "1.0.0-rc.01", "1.0.0+build.1", "", "latest",
                    "1.0.0-rc.1\n1.0.0"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rp.parse(bad)

    def test_pep440_spelling_for_pypi(self):
        self.assertEqual(rp.parse("1.0.0-rc.1").pep440, "1.0.0rc1")
        self.assertEqual(rp.parse("1.0.0-beta.2").pep440, "1.0.0b2")
        self.assertEqual(rp.parse("1.0.0-alpha.3").pep440, "1.0.0a3")
        self.assertEqual(rp.parse("1.0.0").pep440, "1.0.0")
        self.assertEqual(rp.parse("0.10.4").pep440, "0.10.4")

    def test_info_reports_line_and_channel(self):
        proc = run("info", "v1.0.0-rc.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs(proc), {"bare": "1.0.0-rc.1", "line": "pair",
                                         "prerelease": "true", "pypi": "1.0.0rc1"})
        self.assertEqual(outputs(run("info", "0.13.1")),
                         {"bare": "0.13.1", "line": "legacy", "prerelease": "false",
                          "pypi": "0.13.1"})

    def test_info_rejects_pypi_spelling_loudly(self):
        proc = run("info", "1.0.0rc1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("1.0.0rc1", proc.stderr)
        self.assertEqual(proc.stdout, "")


class LatestTagTest(unittest.TestCase):
    def test_final_release_outranks_its_rc(self):
        # git tag --sort=-v:refname and sort -V both put v1.0.0-rc.1 above v1.0.0.
        proc = run("latest-tag", stdin="v0.1.25\nv1.0.0-rc.1\nv1.0.0\n")
        self.assertEqual((proc.returncode, proc.stdout), (0, "v1.0.0\n"))

    def test_rc_is_the_latest_state_until_its_release(self):
        proc = run("latest-tag", stdin="v0.1.25\nv1.0.0-rc.1\n")
        self.assertEqual(proc.stdout, "v1.0.0-rc.1\n")

    def test_stable_skips_prereleases(self):
        self.assertEqual(run("latest-tag", "--stable", stdin="v0.1.25\nv1.0.0-rc.1\n").stdout,
                         "v0.1.25\n")
        self.assertEqual(run("latest-tag", "--stable",
                             stdin="v1.0.0\nv1.0.1-rc.1\nv0.1.25\n").stdout, "v1.0.0\n")

    def test_orders_numerically_and_ignores_foreign_tags(self):
        proc = run("latest-tag", stdin="junk\nv0.1.9\nv0.1.10\nrelease-2\n\n")
        self.assertEqual(proc.stdout, "v0.1.10\n")

    def test_newest_pypi_release_in_semver_spelling(self):
        proc = run("latest-pep440", stdin="0.10.4\n1.0.0rc1\n1.0.0rc2\n0.9.0\n1.0.0.post1\nfoo\n1.0.0a3\n")
        self.assertEqual((proc.returncode, proc.stdout), (0, "1.0.0-rc.2\n"))
        self.assertEqual(run("latest-pep440", stdin="0.10.4\n1.0.0\n1.0.0rc1\n").stdout, "1.0.0\n")

    def test_no_tags_prints_nothing(self):
        proc = run("latest-tag", stdin="")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class DecideLegacyTest(unittest.TestCase):
    """0.x pins keep today's coupled bump until the first 1.0 pair lands."""

    PREV = ("0.13.1", "0.10.4")

    def test_engine_event_bumps_engine_and_freezes_codegen(self):
        proc = decide("engine", "v0.13.2", self.PREV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = outputs(proc)
        self.assertEqual((got["mode"], got["line"], got["engine"], got["codegen"]),
                         ("bump", "legacy", "0.13.2", "0.10.4"))
        self.assertEqual((got["release"], got["prerelease"]), ("", "false"))

    def test_codegen_event_bumps_codegen_and_freezes_engine(self):
        got = outputs(decide("codegen", "0.10.5", self.PREV))
        self.assertEqual((got["mode"], got["engine"], got["codegen"]), ("bump", "0.13.1", "0.10.5"))

    def test_unchanged_pair_is_noop_or_republish(self):
        self.assertEqual(outputs(decide("engine", "0.13.1", self.PREV, image="true"))["mode"], "noop")
        self.assertEqual(outputs(decide("engine", "0.13.1", self.PREV, image="false"))["mode"],
                         "republish")

    def test_downgrade_is_refused_unless_forced(self):
        proc = decide("engine", "0.13.0", self.PREV)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing downgrade engine 0.13.1 -> 0.13.0", proc.stderr)
        self.assertEqual(proc.stdout, "")
        forced = outputs(decide("engine", "0.13.0", self.PREV, force="true"))
        self.assertEqual((forced["mode"], forced["engine"]), ("bump", "0.13.0"))

    def test_unresolved_frozen_component_fails(self):
        proc = decide("engine", "0.13.2", ("0.13.1", ""))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not resolve frozen component", proc.stderr)

    def test_0x_bump_needs_a_0x_hub_version(self):
        # A half-done first pair leaves VERSION=1.0.0 on main with 0.x pins
        # landed; a 0.x event must not patch-bump it into v1.0.1.
        for current in ("1.0.0", "1.0.0-rc.1", "0.1.26-rc.1"):
            with self.subTest(current=current):
                proc = decide("codegen", "0.10.5", self.PREV, current=current)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(f"VERSION {current}", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_prerelease_on_the_0x_line_is_refused(self):
        proc = decide("engine", "0.14.0-rc.1", self.PREV, flag="true")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("0.14.0-rc.1", proc.stderr)
        self.assertIn("prerelease", proc.stderr)


class DecidePairTest(unittest.TestCase):
    """From 1.0.0 on: one shared version, prerelease included."""

    LEGACY = ("0.13.1", "0.10.4")
    RC1 = ("1.0.0-rc.1", "1.0.0-rc.1")
    FINAL = ("1.0.0", "1.0.0")

    def test_first_event_of_a_pair_waits(self):
        proc = decide("engine", "v1.0.0-rc.1", self.LEGACY, flag="true", other="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = outputs(proc)
        self.assertEqual((got["mode"], got["line"], got["awaiting"]), ("wait", "pair", "codegen"))
        self.assertEqual((got["engine"], got["codegen"], got["release"]), ("1.0.0-rc.1", "0.10.4", ""))
        self.assertIn("codegen 1.0.0-rc.1", proc.stderr)

    def test_second_event_completes_the_pair(self):
        proc = decide("codegen", "1.0.0-rc.1", self.LEGACY, flag="true", other="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = outputs(proc)
        self.assertEqual((got["mode"], got["line"], got["release"], got["prerelease"]),
                         ("bump", "pair", "1.0.0-rc.1", "true"))
        self.assertEqual((got["engine"], got["codegen"]), ("1.0.0-rc.1", "1.0.0-rc.1"))

    def test_either_order_completes_the_same_pair(self):
        got = outputs(decide("engine", "v1.0.0-rc.1", self.LEGACY, other="true"))
        self.assertEqual((got["mode"], got["release"], got["engine"], got["codegen"]),
                         ("bump", "1.0.0-rc.1", "1.0.0-rc.1", "1.0.0-rc.1"))

    def test_final_after_rc_is_an_upgrade_not_a_downgrade(self):
        waiting = decide("engine", "v1.0.0", self.RC1, current="1.0.0-rc.1", flag="false")
        self.assertEqual(waiting.returncode, 0, waiting.stderr)
        self.assertEqual(outputs(waiting)["mode"], "wait")
        got = outputs(decide("codegen", "1.0.0", self.RC1, current="1.0.0-rc.1", other="true"))
        self.assertEqual((got["mode"], got["release"], got["prerelease"]), ("bump", "1.0.0", "false"))

    def test_duplicate_event_after_the_pair_landed(self):
        noop = outputs(decide("codegen", "1.0.0-rc.1", self.RC1, current="1.0.0-rc.1", image="true"))
        self.assertEqual(noop["mode"], "noop")
        again = outputs(decide("engine", "v1.0.0-rc.1", self.RC1, current="1.0.0-rc.1", image="false"))
        self.assertEqual(again["mode"], "republish")

    def test_next_version_waits_for_its_partner(self):
        got = outputs(decide("engine", "v1.1.0", self.FINAL, current="1.0.0"))
        self.assertEqual((got["mode"], got["awaiting"]), ("wait", "codegen"))

    def test_0x_event_after_the_pair_landed_fails_naming_both_versions(self):
        for force in ("false", "true"):
            with self.subTest(force=force):
                proc = decide("codegen", "0.10.5", self.FINAL, current="1.0.0", force=force)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("engine 1.0.0", proc.stderr)
                self.assertIn("codegen 0.10.5", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_stale_rc_after_the_final_is_refused(self):
        proc = decide("engine", "v1.0.0-rc.1", self.FINAL, current="1.0.0", flag="true")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing engine 1.0.0-rc.1 below the landed pair 1.0.0, even forced", proc.stderr)
        self.assertNotIn("client_payload.force", proc.stderr)  # forcing cannot help here

    def test_pair_line_rollback_is_not_a_new_release(self):
        proc = decide("engine", "v1.0.0", ("1.0.1", "1.0.1"), current="1.0.1", other="true",
                      force="true")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("1.0.0", proc.stderr)
        self.assertIn("1.0.1", proc.stderr)

    def test_landed_state_must_obey_the_rule(self):
        broken = decide("engine", "v1.0.1", ("1.0.0", "0.10.4"), current="1.0.0")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("engine 1.0.0 + codegen 0.10.4", broken.stderr)
        drifted = decide("engine", "v1.0.1", self.FINAL, current="0.1.25")
        self.assertEqual(drifted.returncode, 1)
        self.assertIn("VERSION 0.1.25", drifted.stderr)

    def test_partner_at_another_new_version_is_a_mismatch(self):
        # engine 1.0.1 while codegen already released 1.0.2: never pairable.
        proc = decide("engine", "v1.0.1", self.FINAL, current="1.0.0", partner="1.0.2")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("engine 1.0.1", proc.stderr)
        self.assertIn("codegen 1.0.2", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # an engine that skips the codegen's rc.2 is a mismatch as well
        skip = decide("engine", "v1.0.0", self.RC1, current="1.0.0-rc.1", partner="1.0.0-rc.2")
        self.assertEqual(skip.returncode, 1)
        self.assertIn("codegen 1.0.0-rc.2", skip.stderr)

    def test_partner_not_moved_or_mid_release_still_waits(self):
        for partner in ("1.0.0", "", "1.0.1"):  # landed, unknown, same version still uploading
            with self.subTest(partner=partner):
                got = outputs(decide("engine", "v1.0.1", self.FINAL, current="1.0.0", partner=partner))
                self.assertEqual(got["mode"], "wait")
        first = outputs(decide("engine", "v1.0.0-rc.1", self.LEGACY, partner="0.10.4"))
        self.assertEqual(first["mode"], "wait")

    def test_unrecorded_0x_partner_release_is_not_a_1x_mismatch(self):
        # codegen 0.10.5 shipped but never reached the hub: it cannot be a 1.0 partner.
        got = outputs(decide("engine", "v1.0.0-rc.1", self.LEGACY, partner="0.10.5"))
        self.assertEqual(got["mode"], "wait")

    def test_half_done_pair_is_tagged_not_bumped_again(self):
        # main got the release commit (VERSION = the pair version) but not the tag.
        first = decide("codegen", "1.0.0-rc.1", self.LEGACY, current="1.0.0-rc.1", other="true")
        self.assertEqual(first.returncode, 0, first.stderr)
        got = outputs(first)
        self.assertEqual((got["mode"], got["retag"], got["release"]), ("bump", "true", "1.0.0-rc.1"))
        later = outputs(decide("engine", "v1.0.1", self.FINAL, current="1.0.1", other="true"))
        self.assertEqual((later["mode"], later["retag"], later["release"]), ("bump", "true", "1.0.1"))
        normal = outputs(decide("codegen", "1.0.0-rc.1", self.LEGACY, other="true"))
        self.assertEqual(normal["retag"], "false")

    def test_version_drift_on_the_pair_line_fails(self):
        proc = decide("engine", "v1.0.1", self.FINAL, current="1.0.3", other="true")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("VERSION 1.0.3", proc.stderr)
        self.assertIn("send an event for 1.0.3 again", proc.stderr)  # the half-done recovery hint

    def test_prerelease_flag_must_agree_with_the_version(self):
        for version, flag in [("v1.0.0-rc.1", "false"), ("v1.0.0", "true"), ("v1.0.0", "yes")]:
            with self.subTest(version=version, flag=flag):
                proc = decide("engine", version, self.LEGACY, flag=flag)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")


class CheckTest(unittest.TestCase):
    """publish.yml refuses to publish an image whose pair breaks the rule."""

    def test_rc_pair_is_a_prerelease(self):
        proc = check("1.0.0-rc.1", "1.0.0-rc.1", "1.0.0-rc.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs(proc), {"line": "pair", "prerelease": "true",
                                         "codegen_pypi": "1.0.0rc1", "moving": "false"})

    def test_final_pair_is_stable(self):
        self.assertEqual(outputs(check("1.0.0", "1.0.0", "1.0.0", newest="v1.0.0")),
                         {"line": "pair", "prerelease": "false", "codegen_pypi": "1.0.0", "moving": "true"})

    def test_mismatched_pair_fails_loud_naming_both_versions(self):
        for engine, codegen in [("1.0.0", "1.0.1"), ("1.0.0-rc.1", "1.0.0-rc.2"), ("1.0.0", "0.10.4")]:
            with self.subTest(engine=engine, codegen=codegen):
                proc = check(engine, engine, codegen)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(f"engine {engine}", proc.stderr)
                self.assertIn(f"codegen {codegen}", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_release_version_is_the_pair_version(self):
        proc = check("1.0.0", "1.0.0-rc.1", "1.0.0-rc.1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("1.0.0-rc.1", proc.stderr)

    def test_0x_release_cannot_carry_a_1x_component(self):
        proc = check("0.1.26", "1.0.0", "0.10.4")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("engine 1.0.0", proc.stderr)
        self.assertIn("codegen 0.10.4", proc.stderr)

    def test_0x_pair_keeps_independent_versions(self):
        self.assertEqual(outputs(check("0.1.26", "0.13.1", "0.10.4", newest="v0.1.26")),
                         {"line": "legacy", "prerelease": "false", "codegen_pypi": "0.10.4", "moving": "true"})

    def test_moving_tags_follow_the_newest_stable_release(self):
        self.assertEqual(outputs(check("1.0.0", "1.0.0", "1.0.0", newest="v1.0.0"))["moving"], "true")
        self.assertEqual(outputs(check("1.0.0", "1.0.0", "1.0.0", newest="v1.0.1"))["moving"], "false")
        self.assertEqual(outputs(check("1.0.1-rc.1", "1.0.1-rc.1", "1.0.1-rc.1", newest="v1.0.0"))["moving"],
                         "false")
        self.assertEqual(outputs(check("0.1.26", "0.13.1", "0.10.4", newest="v0.1.26"))["moving"], "true")

    def test_0x_line_is_stable_only(self):
        self.assertEqual(check("0.1.26", "0.14.0-rc.1", "0.10.4").returncode, 1)
        self.assertEqual(check("0.1.26-rc.1", "0.13.1", "0.10.4").returncode, 1)


class HpoPairingTest(unittest.TestCase):
    """The cutoff is semver precedence; the first 1.6 release needs three pins."""

    COMPONENTS = ("engine", "codegen", "hpo")

    def decision(self, component, version, *, landed="1.5.9", prev_hpo="", current=None,
                 published=None, latest=None, flag="", image=True, force=False):
        return rp.decide(component, version, flag, landed, landed, current or landed,
                         False, image, force, prev_hpo=prev_hpo,
                         partners_published=published, partners_latest=latest)

    def test_named_cutoff_and_info_boundary(self):
        self.assertEqual(rp.HPO_PAIRING_FROM, "1.6.0")
        for version, required in (("1.5.9", False), ("1.6.0-rc.1", False),
                                  ("1.6.0", True), ("1.6.1-rc.1", True)):
            with self.subTest(version=version):
                proc = run("info", version)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs(proc).get("hpo_pairing") == "true", required)

    def test_existing_two_component_callers_below_cutoff_are_unchanged(self):
        for version, waiting, complete in (("1.5.0", "noop", "noop"),
                                            ("1.5.9", "wait", "bump"),
                                            ("1.6.0-rc.1", "wait", "bump")):
            for component in self.COMPONENTS[:2]:
                for published, expected in ((False, waiting), (True, complete)):
                    with self.subTest(version=version, component=component, published=published):
                        args = (component, version, "", "1.5.0", "1.5.0", "1.5.0",
                                published, True, False)
                        old, note = rp.decide(*args)
                        with_hpo, hpo_note = rp.decide(*args, prev_hpo="ignored-invalid",
                                                      partners_published={"hpo": True},
                                                      partners_latest={"hpo": "ignored-invalid"})
                        self.assertEqual(with_hpo, old)
                        self.assertEqual(hpo_note, note)
                        self.assertEqual(old["mode"], expected)
                        self.assertNotIn("hpo", old)

    def test_hpo_below_cutoff_is_refused_even_forced(self):
        for version in ("0.13.2", "1.5.0", "1.5.9", "1.6.0-alpha.1",
                        "1.6.0-beta.1", "1.6.0-rc.1"):
            for force in (False, True):
                with self.subTest(version=version, force=force):
                    with self.assertRaisesRegex(rp.RuleError, "hpo joins the pairing at 1.6.0"):
                        self.decision("hpo", version, force=force)
                    proc = run("info", version, "--component=hpo")
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn("hpo joins the pairing at 1.6.0", proc.stderr)
                    self.assertEqual(proc.stdout, "")

    def test_all_six_arrival_orders_for_stable_and_prerelease(self):
        for version in ("1.6.0", "1.6.1-rc.1"):
            for order in permutations(self.COMPONENTS):
                published = set()
                for component in order:
                    published.add(component)
                    with self.subTest(version=version, order=order, event=component):
                        result, note = self.decision(
                            component, version,
                            published={name: name in published for name in self.COMPONENTS},
                            latest={name: version if name in published else "1.5.9"
                                    for name in self.COMPONENTS})
                        missing = set(self.COMPONENTS) - published
                        self.assertEqual(result["mode"], "wait" if missing else "bump")
                        self.assertEqual(result["line"], "pair")
                        self.assertEqual(result["prerelease"], "true" if "-" in version else "false")
                        self.assertEqual(result["release"], "" if missing else version)
                        self.assertEqual(set(filter(None, result["awaiting"].split(","))), missing)
                        for name in missing:
                            self.assertIn(f"{name} {version}", note)
                        if not missing:
                            self.assertEqual(tuple(result[name] for name in self.COMPONENTS), (version,) * 3)

    def test_old_other_published_flag_cannot_complete_without_hpo(self):
        for component, awaiting in (("engine", "hpo"), ("codegen", "hpo")):
            with self.subTest(component=component):
                result, _ = rp.decide(component, "1.6.0", "", "1.5.9", "1.5.9", "1.5.9",
                                      True, True, False)
                self.assertEqual((result["mode"], result["awaiting"], result["release"]),
                                 ("wait", awaiting, ""))

    def test_hpo_next_patch_waits_for_both_landed_partners(self):
        result, note = self.decision("hpo", "1.6.1", landed="1.6.0", prev_hpo="1.6.0",
                                     latest={"engine": "1.6.0", "codegen": "1.6.0"})
        self.assertEqual((result["mode"], result["awaiting"], result["release"]),
                         ("wait", "engine,codegen", ""))
        for name in self.COMPONENTS[:2]:
            self.assertIn(f"{name} 1.6.1", note)

    def test_every_different_new_partner_version_fails_naming_all_components(self):
        for component in self.COMPONENTS:
            for partner, versions in (("engine", ("1.6.0-rc.2", "1.6.2", "1.6.1-rc.1")),
                                      ("codegen", ("1.6.0-rc.2", "1.6.2", "1.6.1-rc.1")),
                                      ("hpo", ("1.6.2", "1.6.1-rc.1"))):
                if partner == component:
                    continue
                for different in versions:
                    with self.subTest(component=component, partner=partner, different=different):
                        with self.assertRaises(rp.RuleError) as caught:
                            self.decision(component, "1.6.1", latest={partner: different})
                        message = str(caught.exception)
                        self.assertIn("mismatched", message)
                        self.assertIn(f"{component} 1.6.1", message)
                        self.assertIn(f"{partner} {different}", message)
                        for name in self.COMPONENTS:
                            self.assertIn(name, message)

    def test_unknown_landed_or_mid_upload_partners_keep_waiting(self):
        for component in self.COMPONENTS:
            for newest in ("", "1.6.0", "1.6.1"):
                with self.subTest(component=component, newest=newest):
                    result, _ = self.decision(
                        component, "1.6.1", landed="1.6.0", prev_hpo="1.6.0",
                        latest={name: newest for name in self.COMPONENTS if name != component})
                    self.assertEqual(result["mode"], "wait")
                    self.assertEqual(set(result["awaiting"].split(",")), set(self.COMPONENTS) - {component})

    def test_landed_triple_requires_a_valid_matching_hpo_pin(self):
        for prev_hpo in ("", "bad", "1.5.9", "1.6.0-rc.1", "1.6.1"):
            with self.subTest(prev_hpo=prev_hpo), self.assertRaises(rp.RuleError) as caught:
                self.decision("engine", "1.6.1", landed="1.6.0", prev_hpo=prev_hpo,
                              published={"codegen": True, "hpo": True})
            self.assertIn("landed", str(caught.exception))
            self.assertIn("hpo", str(caught.exception))

    def test_all_components_refuse_rollback_even_forced(self):
        for component in self.COMPONENTS:
            for version in ("1.5.9", "1.6.0-rc.1", "1.6.0", "1.6.1-rc.1"):
                with self.subTest(component=component, version=version), self.assertRaises(rp.RuleError):
                    self.decision(component, version, landed="1.6.1", prev_hpo="1.6.1", force=True,
                                  published={name: True for name in self.COMPONENTS})

    def test_duplicate_triple_noop_or_republish_and_retag(self):
        for component in self.COMPONENTS:
            for image, mode in ((True, "noop"), (False, "republish")):
                with self.subTest(component=component, image=image):
                    result, _ = self.decision(component, "1.6.0", landed="1.6.0", prev_hpo="1.6.0", image=image)
                    self.assertEqual((result["mode"], result["hpo"]), (mode, "1.6.0"))
            result, _ = self.decision(component, "1.6.0", current="1.6.0",
                                      published={name: True for name in self.COMPONENTS})
            self.assertEqual((result["mode"], result["retag"], result["release"]), ("bump", "true", "1.6.0"))
            with self.assertRaisesRegex(rp.RuleError, "VERSION 1.6.2"):
                self.decision(component, "1.6.0", current="1.6.2",
                              published={name: True for name in self.COMPONENTS})

    def test_every_event_prerelease_flag_and_version_refusal(self):
        for component in self.COMPONENTS:
            for version, flag in (("1.6.0", "true"), ("1.6.1-rc.1", "false"),
                                  ("1.6.0", "yes"), ("1.6.0", "true\nfalse")):
                with self.subTest(component=component, version=version, flag=flag), self.assertRaises(rp.RuleError):
                    self.decision(component, version, flag=flag)
            for version in ("1.6", "1.6.0rc1", "1.6.0-rc1", "1.6.0+build.1", "1.6.0;id", "1.6.0\n1.6.1"):
                with self.subTest(component=component, version=version), self.assertRaises(ValueError):
                    self.decision(component, version)
            for version, flag in (("v1.6.0", "false"), ("1.6.1-rc.1", "true"), ("1.6.1-rc.1", "")):
                with self.subTest(component=component, version=version, flag=flag):
                    result, _ = self.decision(component, version, flag=flag,
                                              published={name: True for name in self.COMPONENTS})
                    self.assertEqual(result["prerelease"], "true" if "-" in version else "false")

    def test_cli_passes_all_partner_facts_and_hpo_pin(self):
        for component in self.COMPONENTS:
            with self.subTest(component=component):
                proc = run("decide", f"--component={component}", "--version=1.6.0",
                           "--prev-engine=1.5.9", "--prev-codegen=1.5.9", "--prev-hpo=",
                           "--current-release=1.5.9", "--engine-published=true",
                           "--codegen-published=true", "--hpo-published=true")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(tuple(outputs(proc)[name] for name in self.COMPONENTS), ("1.6.0",) * 3)
                self.assertEqual(outputs(proc)["mode"], "bump")


class CheckHpoTest(unittest.TestCase):
    def test_pre_cutoff_positional_newest_stable_callers_keep_their_outputs(self):
        cases = (("0.1.26", "0.13.1", "0.10.4", "true"),
                 ("1.5.0", "1.5.0", "1.5.0", "true"),
                 ("1.5.9", "1.5.9", "1.5.9", "true"),
                 ("1.6.0-rc.1", "1.6.0-rc.1", "1.6.0-rc.1", "false"))
        for version, engine, codegen, moving in cases:
            with self.subTest(version=version):
                old_call = rp.check(version, engine, codegen, f"v{version}")
                explicit = rp.check(version, engine, codegen, newest_stable=f"v{version}")
                self.assertEqual(old_call, explicit)
                self.assertEqual(old_call["moving"], moving)

    def test_hpo_is_ignored_without_parsing_below_cutoff(self):
        for version in ("1.5.0", "1.5.9", "1.6.0-rc.1"):
            for hpo in ("", "invalid", "1.6.99", "1.6.0\n1.6.1"):
                with self.subTest(version=version, hpo=hpo):
                    expected = rp.check(version, version, version, newest_stable=f"v{version}")
                    actual = rp.check(version, version, version, hpo, newest_stable=f"v{version}")
                    self.assertEqual(actual, expected)
                    self.assertNotIn("hpo", actual)

    def test_check_requires_all_four_versions_to_match(self):
        for version, different in (("1.6.0", "1.6.1"), ("1.6.1-rc.1", "1.6.1-rc.2")):
            for odd in ("release", "engine", "codegen", "hpo"):
                versions = {name: version for name in ("release", "engine", "codegen", "hpo")}
                versions[odd] = different
                with self.subTest(version=version, odd=odd), self.assertRaises(rp.RuleError) as caught:
                    rp.check(**versions)
                for name, value in versions.items():
                    self.assertIn(f"{name} {value}", str(caught.exception))

    def test_missing_and_invalid_hpo_fail_loudly(self):
        for version in ("1.6.0", "1.6.1-rc.1"):
            for hpo in ("", "bad", "1.6.0rc1", "1.6.0-foo.1", "1.6.0\n1.6.1"):
                with self.subTest(version=version, hpo=hpo), self.assertRaises(rp.RuleError) as caught:
                    rp.check(version, version, version, hpo)
                self.assertIn("hpo", str(caught.exception))
                self.assertIn(f"release {version}", str(caught.exception))

    def test_matching_triple_channel_build_pin_and_cli(self):
        for version, prerelease, moving, pypi in (("1.6.0", "false", "true", "1.6.0"),
                                                ("1.6.1-rc.1", "true", "false", "1.6.1rc1")):
            with self.subTest(version=version):
                expected = {"line": "pair", "prerelease": prerelease, "moving": moving,
                            "codegen_pypi": pypi, "hpo": version, "target": "hpo",
                            "hpo_version_arg": f"HPO_VERSION={version}"}
                self.assertEqual(rp.check(version, version, version, f"v{version}", newest_stable="v1.6.0"), expected)
                proc = run("check", f"--release={version}", f"--engine={version}", f"--codegen={version}",
                           f"--hpo={version}", "--newest-stable=v1.6.0")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs(proc), expected)
                missing = check(version, version, version)
                self.assertEqual(missing.returncode, 1)
                self.assertIn("hpo", missing.stderr)
                self.assertEqual(missing.stdout, "")


if __name__ == "__main__":
    unittest.main()
