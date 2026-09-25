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
           force: str = "false") -> subprocess.CompletedProcess:
    return run(
        "decide", "--component", component, "--version", version,
        "--prerelease-flag", flag, "--prev-engine", prev[0], "--prev-codegen", prev[1],
        "--current-release", current, "--other-published", other,
        "--image-exists", image, "--force", force,
    )


def check(release: str, engine: str, codegen: str) -> subprocess.CompletedProcess:
    return run("check", "--release", release, "--engine", engine, "--codegen", codegen)


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
        self.assertIn("refusing downgrade engine 1.0.0 -> 1.0.0-rc.1", proc.stderr)

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
                                         "codegen_pypi": "1.0.0rc1"})

    def test_final_pair_is_stable(self):
        self.assertEqual(outputs(check("1.0.0", "1.0.0", "1.0.0")),
                         {"line": "pair", "prerelease": "false", "codegen_pypi": "1.0.0"})

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
        self.assertEqual(outputs(check("0.1.26", "0.13.1", "0.10.4")),
                         {"line": "legacy", "prerelease": "false", "codegen_pypi": "0.10.4"})

    def test_0x_line_is_stable_only(self):
        self.assertEqual(check("0.1.26", "0.14.0-rc.1", "0.10.4").returncode, 1)
        self.assertEqual(check("0.1.26-rc.1", "0.13.1", "0.10.4").returncode, 1)


if __name__ == "__main__":
    unittest.main()
