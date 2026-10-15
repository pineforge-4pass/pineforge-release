#!/usr/bin/env python3
"""Validate plain HPO pins and require a registered Debian source mirror.

This helper only reads files. The workflow obtains dpkg inventories, GitHub
release metadata and the registered SOURCES.list before invoking `check`.
Registry rows are added only after the owner has attached the real bundles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TOOLCHAIN_NAMES = frozenset({
    "HPO_GCC_DEFAULTS_VERSION", "HPO_GCC_VERSION", "HPO_BINUTILS_VERSION",
    "HPO_EIGEN_VERSION", "HPO_CMAKE_VERSION", "HPO_MAKE_VERSION",
})
# Per-release artifact pins come from verified producer bindings, not the
# signed-box Debian toolchain file: argument -> (workflow output, binding field).
HPO_ARTIFACT_ARGS = {
    "HPO_WHEEL_SHA256": ("hpo_wheel_arg", "wheel_sha256"),
    "HPO_SDIST_SHA256": ("hpo_sdist_arg", "sdist_sha256"),
}
REGISTRY_HEADER = "set-id\trelease-tag\tn-pairs\tsources-list-sha256\tlist-asset"
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9.+-]*")
VERSION = re.compile(r"[A-Za-z0-9.+:~_-]+")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+:%~-]*")
RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?")
LIST_NAME = re.compile(r"set-[0-9]{2,}\.SOURCES\.list")
Pair = tuple[str, str]


class SourcesError(ValueError):
    """An invalid input or an incomplete source mirror; never a success."""


def hpo_artifact_args(pin: dict[str, str]) -> dict[str, str]:
    """Validate both bound digests before exposing any Docker build arguments."""
    outputs = {}
    for argument, (output, field) in HPO_ARTIFACT_ARGS.items():
        value = pin.get(field)
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise SourcesError("HPO artifact pin requires lowercase SHA-256 for " + argument)
        outputs[output] = f"{argument}={value}"
    return outputs


def toolchain_args(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    arguments = {}
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([A-Za-z0-9.+:~_-]*)", line)
        prefix = f"toolchain args line {number}: "
        if match is None:
            raise SourcesError(prefix + "expected plain NAME=VALUE with no spaces, quotes or shell syntax")
        name, value = match.groups()
        if name not in TOOLCHAIN_NAMES:
            raise SourcesError(prefix + "unknown toolchain argument: " + name)
        if name in arguments:
            raise SourcesError(prefix + "duplicate toolchain argument: " + name)
        if not value:
            raise SourcesError(prefix + "empty value for " + name)
        arguments[name] = value
    missing = TOOLCHAIN_NAMES - arguments.keys()
    if missing:
        raise SourcesError(f"toolchain args line {len(lines) + 1}: missing toolchain arguments: "
                           + ", ".join(sorted(missing)))
    return [f"{name}={value}" for name, value in arguments.items()]


def inventory_pairs(path: Path) -> frozenset[Pair]:
    pairs = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        columns = line.split("\t")
        prefix = f"{path}: inventory line {number}: "
        if len(columns) != 4 or any(not column.strip() for column in columns):
            raise SourcesError(prefix + "expected 4 non-empty columns")
        binary, binary_version, source, version = columns
        if (any(column != column.strip() for column in columns)
                or not SOURCE_NAME.fullmatch(source) or not VERSION.fullmatch(version)
                or not VERSION.fullmatch(binary_version) or any(char.isspace() for char in binary)):
            raise SourcesError(prefix + "invalid Debian package name or version")
        pairs.add((source, version))
    count = len({source for source, _ in pairs})
    if count < 50:
        raise SourcesError(f"{path}: inventory has {count} distinct source packages; need at least 50")
    return frozenset(pairs)


def canonical_pairs(pairs: frozenset[Pair]) -> str:
    return "".join(f"{source}\t{version}\n" for source, version in sorted(pairs))


def set_id(pairs: frozenset[Pair]) -> str:
    return hashlib.sha256(canonical_pairs(pairs).encode("utf-8")).hexdigest()


def pair_difference(left: frozenset[Pair], right: frozenset[Pair],
                    left_name: str, right_name: str) -> str:
    def preview(items):
        return "; ".join(f"{source}\t{version}" for source, version in sorted(items)[:5]) or "(none)"
    return (f"only in {left_name} (first 5): {preview(left - right)}; "
            f"only in {right_name} (first 5): {preview(right - left)}")


@dataclass(frozen=True)
class SourceSet:
    id: str
    release_tag: str
    n_pairs: int
    list_sha256: str
    list_asset: str


def registry_set(path: Path, wanted: str) -> SourceSet:
    rows = {}
    header_seen = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        prefix = f"registry line {number}: "
        if not header_seen:
            if line != REGISTRY_HEADER:
                raise SourcesError(prefix + "expected header: " + REGISTRY_HEADER)
            header_seen = True
            continue
        columns = line.split("\t")
        if len(columns) != 5 or any(not value for value in columns):
            raise SourcesError(prefix + "expected 5 non-empty columns")
        identity, tag, count, digest, asset = columns
        if not SHA256.fullmatch(identity) or not SHA256.fullmatch(digest):
            raise SourcesError(prefix + "set-id and SOURCES.list SHA-256 must be lowercase SHA-256 hex")
        if not RELEASE_TAG.fullmatch(tag):
            raise SourcesError(prefix + "release tag must be a v-prefixed version")
        if not re.fullmatch(r"[1-9][0-9]*", count):
            raise SourcesError(prefix + "n-pairs must be a positive integer")
        if not LIST_NAME.fullmatch(asset):
            raise SourcesError(prefix + "list-asset must name a set-NN.SOURCES.list asset")
        if identity in rows:
            raise SourcesError(prefix + "duplicate source set: " + identity)
        rows[identity] = SourceSet(identity, tag, int(count), digest, asset)
    if not header_seen:
        raise SourcesError("registry is missing its header")
    if wanted not in rows:
        raise SourcesError(f"source set {wanted} is not in registry {path}")
    return rows[wanted]


def locate(registry: Path, amd64: Path, arm64: Path) -> tuple[SourceSet, frozenset[Pair]]:
    left, right = inventory_pairs(amd64), inventory_pairs(arm64)
    if left != right:
        raise SourcesError("amd64 and arm64 source pair sets differ; "
                           + pair_difference(left, right, "amd64", "arm64"))
    row = registry_set(registry, set_id(left))
    if row.n_pairs != len(left):
        raise SourcesError(f"registry pair count {row.n_pairs} does not match image pair count {len(left)}")
    return row, left


def release_assets(path: Path) -> dict[str, dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SourcesError(f"release assets JSON is invalid at line {error.lineno}") from error
    items = document.get("assets") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise SourcesError("release assets JSON must contain an assets array")
    assets = {}
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise SourcesError(f"release asset {number}: missing asset name")
        name = item["name"]
        if name in assets:
            raise SourcesError("duplicate release asset: " + name)
        assets[name] = item
    return assets


def verify_asset(assets: dict[str, dict], name: str, size: int, digest: str) -> None:
    if name not in assets:
        raise SourcesError("missing release asset: " + name)
    item = assets[name]
    if type(item.get("size")) is not int or item["size"] != size:
        raise SourcesError("size mismatch for " + name)
    if item.get("digest") != "sha256:" + digest:
        raise SourcesError("digest mismatch for " + name)


def list_entries(data: bytes) -> list[tuple[str, int, str, str, str]]:
    entries = []
    seen = set()
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        columns = line.split()
        prefix = f"SOURCES.list line {number}: "
        if len(columns) != 5:
            raise SourcesError(prefix + "expected sha256  size  asset  source  version")
        digest, size, asset, source, version = columns
        if not SHA256.fullmatch(digest):
            raise SourcesError(prefix + "invalid SHA-256")
        if not re.fullmatch(r"[0-9]+", size):
            raise SourcesError(prefix + "size must be a non-negative integer")
        if not ASSET_NAME.fullmatch(asset) or LIST_NAME.fullmatch(asset):
            raise SourcesError(prefix + "invalid source asset name")
        if not SOURCE_NAME.fullmatch(source) or not VERSION.fullmatch(version):
            raise SourcesError(prefix + "invalid Debian source name or version")
        if asset in seen:
            raise SourcesError(prefix + "duplicate source asset: " + asset)
        seen.add(asset)
        entries.append((digest, int(size), asset, source, version))
    if not entries:
        raise SourcesError("SOURCES.list has no source assets")
    return entries


def check(registry: Path, amd64: Path, arm64: Path, assets_path: Path, sources_list: Path) -> str:
    row, pairs = locate(registry, amd64, arm64)
    if sources_list.name != row.list_asset:
        raise SourcesError("SOURCES.list filename does not match registry list-asset: " + row.list_asset)
    data = sources_list.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != row.list_sha256:
        raise SourcesError("SOURCES.list SHA-256 does not match registry")
    entries = list_entries(data)
    listed_pairs = frozenset((source, version) for _, _, _, source, version in entries)
    if pairs != listed_pairs:
        raise SourcesError("SOURCES.list pair set differs from image; "
                           + pair_difference(pairs, listed_pairs, "image", "list"))
    assets = release_assets(assets_path)
    verify_asset(assets, row.list_asset, len(data), row.list_sha256)
    for digest, size, name, _, _ in entries:
        verify_asset(assets, name, size, digest)
    return row.id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    toolchain = commands.add_parser("toolchain-args", help="validate and print the six plain HPO pins")
    toolchain.add_argument("file", type=Path)
    pairs = commands.add_parser("pairs", help="print canonical source pairs and their set-id")
    pairs.add_argument("inventory", type=Path)
    for name in ("locate", "check"):
        command = commands.add_parser(name, help="locate the registered bundle" if name == "locate"
                                      else "refuse an image without all its mirrored Debian sources")
        command.add_argument("--registry", type=Path, required=True)
        command.add_argument("--amd64", type=Path, required=True)
        command.add_argument("--arm64", type=Path, required=True)
        if name == "check":
            command.add_argument("--release-assets", type=Path, required=True)
            command.add_argument("--sources-list", type=Path, required=True,
                                 help="downloaded list asset named by the registry")
    args = parser.parse_args(argv)
    try:
        if args.command == "toolchain-args":
            output = "\n".join(toolchain_args(args.file)) + "\n"
        elif args.command == "pairs":
            found = inventory_pairs(args.inventory)
            output = canonical_pairs(found) + f"set-id={set_id(found)}\n"
        elif args.command == "locate":
            row, _ = locate(args.registry, args.amd64, args.arm64)
            output = (f"set-id={row.id}\nrelease-tag={row.release_tag}\nn-pairs={row.n_pairs}\n"
                      f"sources-list-sha256={row.list_sha256}\nlist-asset={row.list_asset}\n")
        else:
            identity = check(args.registry, args.amd64, args.arm64, args.release_assets, args.sources_list)
            output = f"set-id={identity}\n"
    except (SourcesError, OSError, UnicodeError) as error:
        print(f"release sources: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
