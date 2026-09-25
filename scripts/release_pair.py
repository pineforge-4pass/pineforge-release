#!/usr/bin/env python3
"""Release rules for the pineforge-release hub, run by the release workflows.

Versions are `X.Y.Z` or `X.Y.Z-{alpha,beta,rc}.N` (a leading `v` is dropped):
the semver subset with exactly one PEP 440 spelling, which the codegen's PyPI
release uses (`1.0.0-rc.1` is `1.0.0rc1` on PyPI). Order is semver precedence,
so `1.0.0-rc.1` < `1.0.0`; git's `--sort=v:refname` and `sort -V` get that
backwards.

Pairing: on the 0.x line engine and codegen keep independent versions (an
upstream event moves its component, the other stays frozen) and the hub
patch-bumps its own VERSION. From 1.0.0 on they share one version, prerelease
included: an event whose partner has not published that version yet waits,
a partner that has released a different new version is a mismatch that fails,
and the hub release version is the pair's.

Commands print key=value lines on stdout (for $GITHUB_OUTPUT); notes and
errors go to stderr, and a broken rule exits 1:

  info VERSION                bare, line (legacy | pair), prerelease, pypi
  latest-tag [--stable]       highest version tag of those on stdin
  latest-pep440               highest PyPI (PEP 440) version on stdin, semver spelling
  decide ...                  handle-upstream's action for one upstream event
  check --release R --engine E --codegen C [--newest-stable TAG]
                              publish.yml's rule for the image it builds
"""
from __future__ import annotations

import argparse
import functools
import re
import sys
from dataclasses import dataclass
from typing import Iterable, List, Optional

_VERSION_RE = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(alpha|beta|rc)\.(0|[1-9][0-9]*))?"
)
_PEP440_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)(0|[1-9][0-9]*))?"
)
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}
_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}
_FROM_PEP440 = {"a": "alpha", "b": "beta", "rc": "rc"}


class RuleError(Exception):
    """A release rule refused the request; the message names the versions."""


@functools.total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: Optional[str] = None  # alpha | beta | rc
    pre_num: int = 0

    @property
    def _key(self) -> tuple:
        # A release outranks every prerelease of the same X.Y.Z.
        tail = (1, 0, 0) if self.pre is None else (0, _PRE_RANK[self.pre], self.pre_num)
        return (self.major, self.minor, self.patch) + tail

    def __lt__(self, other: "Version") -> bool:
        return self._key < other._key

    @property
    def prerelease(self) -> bool:
        return self.pre is not None

    @property
    def line(self) -> str:
        return "pair" if self.major >= 1 else "legacy"

    @property
    def pep440(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.pre is None else f"{core}{_PEP440_PRE[self.pre]}{self.pre_num}"

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.pre is None else f"{core}-{self.pre}.{self.pre_num}"


def parse(text: str) -> Version:
    m = _VERSION_RE.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(
            f"rejected version {text!r}: expected X.Y.Z or X.Y.Z-{{alpha,beta,rc}}.N "
            "(for example 1.0.0-rc.1)")
    major, minor, patch, pre, pre_num = m.groups()
    return Version(int(major), int(minor), int(patch), pre, int(pre_num or 0))


def parse_pep440(text: str) -> Version:
    """A PyPI version in the one PEP 440 spelling parse() maps to (1.0.0rc1)."""
    m = _PEP440_RE.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"not a X.Y.Z[a|b|rcN] PyPI version: {text!r}")
    major, minor, patch, pre, pre_num = m.groups()
    return Version(int(major), int(minor), int(patch), _FROM_PEP440.get(pre), int(pre_num or 0))


def latest_pep440(versions: Iterable[str]) -> Optional[Version]:
    best = None
    for raw in versions:
        try:
            version = parse_pep440(raw.strip())
        except ValueError:
            continue
        if best is None or version > best:
            best = version
    return best


def _flag(value: bool) -> str:
    return "true" if value else "false"


def latest_tag(tags: Iterable[str], stable: bool = False) -> Optional[str]:
    best = None
    for raw in tags:
        tag = raw.strip()
        try:
            version = parse(tag)
        except ValueError:
            continue
        if stable and version.prerelease:
            continue
        if best is None or version > best[0]:
            best = (version, tag)
    return best[1] if best else None


def decide(component: str, version: str, prerelease_flag: str, prev_engine: str,
           prev_codegen: str, current_release: str, other_published: bool,
           image_exists: bool, force: bool, partner_latest: str = "") -> tuple:
    """Return (outputs, note) for one upstream event; raise RuleError to refuse it.

    partner_latest is the other component's newest published version (semver,
    "" if unknown); it only matters when that component has not published this
    event's version.
    """
    v = parse(version)
    if prerelease_flag not in ("", "true", "false"):
        raise RuleError(f"bad client_payload.prerelease {prerelease_flag!r} (expected true or false)")
    if prerelease_flag and (prerelease_flag == "true") != v.prerelease:
        raise RuleError(f"client_payload.prerelease={prerelease_flag} contradicts version {v}")
    if not prev_engine or not prev_codegen:
        raise RuleError(f"could not resolve frozen component (engine={prev_engine!r} "
                        f"codegen={prev_codegen!r})")
    ep, cp = parse(prev_engine), parse(prev_codegen)
    cur = parse(current_release)
    landed_pair = ep.line == "pair" or cp.line == "pair"
    if landed_pair and ep != cp:
        raise RuleError(f"landed pins engine {ep} + codegen {cp} break the pairing rule; "
                        "fix the latest release tag before releasing")

    other = "codegen" if component == "engine" else "engine"
    moved = (v, cp) if component == "engine" else (ep, v)  # today's coupled bump
    dprev = ep if component == "engine" else cp
    other_prev = cp if component == "engine" else ep

    def same_or_bump(e: Version, c: Version) -> str:
        if (e, c) == (ep, cp):
            return "noop" if image_exists else "republish"
        return "bump"

    out = {"line": v.line, "eprev": str(ep), "cprev": str(cp), "release": "",
           "prerelease": "false", "awaiting": "", "retag": "false"}
    if v.line == "legacy":
        if landed_pair:
            raise RuleError(
                f"refusing {component} {v}: from 1.0.0 on engine and codegen share one version, "
                f"and this event would pair engine {moved[0]} + codegen {moved[1]}")
        if v.prerelease:
            raise RuleError(f"refusing {component} {v}: the 0.x line is stable-only; "
                            "prereleases start at 1.0.0-rc.1")
        if cur.line != "legacy" or cur.prerelease:
            raise RuleError(f"refusing {component} {v}: VERSION {cur} is not a stable 0.x hub "
                            "version (is a 1.0 pair release half-done?); 0.x events patch-bump "
                            "a 0.x VERSION only")
        if v < dprev and not force:
            raise RuleError(f"refusing downgrade {component} {dprev} -> {v} "
                            "(set client_payload.force=true to override)")
        e, c = moved
        out.update(mode=same_or_bump(e, c), engine=str(e), codegen=str(c))
        note = f"mode={out['mode']} (engine {ep}->{e}, codegen {cp}->{c}; VERSION {cur})"
        return out, note

    # From 1.0.0 on: the release version is the pair's, so a landed pair only
    # moves forward (a forced event cannot re-release an older pair).
    if landed_pair and v < ep:
        raise RuleError(f"refusing {component} {v} below the landed pair {ep}, even forced: "
                        "from 1.0.0 on the release version is the pair's, so this would move "
                        "the hub backwards; roll back by re-pointing image tags instead")
    if not landed_pair and v < dprev and not force:
        raise RuleError(f"refusing downgrade {component} {dprev} -> {v} "
                        "(set client_payload.force=true to override)")
    completes = other_prev == v or other_published
    # VERSION is the landed release; it may equal this event's version only
    # when this pair's release commit reached main but its tag did not.
    retag = cur == v and v != ep and completes
    if not retag:
        if landed_pair and cur != ep:
            raise RuleError(f"VERSION {cur} does not match the landed pair engine {ep} + "
                            f"codegen {cp}; if main carries a release commit for {cur} without "
                            f"its tag, send an event for {cur} again to tag it")
        if not landed_pair and (cur.line != "legacy" or cur.prerelease):
            raise RuleError(f"VERSION {cur} has no release tag: a pair release is half-done "
                            f"(main carries its commit, tag v{cur} is missing); send an "
                            f"event for {cur} again to tag it")
    if completes:
        out.update(mode="bump" if retag else same_or_bump(v, v), engine=str(v), codegen=str(v),
                   release=str(v), prerelease=_flag(v.prerelease), retag=_flag(retag))
        channel = "prerelease" if v.prerelease else "stable"
        if retag:
            note = (f"mode=bump (retag): main already carries release v{v} (engine {v} + "
                    f"codegen {v}) without its tag; tagging it")
        elif out["mode"] == "bump":
            note = (f"mode=bump: pair engine {v} + codegen {v} -> release v{v} ({channel}); "
                    f"landed pair was engine {ep} + codegen {cp}")
        else:
            image = "image exists" if image_exists else "image missing"
            note = f"mode={out['mode']}: pair engine {v} + codegen {v} already released as v{v} ({image})"
        return out, note
    if partner_latest:
        partner = parse(partner_latest)
        # A 0.x partner release the hub never recorded is not a 1.0 partner.
        if partner.line == "pair" and partner > other_prev and partner != v:
            pair = f"engine {v} + codegen {partner}" if component == "engine" else \
                f"engine {partner} + codegen {v}"
            raise RuleError(f"mismatched pair: {pair} are both newer than the landed pair "
                            f"(engine {ep} + codegen {cp}); from 1.0.0 on engine and codegen "
                            "must release the same version, prerelease included")
    e, c = moved
    out.update(mode="wait", engine=str(e), codegen=str(c), prerelease=_flag(v.prerelease),
               awaiting=other)
    note = (f"mode=wait: {component} {v} recorded; {other} {v} is not published yet "
            f"(landed pair engine {ep} + codegen {cp}). Nothing is built; the {other} "
            "release event completes the pair.")
    return out, note


def check(release: str, engine: str, codegen: str, newest_stable: str = "") -> dict:
    """The pair rule for the image publish.yml builds. `moving` is true only for
    the newest stable release (by semver): `latest` and X.Y never go backwards."""
    r, e, c = parse(release), parse(engine), parse(codegen)
    if "pair" in (r.line, e.line, c.line):
        if e != c:
            raise RuleError(f"refusing to publish engine {e} + codegen {c}: from 1.0.0 on "
                            "engine and codegen share one version, prerelease included")
        if r != e:
            raise RuleError(f"refusing to publish release {r} for the pair engine {e} + "
                            f"codegen {c}: from 1.0.0 on the release version is the pair's")
        line = "pair"
    else:
        if r.prerelease or e.prerelease or c.prerelease:
            raise RuleError(f"refusing to publish release {r} (engine {e} + codegen {c}): "
                            "the 0.x line is stable-only")
        line = "legacy"
    moving = not r.prerelease and bool(newest_stable) and parse(newest_stable) == r
    return {"line": line, "prerelease": _flag(r.prerelease), "codegen_pypi": c.pep440,
            "moving": _flag(moving)}


def _emit(pairs: dict) -> None:
    for key, value in pairs.items():
        print(f"{key}={value}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info")
    p_info.add_argument("version")
    p_latest = sub.add_parser("latest-tag")
    p_latest.add_argument("--stable", action="store_true", help="skip prereleases")
    sub.add_parser("latest-pep440")
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--component", required=True, choices=["engine", "codegen"])
    p_decide.add_argument("--version", required=True)
    p_decide.add_argument("--prerelease-flag", default="")
    p_decide.add_argument("--prev-engine", default="")
    p_decide.add_argument("--prev-codegen", default="")
    p_decide.add_argument("--current-release", required=True)
    p_decide.add_argument("--other-published", choices=["true", "false"], default="false")
    p_decide.add_argument("--image-exists", choices=["true", "false"], default="false")
    p_decide.add_argument("--force", default="false", help="only the literal 'true' forces")
    p_decide.add_argument("--partner-latest", default="",
                          help="the other component's newest published version, if known")
    p_check = sub.add_parser("check")
    p_check.add_argument("--release", required=True)
    p_check.add_argument("--engine", required=True)
    p_check.add_argument("--codegen", required=True)
    p_check.add_argument("--newest-stable", default="", help="the newest stable release tag")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "info":
            v = parse(args.version)
            _emit({"bare": str(v), "line": v.line, "prerelease": _flag(v.prerelease),
                   "pypi": v.pep440})
        elif args.cmd == "latest-tag":
            tag = latest_tag(sys.stdin, stable=args.stable)
            if tag:
                print(tag)
        elif args.cmd == "latest-pep440":
            best = latest_pep440(sys.stdin)
            if best:
                print(best)
        elif args.cmd == "decide":
            out, note = decide(args.component, args.version, args.prerelease_flag,
                               args.prev_engine, args.prev_codegen, args.current_release,
                               args.other_published == "true", args.image_exists == "true",
                               args.force == "true", args.partner_latest)
            _emit(out)
            print(note, file=sys.stderr)
        else:
            _emit(check(args.release, args.engine, args.codegen, args.newest_stable))
    except (RuleError, ValueError) as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
