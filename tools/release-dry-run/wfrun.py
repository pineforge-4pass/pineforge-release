#!/usr/bin/env python3
"""Run one job of a GitHub Actions workflow locally, as a dry run.

Every `run:` step of the REAL workflow file runs under bash (>= 4.4, as on
GitHub's runners) with a mocked github / secrets / needs / matrix context, and
`if:` conditions are evaluated. `uses:` steps are stubbed: checkout = the
prepared work tree, App token = a dummy, build / push / publish / deploy = a
logged WOULD. docker/metadata-action runs for real (node) when a checkout of
it is given, so image tags are the action's own. docker, curl, gh and sleep
resolve to the shims next to this file (state in --state, calls logged to
<state>/actions.jsonl); git may only use local paths and ignores the caller's
git config. Only an allowlist of the caller's environment reaches a step.

usage: wfrun.py WORKFLOW --job JOB --event EVENT_JSON --repo OWNER/NAME
                --workdir DIR --state DIR [--event-name NAME] [--ref REF]
                [--needs JSON] [--matrix KEY=VALUE ...] [--inputs JSON]
                [--until STEP_NAME_PREFIX] [--metadata-action DIR]
Prints a transcript; exits 0 when the job succeeded, 1 when a step failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SHIMS = HERE / "shims"
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM")
STEP_TIMEOUT = 300


def clean_env():
    """The caller's environment minus everything but an allowlist: no token or
    other secret in the shell that runs this reaches workflow code."""
    return {k: v for k, v in os.environ.items() if k in KEEP_ENV or k.startswith("LC_")}


def hermetic_git(env, root):
    """git in a step: local paths only, no user/system config (hooks, signing)."""
    empty = Path(root) / "gitconfig-empty"
    empty.touch()
    env.update({"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": str(empty),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def check_bash():
    """Workflow steps use bash >= 4.4 features (empty "${a[@]}" under set -u)."""
    bash = shutil.which("bash")
    out = subprocess.run([bash or "bash", "-c", "echo ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"],
                         capture_output=True, text=True).stdout.split()
    if not out or (int(out[0]), int(out[1])) < (4, 4):
        sys.exit(f"wfrun: bash {'.'.join(out) or '?'} at {bash} is too old; GitHub runners have bash 5 "
                 "(put a newer bash first on PATH)")


# --------------------------------------------------------------- expressions
class Expr:
    TOKEN = re.compile(r"""\s*(?:
        (?P<str>'(?:[^']|'')*')|
        (?P<num>-?\d+(?:\.\d+)?)|
        (?P<op>==|!=|<=|>=|&&|\|\||[!<>(),\[\]])|
        (?P<ident>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_*][A-Za-z0-9_\-]*)*)
    )""", re.X)

    def __init__(self, text, ctx):
        self.toks = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = self.TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise SyntaxError(f"bad expression near {text[pos:]!r}")
            pos = m.end()
            kind = m.lastgroup
            self.toks.append((kind, m.group(kind)))
        self.i = 0
        self.ctx = ctx

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise SyntaxError(f"expected {value!r}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self):
        v = self.p_or()
        if self.i != len(self.toks):
            raise SyntaxError(f"trailing tokens {self.toks[self.i:]}")
        return v

    def p_or(self):
        v = self.p_and()
        while self.peek()[1] == "||":
            self.take()
            r = self.p_and()
            v = v if truthy(v) else r
        return v

    def p_and(self):
        v = self.p_cmp()
        while self.peek()[1] == "&&":
            self.take()
            r = self.p_cmp()
            v = r if truthy(v) else v
        return v

    def p_cmp(self):
        v = self.p_unary()
        while self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.take()[1]
            r = self.p_unary()
            v = compare(v, op, r)
        return v

    def p_unary(self):
        if self.peek()[1] == "!":
            self.take()
            return not truthy(self.p_unary())
        return self.p_primary()

    def p_primary(self):
        kind, val = self.take()
        if val == "(":
            v = self.p_or()
            self.take(")")
            return v
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if kind == "num":
            return float(val) if "." in val else int(val)
        if kind == "ident":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                while self.peek()[1] != ")":
                    args.append(self.p_or())
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")")
                return call(val, args)
            v = lookup(self.ctx, val)
            while self.peek()[1] == "[":
                self.take("[")
                key = self.p_or()
                self.take("]")
                v = v.get(key) if isinstance(v, dict) else None
            return v
        raise SyntaxError(f"unexpected token {val!r}")


def truthy(v):
    return not (v is None or v is False or v == "" or v == 0)


def compare(a, op, b):
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    elif type(a) is not type(b):
        def num(x):
            if x is None:
                return 0
            if isinstance(x, bool):
                return int(x)
            try:
                return float(x)
            except (TypeError, ValueError):
                return float("nan")
        a, b = num(a), num(b)
    return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


def call(name, args):
    s = [render(a) for a in args]
    if name == "startsWith":
        return s[0].lower().startswith(s[1].lower())
    if name == "endsWith":
        return s[0].lower().endswith(s[1].lower())
    if name == "contains":
        if isinstance(args[0], list):
            return any(str(x).lower() == s[1].lower() for x in args[0])
        return s[1].lower() in s[0].lower()
    if name == "format":
        out = s[0]
        for i, a in enumerate(s[1:]):
            out = out.replace("{%d}" % i, a)
        return out
    if name in ("success", "always"):
        return True
    if name in ("failure", "cancelled"):
        return False
    if name == "toJSON":
        return json.dumps(args[0])
    if name == "fromJSON":
        return json.loads(s[0])
    raise SyntaxError(f"unsupported function {name}")


def lookup(ctx, path):
    v = ctx
    for part in path.split("."):
        if isinstance(v, dict):
            v = v.get(part)
        else:
            return None
    return v


def render(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def interpolate(text, ctx):
    if not isinstance(text, str):
        return render(text)
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: render(Expr(m.group(1), ctx).parse()), text, flags=re.S)


def evaluate_if(cond, ctx):
    if cond is None:
        return True
    if isinstance(cond, bool):
        return cond
    text = str(cond).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, flags=re.S)
    if m:
        text = m.group(1)
    return truthy(Expr(text, ctx).parse())


# ---------------------------------------------------------------- the run
@dataclass
class JobResult:
    ok: bool
    skipped: bool = False
    failed_step: str = ""
    outputs: dict = field(default_factory=dict)   # job outputs
    steps: dict = field(default_factory=dict)     # step id -> {"outputs", "outcome"}
    log: list = field(default_factory=list)       # transcript lines

    def text(self):
        return "\n".join(self.log)


def record(state, **entry):
    with open(Path(state) / "actions.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def parse_output_file(path):
    out = {}
    if not Path(path).exists():
        return out
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([^=<]+)<<(.+)$", line)
        if m:
            key, delim = m.group(1), m.group(2)
            buf = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key] = "\n".join(buf)
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        i += 1
    return out


class _FakeGitHubAPI:
    """Local stand-in for the two GitHub API reads docker/metadata-action makes
    (repo info, commit date), so the real action runs offline with a dummy token."""

    def __init__(self, repository, sha):
        import http.server
        import threading
        owner, name = repository.split("/")
        routes = {
            f"/repos/{owner}/{name}": {"name": name, "full_name": repository,
                                       "html_url": f"https://github.com/{repository}",
                                       "description": "dry run", "default_branch": "main",
                                       "license": {"spdx_id": "Apache-2.0"}},
            f"/repos/{owner}/{name}/commits/{sha}": {"sha": sha, "commit": {
                "committer": {"date": "2026-01-01T00:00:00Z"}, "author": {"date": "2026-01-01T00:00:00Z"}}},
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path.split("?")[0])
                self.send_response(200 if body else 404)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body or {"message": "Not Found"}).encode())

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class Job:
    def __init__(self, wf, job_id, ctx, workdir, state, until, metadata_action, echo):
        self.wf, self.job_id, self.ctx = wf, job_id, ctx
        self.workdir, self.state, self.until = workdir, Path(state), until
        self.metadata_action = Path(metadata_action) if metadata_action else None
        self.result = JobResult(ok=True)
        self.echo = echo
        self.tmp = Path(tempfile.mkdtemp(prefix="wfrun-"))

    def log(self, msg):
        self.result.log.append(msg)
        if self.echo:
            self.echo(msg)

    def base_env(self):
        g = self.ctx["github"]
        env = clean_env()
        env["PATH"] = f"{SHIMS}{os.pathsep}{env.get('PATH', '')}"
        hermetic_git(env, self.tmp)
        event_path = self.tmp / "event.json"
        event_path.write_text(json.dumps(g["event"]))
        env.update({
            "HARNESS_STATE": str(self.state), "CI": "true", "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": str(self.workdir), "GITHUB_REPOSITORY": g["repository"],
            "GITHUB_REPOSITORY_OWNER": g["repository"].split("/")[0], "GITHUB_EVENT_NAME": g["event_name"],
            "GITHUB_EVENT_PATH": str(event_path), "GITHUB_REF": g["ref"], "GITHUB_REF_NAME": g["ref_name"],
            "GITHUB_REF_TYPE": "tag" if g["ref"].startswith("refs/tags/") else "branch",
            "GITHUB_SHA": g["sha"], "GITHUB_WORKFLOW_SHA": g["sha"], "GITHUB_RUN_ID": str(g["run_id"]),
            "GITHUB_ACTOR": g["actor"], "GITHUB_SERVER_URL": "https://github.com",
            "RUNNER_OS": "Linux", "RUNNER_TEMP": str(self.tmp),
        })
        return env

    # ----------------------------------------------------------- uses: steps
    def uses(self, step):
        uses = step["uses"]
        name = uses.split("@")[0]
        with_ = {k: interpolate(v, self.ctx) for k, v in (step.get("with") or {}).items()}
        if name == "actions/create-github-app-token":
            self.log(f"  [stub] {name}: dummy token (repositories={with_.get('repositories', '<all>')})")
            return {"token": "dry-run-dummy-token"}
        if name == "actions/checkout":
            self.log(f"  [stub] {name}: the prepared work tree (ref={with_.get('ref', '<event ref>')})")
            return {}
        if name == "docker/metadata-action":
            if not self.metadata_action:
                raise RuntimeError("docker/metadata-action needs --metadata-action DIR (a checkout of it)")
            return self.metadata(with_)
        if name == "docker/build-push-action":
            tags = [t.strip() for t in (with_.get("tags") or "").splitlines() if t.strip()]
            args = [a.strip() for a in (with_.get("build-args") or "").splitlines() if a.strip()]
            record(self.state, tool=name, push=with_.get("push"), platforms=with_.get("platforms"),
                   tags=tags, build_args=args)
            self.log(f"  [stub] {name}: WOULD BUILD, push={with_.get('push')} platforms={with_.get('platforms')}")
            for t in tags:
                self.log(f"           tag: {t}")
            for a in args:
                self.log(f"           build-arg: {a}")
            return {"digest": "sha256:" + "0" * 64}
        if name in ("pypa/gh-action-pypi-publish", "cloudflare/wrangler-action"):
            record(self.state, tool=name, inputs=with_)
            self.log(f"  [stub] {name}: WOULD PUBLISH/DEPLOY with {json.dumps(with_, sort_keys=True)[:400]}")
            return {}
        self.log(f"  [stub] {name}: no-op")
        return {}

    def metadata(self, with_):
        action = self.metadata_action
        spec = yaml.load((action / "action.yml").read_text(), Loader=yaml.BaseLoader)
        g = self.ctx["github"]
        env = clean_env()
        for key, meta in (spec.get("inputs") or {}).items():
            default = (meta or {}).get("default")
            if key not in with_ and default is not None:
                with_[key] = interpolate(default, self.ctx)
        for key, value in with_.items():
            env["INPUT_" + key.upper().replace(" ", "_")] = value
        for key in ("SEP-TAGS", "SEP-LABELS", "SEP-ANNOTATIONS"):
            env.setdefault("INPUT_" + key, "\n")
        out_file = self.tmp / "metadata-out"
        out_file.touch()
        event_path = self.tmp / "metadata-event.json"
        event_path.write_text(json.dumps(g["event"]))
        api = _FakeGitHubAPI(g["repository"], g["sha"])
        env.update({
            "GITHUB_OUTPUT": str(out_file), "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": g["event_name"], "GITHUB_REF": g["ref"], "GITHUB_SHA": g["sha"],
            "GITHUB_REPOSITORY": g["repository"], "GITHUB_REPOSITORY_OWNER": g["repository"].split("/")[0],
            "GITHUB_RUN_ID": str(g["run_id"]), "GITHUB_RUN_NUMBER": "1", "GITHUB_ACTOR": g["actor"],
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_API_URL": api.url,
            "GITHUB_WORKSPACE": str(self.workdir), "RUNNER_TEMP": str(self.tmp),
        })
        try:
            proc = subprocess.run(["node", str(action / "dist" / "index.js")], cwd=self.workdir, env=env,
                                  capture_output=True, text=True, timeout=STEP_TIMEOUT)
        finally:
            api.close()
        outputs = parse_output_file(out_file)
        if proc.returncode != 0:
            self.log(proc.stdout[-3000:] + proc.stderr[-3000:])
            raise RuntimeError("docker/metadata-action failed")
        tags = [t for t in outputs.get("tags", "").splitlines() if t]
        record(self.state, tool="docker/metadata-action", tags=tags)
        self.log("  [real] docker/metadata-action: tags =")
        for t in tags:
            self.log(f"           {t}")
        return outputs

    # ------------------------------------------------------------ run: steps
    def shell(self, step, script):
        shell = step.get("shell") or ((self.job.get("defaults") or {}).get("run") or {}).get("shell") \
            or ((self.wf.get("defaults") or {}).get("run") or {}).get("shell")
        if shell is None:
            return ["bash", "-e", "-c", script]
        if shell == "bash":
            return ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
        raise RuntimeError(f"unsupported shell {shell!r}")

    def run(self):
        wf, ctx = self.wf, self.ctx
        self.job = job = wf["jobs"][self.job_id]
        if "if" in job and not evaluate_if(job["if"], ctx):
            self.log(f"-- job skipped (if: {job['if']})")
            self.result.skipped = True
            return self.result
        ctx["steps"] = {}
        ctx["env"] = {}
        base_env = {}
        for scope in (wf.get("env") or {}, job.get("env") or {}):
            for k, v in scope.items():
                base_env[k] = interpolate(v, ctx)
                ctx["env"][k] = base_env[k]
        extra_env = {}
        summary = self.tmp / "step_summary.md"
        summary.touch()
        for idx, step in enumerate(job["steps"]):
            name = interpolate(step.get("name") or step.get("uses") or f"step {idx}", ctx)
            if self.until and name.startswith(self.until):
                self.log(f"-- stop before: {name}")
                break
            try:
                run_it = evaluate_if(step.get("if"), ctx)
            except SyntaxError as err:
                self.log(f"!! cannot evaluate if: {step.get('if')!r}: {err}")
                return self.fail(name)
            if not run_it:
                self.log(f"-- skip  : {name}   (if: {step.get('if')})")
                continue
            self.log(f"== run   : {name}")
            sid = step.get("id")
            if "uses" in step:
                try:
                    outputs = self.uses(step)
                except Exception as err:  # a failing action fails its step, as on a runner
                    self.log(f"!! step failed: {name}: {err}")
                    return self.fail(name)
                if sid:
                    ctx["steps"][sid] = {"outputs": outputs, "outcome": "success", "conclusion": "success"}
                continue
            env = self.base_env()
            out_file = self.tmp / f"out-{idx}"
            env_file = self.tmp / f"env-{idx}"
            out_file.touch()
            env_file.touch()
            env.update({"GITHUB_OUTPUT": str(out_file), "GITHUB_ENV": str(env_file),
                        "GITHUB_STEP_SUMMARY": str(summary)})
            env.update(base_env)
            env.update(extra_env)
            for k, v in (step.get("env") or {}).items():
                env[k] = interpolate(v, ctx)
            script = interpolate(step["run"], ctx)
            cwd = self.workdir / interpolate(step.get("working-directory", "."), ctx)
            proc = subprocess.run(self.shell(step, script), cwd=cwd, env=env, capture_output=True,
                                  text=True, timeout=STEP_TIMEOUT)
            for line in (proc.stdout + proc.stderr).rstrip("\n").splitlines():
                self.log(f"   | {line}")
            outputs = parse_output_file(out_file)
            extra_env.update(parse_output_file(env_file))
            ok = proc.returncode == 0
            if sid:
                ctx["steps"][sid] = {"outputs": outputs, "outcome": "success" if ok else "failure",
                                     "conclusion": "success" if ok else "failure"}
            if outputs:
                self.log(f"   > outputs: {json.dumps(outputs, sort_keys=True)}")
            if not ok:
                self.log(f"!! step failed (exit {proc.returncode}): {name}")
                if step.get("continue-on-error") not in (True, "true"):
                    return self.fail(name)
        for k, v in (job.get("outputs") or {}).items():
            self.result.outputs[k] = interpolate(v, ctx)
        if summary.read_text().strip():
            self.log("   > step summary: " + summary.read_text().strip().replace("\n", " / "))
        self.result.steps = ctx["steps"]
        return self.result

    def fail(self, name):
        self.result.ok = False
        self.result.failed_step = name
        self.result.steps = self.ctx["steps"]
        return self.result


def run_job(workflow, job, *, event, repo, workdir, state, event_name="repository_dispatch",
            ref="refs/heads/main", needs=None, matrix=None, inputs=None, until="",
            metadata_action=None, run_id=4242, echo=print):
    """Run JOB of the WORKFLOW file; returns a JobResult (outputs, step outputs, transcript)."""
    wf = yaml.load(Path(workflow).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    workdir = Path(workdir).resolve()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True,
                         text=True).stdout.strip() or "0" * 40
    ctx = {
        "github": {"event": event, "event_name": event_name, "ref": ref,
                   "ref_name": ref.split("/", 2)[-1], "repository": repo, "run_id": run_id,
                   "sha": sha, "actor": "dry-run", "token": "dry-run-dummy-token",
                   "action": event.get("action", ""), "repository_owner": repo.split("/")[0]},
        "secrets": {"GITHUB_TOKEN": "dry-run-dummy-token", "PINEFORGE_APP_ID": "0",
                    "PINEFORGE_APP_PRIVATE_KEY": "dry-run-dummy-key"},
        "inputs": inputs or {}, "vars": {}, "needs": needs or {}, "matrix": matrix or {},
        "runner": {"temp": tempfile.gettempdir(), "os": "Linux"},
    }
    runner = Job(wf, job, ctx, workdir, state, until, metadata_action, echo)
    title = f"### {Path(workflow).name} :: job {job}"
    if matrix:
        title += f" (matrix {matrix})"
    runner.log(title + f" :: {event_name} {json.dumps(event.get('client_payload', event.get('inputs', event)))}")
    result = runner.run()
    if result.outputs:
        runner.log(f"   > job outputs: {json.dumps(result.outputs, sort_keys=True)}")
    runner.log(f"### result: {'skipped' if result.skipped else 'success' if result.ok else 'FAILURE'}")
    shutil.rmtree(runner.tmp, ignore_errors=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workflow")
    ap.add_argument("--job", required=True)
    ap.add_argument("--event", required=True, help="JSON (or a file of it): github.event")
    ap.add_argument("--event-name", default="repository_dispatch")
    ap.add_argument("--ref", default="refs/heads/main")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--until", default="")
    ap.add_argument("--needs", default="{}", help="JSON: needs context")
    ap.add_argument("--matrix", action="append", default=[], help="KEY=VALUE")
    ap.add_argument("--inputs", default="{}", help="JSON: inputs context")
    ap.add_argument("--metadata-action", default="", help="a checkout of docker/metadata-action")
    args = ap.parse_args()
    check_bash()
    event = json.loads(Path(args.event).read_text()) if Path(args.event).is_file() else json.loads(args.event)
    result = run_job(args.workflow, args.job, event=event, repo=args.repo, workdir=args.workdir,
                     state=args.state, event_name=args.event_name, ref=args.ref,
                     needs=json.loads(args.needs), matrix=dict(kv.split("=", 1) for kv in args.matrix),
                     inputs=json.loads(args.inputs), until=args.until,
                     metadata_action=args.metadata_action or None)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
