#!/usr/bin/env python3
"""Release dry run: replay this repo's REAL handle-upstream.yml and publish.yml,
offline, through the 0.x line and the 1.0 line (v1.0.0-rc.1, then v1.0.0), and
check what every run would tag, build, release and send downstream.

Each world starts from a local bare origin seeded from a checkout of this repo
(its working tree, on a synthetic history whose newest tag is v0.1.25 = engine
0.13.1 + codegen 0.10.4) and a state directory the shims answer from: which
engine tarballs and PyPI versions exist, which images the registry has, this
repo's GitHub releases. git pushes land in the local origin; builds, releases
and dispatches are logged as what WOULD happen. Nothing leaves the machine and
no token is read or needed.

docker/metadata-action runs for real (node), so the image tags checked are the
action's own: pass a checkout of it with --metadata-action, or the script
clones the major version publish.yml uses into a cache directory once.

usage: python3 tools/release-dry-run/dry_run.py [-k WORLD] [-v] [--out DIR]
           [--checkout DIR] [--metadata-action DIR]
Exits 0 when every check passes, 1 otherwise.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wfrun  # noqa: E402

REPO = "pineforge-4pass/pineforge-release"
IMAGE = f"ghcr.io/{REPO}"
ENGINE_DL = "https://github.com/pineforge-4pass/pineforge-engine/releases/download"
ENGINE_RAW = "https://raw.githubusercontent.com/pineforge-4pass/pineforge-engine"
PYPI = "https://pypi.org/pypi/pineforge-codegen"
CONSUMERS = ("pineforge-backtest-mcp", "release-fixture-hosted", "release-fixture-application")
CONSUMER_MATRIX = (
    {"consumer": "offline"},
    {"consumer": "hosted", "repository_secret": "RELEASE_HOSTED_MCP_REPOSITORY"},
    {"consumer": "application", "repository_secret": "RELEASE_APPLICATION_REPOSITORY"},
)
# A prerelease is not dispatched to the application (the last consumer): its leg stays
# green, mints no App token and logs this line. The rest is text of the real job that
# the synthetic checkouts below alter.
HANDOFF = "Application is not notified for a prerelease; it receives the release by hand-off."
DECISION_STEP = "Decide consumer notification"
DECISION_CONJUNCTION = '[ "$CONSUMER" = application ] && [ "$PRERELEASE" = true ]'
DECISION_MUTANT = '[ "$CONSUMER" = application ] && [ "$PRERELEASE" = never ]'
DISPATCH_STEP = "Dispatch pineforge-release to consumer"
DISPATCH_GUARD = "        if: steps.notify.outputs.dispatch == 'true'\n"
IDENTITY = ["-c", "user.name=dry-run", "-c", "user.email=dry-run@localhost",
            "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def git(*args, cwd=None, env=None):
    proc = subprocess.run(["git", *IDENTITY, *map(str, args)], cwd=cwd, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(map(str, args))}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class Result:
    """A job run: wfrun's JobResult plus the shim calls it made."""

    def __init__(self, job, actions):
        self.job, self.actions, self.notify = job, actions, []

    ok = property(lambda self: self.job.ok and not self.job.skipped and all(child.ok for child in self.notify))
    failed_step = property(lambda self: self.job.failed_step or
                           next((child.failed_step for child in self.notify if not child.ok), ""))
    outputs = property(lambda self: self.job.outputs)

    def step(self, sid):
        return (self.job.steps.get(sid) or {}).get("outputs", {})

    def text(self):
        return "\n".join([self.job.text(), *(n.job.text() for n in self.notify)])

    def calls(self, **match):
        return [a for r in (self, *self.notify) for a in r.actions
                if all(a.get(k) == v for k, v in match.items())]


class World:
    def __init__(self, name, opts, report):
        self.name, self.opts, self.report = name, opts, report
        self.dir = opts.out / name
        self.state = self.dir / "state"
        self.state.mkdir(parents=True)
        self.origin = self.dir / "origin.git"
        self.runs = 0
        self.env = wfrun.hermetic_git(wfrun.clean_env(), self.dir)
        self.transcript = open(self.dir / "transcript.txt", "w", encoding="utf-8")
        self._http, self.pypi = {}, []
        (self.state / "actions.jsonl").touch()
        shutil.copy(opts.checkout / "docker" / "run_json.py", self.state / "run_json.py")
        self.save("images.json", [f"{IMAGE}:0.1.25"])
        self.save("gh-releases.json", [self.gh_release("v0.1.25"), self.gh_release("v0.1.24")])
        self.save("gh-latest.json", {"pineforge-release": "v0.1.25", "pineforge-engine": "v0.13.1"})
        self.save("releases-pineforge-engine.json", [])
        for v in ("0.13.0", "0.13.1"):
            self.engine_published(v)
        for v in ("0.10.3", "0.10.4"):
            self.codegen_published(v)
        self.seed()

    # --------------------------------------------------------------- state
    def save(self, name, value):
        (self.state / name).write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")

    def load(self, name):
        return json.loads((self.state / name).read_text(encoding="utf-8"))

    def http(self, url, status=200, **extra):
        self._http[url] = {"status": status, **extra}
        self.save("http.json", self._http)

    @staticmethod
    def gh_release(tag, draft=False, prerelease=False):
        return {"tagName": tag, "isDraft": draft, "isPrerelease": prerelease}

    def hub_release(self, tag, draft=False):
        """A release of this repo made by hand (for example a draft)."""
        self.save("gh-releases.json", [self.gh_release(tag, draft=draft)] + self.load("gh-releases.json"))

    def engine_published(self, v, status=200):
        """The engine's release.yml: GitHub release, linux tarballs, then its dispatch."""
        rels = self.load("releases-pineforge-engine.json")
        self.save("releases-pineforge-engine.json",
                  [{"tag_name": f"v{v}", "draft": False, "prerelease": "-" in v}] + rels)
        for arch in ("linux-x86_64", "linux-aarch64"):
            self.http(f"{ENGINE_DL}/v{v}/pineforge-v{v}-{arch}.tar.gz", status)
        self.http(f"{ENGINE_RAW}/v{v}/docker/run_json.py", file="run_json.py")

    def codegen_published(self, pep440):
        """The codegen release.yml: the PyPI upload, then its dispatch."""
        self.pypi.append(pep440)
        stable = [v for v in self.pypi if re.fullmatch(r"[0-9.]+", v)]
        self.http(f"{PYPI}/{pep440}/json", body=json.dumps({"info": {"version": pep440}}))
        self.http(f"{PYPI}/json", body=json.dumps({"info": {"version": stable[-1]},
                                                    "releases": {v: [] for v in self.pypi}}))

    def image(self, ref):
        images = self.load("images.json")
        if ref not in images:
            self.save("images.json", images + [ref])

    # -------------------------------------------------------------- origin
    def seed(self):
        """origin.git: the checkout's working tree on a synthetic history, tags v0.1.24 and v0.1.25."""
        seed = self.dir / "seed"
        git("init", "-q", "--bare", self.origin, env=self.env)
        git("init", "-q", seed, env=self.env)
        files = subprocess.run(["git", "-C", str(self.opts.checkout), "ls-files", "-z", "-co",
                                "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        for rel in filter(None, files.split("\0")):
            src = self.opts.checkout / rel
            if src.is_file():
                (seed / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, seed / rel)
        for v in ("0.1.24", "0.1.25"):
            (seed / "VERSION").write_text(f"{v}\n")
            git("add", "-A", cwd=seed, env=self.env)
            git("commit", "-q", "-m", f"chore(release): v{v} — engine 0.13.1 + codegen 0.10.4", cwd=seed, env=self.env)
            git("tag", "-a", f"v{v}", "-m", f"pineforge-release v{v}\n\nengine=0.13.1\ncodegen=0.10.4\n",
                cwd=seed, env=self.env)
        git("push", "-q", "--tags", self.origin, "main", cwd=seed, env=self.env)
        self.log(f"## world {self.name}: origin main = {self.opts.describe} on a synthetic v0.1.24, v0.1.25 history")

    def clone(self, ref):
        self.runs += 1
        work = self.dir / f"run{self.runs:02d}"
        git("clone", "-q", self.origin, work, env=self.env)
        if ref.startswith("refs/tags/"):
            git("checkout", "-q", ref[len("refs/tags/"):], cwd=work, env=self.env)
        return work

    def commit_on_main(self, message, **files):
        work = self.clone("refs/heads/main")
        for path, text in files.items():
            (work / path).write_text(text)
        git("commit", "-q", "-am", message, cwd=work, env=self.env)
        git("push", "-q", "origin", "main", cwd=work, env=self.env)
        return work

    def tags(self):
        return set(git("tag", "-l", "v*", cwd=self.origin, env=self.env).split())

    def pins(self, tag):
        body = git("for-each-ref", "--format=%(contents:body)", f"refs/tags/{tag}", cwd=self.origin, env=self.env)
        kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        return kv.get("engine"), kv.get("codegen")

    def show(self, ref, path):
        return git("show", f"{ref}:{path}", cwd=self.origin, env=self.env).strip()

    def subject(self, ref):
        return git("log", "-1", "--format=%s", ref, cwd=self.origin, env=self.env)

    def rev(self, ref, short=False):
        return git("rev-parse", *(["--short=7"] if short else []), f"{ref}^{{commit}}", cwd=self.origin, env=self.env)

    # ---------------------------------------------------------------- runs
    def log(self, line):
        self.transcript.write(line + "\n")
        self.transcript.flush()
        if self.opts.verbose:
            print(line)

    def run(self, workflow, job, *, event, event_name, ref, work=None, needs=None, matrix=None,
            repository_secrets=None, run_id=4242):
        work = work or self.clone(ref)
        with open(self.state / "actions.jsonl", encoding="utf-8") as fh:
            mark = len(fh.readlines())
        res = wfrun.run_job(work / ".github" / "workflows" / workflow, job, event=event, repo=REPO,
                            workdir=work, state=self.state, event_name=event_name, ref=ref, needs=needs,
                            matrix=matrix, repository_secrets=repository_secrets, run_id=run_id,
                            metadata_action=self.opts.metadata_action, echo=self.log)
        with open(self.state / "actions.jsonl", encoding="utf-8") as fh:
            actions = [json.loads(line) for line in fh.readlines()[mark:]]
        return Result(res, actions)

    def upstream(self, event_type, payload):
        """An upstream release's repository_dispatch into handle-upstream.yml."""
        self.log(f"\n---- {event_type} {json.dumps(payload)}")
        return self.run("handle-upstream.yml", "bump", event={"action": event_type, "client_payload": payload},
                        event_name="repository_dispatch", ref="refs/heads/main")

    def publish(self, tag):
        """The tag push into publish.yml: the publish job, then notify-consumers per consumer."""
        self.log(f"\n---- push {tag}")
        ref = f"refs/tags/{tag}"
        work = self.clone(ref)
        res = self.run("publish.yml", "publish", event={"ref": ref}, event_name="push", ref=ref, work=work)
        if res.ok:
            for build in res.calls(tool="docker/build-push-action"):
                for t in build["tags"]:
                    self.image(t)
            res = self.fanout(res, tag, work=work)
        return res

    def fanout(self, published, tag, *, work=None, repository_secrets=None, outputs=None, run_id=4242):
        ref = f"refs/tags/{tag}"
        work = work or self.clone(ref)
        workflow = work / ".github" / "workflows" / "publish.yml"
        spec = wfrun.yaml.load(workflow.read_text(encoding="utf-8"), Loader=wfrun.yaml.BaseLoader)
        strategy = spec["jobs"]["notify-consumers"]["strategy"]
        # This replay runs each row independently. Bind that behavior to the
        # real workflow instead of silently ignoring GitHub's fail-fast default.
        if strategy.get("fail-fast") != "false":
            raise RuntimeError("notify-consumers requires explicit strategy.fail-fast: false")
        matrix = strategy["matrix"]
        expected = {"consumer": [row["consumer"] for row in CONSUMER_MATRIX],
                    "include": [dict(row) for row in CONSUMER_MATRIX if "repository_secret" in row]}
        if matrix != expected:
            raise RuntimeError("notify-consumers matrix differs from the three neutral fixture roles")
        rows = [{"consumer": consumer} for consumer in matrix["consumer"]]
        for include in matrix["include"]:
            next(row for row in rows if row["consumer"] == include["consumer"]).update(include)
        res = Result(published.job, published.actions)
        needs = {"publish": {"outputs": published.outputs if outputs is None else outputs, "result": "success"}}
        for row in rows:
            res.notify.append(self.run("publish.yml", "notify-consumers", event={"ref": ref},
                                       event_name="push", ref=ref, work=work, needs=needs, matrix=row,
                                       repository_secrets=repository_secrets, run_id=run_id))
        return res

    # -------------------------------------------------------------- checks
    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.report.append((self.name, name, ok, "" if ok else str(detail)))
        line = f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        got: {detail}")
        print(line)
        self.log(line)

    def pushed(self, res):
        return {t for b in res.calls(tool="docker/build-push-action") for t in b["tags"]}

    def release(self, res, tag):
        found = res.calls(tool="gh", action="release create", tag=tag)
        return found[0] if found else {}

    def dispatched(self, res):
        return {c["repo"]: c["body"]
                for c in res.calls(tool="gh", action="dispatch")}

    def withheld(self, leg):
        """The application leg of a prerelease: green, its decision step said no and logged the
        hand-off line once, and the leg minted no App token and sent no dispatch."""
        decision = leg.job.steps.get("notify") or {}
        return bool(leg.ok and decision.get("outcome") == "success"
                    and decision.get("outputs") == {"dispatch": "false"}
                    and leg.job.log.count(f"   | {HANDOFF}") == 1
                    and not leg.calls(tool="actions/create-github-app-token")
                    and not leg.calls(tool="gh", action="dispatch"))

    def fanout_matches(self, res, version, prerelease, run_id=4242):
        """A prerelease is dispatched to every consumer but the application, whose leg stays green
        and sends nothing; a stable release is dispatched to all of them. Three green legs always."""
        sent = list(CONSUMERS[:-1] if prerelease else CONSUMERS)
        calls = res.calls(tool="gh", action="dispatch")
        body = {"event_type": "pineforge-release", "client_payload": {
            "release_version": version, "prerelease": prerelease, "run_id": run_id}}

        def body_matches(entry):
            actual = entry.get("body")
            if actual != body:
                return False
            payload = actual["client_payload"]
            # Python's True == 1 must not make incorrect JSON types pass.
            return type(payload["prerelease"]) is bool and type(payload["run_id"]) is int

        scopes = res.calls(tool="actions/create-github-app-token")
        return (type(prerelease) is bool and type(run_id) is int
                and res.ok and len(res.notify) == len(CONSUMERS) and len(calls) == len(sent)
                and [entry["repo"] for entry in calls] == sent
                and all(body_matches(entry) and entry.get("hostname") == "github.com"
                        and entry.get("endpoint") == f"repos/pineforge-4pass/{entry['repo']}/dispatches"
                        for entry in calls)
                and [entry.get("repositories") for entry in scopes] == sent
                and all(entry.get("owner") == "pineforge-4pass"
                        and entry.get("github_api_url") == "https://api.github.com" for entry in scopes)
                and (not prerelease or self.withheld(res.notify[-1])))

    def refused(self, res, step, message):
        return not res.ok and res.failed_step.startswith(step) and message in res.job.text()

    def unchanged(self, tags, main, why=False):
        """No tag added and main where it was (with why=True: what changed, for the report)."""
        if why:
            return {"new tags": sorted(self.tags() - tags), "main moved": self.rev("main") != main}
        return self.tags() == tags and self.rev("main") == main


# ------------------------------------------------------------------ worlds
def pair(w):
    """1.0 line, engine first: rc.1 waits, pairs, ships as a prerelease; 1.0.0 promotes; refusals."""
    before, main = w.tags(), w.rev("main")
    w.engine_published("1.0.0-rc.1")
    r = w.upstream("engine-release", {"version": "v1.0.0-rc.1", "prerelease": True, "run_id": 101})
    d = r.step("decide")
    w.check("engine rc.1 first: green run, mode=wait, awaiting codegen",
            r.ok and d.get("mode") == "wait" and d.get("awaiting") == "codegen", (r.failed_step, d))
    w.check("engine rc.1 first: no tag, main unchanged", w.unchanged(before, main), w.unchanged(before, main, why=True))
    w.check("engine rc.1 first: the run says the pair is pending",
            "pair pending: engine 1.0.0-rc.1 recorded, waiting for codegen 1.0.0-rc.1" in r.text())

    w.codegen_published("1.0.0rc1")
    r = w.upstream("codegen-release", {"version": "1.0.0-rc.1", "prerelease": True, "run_id": 102})
    d = r.step("decide")
    w.check("codegen rc.1 second: mode=bump line=pair release=1.0.0-rc.1 prerelease=true",
            r.ok and (d.get("mode"), d.get("line"), d.get("release"), d.get("prerelease"))
            == ("bump", "pair", "1.0.0-rc.1", "true"), (r.failed_step, d))
    w.check("codegen rc.1 second: tag v1.0.0-rc.1 records engine 1.0.0-rc.1 + codegen 1.0.0-rc.1",
            w.pins("v1.0.0-rc.1") == ("1.0.0-rc.1", "1.0.0-rc.1"), w.pins("v1.0.0-rc.1"))
    w.check("codegen rc.1 second: main = the release commit, VERSION 1.0.0-rc.1",
            w.show("main", "VERSION") == "1.0.0-rc.1"
            and w.subject("main") == "chore(release): v1.0.0-rc.1 — engine 1.0.0-rc.1 + codegen 1.0.0-rc.1",
            (w.show("main", "VERSION"), w.subject("main")))
    w.check("codegen rc.1 second: docker/run_json.py comes from the engine tag v1.0.0-rc.1",
            r.calls(tool="curl", url=f"{ENGINE_RAW}/v1.0.0-rc.1/docker/run_json.py"))

    p = w.publish("v1.0.0-rc.1")
    c = p.step("pair")
    w.check("publish v1.0.0-rc.1: green, prerelease=true moving=false codegen_pypi=1.0.0rc1",
            p.ok and (c.get("prerelease"), c.get("moving"), c.get("codegen_pypi")) == ("true", "false", "1.0.0rc1"),
            (p.failed_step, c))
    sha = w.rev("v1.0.0-rc.1", short=True)
    w.check("publish v1.0.0-rc.1: image tags are the fixed ones only (no latest, no 1.0)",
            w.pushed(p) == {f"{IMAGE}:1.0.0-rc.1", f"{IMAGE}:engine1.0.0-rc.1-codegen1.0.0-rc.1", f"{IMAGE}:sha-{sha}"},
            sorted(w.pushed(p)))
    rel = w.release(p, "v1.0.0-rc.1")
    w.check("publish v1.0.0-rc.1: GitHub prerelease with --latest=false",
            rel.get("prerelease") is True and rel.get("latest") == "false", rel.get("flags"))
    w.check("publish v1.0.0-rc.1: GitHub Latest stays v0.1.25",
            w.load("gh-latest.json")["pineforge-release"] == "v0.1.25", w.load("gh-latest.json"))
    w.check("publish v1.0.0-rc.1: offline and hosted get release_version=1.0.0-rc.1 prerelease=true; "
            "the application gets no dispatch",
            w.fanout_matches(p, "1.0.0-rc.1", True), w.dispatched(p))
    w.check("publish v1.0.0-rc.1: the application leg is green, mints no App token, sends no dispatch "
            "and logs the hand-off line",
            len(p.notify) == len(CONSUMERS) and w.withheld(p.notify[-1]), (p.failed_step, w.dispatched(p)))
    fanout_failures(w, p, "v1.0.0-rc.1", True)

    before = w.tags()
    r = w.upstream("engine-release", {"version": "v1.0.0-rc.1", "prerelease": True, "run_id": 103})
    w.check("a duplicate rc.1 event: mode=noop, no tag",
            r.ok and r.step("decide").get("mode") == "noop" and w.tags() == before, (r.failed_step, r.step("decide")))

    w.engine_published("1.0.0")
    before, main = w.tags(), w.rev("main")
    r = w.upstream("engine-release", {"version": "v1.0.0", "prerelease": False, "run_id": 104})
    w.check("engine 1.0.0 first: mode=wait; no tag, main unchanged",
            r.ok and r.step("decide").get("mode") == "wait" and w.unchanged(before, main),
            (r.failed_step, r.step("decide"), w.unchanged(before, main, why=True)))
    w.codegen_published("1.0.0")
    r = w.upstream("codegen-release", {"version": "1.0.0", "prerelease": False, "run_id": 105})
    d = r.step("decide")
    w.check("codegen 1.0.0 second: mode=bump release=1.0.0 prerelease=false; tag v1.0.0 pairs 1.0.0 + 1.0.0",
            r.ok and (d.get("mode"), d.get("release"), d.get("prerelease")) == ("bump", "1.0.0", "false")
            and w.pins("v1.0.0") == ("1.0.0", "1.0.0"), (r.failed_step, d))

    w.hub_release("v9.9.9", draft=True)   # a hand-made draft must not hold Latest back
    p = w.publish("v1.0.0")
    c = p.step("pair")
    w.check("publish v1.0.0: green, prerelease=false moving=true",
            p.ok and (c.get("prerelease"), c.get("moving")) == ("false", "true"), (p.failed_step, c))
    sha = w.rev("v1.0.0", short=True)
    w.check("publish v1.0.0: image tags 1.0.0, 1.0, latest, engine1.0.0-codegen1.0.0, sha",
            w.pushed(p) == {f"{IMAGE}:{t}" for t in ("1.0.0", "1.0", "latest", "engine1.0.0-codegen1.0.0",
                                                      f"sha-{sha}")}, sorted(w.pushed(p)))
    rel = w.release(p, "v1.0.0")
    w.check("publish v1.0.0: GitHub Latest although a draft v9.9.9 exists",
            rel.get("prerelease") is False and rel.get("latest") == "true"
            and w.load("gh-latest.json")["pineforge-release"] == "v1.0.0", rel.get("flags"))
    w.check("publish v1.0.0: every consumer gets release_version=1.0.0 prerelease=false",
            w.fanout_matches(p, "1.0.0", False), w.dispatched(p))
    fanout_failures(w, p, "v1.0.0", False)

    before, main = w.tags(), w.rev("main")
    r = w.upstream("engine-release", {"version": "v1.0.0-rc.1", "prerelease": True, "run_id": 106})
    w.check("a late rc.1 event after 1.0.0 is refused",
            w.refused(r, "Decide action", "refusing engine 1.0.0-rc.1 below the landed pair 1.0.0"), r.failed_step)
    r = w.upstream("codegen-release", {"version": "0.10.5", "run_id": 107})
    w.check("a 0.x codegen event after 1.0.0 is refused",
            w.refused(r, "Decide action", "refusing codegen 0.10.5"), r.failed_step)
    r = w.upstream("engine-release", {"version": "v1.0.1-rc.1", "prerelease": False, "run_id": 108})
    w.check("a prerelease flag that contradicts the version is refused",
            w.refused(r, "Decide action", "client_payload.prerelease=false contradicts version 1.0.1-rc.1"),
            r.failed_step)
    w.engine_published("1.0.1")
    r = w.upstream("engine-release", {"version": "v1.0.1", "prerelease": False, "run_id": 109})
    w.check("engine 1.0.1 first: mode=wait", r.ok and r.step("decide").get("mode") == "wait",
            (r.failed_step, r.step("decide")))
    w.codegen_published("1.0.2")
    r = w.upstream("codegen-release", {"version": "1.0.2", "prerelease": False, "run_id": 110})
    w.check("codegen 1.0.2 against engine 1.0.1 fails, naming both",
            w.refused(r, "Decide action", "mismatched pair: engine 1.0.1 + codegen 1.0.2"), r.failed_step)
    w.engine_published("1.0.3", status=503)
    w.codegen_published("1.0.3")
    r = w.upstream("codegen-release", {"version": "1.0.3", "prerelease": False, "run_id": 111})
    w.check("a partner probe answered 503 fails the run instead of waiting",
            w.refused(r, "Probe the partner release", "could not tell whether engine v1.0.3 tarballs is published"),
            r.failed_step)
    w.check("no refused or waiting event tagged or pushed anything", w.unchanged(before, main),
            w.unchanged(before, main, why=True))


def reverse(w):
    """1.0 line, codegen first: the engine's event completes rc.1, then 1.0.0."""
    before, main = w.tags(), w.rev("main")
    w.codegen_published("1.0.0rc1")
    r = w.upstream("codegen-release", {"version": "1.0.0-rc.1", "prerelease": True, "run_id": 201})
    d = r.step("decide")
    w.check("codegen rc.1 first: mode=wait, awaiting engine; no tag, main unchanged",
            r.ok and d.get("mode") == "wait" and d.get("awaiting") == "engine" and w.unchanged(before, main),
            (r.failed_step, d, w.unchanged(before, main, why=True)))
    w.engine_published("1.0.0-rc.1")
    r = w.upstream("engine-release", {"version": "v1.0.0-rc.1", "prerelease": True, "run_id": 202})
    w.check("engine rc.1 second: mode=bump, tag v1.0.0-rc.1 pairs 1.0.0-rc.1 + 1.0.0-rc.1",
            r.ok and r.step("decide").get("mode") == "bump" and w.pins("v1.0.0-rc.1") == ("1.0.0-rc.1", "1.0.0-rc.1"),
            (r.failed_step, r.step("decide")))

    published = w.publish("v1.0.0-rc.1")
    w.check("reverse rc.1: real publish; the offline and hosted scoped dispatches carry the same payload/run_id "
            "and the application is withheld",
            w.fanout_matches(published, "1.0.0-rc.1", True), w.dispatched(published))
    sha = w.rev("v1.0.0-rc.1", short=True)
    w.check("reverse rc.1: real metadata-action emits only fixed image tags",
            w.pushed(published) == {f"{IMAGE}:{tag}" for tag in
                                   ("1.0.0-rc.1", "engine1.0.0-rc.1-codegen1.0.0-rc.1", f"sha-{sha}")},
            sorted(w.pushed(published)))
    before, main = w.tags(), w.rev("main")
    w.codegen_published("1.0.0")
    r = w.upstream("codegen-release", {"version": "1.0.0", "prerelease": False, "run_id": 203})
    d = r.step("decide")
    w.check("codegen 1.0.0 first: mode=wait, awaiting engine; no tag, main unchanged",
            r.ok and d.get("mode") == "wait" and d.get("awaiting") == "engine" and w.unchanged(before, main),
            (r.failed_step, d, w.unchanged(before, main, why=True)))
    w.engine_published("1.0.0")
    r = w.upstream("engine-release", {"version": "v1.0.0", "prerelease": False, "run_id": 204})
    d = r.step("decide")
    w.check("engine 1.0.0 second: mode=bump release=1.0.0 prerelease=false; tag v1.0.0 pairs 1.0.0 + 1.0.0",
            r.ok and (d.get("mode"), d.get("release"), d.get("prerelease")) == ("bump", "1.0.0", "false")
            and w.pins("v1.0.0") == ("1.0.0", "1.0.0"), (r.failed_step, d))
    published = w.publish("v1.0.0")
    w.check("reverse stable: real publish and all three scoped dispatches carry the same payload/run_id",
            w.fanout_matches(published, "1.0.0", False), w.dispatched(published))
    sha = w.rev("v1.0.0", short=True)
    w.check("reverse stable: real metadata-action emits stable moving and fixed image tags",
            w.pushed(published) == {f"{IMAGE}:{tag}" for tag in
                                   ("1.0.0", "1.0", "latest", "engine1.0.0-codegen1.0.0", f"sha-{sha}")},
            sorted(w.pushed(published)))


def lost_partner(w):
    """The partner's dispatch never arrives: re-running the waiting run completes the pair."""
    event = {"version": "v1.0.0-rc.1", "prerelease": True, "run_id": 301}
    before, main = w.tags(), w.rev("main")
    w.engine_published("1.0.0-rc.1")
    r = w.upstream("engine-release", event)
    w.check("engine rc.1: mode=wait", r.ok and r.step("decide").get("mode") == "wait", (r.failed_step, r.step("decide")))
    w.codegen_published("1.0.0rc1")   # codegen ships, but its dispatch is lost: no run
    w.check("codegen on PyPI with no event: still no tag, main unchanged", w.unchanged(before, main),
            w.unchanged(before, main, why=True))
    r = w.upstream("engine-release", event)   # "Re-run all jobs" on the waiting run: same event, same main
    w.check("re-running the waiting run: mode=bump, tag v1.0.0-rc.1",
            r.ok and r.step("decide").get("mode") == "bump" and w.pins("v1.0.0-rc.1") == ("1.0.0-rc.1", "1.0.0-rc.1"),
            (r.failed_step, r.step("decide")))


def retag(w):
    """A pair whose tag push failed after its release commit reached main."""
    w.engine_published("1.0.0-rc.1")
    w.codegen_published("1.0.0rc1")
    w.commit_on_main("chore(release): v1.0.0-rc.1 — engine 1.0.0-rc.1 + codegen 1.0.0-rc.1", VERSION="1.0.0-rc.1\n")
    main = w.rev("main")
    r = w.upstream("codegen-release", {"version": "1.0.0-rc.1", "prerelease": True, "run_id": 401})
    d = r.step("decide")
    w.check("the next event: mode=bump retag=true",
            r.ok and (d.get("mode"), d.get("retag")) == ("bump", "true"), (r.failed_step, d))
    w.check("v1.0.0-rc.1 tags the existing release commit; main unchanged",
            "v1.0.0-rc.1" in w.tags() and w.rev("v1.0.0-rc.1") == main and w.rev("main") == main,
            (sorted(w.tags()), main))


def legacy(w):
    """0.x line as before: coupled patch bump, stable channel, Latest; plus a refused hand-made tag."""
    w.engine_published("0.13.2")
    r = w.upstream("engine-release", {"version": "v0.13.2", "run_id": 501})
    d = r.step("decide")
    w.check("engine 0.13.2: mode=bump line=legacy; tag v0.1.26 = engine 0.13.2 + codegen 0.10.4",
            r.ok and (d.get("mode"), d.get("line")) == ("bump", "legacy")
            and w.pins("v0.1.26") == ("0.13.2", "0.10.4"), (r.failed_step, d))

    (w.state / "gh-release-list.fail").touch()
    p = w.publish("v0.1.26")
    w.check("publish v0.1.26 with `gh release list` failing: the step fails, no release is created",
            not p.ok and p.failed_step == "GitHub Release" and not p.calls(action="release create"), p.failed_step)
    (w.state / "gh-release-list.fail").unlink()
    p = w.publish("v0.1.26")   # re-run the failed publish run
    c = p.step("pair")
    sha = w.rev("v0.1.26", short=True)
    w.check("re-run publish v0.1.26: prerelease=false moving=true",
            p.ok and (c.get("prerelease"), c.get("moving")) == ("false", "true"), (p.failed_step, c))
    w.check("re-run publish v0.1.26: image tags 0.1.26, 0.1, latest, engine0.13.2-codegen0.10.4, sha",
            w.pushed(p) == {f"{IMAGE}:{t}" for t in ("0.1.26", "0.1", "latest", "engine0.13.2-codegen0.10.4",
                                                      f"sha-{sha}")}, sorted(w.pushed(p)))
    w.check("re-run publish v0.1.26: GitHub Latest; consumers get prerelease=false",
            w.release(p, "v0.1.26").get("latest") == "true"
            and w.fanout_matches(p, "0.1.26", False), w.dispatched(p))

    work = w.commit_on_main("hand-made 1.0.1", VERSION="1.0.1\n")
    git("tag", "-a", "v1.0.1", "-m", "pineforge-release v1.0.1\n\nengine=1.0.1\ncodegen=1.0.0\n", cwd=work, env=w.env)
    git("push", "-q", "origin", "v1.0.1", cwd=work, env=w.env)
    p = w.publish("v1.0.1")
    w.check("a hand-made tag pairing engine 1.0.1 with codegen 1.0.0 fails before any build",
            w.refused(p, "Pairing rule", "refusing to publish engine 1.0.1 + codegen 1.0.0")
            and not p.calls(tool="docker/build-push-action"), p.failed_step)


def replace_once(text, old, new, what):
    """TEXT with its one OLD replaced by NEW; no match or several is a harness error, never a broad replace."""
    if text.count(old) != 1:
        raise RuntimeError(f"cannot inject {what} into the synthetic checkout")
    return text.replace(old, new)


def replace_in_step(text, step, old, new, what):
    """replace_once inside the one workflow step named STEP, so the edit cannot land in another step."""
    head = f"      - name: {step}\n"
    if text.count(head) != 1:
        raise RuntimeError(f"cannot inject {what}: step {step!r} is not in the synthetic checkout exactly once")
    start = text.index(head)
    end = text.find("\n      - ", start + len(head))
    end = len(text) if end < 0 else end
    return text[:start] + replace_once(text[start:end], old, new, what) + text[end:]


def fanout_failures(w, published, tag, prerelease):
    """Exercise consumer isolation and verify the fanout oracle rejects real defective runs.

    PUBLISHED is the successful publish run of TAG and PRERELEASE its channel: a prerelease is
    dispatched to every consumer but the application, a stable release to all of them.
    """
    channel = "prerelease" if prerelease else "stable"
    if published.outputs.get("prerelease") != ("true" if prerelease else "false"):
        raise RuntimeError(f"the publish run of {tag} is not a {channel} run")
    dispatched, version = CONSUMERS[:-1] if prerelease else CONSUMERS, tag[1:]
    for label, replacement in (("true", "      fail-fast: true\n"), ("missing", "")):
        work = w.clone(f"refs/tags/{tag}")
        workflow = work / ".github" / "workflows" / "publish.yml"
        text = workflow.read_text(encoding="utf-8")
        original = "      fail-fast: false\n"
        if text.count(original) != 1:
            raise RuntimeError("cannot inject fail-fast control into the synthetic checkout")
        workflow.write_text(text.replace(original, replacement), encoding="utf-8")
        before = (w.state / "actions.jsonl").read_bytes()
        try:
            w.fanout(published, tag, work=work)
        except RuntimeError as error:
            refused = "strategy.fail-fast: false" in str(error)
        else:
            refused = False
        w.check(f"consumer isolation rejects fail-fast {label} before any consumer runs ({channel})",
                refused and (w.state / "actions.jsonl").read_bytes() == before)

    def leg_ok(repo, leg):
        """A leg with valid configuration: dispatched to once, or (the application on a prerelease) withheld."""
        if repo in dispatched:
            return leg.ok and len(leg.calls(tool="gh", action="dispatch")) == 1
        return w.withheld(leg)

    for index, row in enumerate(CONSUMER_MATRIX[1:], 1):
        secret = row["repository_secret"]
        for label, value in (("missing", None), ("empty", ""), ("owner/path", "fixture/target"),
                             ("multiline", "fixture\ntarget")):
            targets = dict(wfrun.DUMMY_REPOSITORY_SECRETS)
            if value is None:
                targets.pop(secret)
            else:
                targets[secret] = value
            result = w.fanout(published, tag, repository_secrets=targets)
            leg = result.notify[index]
            peers = [(CONSUMERS[peer], child) for peer, child in enumerate(result.notify) if peer != index]
            if CONSUMERS[index] in dispatched:
                name = f"{row['consumer']} {label} target: only that consumer fails before App token/dispatch"
                outcome = (not result.ok and w.refused(leg, "Require one configured", "Missing or malformed")
                           and not leg.calls(tool="actions/create-github-app-token")
                           and not leg.calls(tool="gh", action="dispatch"))
            else:
                # The withheld application never reads its target: green, no token, no dispatch.
                name = (f"{row['consumer']} {label} target on a prerelease: the withheld leg stays green "
                        "with no App token/dispatch")
                outcome = result.ok and w.withheld(leg)
            w.check(f"{name} ({channel})",
                    outcome and all(leg_ok(repo, child) for repo, child in peers)
                    and set(w.dispatched(result)) == set(dispatched) - {CONSUMERS[index]},
                    (result.failed_step, w.dispatched(result)))
    targets = {**wfrun.DUMMY_REPOSITORY_SECRETS, "RELEASE_HOSTED_MCP_REPOSITORY": "release-fixture-wrong"}
    defective = w.fanout(published, tag, repository_secrets=targets)
    w.check(f"fanout oracle rejects a successful dispatch to the wrong configured target ({channel})",
            defective.ok and not w.fanout_matches(defective, version, prerelease), w.dispatched(defective))
    # The wrong prerelease flag is the opposite of the real one, whichever channel this is.
    for label, kwargs in (("release_version", {"tag": "v0.1.25"}),
                          ("prerelease", {"outputs": {"prerelease": "false" if prerelease else "true"}}),
                          ("run_id", {"run_id": 4243})):
        defective = w.fanout(published, kwargs.pop("tag", tag), **kwargs)
        w.check(f"fanout oracle rejects a successful dispatch with wrong {label} ({channel})",
                defective.ok and not w.fanout_matches(defective, version, prerelease), w.dispatched(defective))
    for field in ("prerelease", "run_id"):
        work = w.clone(f"refs/tags/{tag}")
        workflow = work / ".github" / "workflows" / "publish.yml"
        text = workflow.read_text(encoding="utf-8")
        original = f'-F "client_payload[{field}]='
        if text.count(original) != 1:
            raise RuntimeError(f"cannot inject raw {field} control into the synthetic checkout")
        workflow.write_text(text.replace(original, f'-f "client_payload[{field}]='), encoding="utf-8")
        defective = w.fanout(published, tag, work=work)
        calls = defective.calls(tool="gh", action="dispatch")
        w.check(f"fanout oracle rejects string instead of typed {field} ({channel})",
                defective.ok and len(calls) == len(dispatched)
                and all(type(call["body"]["client_payload"][field]) is str for call in calls)
                and not w.fanout_matches(defective, version, prerelease), w.dispatched(defective))
    for label in ("job", "dispatch step"):
        work = w.clone(f"refs/tags/{tag}")
        workflow = work / ".github" / "workflows" / "publish.yml"
        text = workflow.read_text(encoding="utf-8")
        if label == "job":
            text = replace_once(text, "  notify-consumers:\n", "  notify-consumers:\n    if: false\n",
                                "skipped fanout job")
        else:
            # The dispatch step already has its guard: that one value becomes false, no second `if:` key.
            text = replace_in_step(text, DISPATCH_STEP, DISPATCH_GUARD, "        if: false\n",
                                   "skipped fanout dispatch step")
        workflow.write_text(text, encoding="utf-8")
        defective = w.fanout(published, tag, work=work)
        w.check(f"fanout oracle rejects a skipped {label} (no dispatch is hidden) ({channel})",
                not w.fanout_matches(defective, version, prerelease)
                and not defective.calls(tool="gh", action="dispatch")
                and (not defective.ok if label == "job" else defective.ok), defective.text())
    if prerelease:
        # A broken decision: only its first condition changes and every guard stays, so the application
        # leg dispatches and mints its token on an all-green run. The unmodified oracle must reject it.
        work = w.clone(f"refs/tags/{tag}")
        workflow = work / ".github" / "workflows" / "publish.yml"
        workflow.write_text(replace_in_step(workflow.read_text(encoding="utf-8"), DECISION_STEP,
                                            DECISION_CONJUNCTION, DECISION_MUTANT,
                                            "application dispatch on a prerelease"), encoding="utf-8")
        defective = w.fanout(published, tag, work=work)
        application = defective.notify[-1]
        w.check(f"fanout oracle rejects an application dispatch on a prerelease ({channel})",
                defective.ok and len(defective.notify) == len(CONSUMERS)
                and [call["repo"] for call in defective.calls(tool="gh", action="dispatch")] == list(CONSUMERS)
                and application.calls(tool="gh", action="dispatch", repo=CONSUMERS[-1])
                and application.calls(tool="actions/create-github-app-token", repositories=CONSUMERS[-1])
                and not w.fanout_matches(defective, version, prerelease), w.dispatched(defective))


WORLDS = {"pair": pair, "reverse": reverse, "lost-partner": lost_partner, "retag": retag, "legacy": legacy}


# ------------------------------------------------------------------- setup
def metadata_action(arg, checkout):
    if arg:
        path = Path(arg).resolve()
    else:
        text = (checkout / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        major = re.search(r"docker/metadata-action@(v\d+)", text).group(1)
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        path = cache / "pineforge-release-dry-run" / f"metadata-action-{major}"
        if not (path / "dist" / "index.js").is_file():
            print(f"cloning docker/metadata-action {major} (the version publish.yml uses) into {path}")
            shutil.rmtree(path, ignore_errors=True)
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", major,
                            "https://github.com/docker/metadata-action", str(path)], check=True)
    if not (path / "dist" / "index.js").is_file():
        sys.exit(f"dry_run: {path} is not a checkout of docker/metadata-action (no dist/index.js)")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-k", dest="only", action="append", default=[], help=f"run only these worlds: {', '.join(WORLDS)}")
    ap.add_argument("-v", dest="verbose", action="store_true", help="print every run's transcript")
    ap.add_argument("--out", help="where worlds and transcripts go (default: a new temporary directory)")
    ap.add_argument("--checkout", default=str(HERE.parent.parent), help="the checkout to seed origin from")
    ap.add_argument("--metadata-action", help="a checkout of docker/metadata-action (default: clone it once)")
    opts = ap.parse_args()
    unknown = set(opts.only) - set(WORLDS)
    if unknown:
        ap.error(f"unknown world(s): {', '.join(sorted(unknown))}")
    if opts.out and Path(opts.out).is_dir() and any(Path(opts.out).iterdir()):
        ap.error(f"--out {opts.out} is not empty; give a new or empty directory")
    for tool in ("git", "jq", "node", "python3"):
        if not shutil.which(tool):
            sys.exit(f"dry_run: {tool} is not on PATH")
    wfrun.check_bash()
    opts.checkout = Path(opts.checkout).resolve()
    opts.metadata_action = metadata_action(opts.metadata_action, opts.checkout)
    opts.out = Path(opts.out).resolve() if opts.out else Path(tempfile.mkdtemp(prefix="release-dry-run-"))
    opts.out.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "-C", str(opts.checkout), "log", "-1", "--format=%h %s"],
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(opts.checkout), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    opts.describe = f"{head}{' + uncommitted changes' if dirty else ''}"
    print(f"release dry run of {opts.checkout} ({opts.describe})\n")

    report = []
    for name, fn in WORLDS.items():
        if opts.only and name not in opts.only:
            continue
        print(f"{name}: {fn.__doc__}")
        world = World(name, opts, report)
        try:
            fn(world)
        except Exception as err:  # a broken world is a failed check, not a crash
            world.check(f"the world ran to the end ({type(err).__name__})", False, err)
        finally:
            world.transcript.close()
        print()
    failed = [r for r in report if not r[2]]
    print(f"{len(report) - len(failed)}/{len(report)} checks passed; transcripts in {opts.out}")
    for world, name, _, detail in failed:
        print(f"FAILED  {world}: {name}\n        got: {detail}")
    sys.exit(1 if failed or not report else 0)


if __name__ == "__main__":
    main()
