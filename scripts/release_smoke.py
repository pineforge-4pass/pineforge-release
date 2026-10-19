#!/usr/bin/env python3
"""CI-only real image verification, by immutable child digest on both platforms."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from release_artifacts import command
from release_contract import IMAGE, IMAGES, decode, require, sha256, validate_image

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/release-smoke"


def inspect_index(digest):
    from release_pipeline import Registry
    raw = Registry().raw(digest)
    require("sha256:" + sha256(raw) == digest, "image index bytes differ from digest")
    index = decode(raw)
    out = {"repository": IMAGE, "repositories": list(IMAGES), "digest": digest}
    for arch in ("amd64", "arm64"):
        rows = [row for row in index["manifests"] if row.get("platform") == {"architecture": arch, "os": "linux"}
                or (row.get("platform", {}).get("architecture") == arch and row.get("platform", {}).get("os") == "linux")]
        require(len(rows) == 1, "expected unique platform in image index")
        out[arch + "_digest"] = rows[0]["digest"]
    require(out["amd64_digest"] != out["arm64_digest"], "architecture digests must differ")
    return out


def verify_runtime(image, versions, hub_commit, release_version, inventories):
    """Every command must exit zero and each JSON output must have nonempty real results."""
    validate_image(image)
    proof = {}
    for arch in ("amd64", "arm64"):
        ref = image["repository"] + "@" + image[arch + "_digest"]
        command("docker", "pull", "--platform", "linux/" + arch, ref)
        labels = decode(command("docker", "image", "inspect", ref))[0]["Config"]["Labels"]
        for name, expected in (("org.opencontainers.image.revision", hub_commit),
                               ("org.opencontainers.image.version", "v" + release_version),
                               ("io.pineforge.engine.version", versions["engine"]["version"]),
                               ("io.pineforge.codegen.version", versions["codegen"]["version"])):
            require(labels.get(name) == expected, "candidate image label differs")
        hpo = versions["hpo"]
        if hpo:
            require(labels.get("io.pineforge.hpo.version") == hpo["package_version"], "HPO label differs")
        base = ["docker", "run", "--rm", "--network=none", "--platform", "linux/" + arch,
                "-v", str(FIXTURE) + ":/in:ro"]
        # Byte inventories bind the installed Python delivery paths to the
        # checked PyPI sdist/HPO wheel, rather than trusting version labels.
        script = '''import hashlib,json,pathlib,sys
expected=json.load(sys.stdin)
for role,files in expected.items():
 root=pathlib.Path('/opt/pineforge/pycodegen' if role=='codegen' else '/opt/pineforge/hpo')
 actual={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
         for package in {name.split('/')[0] for name in files}
         for p in (root/package).rglob('*') if p.is_file() and (p.suffix=='.py' or p.name.startswith('hpo_failure_codes'))}
 if actual!=files: raise SystemExit('installed package bytes differ')
'''
        command(*base, "-i", "--entrypoint", "python3", ref, "-c", script,
                input=json.dumps(inventories).encode())
        # The Dockerfile's build checks bind the harness, ABI and licenses;
        # recheck installed harness/license bytes on the pushed digest too.
        check = "cd /opt/pineforge/bin && sha256sum -c --strict HARNESS-SHA256SUMS"
        if hpo:
            check += " && cd /opt/pineforge/licenses && sha256sum -c --strict LICENSE-SHA256SUMS"
        command(*base, "--entrypoint", "bash", ref, "-euc", check)
        report_raw = command(*base, ref)
        report = decode(report_raw)
        require(report.get("engine") == "pineforge" and not report.get("error"), "backtest failed")
        require(len(report.get("trades", [])) > 0 and report["metrics"]["all"]["num_trades"] > 0,
                "backtest fixture produced no trades")
        record = {"backtest_sha256": sha256(report_raw), "trades": len(report["trades"])}
        if hpo:
            hpo_raw = command(*base, ref, "hpo", "run", "/in/study.json", "--engine-root", "/opt/pineforge")
            result = decode(hpo_raw)
            require(result.get("ok") is True and result.get("trials_completed") == 9
                    and result.get("full_parameter_coverage") is True, "HPO study smoke failed")
            record["hpo_sha256"] = sha256(hpo_raw)
            record["hpo_trials"] = 9
        proof[arch] = record
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    from release_contract import match, SHA
    match("sha256:" + SHA, args.digest)
    bound = decode(Path(args.bindings).read_bytes())
    image = inspect_index(args.digest)
    proof = verify_runtime(image, bound["versions"], bound["hub_commit"], bound["release_version"], bound["inventories"])
    from release_contract import canonical
    Path(args.output).write_bytes(canonical({**bound, "image": image, "verification": proof}))


if __name__ == "__main__":
    main()
