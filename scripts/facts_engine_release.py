"""Select the engine README release identified by VERSION and public facts."""
from __future__ import annotations

import re
from dataclasses import dataclass

import facts_validate
from facts_validate import InputError, require


@dataclass(frozen=True)
class EngineReleaseSelection:
    hub_commit: str
    facts_sha256: str
    release_version: str
    release_tag: str
    source_url: str


def select_engine_release(
    facts_raw: bytes,
    version_raw: bytes,
    *,
    hub_commit: str,
    expected_facts_sha256: str,
) -> EngineReleaseSelection:
    """Bind the exact VERSION release to the verified original facts bytes."""
    require(type(facts_raw) is bytes, "facts input must be bytes")
    require(type(version_raw) is bytes, "VERSION input must be bytes")
    require(type(hub_commit) is str
            and re.fullmatch(r"[0-9a-f]{40}", hub_commit) is not None,
            "invalid hub commit")
    require(type(expected_facts_sha256) is str
            and re.fullmatch(r"[0-9a-f]{64}", expected_facts_sha256) is not None,
            "invalid expected facts SHA-256")

    facts_sha256 = facts_validate.digest(facts_raw)
    require(facts_sha256 == expected_facts_sha256, "facts SHA-256 mismatch")
    facts = facts_validate.validate_facts(facts_validate.decode(facts_raw))

    require(len(version_raw) <= 256, "VERSION exceeds size limit")
    version_bytes = version_raw[:-1] if version_raw.endswith(b"\n") else version_raw
    require(re.fullmatch(rb"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?",
                         version_bytes) is not None, "invalid VERSION")
    version = version_bytes.decode("ascii")
    try:
        release = facts["releases"][version]
    except KeyError:
        raise InputError("selected release is missing") from None
    return EngineReleaseSelection(
        hub_commit=hub_commit,
        facts_sha256=facts_sha256,
        release_version=version,
        release_tag="v" + version,
        source_url=release["source"],
    )
