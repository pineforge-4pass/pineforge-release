"""PATH shims for the release dry run: curl, gh, docker and sleep.

They answer from the world's state directory ($HARNESS_STATE) and never touch
the network. Every call is appended to $HARNESS_STATE/actions.jsonl, so a
scenario can check what WOULD have been fetched, created, pushed or sent.
A call the shims do not model fails (exit 1) instead of pretending to work.

State files (all optional):
  http.json          {url: {"status": N, "body": str | "file": name}}; other URLs
                     answer 404, status 0 means "no answer" (curl prints 000)
  images.json        [image refs that exist in the registry]
  gh-releases.json   this repo's GitHub releases, newest first:
                     [{"tagName", "isDraft", "isPrerelease"}]
  gh-latest.json     {repo name: tag GitHub shows as Latest}
  releases-<repo>.json  REST `repos/<owner>/<repo>/releases` answer
  gh-release-list.fail  present: `gh release list` fails
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ["HARNESS_STATE"])
SELF_REPO = os.environ.get("GITHUB_REPOSITORY", "owner/repo").split("/")[-1]


def record(**entry):
    with open(STATE / "actions.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def say(tool, msg):
    print(f"[shim {tool}] {msg}", file=sys.stderr)


def load(name, default):
    path = STATE / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(name, value):
    (STATE / name).write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")


def parse(argv, valued):
    """(positional args, {option: value}, {flags}) for gh-style argv."""
    pos, opts, flags = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in valued:
            opts.setdefault(a, []).append(argv[i + 1])
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            key, value = a.split("=", 1)
            opts.setdefault(key, []).append(value)
        elif a.startswith("-"):
            flags.add(a)
        else:
            pos.append(a)
        i += 1
    return pos, opts, flags


def emit_json(data, query):
    """gh --json/-q: print raw strings from a jq filter, like gh does."""
    text = json.dumps(data)
    if not query:
        print(text)
        return 0
    proc = subprocess.run(["jq", "-r", query], input=text, text=True, capture_output=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def api_fields(opts):
    """Model gh's named object fields; refuse unmodeled arrays/files/placeholders.

    Both flag kinds build bracketed objects. Only -F/--field performs gh's
    magic conversion (Go Atoi, then true/false/null); -f/--raw-field stays text.
    """
    body, fields = {}, []
    for flag in ("-f", "--raw-field", "-F", "--field"):
        for argument in opts.get(flag, []):
            if "=" not in argument:
                raise ValueError("field requires a value")
            name, raw = argument.split("=", 1)
            if not re.fullmatch(r"[^\[\]=]+(?:\[[^\[\]=]+\])*", name):
                raise ValueError("unmodeled field path")
            value = raw
            if flag in {"-F", "--field"}:
                if raw.startswith("@") or re.search(r"\{(?:owner|repo|branch)\}", raw):
                    raise ValueError("unmodeled field file or placeholder")
                if re.fullmatch(r"[+-]?[0-9]+", raw):
                    integer = int(raw)
                    # gh uses a signed machine integer on the supported 64-bit hosts.
                    if -(2**63) <= integer < 2**63:
                        value = integer
                elif raw in {"true", "false", "null"}:
                    value = {"true": True, "false": False, "null": None}[raw]
            keys = name.replace("]", "").split("[")
            target = body
            for key in keys[:-1]:
                if key not in target:
                    target[key] = {}
                if not isinstance(target[key], dict):
                    raise ValueError("conflicting object field")
                target = target[key]
            if keys[-1] in target:
                raise ValueError("duplicate field")
            target[keys[-1]] = value
            fields.append({"flag": flag, "name": name, "raw": raw})
    return body, fields


# ---------------------------------------------------------------------- curl
def curl(argv):
    url = out = fmt = None
    fail = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-o", "--output", "-w", "--write-out", "--max-time", "--retry", "--retry-delay",
                 "--connect-timeout", "-H", "--header", "-X", "--request"):
            if a in ("-o", "--output"):
                out = argv[i + 1]
            elif a in ("-w", "--write-out"):
                fmt = argv[i + 1]
            i += 2
            continue
        if a.startswith(("http://", "https://")):
            url = a
        elif a == "--fail" or (re.fullmatch(r"-[A-Za-z]+", a) and "f" in a):
            fail = True
        i += 1
    if url is None:
        say("curl", f"unsupported call: curl {' '.join(argv)}")
        return 2
    hit = load("http.json", {}).get(url)
    status = hit["status"] if hit else 404
    record(tool="curl", url=url, status=status)
    say("curl", f"{url} -> {status:03d}")
    if status == 200 and out != "/dev/null":
        body = (STATE / hit["file"]).read_text(encoding="utf-8") if "file" in hit else hit.get("body", "")
        if out:
            Path(out).write_text(body, encoding="utf-8")
        else:
            sys.stdout.write(body)
    if fmt:
        sys.stdout.write(fmt.replace("%{http_code}", f"{status:03d}"))
    if status == 0:
        return 7   # could not connect
    return 22 if fail and status >= 400 else 0


# ------------------------------------------------------------------------ gh
def gh(argv):
    if argv[:2] == ["release", "list"]:
        _, opts, flags = parse(argv[2:], {"--limit", "-L", "--json", "-q", "--jq", "-R", "--repo"})
        if (STATE / "gh-release-list.fail").exists():
            record(tool="gh", action="release list", failed=True)
            say("gh", "release list -> HTTP 502 (state says so)")
            return 1
        rels = load("gh-releases.json", [])
        if "--exclude-drafts" in flags:
            rels = [r for r in rels if not r["isDraft"]]
        if "--exclude-pre-releases" in flags:
            rels = [r for r in rels if not r["isPrerelease"]]
        limit = int((opts.get("--limit") or opts.get("-L") or ["30"])[-1])
        rels = rels[:limit]
        record(tool="gh", action="release list", flags=sorted(flags))
        say("gh", f"release list {' '.join(sorted(flags))} -> {[r['tagName'] for r in rels]}")
        if "--json" not in opts:
            print("\n".join(r["tagName"] for r in rels))
            return 0
        fields = opts["--json"][-1].split(",")
        return emit_json([{k: r.get(k) for k in fields} for r in rels],
                         (opts.get("-q") or opts.get("--jq") or [None])[-1])
    if argv[:2] == ["release", "create"]:
        tag, rest = argv[2], argv[3:]
        notes, flags = "", []
        i = 0
        while i < len(rest):
            if rest[i] in ("--notes", "-n"):
                notes = rest[i + 1]
                i += 2
                continue
            flags.append(rest[i])
            i += 1
        rels = load("gh-releases.json", [])
        if any(r["tagName"] == tag for r in rels):
            say("gh", f"release create {tag} -> HTTP 422 (a release for this tag exists)")
            return 1
        pre = "--prerelease" in flags
        latest = next((f.split("=", 1)[1] for f in flags if f.startswith("--latest=")), None)
        rels.insert(0, {"tagName": tag, "isDraft": "--draft" in flags, "isPrerelease": pre})
        save("gh-releases.json", rels)
        if latest == "true" or (latest is None and not pre):
            save("gh-latest.json", {**load("gh-latest.json", {}), SELF_REPO: tag})
        record(tool="gh", action="release create", tag=tag, prerelease=pre, latest=latest,
               flags=flags, notes=notes)
        say("gh", f"WOULD CREATE RELEASE {tag} {' '.join(flags)}")
        return 0
    if argv[:2] == ["release", "view"]:
        pos, opts, _ = parse(argv[2:], {"-R", "--repo", "--json", "-q", "--jq"})
        repo = ((opts.get("-R") or opts.get("--repo") or [SELF_REPO])[-1]).split("/")[-1]
        tag = pos[0] if pos else load("gh-latest.json", {}).get(repo)
        record(tool="gh", action="release view", repo=repo, tag=tag)
        if not tag:
            say("gh", f"release view -R {repo} -> release not found")
            return 1
        return emit_json({"tagName": tag}, (opts.get("-q") or opts.get("--jq") or [None])[-1])
    if argv[:1] == ["api"]:
        try:
            pos, opts, flags = parse(argv[1:], {"-f", "-F", "--field", "--raw-field", "-q", "--jq",
                                             "-X", "--method", "-H", "--header", "--hostname"})
        except IndexError:
            record(tool="gh", unsupported=argv)
            say("gh", "unsupported api arguments: missing option value")
            return 1
        endpoint = pos[0] if pos else ""
        hostname = (opts.get("--hostname") or ["github.com"])[-1]
        supported = {"-f", "-F", "--field", "--raw-field", "-q", "--jq", "-X", "--method",
                     "-H", "--header", "--hostname"}
        if hostname != "github.com" or flags or len(pos) != 1 or set(opts) - supported:
            record(tool="gh", unsupported=argv)
            say("gh", "unsupported api hostname or arguments")
            return 1
        if re.fullmatch(r"repos/[^/]+/[A-Za-z0-9][A-Za-z0-9_.-]*/dispatches", endpoint):
            if set(opts) - {"-f", "-F", "--field", "--raw-field", "--hostname"}:
                record(tool="gh", unsupported=argv)
                say("gh", "unsupported dispatch arguments")
                return 1
            try:
                body, fields = api_fields(opts)
            except ValueError as error:
                record(tool="gh", unsupported=argv)
                say("gh", f"unsupported dispatch fields: {error}")
                return 1
            record(tool="gh", action="dispatch", repo=endpoint.split("/")[2], body=body, fields=fields,
                   endpoint=endpoint, hostname=hostname)
            say("gh", f"WOULD DISPATCH to {endpoint.split('/')[2]}: {json.dumps(body, sort_keys=True)}")
            return 0
        m = re.fullmatch(r"repos/[^/]+/([^/?]+)/releases(\?.*)?", endpoint)
        if m and not (set(opts) - {"-q", "--jq", "--hostname"}):
            data = load(f"releases-{m.group(1)}.json", None)
            record(tool="gh", action="api", endpoint=endpoint, found=data is not None)
            if data is None:
                say("gh", f"api {endpoint} -> HTTP 404")
                return 1
            say("gh", f"api {endpoint} -> {len(data)} releases")
            return emit_json(data, (opts.get("-q") or opts.get("--jq") or [None])[-1])
    record(tool="gh", unsupported=argv)
    say("gh", f"unsupported call: gh {' '.join(argv)}")
    return 1


# -------------------------------------------------------------------- docker
def docker(argv):
    if argv[:2] == ["manifest", "inspect"]:
        image = argv[-1]
        exists = image in load("images.json", [])
        record(tool="docker", action="manifest inspect", image=image, exists=exists)
        say("docker", f"manifest inspect {image} -> {'exists' if exists else 'missing'}")
        if exists:
            print("{}")
            return 0
        print(f"no such manifest: {image}", file=sys.stderr)
        return 1
    record(tool="docker", unsupported=argv)
    say("docker", f"unsupported call: docker {' '.join(argv)}")
    return 1


# --------------------------------------------------------------------- sleep
def sleep(argv):
    record(tool="sleep", seconds=" ".join(argv))
    return 0


def main(tool, argv):
    return {"curl": curl, "gh": gh, "docker": docker, "sleep": sleep}[tool](argv)
