#!/usr/bin/env python3
"""Resolve a published HPO wheel with the image's already installed codegen.

Build-only helper, shared with the real-wheel red/green test. The actual install
uses --target separately: pip's --target ignores installed distributions, so it
must not be used for this check. No package is installed by this helper.
"""
from __future__ import annotations

import argparse
import email.parser
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def check(args) -> None:
    # Debian bookworm installs pip in the codegen stage. Use its own PEP 440
    # parser; never compare a prerelease's PyPI spelling as a raw string.
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version

    wheel = args.hpo_wheel.resolve(strict=True)
    codegen_root = args.codegen_root.resolve(strict=True)
    report_path = args.report.resolve()
    if report_path.exists():
        raise ValueError("resolution report must be a new file, not a stale result")
    codegen_dist = getattr(args, "codegen_dist", "pineforge-codegen")
    installed = [dist for dist in importlib.metadata.distributions(path=[str(codegen_root)])
                 if _name(dist.metadata["Name"]) == codegen_dist]
    if len(installed) != 1:
        raise ValueError("the codegen target must contain exactly one installed " + codegen_dist)
    if Version(installed[0].version) != Version(args.codegen_version):
        raise ValueError("installed codegen does not match the requested CODEGEN_VERSION")

    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist()
                 if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("the HPO wheel must contain exactly one METADATA")
        metadata = email.parser.BytesParser().parsebytes(archive.read(names[0]))
    if _name(metadata["Name"]) != "pineforge-hpo":
        raise ValueError("the supplied wheel is not pineforge-hpo")
    if Version(metadata["Version"]) != Version(args.hpo_version):
        raise ValueError("the HPO wheel does not match the requested HPO_VERSION")
    if "transpile" not in metadata.get_all("Provides-Extra", []):
        raise ValueError("the HPO wheel must declare its transpile extra for resolution")

    # Do not inherit index credentials, pip configuration or a user site. The
    # published wheel and the installed codegen are sufficient for resolution;
    # --no-index ensures a network failure cannot resemble the old bound conflict.
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(codegen_root),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }
    pip = [sys.executable, "-m", "pip", "install"]
    # Check the flags of the pip actually provided by bookworm instead of
    # assuming that another host's pip supports them. Unsupported pip is a
    # setup failure, never the red test's expected dependency failure.
    help_result = subprocess.run(pip + ["--help"], cwd=wheel.parent, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, timeout=60)
    if help_result.returncode:
        raise ValueError("pip is unavailable for the resolution check")
    for option in ("--dry-run", "--report", "--break-system-packages"):
        if option not in help_result.stdout:
            raise ValueError("installed pip lacks the required " + option + " option")

    pin = codegen_dist + "==" + installed[0].version
    result = subprocess.run(
        pip + ["--break-system-packages", "--no-cache-dir", "--no-index",
               "--only-binary=:all:", "--dry-run", "--report", str(report_path),
               str(wheel) + "[transpile]", pin],
        cwd=wheel.parent, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60)
    if result.returncode:
        requirements = []
        for raw in metadata.get_all("Requires-Dist", []):
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": "transpile"}):
                requirements.append(raw)
        raise ValueError(
            "pip resolution failed with " + pin + "; wheel METADATA requires: "
            + ", ".join(requirements) + "\n" + result.stdout + result.stderr)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["version"] != "1" or not isinstance(report["install"], list):
        raise ValueError("pip returned an unsupported resolution report")
    hpo = []
    for item in report["install"]:
        candidate = item["metadata"]
        name = _name(candidate["name"])
        if name in ("pineforge-codegen", "tunaxis-codegen"):
            raise ValueError("pip would replace the installed codegen; refusing the HPO build")
        if name == "pineforge-hpo":
            hpo.append(candidate)
    if len(hpo) != 1 or Version(hpo[0]["version"]) != Version(args.hpo_version):
        raise ValueError("pip did not resolve exactly the requested HPO wheel")
    print("Tunaxis hpo: pip resolves with the installed codegen; no replacement is planned")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hpo-wheel", type=Path, required=True)
    parser.add_argument("--hpo-version", required=True)
    parser.add_argument("--codegen-root", type=Path, required=True)
    parser.add_argument("--codegen-version", required=True)
    parser.add_argument("--codegen-dist", choices=("pineforge-codegen", "tunaxis-codegen"), default="pineforge-codegen")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        check(args)
    except (OSError, ValueError, KeyError, TypeError, ImportError,
            zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
