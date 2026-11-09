#!/usr/bin/env python3
"""Read, verify and stage the reviewed engine parity bundle at one full commit."""
from __future__ import annotations

import binascii
import ctypes
import errno
import os
from pathlib import Path
import re
import sys
import tempfile
from dataclasses import dataclass

import release_pair as pair
from release_contract import (COMMIT, Invalid, Pending, canonical, decode,
                              match, require, sha256)

# Keep the release threshold isolated from the source-byte contract.
# Released 1.8, 1.9 and 1.10 manifests keep their historical shape and bytes.
PARITY_CAPABILITY_FROM = "1.11.0"
SOURCE_MANIFEST = "scripts/parity-bundle.json"
SOURCE_SCHEMA = "pineforge/engine-parity-source/v1"
DESCRIPTOR_SCHEMA = "pineforge/parity-descriptor/v1"
DESCRIPTOR_NAME = "PARITY-MANIFEST.json"
SUMS_NAME = "PARITY-SHA256SUMS"
APPROVED_FILES = (
    ("scripts/derive_corpus_feeds.py", "derive_corpus_feeds.py"),
    ("scripts/parity_paths.py", "parity_paths.py"),
    ("scripts/pf_release_run.py", "pf_release_run.py"),
    ("scripts/run_parity_jailed.py", "run_parity_jailed.py"),
    ("scripts/run_strategy.py", "run_strategy.py"),
    ("scripts/verify_corpus.py", "verify_corpus.py"),
)


class SourceManifestError(Invalid):
    """The fetched source declaration has invalid schema or canonical bytes."""


class SourceAllowlistError(Invalid):
    """The fetched source declaration is outside the reviewed payload list."""


def _safe_flat_name(name):
    return (isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is not None
            and ".." not in name)


def _safe_source_path(path):
    return (isinstance(path, str) and path.startswith("scripts/")
            and _safe_flat_name(path[len("scripts/"):]))


class _RegularContent:
    """Add type/path checks to the existing Client.file integrity primitive."""

    def __init__(self, client, path):
        self.client = client
        self.path = path

    def api(self, route):
        obj = self.client.api(route)
        require(obj is not None and not (isinstance(obj, dict) and obj.get("type") == "missing"),
                "parity source file missing")
        require(isinstance(obj, dict) and obj.get("type") == "file"
                and "target" not in obj and not obj.get("submodule_git_url")
                and obj.get("mode", "100644") in ("100644", "100755"),
                "parity source is not a regular file")
        require(obj.get("path") == self.path, "parity path is unsafe")
        match(COMMIT, obj.get("sha"))
        return obj


def read_content_blob(client, commit, path):
    """Fetch a nonempty regular file, pinned to commit, checking its Git blob."""
    from release_artifacts import Client, PRODUCERS, RequestError

    match(COMMIT, commit)
    require(_safe_source_path(path), "parity path is unsafe")
    try:
        raw = Client.file(_RegularContent(client, path), PRODUCERS["engine"], commit, path)
    except Pending as error:
        raise Invalid("parity source file missing") from error
    except RequestError as error:
        raise Invalid("parity bundle fetch failed; previous bundle preserved") from error
    except Invalid as error:
        if str(error) == "git blob integrity differs":
            raise Invalid("parity payload blob digest differs") from error
        if str(error) == "unexpected git content encoding":
            raise Invalid("malformed identity") from error
        raise
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise Invalid("malformed identity") from error
    except OSError as error:
        raise Invalid("parity bundle fetch failed; previous bundle preserved") from error
    require(bool(raw), "parity payload is empty")
    return raw


def read_source_manifest(client, commit):
    """Return the verified declaration and its exact, canonical source bytes."""
    raw = read_content_blob(client, commit, SOURCE_MANIFEST)
    try:
        manifest = decode(raw)
        require(canonical(manifest) == raw, "parity manifest is not canonical")
    except (Invalid, UnicodeError, ValueError, RecursionError) as error:
        raise SourceManifestError("parity manifest is not canonical") from error
    if (not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}
            or manifest["schema"] != SOURCE_SCHEMA or not isinstance(manifest["files"], list)):
        raise SourceManifestError("parity manifest payload differs from approved allowlist")

    rows = []
    destinations = set()
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {"source", "destination"}:
            raise SourceAllowlistError("parity manifest payload differs from approved allowlist")
        source, destination = row["source"], row["destination"]
        if not _safe_source_path(source) or not _safe_flat_name(destination):
            raise SourceAllowlistError("parity path is unsafe")
        if destination in destinations:
            raise SourceAllowlistError("parity manifest has duplicate destination")
        destinations.add(destination)
        rows.append((source, destination))
    if tuple(rows) != APPROVED_FILES:
        raise SourceAllowlistError("parity manifest payload differs from approved allowlist")
    return manifest, raw


def resolve_engine_commit(client, engine_tag, *, release_mode=True, proof_commit=None):
    from release_artifacts import PRODUCERS

    require(not (release_mode and proof_commit is not None),
            "proof commit override refused in release mode")
    try:
        version = pair.parse(engine_tag)
    except ValueError as error:
        raise Invalid("malformed identity") from error
    require(engine_tag == "v" + str(version), "malformed identity")
    commit = proof_commit if proof_commit is not None else client.tag_commit(PRODUCERS["engine"], engine_tag)
    return match(COMMIT, commit)


def sums_bytes(descriptor_raw, payload_sha256):
    rows = [(DESCRIPTOR_NAME, sha256(descriptor_raw))]
    rows.extend((name, payload_sha256[name]) for name in sorted(payload_sha256))
    return "".join(f"{digest}  {name}\n" for name, digest in rows).encode("ascii")


@dataclass(frozen=True)
class SourceBundle:
    engine_commit: str
    manifest_raw: bytes
    payload: dict[str, bytes]

    @property
    def payload_sha256(self):
        return {name: sha256(raw) for name, raw in self.payload.items()}

    @property
    def source_manifest_sha256(self):
        return sha256(self.manifest_raw)

    def files(self):
        descriptor_raw = canonical({
            "schema": DESCRIPTOR_SCHEMA,
            "source_manifest_sha256": self.source_manifest_sha256,
            "engine_commit": self.engine_commit,
            "payload_sha256": self.payload_sha256,
        })
        return {**self.payload, DESCRIPTOR_NAME: descriptor_raw,
                SUMS_NAME: sums_bytes(descriptor_raw, self.payload_sha256)}


def read_source_bundle(client, commit):
    """Read one commit-bound source bundle; W3 uses this for its second fetch."""
    manifest, raw = read_source_manifest(client, commit)
    payload = {row["destination"]: read_content_blob(client, commit, row["source"])
               for row in manifest["files"]}
    require(bool(payload), "empty parity source inventory")
    _verify_source_tree(client, commit, {
        SOURCE_MANIFEST: raw,
        **{row["source"]: payload[row["destination"]] for row in manifest["files"]},
    })
    return SourceBundle(commit, raw, payload)


def _verify_source_tree(client, commit, files):
    """Contents can follow symlinks; the tagged tree owns modes and blob IDs."""
    from release_artifacts import PRODUCERS, RequestError, git_blob

    try:
        tree = client.api(f"repos/{PRODUCERS['engine']}/git/trees/{commit}?recursive=1")
    except Pending as error:
        raise Invalid("parity source file missing") from error
    except (RequestError, OSError) as error:
        raise Invalid("parity bundle fetch failed; previous bundle preserved") from error
    require(isinstance(tree, dict) and isinstance(tree.get("tree"), list), "malformed identity")
    require(not tree.get("truncated"), "parity source inventory truncated")
    verified = set()
    for row in tree["tree"]:
        require(isinstance(row, dict) and isinstance(row.get("path"), str), "malformed identity")
        path = row["path"]
        if path not in files:
            continue
        require(path not in verified, "malformed identity")
        require(row.get("type") == "blob" and row.get("mode") in ("100644", "100755"),
                "parity source is not a regular file")
        match(COMMIT, row.get("sha"))
        require(git_blob(files[path]) == row["sha"], "parity payload blob digest differs")
        verified.add(path)
    require(verified == set(files), "parity source file missing")


def verify_bundle(bundle_root, source):
    """Compare freshly synced local bytes with a second tagged-source fetch."""
    root = Path(bundle_root)
    require(root.is_dir() and not root.is_symlink(), "parity source is not a regular file")
    entries = {path.name: path for path in root.iterdir()}
    expected = {DESCRIPTOR_NAME, SUMS_NAME, *source.payload}
    require(not (set(entries) - expected), "unapproved parity source file")
    require(set(entries) == expected, "parity source file missing")
    require(all(path.is_file() and not path.is_symlink() for path in entries.values()),
            "parity source is not a regular file")
    local = {name: path.read_bytes() for name, path in entries.items()}
    descriptor_raw = local[DESCRIPTOR_NAME]
    try:
        descriptor = decode(descriptor_raw)
    except Invalid as error:
        raise Invalid("parity source manifest differs from tagged source") from error
    require(isinstance(descriptor, dict)
            and set(descriptor) == {"schema", "source_manifest_sha256", "engine_commit", "payload_sha256"}
            and descriptor["schema"] == DESCRIPTOR_SCHEMA
            and canonical(descriptor) == descriptor_raw,
            "parity source manifest differs from tagged source")
    payload_hashes = descriptor["payload_sha256"]
    require(isinstance(payload_hashes, dict) and bool(payload_hashes), "empty parity source inventory")
    match(COMMIT, descriptor["engine_commit"])
    # Self-consistent descriptor/SUMS bytes cannot establish tagged-source
    # identity: compare them with the independent fetch's commit and payload.
    require(descriptor["engine_commit"] == source.engine_commit,
            "parity descriptor commit differs from engine tag")
    require(descriptor["source_manifest_sha256"] == source.source_manifest_sha256,
            "parity source manifest differs from tagged source")
    require(payload_hashes == source.payload_sha256, "parity source bytes differ")
    require(all(local[name] == raw for name, raw in source.payload.items()),
            "parity source bytes differ")
    require(local[SUMS_NAME] == sums_bytes(descriptor_raw, source.payload_sha256),
            "parity SUMS verification failed")
    return {
        "engine_commit": source.engine_commit,
        "source_manifest_sha256": source.source_manifest_sha256,
        "payload_sha256": source.payload_sha256,
        "descriptor_sha256": sha256(descriptor_raw),
        "sums_sha256": sha256(local[SUMS_NAME]),
    }


def _install_bundle(stage, destination):
    """Install a new directory, or atomically exchange it with the old one."""
    require(not destination.is_symlink(), "parity source is not a regular file")
    if not destination.exists():
        os.replace(stage, destination)
        return
    require(destination.is_dir(), "parity source is not a regular file")
    # Replacing a nonempty directory with os.replace is not atomic/portable.
    # Unsupported hosts refuse before changing the previous bundle.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        exchange = libc.renameat2
        exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(stage), -100, os.fsencode(destination), 2)  # AT_FDCWD, RENAME_EXCHANGE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        exchange = libc.renamex_np
        exchange.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(stage), os.fsencode(destination), 2)  # RENAME_SWAP
    else:
        raise OSError(errno.ENOTSUP, "atomic parity directory exchange unavailable")
    exchange.restype = ctypes.c_int
    if exchange(*args) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def sync_parity_bundle(client, engine_tag, destination, *, release_mode=True, proof_commit=None):
    """Publish-time sync: install only after the complete staged bundle verifies."""
    commit = resolve_engine_commit(client, engine_tag, release_mode=release_mode, proof_commit=proof_commit)
    try:
        source = read_source_bundle(client, commit)
        destination = Path(destination).absolute()
        require(not destination.is_symlink(), "parity source is not a regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".parity-stage-", dir=destination.parent,
                                         ignore_cleanup_errors=True) as temporary:
            stage = Path(temporary)
            for name, raw in source.files().items():
                (stage / name).write_bytes(raw)
            identity = verify_bundle(stage, source)
            _install_bundle(stage, destination)
        return identity
    except OSError as error:
        if error.errno in {
                errno.ENOSYS, errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            raise Invalid(
                "parity atomic directory exchange unsupported; previous bundle preserved"
            ) from error
        raise Invalid("parity bundle fetch failed; previous bundle preserved") from error
