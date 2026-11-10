#!/usr/bin/env python3
"""CI-only real image verification, by immutable child digest on both platforms."""
from __future__ import annotations

import argparse
from pathlib import Path

from release_artifacts import command
from release_contract import (IMAGE, Invalid, canonical, decode, parity_capable,
                              parity_child_digests, require, sha256, validate_parity)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/release-smoke"
RUNTIME_ROOTS = {
    "codegen": "/opt/pineforge/pycodegen",
    "hpo": "/opt/pineforge/hpo",
    "parity": "/opt/pineforge/parity",
}


def runtime_inventory_script():
    """Run inside each child; report observed bytes or an explicit refusal."""
    return "ROOTS = " + repr(RUNTIME_ROOTS) + "\n" + r'''import hashlib,json,pathlib,sys
class Refusal(Exception):
 pass
def refuse(message):
 raise Refusal(message)
def digest(raw):
 return hashlib.sha256(raw).hexdigest()
def canonical(value):
 return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
def verify():
 request=json.load(sys.stdin)
 expected=request['inventories']
 if set(expected)-set(ROOTS): refuse('unknown runtime inventory role')
 observed={}
 for role,files in expected.items():
  root=pathlib.Path(ROOTS[role])
  if role=='parity':
   if not root.is_dir() or root.is_symlink(): refuse('parity root is missing')
   try:
    entries={p.name:p for p in root.iterdir()}
    if set(entries)!=set(files) or any(p.is_symlink() or not p.is_file() for p in entries.values()):
     refuse('parity installed file inventory differs')
    data={name:p.read_bytes() for name,p in entries.items()}
   except OSError:
    refuse('parity installed file inventory differs')
   identity=request['parity']
   if not isinstance(identity,dict): refuse('parity child proof missing')
   descriptor={key:identity[key] for key in ('schema','source_manifest_sha256','engine_commit','payload_sha256')}
   descriptor_raw=data['PARITY-MANIFEST.json']
   descriptor_sha256=digest(descriptor_raw)
   if descriptor_raw!=canonical(descriptor) or descriptor_sha256!=identity['descriptor_sha256']:
    refuse('parity descriptor identity differs')
   payload={name:digest(data[name]) for name in identity['payload_sha256']}
   if payload!=identity['payload_sha256'] or any(not data[name] for name in payload):
    refuse('parity payload hash differs')
   rows=[('PARITY-MANIFEST.json',descriptor_sha256),*sorted(payload.items())]
   sums_raw=data['PARITY-SHA256SUMS']
   sums_sha256=digest(sums_raw)
   sums_expected=''.join(value+'  '+name+'\n' for name,value in rows).encode('ascii')
   if sums_raw!=sums_expected or sums_sha256!=identity['sums_sha256']:
    refuse('parity SUMS verification failed')
   inventory={name:digest(raw) for name,raw in data.items()}
   if inventory!=files: refuse('parity installed file inventory differs')
   observed['parity']={'identity':{**json.loads(descriptor_raw),
                                  'descriptor_sha256':descriptor_sha256,'sums_sha256':sums_sha256},
                       'inventory':inventory}
  else:
   actual={str(p.relative_to(root)):digest(p.read_bytes())
           for package in {name.split('/')[0] for name in files}
           for p in (root/package).rglob('*') if p.is_file() and (p.suffix=='.py' or p.name.startswith('hpo_failure_codes'))}
   if actual!=files: refuse('installed package bytes differ')
 return observed
# command() captures stderr and exposes only a generic failure. Return explicit
# refusals as data, which the parent rejects before authorizing smoke commands.
try:
 result=verify()
except Refusal as error:
 result={'error':str(error)}
print(json.dumps(result,sort_keys=True,separators=(',',':')))
'''


def selected_admit_runtime_script():
    """Verify the helper's exact layout, manifest and canonical description inside the image."""
    return r'''import hashlib,json,pathlib,stat,subprocess,sys
root=pathlib.Path("/opt/pineforge")
def require(condition,message):
 if not condition: sys.exit("ERROR: "+message)
def inventory(directory,expected):
 try:
  present=sorted(path.name for path in (root/directory).iterdir())
 except OSError:
  sys.exit("ERROR: helper "+directory+"/ inventory is unavailable")
 require(present==expected,"helper "+directory+"/ must hold exactly ["+" ".join(expected)+"]")
inventory("bin","HARNESS-SHA256SUMS bind_compiled_inventory.py entrypoint.sh pf_bars.py pf_sink.py pineforge-selected-admit pineforge_report_deadlines.py pineforge_report_supervise.py pineforge_sink_finalize.py pineforge_sink_to_json.py quota_columns.py request_feed_inventory.py run_execution_observer.py run_json.py run_phase_transport.py run_quota.py selected_window_plan.py selected_window_report.py".split())
inventory("share",["pineforge"])
inventory("share/pineforge",["pineforge-selected-admit.manifest.json"])
for name,mode in (("bin/pineforge-selected-admit",0o755),("share/pineforge/pineforge-selected-admit.manifest.json",0o644)):
 path=root/name
 require(not path.is_symlink() and path.is_file(),"helper file "+name+" must be a regular file without symlinks")
 info=path.stat()
 require((info.st_uid,info.st_gid)==(0,0),"helper file "+name+" must be owned by root:root")
 require(stat.S_IMODE(info.st_mode)==mode,"helper "+name+" must have mode "+format(mode,"o"))
binary=root/"bin/pineforge-selected-admit"
data=binary.read_bytes()
try:
 manifest=json.loads((root/"share/pineforge/pineforge-selected-admit.manifest.json").read_bytes())
 helper=manifest["helper"]
 require(isinstance(helper,dict),"helper manifest helper must be an object")
except (OSError,ValueError,KeyError,TypeError):
 sys.exit("ERROR: helper manifest validation failed")
require(helper.get("name")=="pineforge-selected-admit","helper manifest name must be pineforge-selected-admit")
require(helper.get("path")=="bin/pineforge-selected-admit","helper manifest path must be bin/pineforge-selected-admit")
require(helper.get("sha256")==hashlib.sha256(data).hexdigest(),"helper manifest sha256 differs from the helper")
require(helper.get("bytes")==str(len(data)),"helper manifest bytes differs from the helper")
del helper["bytes"]
del helper["sha256"]
expected=json.dumps(manifest,ensure_ascii=True,separators=(",",":")).encode("ascii")+b"\n"
described=subprocess.run([str(binary),"--describe"],stdin=subprocess.DEVNULL,capture_output=True,timeout=60)
require(described.returncode==0,"helper --describe must exit zero")
require(described.stdout==expected,"helper --describe differs from the canonical manifest projection")
print("helper: installed files and --describe match the manifest")
'''


def inspect_index(digest):
    from release_pipeline import Registry
    raw = Registry().raw(digest)
    require("sha256:" + sha256(raw) == digest, "image index bytes differ from digest")
    index = decode(raw)
    out = {"repository": IMAGE, "digest": digest}
    for arch in ("amd64", "arm64"):
        rows = [row for row in index["manifests"] if row.get("platform") == {"architecture": arch, "os": "linux"}
                or (row.get("platform", {}).get("architecture") == arch and row.get("platform", {}).get("os") == "linux")]
        require(len(rows) == 1, "expected unique platform in image index")
        out[arch + "_digest"] = rows[0]["digest"]
    require(out["amd64_digest"] != out["arm64_digest"], "architecture digests must differ")
    return out


def verify_runtime(image, versions, hub_commit, release_version, inventories):
    """Every command must exit zero and each JSON output must have nonempty real results."""
    require(type(inventories) is dict and not (set(inventories) - set(RUNTIME_ROOTS)),
            "unknown runtime inventory role")
    capable = parity_capable(release_version)
    identity = validate_parity(versions["engine"], inventories) if capable else None
    children = parity_child_digests(image) if capable else None
    proof = {}
    checked = []
    for arch in ("amd64", "arm64"):
        ref = IMAGE + "@" + image[arch + "_digest"]
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
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", str(FIXTURE) + ":/in:ro"]
        # Byte inventories bind the installed Python delivery paths to the
        # checked PyPI sdist/HPO wheel, rather than trusting version labels.
        inventory_raw = command(*base, "-i", "--entrypoint", "python3", ref, "-c", runtime_inventory_script(),
                                input=canonical({"inventories": inventories, "parity": identity}))
        observed = None
        if inventory_raw:
            try:
                observed = decode(inventory_raw)
            except Invalid as error:
                raise Invalid("parity child proof missing" if capable else "installed package bytes differ") from error
            require(type(observed) is dict, "parity child proof missing" if capable else "installed package bytes differ")
            require("error" not in observed, str(observed.get("error")))
        if capable:
            require(type(observed) is dict and set(observed) == {"parity"}
                    and type(observed["parity"]) is dict
                    and set(observed["parity"]) == {"identity", "inventory"}, "parity child proof missing")
            require(observed["parity"]["identity"] == identity, "parity descriptor identity differs")
            require(observed["parity"]["inventory"] == inventories["parity"], "parity installed file inventory differs")
        # The Dockerfile's build checks bind the harness, ABI and licenses;
        # recheck installed harness/license bytes on the pushed digest too.
        check = "cd /opt/pineforge/bin && sha256sum -c --strict HARNESS-SHA256SUMS"
        if hpo:
            check += " && cd /opt/pineforge/licenses && sha256sum -c --strict LICENSE-SHA256SUMS"
        command(*base, "--entrypoint", "bash", ref, "-euc", check)
        # The build compares both files with the verified engine tarball.
        # Bindings omit those asset hashes; recheck the runtime files and description here.
        command(*base, "--entrypoint", "/usr/bin/python3", ref, "-I", "-c", selected_admit_runtime_script())
        checked.append((arch, base, ref, hpo, observed))
    # Both installed child inventories must verify before either smoke starts.
    for arch, base, ref, hpo, observed in checked:
        report_raw = command(*base, ref)
        report = decode(report_raw)
        require(report.get("engine") == "pineforge" and not report.get("error"), "backtest failed")
        require(len(report.get("trades", [])) > 0 and report["metrics"]["all"]["num_trades"] > 0,
                "backtest fixture produced no trades")
        record = {"backtest_sha256": sha256(report_raw), "trades": len(report["trades"])}
        if capable:
            record["parity"] = {"image_digest": children[arch], **observed["parity"]}
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
    Path(args.output).write_bytes(canonical({**bound, "image": image, "verification": proof}))


if __name__ == "__main__":
    main()
