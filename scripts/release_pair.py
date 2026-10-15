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
and the hub release version is the pair's. HPO_PAIRING_FROM is the single
cutoff: by semver precedence, hpo joins at that version, including that rc and
every later version. Then all three components must share the release version,
an event waits for every missing partner, and the tag must carry an explicit hpo pin.
Hpo events may use either spelling; decisions and tag pins use semver, while
its GitHub release tags, wheel assets and build argument use PEP 440.

Commands print key=value lines on stdout (for $GITHUB_OUTPUT); notes and
errors go to stderr, and a broken rule exits 1:

  info VERSION                bare, line (legacy | pair), prerelease, pypi
  latest-tag [--stable]       highest version tag of those on stdin
  latest-pep440 [--tags]      highest PEP 440 version on stdin, semver spelling
  decide ...                  handle-upstream's action for one upstream event
  check --release R --engine E --codegen C [--hpo H] [--newest-stable TAG]
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
HPO_PAIRING_FROM = "1.6.0-rc.1"
_COMPONENTS = ("engine", "codegen", "hpo")


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


def hpo_version(version: str, *, to_semver: bool = False) -> str:
    """Map a hub version to hpo's PEP 440 spelling, or normalize an hpo event.

    Incoming events accept the existing semver subset or its canonical PEP 440
    spelling, with an optional leading v. Keep the codegen mapping in Version.
    """
    if not to_semver:
        return parse(version).pep440
    try:
        parsed = parse(version)
    except ValueError as semver_error:
        try:
            parsed = parse_pep440(version.removeprefix("v") if isinstance(version, str) else version)
        except ValueError:
            raise semver_error from None
    return str(parsed)


def hpo_pairing(version: Version) -> bool:
    """One cutoff for the decision, publish check and workflow `info` output."""
    return version >= parse(HPO_PAIRING_FROM)


def _component(component: str, version: Version) -> None:
    if component not in _COMPONENTS:
        raise RuleError(f"unexpected component {component!r}")
    if component == "hpo" and not hpo_pairing(version):
        raise RuleError(f"refusing hpo {version}: hpo joins the pairing at {HPO_PAIRING_FROM}")


def _triple_pin(r: Version, e: Version, c: Version, raw_hpo: str, context: str) -> Version:
    """Validate all pins together so an error names every version, including hpo."""
    pins = f"release {r} + engine {e} + codegen {c}"
    if not raw_hpo:
        raise RuleError(f"{context}: {pins} is missing hpo=; hpo joins the pairing at {HPO_PAIRING_FROM}")
    try:
        h = parse(raw_hpo)
    except ValueError as err:
        raise RuleError(f"{context}: {pins} + invalid hpo {raw_hpo!r}: {err}") from err
    if not r == e == c == h:
        raise RuleError(f"{context}: {pins} + hpo {h} break the pairing rule; from "
                        f"{HPO_PAIRING_FROM} on release, engine, codegen and hpo must share "
                        "one version, prerelease included")
    return h


def latest_pep440(versions: Iterable[str], *, tags: bool = False) -> Optional[Version]:
    """Order canonical PEP 440 releases; --tags also accepts a leading v."""
    best = None
    for raw in versions:
        text = raw.strip()
        try:
            version = parse_pep440(text.removeprefix("v") if tags else text)
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


def _decide_triple(component: str, v: Version, ep: Version, cp: Version,
                   hp: Optional[Version], cur: Version, other_published: bool,
                   image_exists: bool, partner_latest: str,
                   partners_published: Optional[dict], partners_latest: Optional[dict]) -> tuple:
    prev = {"engine": ep, "codegen": cp, "hpo": hp}
    published, latest = dict(partners_published or {}), dict(partners_latest or {})
    # Retain the old facts for the engine/codegen partner; they never supply hpo.
    if component in ("engine", "codegen"):
        other = "codegen" if component == "engine" else "engine"
        published.setdefault(other, other_published)
        latest.setdefault(other, partner_latest)
    if (set(published) | set(latest)) - set(_COMPONENTS):
        raise RuleError("unexpected component in partner facts")
    if any(type(value) is not bool for value in published.values()):
        raise RuleError("partner publication facts must be booleans")
    partners = [name for name in _COMPONENTS if name != component]
    missing = [name for name in partners if prev[name] != v and not published.get(name, False)]
    landed = f"engine {ep} + codegen {cp} + hpo {hp or '(not paired yet)'}"
    landed_pair = ep.line == "pair" or cp.line == "pair"
    if landed_pair and v < ep:
        raise RuleError(f"refusing {component} {v} below the landed pair {ep}, even forced: "
                        f"the release version is the pair's ({landed})")
    retag = cur == v and v != ep and not missing
    if not retag:
        if landed_pair and cur != ep:
            raise RuleError(f"VERSION {cur} does not match the landed pair {landed}; "
                            f"if main carries its release commit without its tag, send an "
                            f"event for {cur} again to tag it")
        if not landed_pair and (cur.line != "legacy" or cur.prerelease):
            raise RuleError(f"VERSION {cur} has no release tag: a pair release is half-done; "
                            f"send an event for {cur} again to tag it")
    out = {"line": v.line, "eprev": str(ep), "cprev": str(cp), "hprev": str(hp) if hp else "",
           "release": "", "prerelease": _flag(v.prerelease), "awaiting": "", "retag": _flag(retag)}
    if not missing:
        mode = ("noop" if image_exists else "republish") if ep == cp == hp == v else "bump"
        out.update(mode="bump" if retag else mode, release=str(v),
                   **{name: str(v) for name in _COMPONENTS})
        note = (f"mode={out['mode']}{' (retag)' if retag else ''}: "
                f"engine {v} + codegen {v} + hpo {v} -> release v{v}; landed {landed}")
        return out, note
    mismatched = {}
    for name in missing:
        if not latest.get(name):
            continue
        partner = parse(hpo_version(latest[name], to_semver=True) if name == "hpo" else latest[name])
        # A pre-cutoff hpo release cannot participate; old engine/codegen
        # releases still count as mismatches if they advanced since the tag.
        participates = hpo_pairing(partner) if name == "hpo" else partner.line == "pair"
        if participates and (prev[name] is None or partner > prev[name]) and partner != v:
            mismatched[name] = partner
    if mismatched:
        candidates = {name: v if name == component or name not in missing else
                      mismatched.get(name, prev[name]) for name in _COMPONENTS}
        pins = " + ".join(f"{name} {candidates[name] or '(pending)'}" for name in _COMPONENTS)
        raise RuleError(f"mismatched pair: {pins}; landed {landed}; from {HPO_PAIRING_FROM} on "
                        "engine, codegen and hpo must release the same version, prerelease included")
    out.update(mode="wait", awaiting=",".join(missing),
               **{name: str(prev[name]) if prev[name] is not None else "" for name in _COMPONENTS})
    out[component] = str(v)
    for name in partners:
        if name not in missing:
            out[name] = str(v)
    awaiting = ", ".join(f"{name} {hpo_version(str(v)) if name == 'hpo' else str(v)}" for name in missing)
    event_version = hpo_version(str(v)) if component == "hpo" else str(v)
    return out, (f"mode=wait: {component} {event_version} recorded; waiting for {awaiting}; "
                 f"landed {landed}. Nothing is built or published.")


def decide(component: str, version: str, prerelease_flag: str, prev_engine: str,
           prev_codegen: str, current_release: str, other_published: bool,
           image_exists: bool, force: bool, partner_latest: str = "", *, prev_hpo: str = "",
           partners_published: Optional[dict] = None, partners_latest: Optional[dict] = None,
           candidate_exists: bool = False) -> tuple:
    """Return (outputs, note) for one upstream event; raise RuleError to refuse it.

    partner_latest is the other component's newest published version (semver,
    "" if unknown); it only matters when that component has not published this
    event's version. From HPO_PAIRING_FROM, partners_published and partners_latest
    map component names to their publication facts. The original arguments and
    outputs are unchanged below the cutoff; hpo facts there are ignored.
    """
    v = parse(hpo_version(version, to_semver=True) if component == "hpo" else version)
    _component(component, v)
    if prerelease_flag not in ("", "true", "false"):
        raise RuleError(f"bad client_payload.prerelease {prerelease_flag!r} (expected true or false)")
    if prerelease_flag and (prerelease_flag == "true") != v.prerelease:
        raise RuleError(f"client_payload.prerelease={prerelease_flag} contradicts version {v}")
    if not prev_engine or not prev_codegen:
        raise RuleError(f"could not resolve frozen component (engine={prev_engine!r} "
                        f"codegen={prev_codegen!r})")
    ep, cp = parse(prev_engine), parse(prev_codegen)
    cur = parse(current_release)
    # A stored verified candidate owns completion even before fixed image tags
    # exist. The scheduled finalizer, rather than another build, finishes it.
    image_exists = image_exists or candidate_exists
    landed_pair = ep.line == "pair" or cp.line == "pair"
    hp = _triple_pin(ep, ep, cp, prev_hpo, "landed pins") \
        if hpo_pairing(ep) or hpo_pairing(cp) else None
    if landed_pair and ep != cp:
        raise RuleError(f"landed pins engine {ep} + codegen {cp} break the pairing rule; "
                        "fix the latest release tag before releasing")

    if hpo_pairing(v):
        return _decide_triple(component, v, ep, cp, hp, cur, other_published,
                              image_exists, partner_latest, partners_published, partners_latest)

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


def check(release: str, engine: str, codegen: str, pin: str = "", *, hpo: Optional[str] = None,
          newest_stable: Optional[str] = None, hpo_wheel_digest: str = "") -> dict:
    """The pair rule for the image publish.yml builds. `moving` is true only for
    the newest stable release (by semver): `latest` and X.Y never go backwards.

    The fourth positional argument retains its original newest-stable meaning
    below HPO_PAIRING_FROM; from the cutoff it is the required hpo pin. Use the
    named arguments to pass hpo and newest_stable independently at any version.
    """
    r, e, c = parse(release), parse(engine), parse(codegen)
    paired = hpo_pairing(r)
    if hpo is None:
        hpo = pin if paired else ""
    if newest_stable is None:
        newest_stable = "" if paired else pin
    h = _triple_pin(r, e, c, hpo, "refusing to publish") if paired else None
    if h is not None:
        line = "pair"
    elif "pair" in (r.line, e.line, c.line):
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
    out = {"line": line, "prerelease": _flag(r.prerelease), "codegen_pypi": c.pep440,
           "moving": _flag(moving)}
    if h is not None:
        pep440 = hpo_version(str(h))
        out.update(hpo=str(h), hpo_pep440=pep440, target="hpo", hpo_version_arg=f"HPO_VERSION={pep440}")
        if hpo_wheel_digest:
            digest = hpo_wheel_digest.removeprefix("sha256:")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise RuleError(f"refusing to publish hpo {h}: invalid wheel SHA256 digest {hpo_wheel_digest!r}")
            out["hpo_wheel_arg"] = f"HPO_WHEEL_SHA256={digest}"
    return out


def _emit(pairs: dict) -> None:
    for key, value in pairs.items():
        print(f"{key}={value}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info")
    p_info.add_argument("version")
    p_info.add_argument("--component", choices=_COMPONENTS, default="",
                        help="refuse a component event that cannot participate at this version")
    p_latest = sub.add_parser("latest-tag")
    p_latest.add_argument("--stable", action="store_true", help="skip prereleases")
    p_pep440 = sub.add_parser("latest-pep440")
    p_pep440.add_argument("--tags", action="store_true", help="read PEP 440 GitHub release tags")
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--component", required=True, choices=_COMPONENTS)
    p_decide.add_argument("--version", required=True)
    p_decide.add_argument("--prerelease-flag", default="")
    p_decide.add_argument("--prev-engine", default="")
    p_decide.add_argument("--prev-codegen", default="")
    p_decide.add_argument("--prev-hpo", default="")
    p_decide.add_argument("--current-release", required=True)
    p_decide.add_argument("--other-published", choices=["true", "false"], default="false")
    p_decide.add_argument("--image-exists", choices=["true", "false"], default="false")
    p_decide.add_argument("--candidate-exists", choices=["true", "false"], default="false")
    p_decide.add_argument("--force", default="false", help="only the literal 'true' forces")
    p_decide.add_argument("--partner-latest", default="",
                          help="the other component's newest published version, if known")
    for component in _COMPONENTS:
        p_decide.add_argument(f"--{component}-published", choices=["true", "false"])
        p_decide.add_argument(f"--{component}-latest", default="")
    p_check = sub.add_parser("check")
    p_check.add_argument("--release", required=True)
    p_check.add_argument("--engine", required=True)
    p_check.add_argument("--codegen", required=True)
    p_check.add_argument("--hpo", default="")
    p_check.add_argument("--hpo-wheel-digest", default="", help="optional GitHub Release asset SHA256 digest")
    p_check.add_argument("--newest-stable", default="", help="the newest stable release tag")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "info":
            v = parse(hpo_version(args.version, to_semver=True) if args.component == "hpo" else args.version)
            if args.component:
                _component(args.component, v)
            out = {"bare": str(v), "line": v.line, "prerelease": _flag(v.prerelease), "pypi": v.pep440}
            if hpo_pairing(v):
                out["hpo_pairing"] = "true"
                out["hpo_pep440"] = hpo_version(str(v))
            _emit(out)
        elif args.cmd == "latest-tag":
            tag = latest_tag(sys.stdin, stable=args.stable)
            if tag:
                print(tag)
        elif args.cmd == "latest-pep440":
            best = latest_pep440(sys.stdin, tags=args.tags)
            if best:
                print(best)
        elif args.cmd == "decide":
            published = {name: getattr(args, f"{name}_published") == "true" for name in _COMPONENTS
                         if getattr(args, f"{name}_published") is not None}
            latest = {name: getattr(args, f"{name}_latest") for name in _COMPONENTS
                      if getattr(args, f"{name}_latest")}
            out, note = decide(args.component, args.version, args.prerelease_flag,
                               args.prev_engine, args.prev_codegen, args.current_release,
                               args.other_published == "true", args.image_exists == "true",
                               args.force == "true", args.partner_latest, prev_hpo=args.prev_hpo,
                               partners_published=published, partners_latest=latest,
                               candidate_exists=args.candidate_exists == "true")
            _emit(out)
            print(note, file=sys.stderr)
        else:
            _emit(check(args.release, args.engine, args.codegen, hpo=args.hpo, newest_stable=args.newest_stable,
                        hpo_wheel_digest=args.hpo_wheel_digest))
    except (RuleError, ValueError) as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
