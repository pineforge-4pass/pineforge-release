#!/usr/bin/env python3
"""Validate native AMD64 diagnostic files; never execute another program.

The hosted job uses `verify OUT PRODUCT`. After it finishes, the operator supplies the
actual GET /repos/{owner}/{repo}/actions/jobs/{job_id} JSON with --job-json FILE.
Only that final file validation can set COMPLETE. This is diagnostic completion,
not release acceptance, source mirror coverage, ARM evidence or policy repair.
"""

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys


PRODUCT_SHA = "54d13b3809c4f321dfe94a58e46bee8c16f2fdfa"
PRODUCT_TREE = "fdf6a620757b5725afc140cf843a236dd6b9ed16"
REPOSITORY = "pineforge-4pass/pineforge-release"
REF = "refs/heads/diag/debian-inputs-proof"
GUARD = "02235ead051bdd9e6a53d74fc62048f58611c11469199b9febb31cf104f433c3"
STAGES = (
    "identity", "host", "inputs", "builder", "missing-ca", "hpo-build",
    "default-build", "default-image", "hpo-image", "hpo-licences",
    "hpo-study", "frozen-after",
)
INPUTS = (
    "docker/Dockerfile", "docker/debian-inputs.args", "docker/hpo-toolchain.args",
    "docker/GUARD-POLICY-SETS.pin", "docker/HARNESS-SHA256SUMS",
    "docker/LICENSE-SHA256SUMS", "docker/SOURCE-SETS.tsv",
    "scripts/release_sources.py", "tests/fixtures/release-smoke/study.json",
    "tests/fixtures/release-smoke/strategy.pine",
    "tests/fixtures/release-smoke/ohlcv.csv",
)
PRODUCER_ARGS = {
    "ENGINE_VERSION": "1.10.0", "CODEGEN_VERSION": "1.10.0",
    "HPO_VERSION": "1.10.0", "RELEASE_VERSION": "1.10.0",
    "RELEASE_GIT_SHA": PRODUCT_SHA, "GUARD_POLICY_SETS_SHA256": GUARD,
    "HPO_WHEEL_SHA256": "59abc767e8f7ac55ac3304283eca95c5afc68aaa27f041457593fc4a235f6417",
    "HPO_SDIST_SHA256": "5139a4b2cd47f0e4ec14cac6f58100dbe0274322dd659a2807c1c23c2c698737",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def raw(path, empty=False):
    require(path.is_file() and not path.is_symlink(), f"missing regular file: {path.name}")
    value = path.read_bytes()
    require(empty or bool(value), f"empty file: {path.name}")
    return value


def text(path, empty=False):
    return raw(path, empty).decode("utf-8")


def line(path):
    value = text(path)
    require(value.endswith("\n") and value.count("\n") == 1, f"expected one line: {path.name}")
    return value[:-1]


def iid(path):
    """Read a Buildx IID digest with EOF or exactly one terminal LF."""
    match = re.fullmatch(rb"(sha256:[0-9a-f]{64})\n?", raw(path))
    require(match is not None, f"invalid IID file: {path.name}")
    return match.group(1).decode("ascii")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def finite_float(value):
    number = float(value)
    require(math.isfinite(number), "non-finite JSON number")
    return number


def load(path):
    return json.loads(text(path), object_pairs_hook=unique,
                      parse_constant=invalid_constant, parse_float=finite_float)


def atomic_json(path, value):
    # Exclusive temporary file and replacement in the same directory.
    temporary = path.with_name(path.name + ".next")
    require(not path.is_symlink(), "refuse symlink output")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")
        stream.flush()
    temporary.replace(path)


def tsv(path):
    return unique(row.split("\t") for row in text(path).splitlines())


def integer(value, name):
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value), f"invalid integer: {name}")
    return int(value)


def cpu_set(value):
    require(re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", value), "invalid CPU mask")
    result = set()
    for item in value.split(","):
        bounds = item.split("-")
        first, last = int(bounds[0]), int(bounds[-1])
        require(0 <= first <= last < 1048576, "invalid CPU range")
        expanded = set(range(first, last + 1))
        require(not result.intersection(expanded), "duplicate CPU mask entries")
        result.update(expanded)
    return result


def resources(root):
    request = tsv(root / "resource-request.tsv")
    require(set(request) == {
        "cpus", "memory_gib", "cpuset", "host_cpus", "daemon_cpus",
        "host_memory_bytes", "available_memory_bytes", "daemon_memory_bytes",
        "host_allowed_cpus",
    }, "missing/extra resource observations")
    cpus = integer(request["cpus"], "cpus")
    memory = integer(request["memory_gib"], "memory_gib") * 1073741824
    require(2 <= cpus <= 8 and memory <= 99 * 1073741824, "resource parameter bounds")
    require(cpus <= min(integer(request[key], key) for key in ("host_cpus", "daemon_cpus")), "CPU request exceeds observed host")
    require(memory <= min(integer(request[key], key) for key in (
        "host_memory_bytes", "available_memory_bytes", "daemon_memory_bytes",
    )), "memory request exceeds observed host")
    selected = cpu_set(request["cpuset"])
    require(len(selected) == cpus and selected <= cpu_set(request["host_allowed_cpus"]), "CPU set exceeds observed host")
    observed = line(root / "builder-resources.txt").split()
    require(len(observed) == 4, "malformed Docker resource readback")
    require(cpu_set(observed[0]) == selected, "Docker CPU set differs")
    require(observed[1:] == [str(cpus * 100000), "100000", str(memory)], "Docker quota/memory differs")
    require(line(root / "builder-cpu.txt") == f"{cpus * 100000} 100000", "actual cgroup quota differs")
    require(cpu_set(line(root / "builder-cpuset.txt")) == selected, "actual cgroup CPU set differs")
    require(line(root / "builder-memory.txt") == str(memory), "actual cgroup memory differs")
    require(text(root / "buildkit.toml") == "[worker.oci]\n  max-parallelism = 1\n  gc = false\n", "worker configuration differs")
    return {"cpus": cpus, "memory_bytes": memory, "cpuset": sorted(selected),
            "worker_parallelism": 1, "cmake_parallelism": 2, "fixture_workers": 2,
            "host_observations": request}


def clock(value):
    require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value,
    ), "invalid actual UTC clock")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clocks(root):
    helper_start = line(root / "helper.started")
    helper_end = line(root / "helper.ended")
    start, end = clock(helper_start), clock(helper_end)
    require(end >= start and line(root / "helper.exit") == "0", "collector failed")
    previous = start
    result = {}
    for name in STAGES:
        first, last = line(root / (name + ".started")), line(root / (name + ".ended"))
        a, b = clock(first), clock(last)
        require(previous <= a <= b <= end, f"stage clocks out of order: {name}")
        require(line(root / (name + ".exit")) == "0", f"failed stage: {name}")
        result[name] = {"started_at": first, "completed_at": last, "elapsed_seconds": (b - a).total_seconds(), "exit_code": 0}
        previous = b
    return {"started_at": helper_start, "completed_at": helper_end,
            "elapsed_seconds": (end - start).total_seconds(), "stages": result}


def hashes(path):
    rows = []
    for entry in text(path).splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_+./-]+)", entry)
        require(match is not None, f"malformed hashes: {path.name}")
        digest, name = match.groups()
        require(not name.startswith("/") and ".." not in name, "unsafe hashed path")
        rows.append((name, digest))
    require(rows, f"empty hashes: {path.name}")
    return unique(rows)


def args_file(path):
    rows = []
    for entry in text(path).splitlines():
        if not entry.strip() or entry.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([A-Za-z0-9.+:@/~_-]+)", entry)
        require(match is not None, "invalid plain build argument")
        rows.append(match.groups())
    return unique(rows)


def inventory(root, target):
    pairs, binaries = set(), set()
    rows = text(root / (target + "-amd64.tsv")).splitlines()
    for row in rows:
        fields = row.split("\t")
        require(len(fields) == 4 and all(fields), "invalid installed package inventory")
        binary, binary_version, source, version = fields
        require(re.fullmatch(r"[a-z0-9][a-z0-9.+-]*(?::amd64)?", binary), "unexpected installed binary/architecture")
        require(binary not in binaries, "duplicate installed binary")
        binaries.add(binary)
        require(re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", source), "invalid installed source")
        require(all(re.fullmatch(r"[A-Za-z0-9.+:~_-]+", item) for item in (binary_version, version)), "invalid installed version")
        pairs.add((source, version))
    require(len({name for name, _ in pairs}) >= 50, "incomplete installed source inventory")
    canonical = "".join(f"{name}\t{version}\n" for name, version in sorted(pairs))
    identity = sha(canonical.encode())
    require(text(root / (target + "-source-pairs.txt")) == canonical + f"set-id={identity}\n", "source pairs do not match actual installed inventory")
    return {"binary_packages": len(rows), "source_pairs": len(pairs), "set_id": identity,
            "mirror_coverage": "UNPROVEN"}


def checked_hashes(root, product, target, kind, manifest):
    expected = hashes(product / manifest)
    actual = hashes(root / (target + "-" + kind + ".sha256"))
    require(actual == expected, f"installed {kind} hashes differ from frozen manifest")
    log = text(root / (target + "-" + kind + ".log")).splitlines()
    require(log == [f"{name}: OK" for name in expected], f"installed {kind} check incomplete")
    return {"files": len(actual), "manifest_sha256": sha(raw(product / manifest)),
            "installed_hashes_sha256": sha(raw(root / (target + "-" + kind + ".sha256")))}


def validate_job(path, run, collection):
    job = load(path)
    require(isinstance(job, dict), "job API JSON must be an object")
    require(type(job.get("id")) is int and job["id"] > 0, "missing API job id")
    require(type(job.get("run_id")) is int and str(job["run_id"]) == run["run_id"], "API run identity differs")
    require(type(job.get("run_attempt")) is int and str(job["run_attempt"]) == run["run_attempt"], "API attempt differs")
    require(job.get("head_sha") == run["controller"], "API controller differs")
    require(job.get("name") == "Frozen Debian inputs native AMD64", "wrong API job")
    require(job.get("status") == "completed" and job.get("conclusion") == "success", "actual hosted job did not complete successfully")
    require(job.get("url") == f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{job['id']}", "wrong API repository")
    labels = job.get("labels")
    require(isinstance(labels, list) and "ubuntu-24.04" in labels and "self-hosted" not in labels, "API runner labels differ")
    start, end = clock(job.get("started_at")), clock(job.get("completed_at"))
    require(start <= clock(collection["started_at"]) <= clock(collection["completed_at"]) <= end, "API job clocks do not enclose collection")
    steps = job.get("steps")
    require(isinstance(steps, list) and steps, "missing API step outcomes")
    required = {
        "Initialize incomplete diagnostic and guard execution", "Checkout controller",
        "Checkout frozen product", "Collect actual native AMD64 image execution",
        "Validate collected files", "Preserve curated diagnostic files",
    }
    by_name = unique((step["name"], step) for step in steps)
    require(required <= by_name.keys(), "missing actual hosted step")
    for step in steps:
        require(step.get("status") == "completed" and step.get("conclusion") == "success", "failed/skipped hosted step")
        a, b = clock(step.get("started_at")), clock(step.get("completed_at"))
        require(start <= a <= b <= end, "API step clocks outside job")
    return {"id": job["id"], "run_id": job["run_id"], "run_attempt": job["run_attempt"],
            "started_at": job["started_at"], "completed_at": job["completed_at"],
            "elapsed_seconds": (end - start).total_seconds(), "source": job["url"],
            "api_json_sha256": sha(raw(path)), "conclusion": job["conclusion"]}


def verify(root, product, job_path):
    state = load(root / "status.json")
    require(isinstance(state, dict) and type(state.get("schema")) is int
            and state["schema"] == 1 and state.get("status") == "INCOMPLETE", "status must start INCOMPLETE")
    run = tsv(root / "run.tsv")
    require(set(run) == {"repository", "ref", "event", "controller", "run_id", "run_attempt",
                         "runner_environment", "runner_os", "runner_arch"}, "invalid run observations")
    require((run["repository"], run["ref"], run["event"]) == (REPOSITORY, REF, "push"), "execution event differs")
    require((run["runner_environment"], run["runner_os"], run["runner_arch"]) == ("github-hosted", "Linux", "X64"), "runner differs")
    require(re.fullmatch(r"[0-9a-f]{40}", run["controller"]), "malformed controller")
    integer(run["run_id"], "run_id")
    integer(run["run_attempt"], "run_attempt")
    require(line(root / "controller.sha") == run["controller"], "controller checkout differs")
    require(re.fullmatch(r"[0-9a-f]{40}", line(root / "controller.tree")), "missing controller tree")
    controller_files = hashes(root / "controller-files.sha256")
    require(set(controller_files) == {
        ".github/workflows/debian-inputs-proof.yml", "scripts/debian_inputs_image_proof.sh",
        "scripts/debian_inputs_image_results.py",
    }, "missing controller file identities")
    control = Path(__file__).resolve().parent.parent
    for name, digest in controller_files.items():
        require(sha(raw(control / name)) == digest, f"controller file differs: {name}")
    for suffix in ("", "-after"):
        require(line(root / ("product" + suffix + ".sha")) == PRODUCT_SHA, "wrong frozen product commit")
        require(line(root / ("product" + suffix + ".tree")) == PRODUCT_TREE, "wrong frozen product tree")
    require(not raw(root / "product-before.log", empty=True) and not raw(root / "product-after.log", empty=True), "frozen product was modified")
    observed_inputs = hashes(root / "product-inputs.sha256")
    require(set(observed_inputs) == set(INPUTS), "missing/extra frozen input hashes")
    for name, digest in observed_inputs.items():
        require(sha(raw(product / name)) == digest, f"frozen file differs: {name}")
    require(line(root / "host.platform") == "x86_64" and line(root / "daemon.platform") == "linux/x86_64", "host/daemon is not native AMD64")
    collection = clocks(root)
    measured_resources = resources(root)
    require(load(root / "resources.json") == measured_resources, "resource check output differs")
    for emitted, original in (("debian.args", "docker/debian-inputs.args"), ("hpo.args", "docker/hpo-toolchain.args")):
        require(args_file(root / emitted) == args_file(product / original), "validated build arguments differ from product")
    arguments = text(root / "build-arguments.log").splitlines()
    expected_args = {**args_file(root / "debian.args"), **args_file(root / "hpo.args"), **PRODUCER_ARGS}
    require(len(arguments) == len(expected_args) * 2 and arguments[::2] == ["--build-arg"] * len(expected_args), "missing actual build arguments")
    require(unique(item.split("=", 1) for item in arguments[1::2]) == expected_args, "actual build arguments differ")
    ca_rc = integer(line(root / "missing-ca.rc"), "missing CA exit")
    require(ca_rc <= 255 and "secret debian_ca: not found" in text(root / "missing-ca.log"), "missing required CA did not fail specifically")
    for name in ("builder-create.log", "builder-bootstrap.log", "versions.log", "hpo-build.log", "default-build.log"):
        raw(root / name)
    plan = load(root / "study.json")
    require(raw(root / "study.json") == raw(product / "tests/fixtures/release-smoke/study.json"), "executed study differs from frozen study")
    require(isinstance(plan, dict), "invalid study")
    expected_trials = plan["sampler"]["trials"]
    require(type(expected_trials) is int and expected_trials > 0, "invalid study trial count")
    require(plan["execution"]["workers"] == 2 and type(plan["execution"]["workers"]) is int, "fixture worker bound differs")
    images = {}
    for target in ("default", "hpo"):
        built, observed = iid(root / (target + ".iid")), line(root / (target + ".id"))
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", built) and built == observed, "built/inspected image identities differ")
        require(line(root / (target + ".platform")) == "linux/amd64", "image platform differs")
        labels = load(root / (target + ".labels.json"))
        require(isinstance(labels, dict), "missing image labels")
        expected_labels = {
            "org.opencontainers.image.revision": PRODUCT_SHA,
            "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
            "org.opencontainers.image.version": "1.10.0",
            "io.pineforge.engine.version": "1.10.0", "io.pineforge.codegen.version": "1.10.0",
            "io.pineforge.guard.policy-sets-sha256": GUARD,
        }
        if target == "hpo":
            expected_labels["io.pineforge.hpo.version"] = "1.10.0"
        require(all(labels.get(key) == value for key, value in expected_labels.items()), "actual image source/producer labels differ")
        require(line(root / (target + ".guard")) == GUARD, "old guard carry differs")
        require(raw(root / (target + "-ca.log"), empty=True) == b"", "CA absence check had output")
        raw(root / (target + "-inventory.stderr"), empty=True)
        raw(root / (target + "-backtest.stderr"), empty=True)
        report = load(root / (target + "-backtest.json"))
        require(isinstance(report, dict) and report.get("engine") == "pineforge" and not report.get("error"), "actual backtest failed")
        trades = report.get("trades")
        require(isinstance(trades, list) and trades and all(isinstance(trade, dict) and trade for trade in trades), "actual backtest produced no valid trades")
        count = report["metrics"]["all"]["num_trades"]
        require(type(count) is int and count > 0, "actual backtest has no measured trades")
        images[target] = {
            "id": built, "platform": "linux/amd64", "labels": labels,
            "inventory": inventory(root, target),
            "harness": checked_hashes(root, product, target, "harness", "docker/HARNESS-SHA256SUMS"),
            "backtest": {"trades": len(trades), "num_trades": count,
                         "sha256": sha(raw(root / (target + "-backtest.json")))},
            "ca_mount_absent": True,
        }
    require(images["default"]["id"] != images["hpo"]["id"], "distinct targets have identical image IDs")
    images["hpo"]["licences"] = checked_hashes(root, product, "hpo", "licences", "docker/LICENSE-SHA256SUMS")
    study = load(root / "hpo-study.json")
    raw(root / "hpo-study.stderr", empty=True)
    require(isinstance(study, dict) and study.get("ok") is True and not study.get("error"), "actual HPO study failed")
    require(type(study.get("trials_completed")) is int and study["trials_completed"] == expected_trials, "actual HPO trial count differs from supplied study")
    require(study.get("full_parameter_coverage") is True, "actual HPO study lacks declared coverage")
    api_job = validate_job(job_path, run, collection) if job_path is not None else None
    result = {
        "schema": 1, "status": "COMPLETE" if api_job is not None else "INCOMPLETE",
        "scope": "frozen-product native AMD64 diagnostic", "execution_outputs_checked": True,
        "run": run, "controller_tree": line(root / "controller.tree"),
        "controller_files": hashes(root / "controller-files.sha256"),
        "product": {"commit": PRODUCT_SHA, "tree": PRODUCT_TREE, "input_sha256": observed_inputs},
        "resources": measured_resources, "collection_clocks": collection, "job_clocks": api_job,
        "images": images, "missing_ca_exit": ca_rc,
        "hpo_study": {"declared_trials": expected_trials, "trials_completed": study["trials_completed"],
                      "full_parameter_coverage": study["full_parameter_coverage"],
                      "plan_sha256": sha(raw(root / "study.json")),
                      "result_sha256": sha(raw(root / "hpo-study.json"))},
        "cache": {"hpo": "fresh no-cache build", "default": "only same-run owned builder stages may be reused",
                  "docker_layers_or_downloads_cold": "NOT_CLAIMED"},
        "guard_policy": {"value": GUARD, "status": "OLD_KNOWN_CARRY_NO_REPAIR"},
        "source_mirror_coverage": "UNPROVEN for AMD64 and ARM; pair inventory is not mirrored-source coverage",
        "needs": ([] if api_job is not None else ["operator completed-job API JSON"]) + [
            "independent review", "AMD64 and ARM source mirror coverage", "release acceptance",
        ],
    }
    atomic_json(root / "status.json", result)
    print(result["status"] + ": checked actual native AMD64 files; release/source acceptance remains outside this diagnostic")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resource_parser = commands.add_parser("resources")
    resource_parser.add_argument("out", type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("out", type=Path)
    verify_parser.add_argument("product", type=Path)
    verify_parser.add_argument("--job-json", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "resources":
            atomic_json(args.out / "resources.json", resources(args.out))
        else:
            verify(args.out, args.product, args.job_json)
    except (ValueError, OSError, UnicodeError, KeyError, TypeError, AttributeError) as error:
        print(f"native AMD64 diagnostic refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
