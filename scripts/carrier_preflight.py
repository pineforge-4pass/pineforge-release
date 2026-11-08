"""Build the release stage and check carrier access without publication."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys

out = Path(sys.argv[1]).resolve()
request = {
    "head": "83580ac78ec5e3ea17ee7032ca08469549193db9",
    "tree": "5ff3da85e0551e197a7685ad45efb9e0029b17e8",
    "engine_version": "1.10.0",
    "engine_commit": "59f98ba80e69a624f97ada11935e1bc973fd0e19",
    "codegen_version": "1.10.0",
    "guard_policy_sets_sha256": "02235ead051bdd9e6a53d74fc62048f58611c11469199b9febb31cf104f433c3"
}
root = Path.cwd().resolve()
for key, width in (("head", 40), ("tree", 40), ("engine_commit", 40), ("guard_policy_sets_sha256", 64)):
    if re.fullmatch(r"[0-9a-f]{%d}" % width, request[key]) is None:
        raise ValueError("invalid build input identity: " + key)
if request["engine_version"] != "1.10.0" or request["codegen_version"] != "1.10.0":
    raise ValueError("build input differs from the published pair")
if request["engine_commit"] != "59f98ba80e69a624f97ada11935e1bc973fd0e19":
    raise ValueError("build input differs from the approved engine source")
identity = subprocess.run(["git", "rev-parse", "HEAD", "HEAD^{tree}"], capture_output=True, text=True, check=True).stdout.splitlines()
if identity != [request["head"], request["tree"]]:
    raise ValueError("checkout differs from approved source")
status = subprocess.run(["git", "status", "--porcelain=v1"], capture_output=True, text=True, check=True).stdout
if status:
    raise ValueError("build requires an initially clean source checkout")
pin = Path("docker/GUARD-POLICY-SETS.pin").read_text().strip()
if pin != request["guard_policy_sets_sha256"]:
    raise ValueError("committed guard policy input differs")
protected_names = ("docker/Dockerfile", "scripts/parity_bundle.py", "scripts/release_smoke.py", "scripts/sync-harness.sh", "docker/GUARD-POLICY-SETS.pin")
protected = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in protected_names}
sys.path.insert(0, str(root / "scripts"))
from parity_bundle import sync_parity_bundle
from release_artifacts import Client, PRODUCERS
from release_contract import canonical
from release_smoke import runtime_inventory_script

client = Client()
tag = "v" + request["engine_version"]
if client.tag_commit(PRODUCERS["engine"], tag) != request["engine_commit"]:
    raise ValueError("released engine tag differs before preparation")
parity_identity = sync_parity_bundle(client, tag, Path("docker/parity"))
if parity_identity["engine_commit"] != request["engine_commit"]:
    raise ValueError("synced parity source differs from approved release")
harness_env = os.environ.copy()
harness_env.pop("PF_ENGINE_REF", None)
with (out / "harness-sync.log").open("xb") as log:
    sync = subprocess.run(["bash", "scripts/sync-harness.sh", request["engine_version"]], env=harness_env, stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False)
if sync.returncode:
    print(json.dumps({"phase": "input-preparation", "harness_exit": sync.returncode}))
    raise SystemExit(sync.returncode)
if client.tag_commit(PRODUCERS["engine"], tag) != request["engine_commit"]:
    raise ValueError("released engine tag changed during preparation")
parity_root = Path("docker/parity")
staged = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in parity_root.iterdir() if path.is_file() and not path.is_symlink()}
descriptor_raw = (parity_root / "PARITY-MANIFEST.json").read_bytes()
descriptor = json.loads(descriptor_raw)
descriptor_hash = hashlib.sha256(descriptor_raw).hexdigest()
if descriptor_hash != parity_identity["descriptor_sha256"]:
    raise ValueError("staged descriptor bytes differ from verified identity")
expected_identity = {**descriptor, "descriptor_sha256": descriptor_hash, "sums_sha256": parity_identity["sums_sha256"]}
iid_file = out / "image.id"
print(json.dumps({"phase": "build-start", "head": request["head"], "tree": request["tree"], "engine_tag": tag, "engine_commit": request["engine_commit"], "descriptor_sha256": descriptor_hash, "platform": "linux/amd64", "target": "release"}), flush=True)
with (out / "build.log").open("xb") as log:
    build = subprocess.run(["docker", "build", "--progress=plain", "--platform", "linux/amd64", "--target", "release", "--iidfile", str(iid_file),
                            "--build-arg", "ENGINE_VERSION=" + request["engine_version"],
                            "--build-arg", "CODEGEN_VERSION=" + request["codegen_version"],
                            "--build-arg", "GUARD_POLICY_SETS_SHA256=" + pin,
                            "--build-arg", "PARITY_CARRIER=1",
                            "--build-arg", "PARITY_DESCRIPTOR_SHA256=" + descriptor_hash,
                            "--build-arg", "RELEASE_GIT_SHA=" + request["head"],
                            "--file", "docker/Dockerfile", "."], stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False)
if build.returncode:
    print(json.dumps({"phase": "build-failed", "exit": build.returncode, "log": "build.log"}))
    raise SystemExit(build.returncode)
image_id = iid_file.read_text().strip()
if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
    raise ValueError("missing actual built image identity")
inspected = subprocess.run(["docker", "image", "inspect", image_id], capture_output=True, text=True, check=True)
actual = json.loads(inspected.stdout)[0]
image = {"id": actual["Id"], "os": actual["Os"], "architecture": actual["Architecture"], "user": actual["Config"]["User"]}
if image != {"id": image_id, "os": "linux", "architecture": "amd64", "user": "10001"}:
    raise ValueError("actual built image platform/default user differs")
installed_paths = ["/opt/pineforge/parity/" + name for name in sorted(staged)]
with (out / "default-user-listing.log").open("xb") as log:
    listing = subprocess.run(["docker", "run", "--network=none", "--platform", "linux/amd64", "--cidfile", str(out / "listing-container.id"), "--entrypoint", "ls", image_id, "-ldn", "/opt/pineforge/parity", *installed_paths], stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
if listing.returncode:
    print(json.dumps({"phase": "default-user-listing-failed", "exit": listing.returncode}))
    raise SystemExit(listing.returncode)
inventory_request = canonical({"inventories": {"parity": staged}, "parity": expected_identity})
with (out / "default-user-inventory.log").open("xb") as log:
    observed_run = subprocess.run(["docker", "run", "--network=none", "--platform", "linux/amd64", "--cidfile", str(out / "inventory-container.id"), "--interactive", "--entrypoint", "python3", image_id, "-c", runtime_inventory_script()], input=inventory_request, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=False)
if observed_run.returncode:
    print(json.dumps({"phase": "default-user-inventory-failed", "exit": observed_run.returncode}))
    raise SystemExit(observed_run.returncode)
observed = json.loads((out / "default-user-inventory.log").read_bytes())
if observed != {"parity": {"identity": expected_identity, "inventory": staged}}:
    raise ValueError("real default-user installed-byte inventory differs or refused")
if {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in protected_names} != protected:
    raise ValueError("protected source changed during normal input preparation/build")
report = {"scope": "release-stage carrier copy and default-user access with published 1.10.0 inputs; excludes HPO compatibility and publication", "head": request["head"], "tree": request["tree"], "image": image, "engine_tag": tag, "engine_commit": request["engine_commit"], "codegen_version": request["codegen_version"], "guard_policy_sets_sha256": pin, "parity_identity": expected_identity, "staged_inventory": staged, "protected_source": protected, "exits": {"harness_sync": sync.returncode, "build": build.returncode, "default_user_listing": listing.returncode, "default_user_inventory": observed_run.returncode}}
(out / "image-proof.json").write_bytes(canonical(report))
print(json.dumps({"phase": "actual-build-and-default-user-checks-completed", "image": image, "exits": report["exits"]}), flush=True)
