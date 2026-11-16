#!/usr/bin/env python3
"""Build a public, read-only proposal for the engine README."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from facts_engine_release import select_engine_release
from facts_validate import (FACTS_SCHEMA, MAX_INPUT_BYTES, InputError, decode, digest,
                            read_bytes, require)

HUB = "pineforge-4pass/pineforge-release"
ENGINE = "pineforge-4pass/pineforge-engine"
OWNER = "pineforge-4pass"
SCHEMA = "pineforge/engine-readme-plan/v1"
API_ORIGIN = "https://api.github.com"
BRIDGE = Path(__file__).with_name("facts_engine_render.mjs")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
MACHINE_MARKER = re.compile(r"<!-- pineforge:engine-readme-plan/v1 plan_identity=([0-9a-f]{64}) -->")
GENERATION_LIMIT = (
    "Changed facts under the same release tag produce a different identity and branch. "
    "A separately authorized publisher must settle superseded open generations before activation."
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def commit(value):
    require(type(value) is str and COMMIT.fullmatch(value), "invalid commit identity")
    return value


def branch(value):
    require(type(value) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)
            and not any(part in value for part in ("..", "//", "@{"))
            and all(part and not part.startswith(".") and not part.endswith((".", ".lock"))
                    for part in value.split("/")), "unsafe Git ref name")
    return value


def repository(value, expected, expected_id=None):
    require(type(value) is dict and expected in (HUB, ENGINE), "invalid repository metadata")
    owner, name = expected.split("/")
    require(value.get("private") is False and value.get("full_name") == expected
            and value.get("name") == name and type(value.get("owner")) is dict
            and value["owner"].get("login") == owner
            and type(value.get("id")) is int and value["id"] > 0
            and value.get("html_url") == "https://github.com/" + expected
            and value.get("url") == API_ORIGIN + "/repos/" + expected
            and value.get("visibility", "public") == "public",
            "exact public repository was not confirmed")
    if expected_id is not None:
        require(value["id"] == expected_id, "repository numeric identity changed")
    branch(value.get("default_branch"))
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InputError("GitHub redirect refused")


class PublicGitHub:
    """A dedicated GET transport; no write credential or mutation interface."""

    def __init__(self, test_api=None):
        self.origin = API_ORIGIN
        self.token = os.environ.get("FACTS_READ_TOKEN", "")
        require(len(self.token) <= 4096 and (not self.token or
                re.fullmatch(r"[A-Za-z0-9_.-]+", self.token)), "invalid read credential")
        if test_api is not None:
            try:
                parsed = urlsplit(test_api)
                valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                         and parsed.port is not None and 1 <= parsed.port <= 65535
                         and parsed.netloc == "127.0.0.1:" + str(parsed.port)
                         and not parsed.path and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            require(valid, "test API must be an explicit loopback origin")
            require(not self.token or self.token.startswith("test-"),
                    "real credentials are forbidden on the test transport")
            self.origin = test_api
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _get(self, path):
        require(type(path) is str and len(path) <= 4096
                and any(path == "/repos/" + repo or path.startswith("/repos/" + repo + "/")
                        for repo in (HUB, ENGINE))
                and not any(char in path for char in ("\r", "\n", "\\", "#"))
                and ".." not in path and "//" not in path, "invalid public API path")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "pineforge-engine-readme-plan", "Cache-Control": "no-cache",
                   "Accept-Encoding": "identity"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with self.opener.open(Request(self.origin + path, headers=headers, method="GET"),
                                  timeout=30) as response:
                require(response.status == 200, "unexpected GitHub response status")
                require(response.headers.get("Content-Encoding", "identity") == "identity",
                        "encoded GitHub response refused")
                length = response.headers.get("Content-Length")
                require(length is None or (re.fullmatch(r"[0-9]{1,10}", length)
                        and int(length) <= MAX_INPUT_BYTES), "GitHub response exceeds size limit")
                raw = response.read(MAX_INPUT_BYTES + 1)
                require(length is None or len(raw) == int(length), "truncated GitHub response")
                links = response.headers.get("Link", "")
                require(len(links) <= 8192, "oversize pagination metadata")
            return decode(raw), links
        except HTTPError as error:
            error.close()
            raise InputError("GitHub GET failed") from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise InputError("GitHub GET unavailable") from None

    def get(self, path):
        return self._get(path)[0]

    def public_repository(self, name, expected_id=None):
        require(name in (HUB, ENGINE), "repository is outside the public plan")
        return repository(self.get("/repos/" + name), name, expected_id)

    def ref(self, repo, kind, name):
        require(repo in (HUB, ENGINE) and kind in ("heads", "tags"), "invalid ref role")
        branch(name)
        value = self.get(f"/repos/{repo}/git/ref/{kind}/{quote(name, safe='')}")
        require(type(value) is dict and value.get("ref") == f"refs/{kind}/{name}",
                "Git ref identity differs")
        obj = value.get("object")
        require(type(obj) is dict and obj.get("type") in ("commit", "tag"), "invalid Git ref object")
        commit(obj.get("sha"))
        if kind == "heads":
            require(obj["type"] == "commit", "branch does not identify a commit")
        return obj

    def branch_commit(self, repo, name):
        return self.ref(repo, "heads", name)["sha"]

    def tag_commit(self, release_tag):
        obj = self.ref(ENGINE, "tags", release_tag)
        seen = set()
        depth = 0
        while obj["type"] == "tag":
            sha = commit(obj["sha"])
            require(sha not in seen and depth < 8, "tag cycle or depth limit")
            seen.add(sha)
            value = self.get(f"/repos/{ENGINE}/git/tags/{sha}")
            require(type(value) is dict and value.get("sha") == sha
                    and type(value.get("tag")) is str, "annotated tag identity differs")
            branch(value["tag"])
            obj = value.get("object")
            require(type(obj) is dict and obj.get("type") in ("commit", "tag"),
                    "invalid annotated tag target")
            commit(obj.get("sha"))
            depth += 1
        return commit(obj["sha"])

    def contents(self, repo, sha, name):
        commit(sha)
        require((repo == HUB and name in ("facts/facts.json", "facts/facts.schema.json", "VERSION"))
                or (repo == ENGINE and name == "README.md"), "invalid immutable content role")
        value = self.get(f"/repos/{repo}/contents/{name}?ref={sha}")
        require(type(value) is dict and value.get("type") == "file"
                and value.get("path") == name and value.get("name") == name.rsplit("/", 1)[-1]
                and value.get("encoding") == "base64" and type(value.get("content")) is str
                and type(value.get("size")) is int and 0 <= value["size"] <= MAX_INPUT_BYTES,
                "invalid Contents envelope")
        try:
            raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        except (ValueError, UnicodeError):
            raise InputError("invalid Contents encoding") from None
        require(len(raw) == value["size"] and len(raw) <= MAX_INPUT_BYTES, "Contents size differs")
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        require(value.get("sha") == blob, "Contents Git blob digest differs")
        return raw

    def pull_page(self, head, base, page):
        require(type(page) is int and 1 <= page <= 20, "pull request pagination limit")
        query = {"state": "open", "head": OWNER + ":" + branch(head), "base": branch(base),
                 "per_page": "100", "page": str(page)}
        path = f"/repos/{ENGINE}/pulls"
        rows, links = self._get(path + "?" + urlencode(query))
        require(type(rows) is list and len(rows) <= 100, "invalid pull request page")
        relations = {}
        if links:
            for part in links.split(","):
                match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|last|first|prev)"\s*', part)
                require(match is not None and match[2] not in relations, "invalid pagination metadata")
                url = urlsplit(match[1])
                expected = urlsplit(self.origin)
                params = parse_qs(url.query, keep_blank_values=True)
                require(url.scheme == expected.scheme and url.netloc == expected.netloc
                        and url.path == path and not url.fragment
                        and set(params) == set(query)
                        and all(params[key] == [value] for key, value in query.items() if key != "page")
                        and len(params["page"]) == 1
                        and re.fullmatch(r"[1-9][0-9]*", params["page"][0]),
                        "foreign or malformed pagination target")
                number = int(params["page"][0])
                require(1 <= number <= 20, "pull request pagination limit")
                relations[match[2]] = number
            require("next" not in relations or relations["next"] == page + 1,
                    "discontinuous pull request pagination")
            require("first" not in relations or relations["first"] == 1,
                    "invalid first pagination page")
            require("prev" not in relations or relations["prev"] == page - 1,
                    "invalid previous pagination page")
            require("last" not in relations or relations["last"] >= page,
                    "invalid last pagination page")
            require("last" not in relations or relations["last"] <= page or "next" in relations,
                    "truncated pull request pagination")
        require(not (len(rows) < 100 and "next" in relations), "truncated pull request page")
        return rows

    def pull(self, number):
        require(type(number) is int and number > 0, "invalid pull request number")
        return self.get(f"/repos/{ENGINE}/pulls/{number}")


def utf8(raw):
    require(type(raw) is bytes and len(raw) <= MAX_INPUT_BYTES, "invalid README bytes")
    try:
        text = raw.decode("utf-8")
        require(text.encode("utf-8") == raw, "README UTF-8 roundtrip differs")
        return text
    except UnicodeError:
        raise InputError("README is not UTF-8") from None


def checked_render(value):
    require(type(value) is dict and set(value) == {"text", "markers"}
            and type(value["text"]) is str and type(value["markers"]) is int
            and 0 < value["markers"] <= MAX_INPUT_BYTES, "invalid or empty renderer result")
    try:
        raw = value["text"].encode("utf-8")
    except UnicodeError:
        raise InputError("invalid renderer UTF-8") from None
    utf8(raw)
    return raw


def render_with_node(text, facts):
    """Execute the local Node 20 bridge; renderer errors never qualify as a plan."""
    payload = canonical({"text": text, "facts": facts})
    require(len(payload) <= MAX_INPUT_BYTES, "renderer request exceeds size limit")
    try:
        result = subprocess.run(["node", "--max-old-space-size=128", str(BRIDGE)], input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                check=False, shell=False,
                                env={"PATH": os.environ.get("PATH", os.defpath)})
    except (OSError, subprocess.SubprocessError):
        raise InputError("engine README renderer unavailable") from None
    require(result.returncode == 0 and not result.stderr, "engine README renderer failed")
    value = decode(result.stdout)
    checked_render(value)
    return value


def pull_identity(value, repo_id, head, base, plan_identity):
    require(type(value) is dict and type(value.get("id")) is int and value["id"] > 0
            and type(value.get("number")) is int and value["number"] > 0
            and value.get("state") == "open", "invalid open pull request")
    require(value.get("html_url") == f'https://github.com/{ENGINE}/pull/{value["number"]}'
            and value.get("url") == f'{API_ORIGIN}/repos/{ENGINE}/pulls/{value["number"]}',
            "pull request public URL differs")
    for side, expected in (("head", head), ("base", base)):
        endpoint = value.get(side)
        require(type(endpoint) is dict and endpoint.get("ref") == expected
                and endpoint.get("label") == OWNER + ":" + expected,
                "pull request branch differs")
        repository(endpoint.get("repo"), ENGINE, repo_id)
        commit(endpoint.get("sha"))
    body = value.get("body")
    require(type(body) is str, "pull request body is unavailable")
    try:
        require(len(body.encode("utf-8")) <= MAX_INPUT_BYTES, "pull request body exceeds size limit")
    except UnicodeError:
        raise InputError("invalid pull request body UTF-8") from None
    markers = list(MACHINE_MARKER.finditer(body))
    require(len(markers) == 1 and markers[0][1] == plan_identity
            and body.count("pineforge:engine-readme-plan") == 1,
            "foreign or corrupt pull request machine identity")
    return value


def existing_pull(api, repo_id, head, base, plan_identity):
    matches = []
    seen_ids, seen_numbers = set(), set()
    for page in range(1, 21):
        rows = api.pull_page(head, base, page)
        for row in rows:
            pull_identity(row, repo_id, head, base, plan_identity)
            require(row["id"] not in seen_ids and row["number"] not in seen_numbers,
                    "duplicate pull request result")
            seen_ids.add(row["id"])
            seen_numbers.add(row["number"])
            matches.append(row)
        if len(rows) < 100:
            break
    else:
        raise InputError("pull request pagination limit")
    require(len(matches) <= 1, "ambiguous open pull requests")
    if not matches:
        return None
    listed = matches[0]
    current = pull_identity(api.pull(listed["number"]), repo_id, head, base, plan_identity)
    require(current["id"] == listed["id"] and current["number"] == listed["number"],
            "pull request identity changed")
    return current


def build_engine_plan(api, renderer):
    """Return a JSON-safe review artifact. This function has no remote write path."""
    hub_repo = api.public_repository(HUB)
    target_repo = api.public_repository(ENGINE)
    hub_commit = api.branch_commit(HUB, "main")
    facts_raw = api.contents(HUB, hub_commit, "facts/facts.json")
    schema_raw = api.contents(HUB, hub_commit, "facts/facts.schema.json")
    version_raw = api.contents(HUB, hub_commit, "VERSION")
    require(schema_raw == read_bytes(FACTS_SCHEMA), "source schema differs from the local schema")
    selection = select_engine_release(facts_raw, version_raw, hub_commit=hub_commit,
                                      expected_facts_sha256=digest(facts_raw))
    tag_commit = api.tag_commit(selection.release_tag)
    base = target_repo["default_branch"]
    target_commit = api.branch_commit(ENGINE, base)
    before = api.contents(ENGINE, target_commit, "README.md")
    rendered = renderer(utf8(before), decode(facts_raw))
    after = checked_render(rendered)
    identity_input = {"repository_role": "engine", "repository": ENGINE,
                      "release_tag": selection.release_tag, "facts_sha256": selection.facts_sha256}
    plan_identity = digest(canonical(identity_input))
    require(IDENTITY.fullmatch(plan_identity), "invalid plan identity")
    head = "automation/facts-engine-" + plan_identity[:24]
    require(head != base, "proposal branch equals target branch")
    binding = {"plan_identity": plan_identity, "repository": ENGINE,
               "hub_commit": hub_commit, "facts_sha256": selection.facts_sha256,
               "version": selection.release_version, "version_sha256": digest(version_raw),
               "release_tag": selection.release_tag, "tag_commit": tag_commit,
               "base": base, "base_commit": target_commit, "head": OWNER + ":" + head,
               "readme_before_sha256": digest(before), "readme_after_sha256": digest(after)}
    body = (f"<!-- pineforge:engine-readme-plan/v1 plan_identity={plan_identity} -->\n\n"
            "Update the existing engine README fact markers from the selected public release facts.\n\n"
            "```json\n" + json.dumps(binding, sort_keys=True, indent=2) + "\n```\n\n"
            + GENERATION_LIMIT + "\n")
    expected_body_sha = digest(body.encode("utf-8"))
    current = existing_pull(api, target_repo["id"], head, base, plan_identity)
    existing = None
    equal = False
    if current is not None:
        current_head = api.branch_commit(ENGINE, head)
        current_readme = api.contents(ENGINE, current_head, "README.md")
        body_sha = digest(current["body"].encode("utf-8"))
        equal = (current_head == current["head"]["sha"] and current["base"]["sha"] == target_commit
                 and digest(current_readme) == digest(after) and body_sha == expected_body_sha)
        existing = {"id": current["id"], "number": current["number"], "url": current["html_url"],
                    "head_commit": current_head, "reported_head_commit": current["head"]["sha"],
                    "readme_sha256": digest(current_readme), "body_sha256": body_sha}
    changed = before != after
    status = ("noop" if not changed else "create-needed" if current is None
              else "already-planned" if equal else "update-needed")
    plan = {"schema": SCHEMA, "status": status, "plan_identity": plan_identity,
            "identity_input": identity_input,
            "snapshot": {"repository": HUB, "repository_id": hub_repo["id"], "ref": "main",
                         "commit": hub_commit, "facts_sha256": selection.facts_sha256,
                         "schema_sha256": digest(schema_raw), "version": selection.release_version,
                         "version_sha256": digest(version_raw),
                         "version_bytes_base64": base64.b64encode(version_raw).decode("ascii")},
            "release": {"tag": selection.release_tag, "commit": tag_commit,
                        "source_url": selection.source_url},
            "target": {"repository": ENGINE, "repository_id": target_repo["id"], "base": base,
                       "base_commit": target_commit, "head": head},
            "readme": {"path": "README.md", "before_sha256": digest(before),
                       "after_sha256": digest(after), "markers": rendered["markers"], "changed": changed},
            "limitations": [GENERATION_LIMIT],
            "review_only": True, "remote_writes": 0}
    if existing is not None:
        plan["existing_pr"] = existing
    # The last source reads precede emission. A changed default branch also invalidates the plan.
    observed_hub = api.branch_commit(HUB, "main")
    observed_repo = api.public_repository(ENGINE, target_repo["id"])
    observed_base = observed_repo["default_branch"]
    observed_target = api.branch_commit(ENGINE, observed_base)
    plan["recheck"] = {"hub_commit": observed_hub, "base": observed_base, "base_commit": observed_target}
    if observed_hub != hub_commit or observed_base != base or observed_target != target_commit:
        plan["status"] = "superseded"
    elif changed:
        plan["proposed_readme"] = utf8(after)
        plan["proposed_pr"] = {"title": "Update engine README facts for " + selection.release_tag,
                               "body": body, "body_sha256": expected_body_sha}
    return plan


def write_plan(directory, plan):
    """Publish local review files only after every source check has completed."""
    payloads = {}
    if "proposed_readme" in plan:
        payloads["README.md"] = plan["proposed_readme"].encode("utf-8")
    manifest = {key: value for key, value in plan.items() if key != "proposed_readme"}
    payloads["plan.json"] = json.dumps(manifest, sort_keys=True, indent=2,
                                       ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
    # mkdir and exclusive opens protect every pre-existing output, including symlinks.
    directory.mkdir()
    created = []
    try:
        for name, raw in payloads.items():
            path = directory / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(raw)
    except OSError:
        for path in reversed(created):
            path.unlink()
        directory.rmdir()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("plan", allow_abbrev=False)
    command.add_argument("--output-dir", type=Path, required=True)
    command.add_argument("--test-api")
    args = parser.parse_args(argv)
    try:
        require(not os.path.lexists(args.output_dir), "output directory already exists")
        plan = build_engine_plan(PublicGitHub(args.test_api), render_with_node)
        write_plan(args.output_dir, plan)
        print(json.dumps({"schema": SCHEMA, "status": plan["status"],
                          "plan_identity": plan["plan_identity"], "review_only": True,
                          "remote_writes": 0}, sort_keys=True))
        return 0
    except (InputError, OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
        print("engine-readme-plan: input unavailable or refused; no remote changes", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
