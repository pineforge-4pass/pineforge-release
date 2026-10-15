#!/usr/bin/env python3
"""Immutable release-ready/v1 bytes and the section 3.2 dispatch contract.

The manifest is canonical UTF-8 JSON plus LF, with neither event_id nor manifest
(both contain its hash). Delivery attempts never change these bytes or run_id.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

import release_pair as pair

HUB = "pineforge-4pass/pineforge-release"
IMAGE = "ghcr.io/" + HUB
SCHEMA = "pineforge/release-ready/v1"
ASSET = "release-manifest.json"
ROLES = ("offline", "hosted", "application", "web", "data", "license")
SHA = r"[0-9a-f]{64}"
COMMIT = r"[0-9a-f]{40}"
MANIFEST_KEYS = {"schema", "release_version", "prerelease", "hub_commit", "versions", "image", "facts", "run_id"}


class Invalid(ValueError):
    pass


class Pending(Exception):
    """A real 404 or a legitimately unfinished prerequisite, never an API error."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False, "nonfinite JSON number"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Invalid("invalid JSON") from error


def keys(obj, names):
    require(type(obj) is dict and set(obj) == set(names), "unexpected object fields")


def match(pattern, value):
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, "malformed identity")
    return value


def positive(value):
    require(type(value) is int and 0 < value <= 2**53 - 1, "expected positive integer")


def version(value):
    v = pair.parse(value)
    require(str(v) == value, "version must be bare canonical semver")
    return v


def validate(manifest):
    keys(manifest, MANIFEST_KEYS)
    require(manifest["schema"] == SCHEMA, "unsupported release schema")
    v = version(manifest["release_version"])
    require(type(manifest["prerelease"]) is bool and manifest["prerelease"] == v.prerelease,
            "contradictory prerelease flag")
    match(COMMIT, manifest["hub_commit"])
    positive(manifest["run_id"])
    versions = manifest["versions"]
    keys(versions, ("engine", "codegen", "hpo"))
    e, c, h = (versions[name] for name in ("engine", "codegen", "hpo"))
    keys(e, ("version", "tag", "commit"))
    keys(c, ("version", "pypi_version", "commit", "sdist_sha256", "npm_version", "npm_integrity"))
    version(e["version"])
    cv = version(c["version"])
    require(e["tag"] == "v" + e["version"], "unexpected engine tag")
    require(c["pypi_version"] == cv.pep440 and c["npm_version"] == str(cv), "codegen delivery versions differ")
    match(SHA, c["sdist_sha256"])
    match(r"sha512-[A-Za-z0-9+/]{86}==", c["npm_integrity"])
    require(len(base64.b64decode(c["npm_integrity"][7:], validate=True)) == 64, "invalid npm integrity")
    for item in (e, c):
        match(COMMIT, item["commit"])
    if pair.hpo_pairing(v):
        keys(h, ("version", "package_version", "tag", "commit", "wheel_sha256", "sdist_sha256"))
        hv = version(h["version"])
        require(h["package_version"] == hv.pep440 and h["tag"] == "v" + hv.pep440, "unexpected HPO package spelling")
        match(COMMIT, h["commit"])
        for name in ("wheel_sha256", "sdist_sha256"):
            match(SHA, h[name])
    else:
        require(h is None, "HPO must be explicitly null before cutoff")
    pair.check(str(v), e["version"], c["version"], hpo=h["version"] if h else "")
    image = manifest["image"]
    keys(image, ("repository", "digest", "amd64_digest", "arm64_digest"))
    require(image["repository"] == IMAGE, "unexpected image repository")
    for name in ("digest", "amd64_digest", "arm64_digest"):
        match("sha256:" + SHA, image[name])
    require(len({image[name] for name in ("digest", "amd64_digest", "arm64_digest")}) == 3,
            "image index and architecture identities must be distinct")
    facts = manifest["facts"]
    keys(facts, ("commit", "path", "sha256", "release_key"))
    match(COMMIT, facts["commit"])
    match(SHA, facts["sha256"])
    require(facts["path"] == "facts/facts.json" and facts["release_key"] == str(v), "unexpected facts identity")
    return manifest


def payload(raw):
    manifest = validate(decode(raw))
    require(canonical(manifest) == raw, "noncanonical manifest bytes")
    digest = sha256(raw)
    return {**manifest, "event_id": "sha256:" + digest, "manifest": {"asset": ASSET, "sha256": digest}}


def validate_payload(event, raw):
    keys(event, MANIFEST_KEYS | {"event_id", "manifest"})
    require(event == payload(raw), "payload differs from immutable manifest")
    # Python treats true == 1, so validate the original fields as well.
    validate({key: event[key] for key in MANIFEST_KEYS})
    return event


def bind_facts(raw, commit, release, versions):
    # Import lazily so the byte contract is usable by consumers without jsonschema.
    from facts_validate import validate_facts
    facts = decode(raw)
    validate_facts(facts)
    match(COMMIT, commit)
    # Prerelease measurements are not exported under releases[rc]. Bind the
    # validated main snapshot bytes, without substituting them for stable proof.
    if version(release).prerelease:
        return {"commit": commit, "path": "facts/facts.json", "sha256": sha256(raw), "release_key": release}
    entry = facts["releases"].get(release)
    if entry is None:
        raise Pending("measured immutable release facts unavailable")
    scoreboard = entry["scoreboard"]
    for role in ("engine", "codegen"):
        require(scoreboard[role + "Commit"] == versions[role]["commit"], "release facts source commit differs")
    require(scoreboard["graded"] > 0, "release facts have no graded evidence")
    return {"commit": commit, "path": "facts/facts.json", "sha256": sha256(raw), "release_key": release}


def retain_facts(manifest, manifest_raw, installed_raw):
    """Consumers may keep newer verified canonical facts only with the same release entry."""
    a, b = decode(manifest_raw), decode(installed_raw)
    key = manifest["facts"]["release_key"]
    require(sha256(manifest_raw) == manifest["facts"]["sha256"], "release facts digest differs")
    if manifest["prerelease"] and key not in a["releases"]:
        require(installed_raw == manifest_raw, "prerelease without measured entry requires pinned facts bytes")
    else:
        require(key in a["releases"] and a["releases"][key] == b["releases"].get(key), "release facts entry changed")
    from facts_validate import validate_facts
    validate_facts(b)
    return sha256(installed_raw)
