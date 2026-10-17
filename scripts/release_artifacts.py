#!/usr/bin/env python3
"""Verified producer artifacts; fixed public origins, no credentials on downloads."""
from __future__ import annotations

import base64
from email.parser import BytesParser
import hashlib
import io
import os
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import release_pair as pair
from release_contract import (COMMIT, Invalid, Pending, canonical, decode,
                              match, require, sha256)

PRODUCERS = {"engine": "pineforge-4pass/pineforge-engine",
             "codegen": "pineforge-4pass/pineforge-codegen-oss",
             "hpo": "pineforge-4pass/pineforge-hpo"}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected is not None and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(new_url).netloc:
            redirected.remove_header("Authorization")
        require(urllib.parse.urlsplit(new_url).scheme == "https", "non-HTTPS redirect refused")
        return redirected


class Client:
    """404 is missing. Auth, rate limits and every other HTTP/transport error fail."""

    def __init__(self, token=None):
        self.token = os.environ.get("GH_TOKEN", "") if token is None else token

    def request(self, url, *, method="GET", data=None, github=False, binary=False, accept=None, extra_headers=None):
        headers = {"User-Agent": "pineforge-release-reconciler", "Accept": accept or "application/vnd.github+json"}
        if extra_headers:
            require(url.startswith("https://ghcr.io/v2/"), "unexpected registry origin")
            headers.update(extra_headers)
        if github:
            require(url.startswith(("https://api.github.com/", "https://uploads.github.com/")), "unexpected authenticated origin")
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
            if not binary:
                data = canonical(data)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(SafeRedirect()).open(request, timeout=120) as response:
                raw = response.read(512 * 1024 * 1024 + 1)
                require(len(raw) <= 512 * 1024 * 1024, "artifact exceeds size limit")
                return raw
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise Pending("published artifact unavailable") from None
            raise Invalid(f"artifact request failed (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Invalid("artifact transport failed") from None

    def api(self, route, *, method="GET", data=None):
        return decode(self.request("https://api.github.com/" + route, method=method, data=data, github=True) or b"null")

    def pages(self, route):
        result = []
        for page in range(1, 1001):
            rows = self.api(route + ("&" if "?" in route else "?") + f"per_page=100&page={page}")
            require(isinstance(rows, list), "unexpected paginated API response")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Invalid("pagination limit exceeded")

    def download(self, url, origins):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == "https" and parsed.netloc in origins and not parsed.username,
                "unexpected artifact origin")
        # Downloads never receive the GitHub credential, including redirects.
        # Nor GitHub's media type: registry.npmjs.org answers 406 to
        # `Accept: application/vnd.github+json` (hub v1.6.0 readiness, run 38995200706).
        return self.request(url, accept="*/*")

    def tag_commit(self, repo, tag):
        match(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+|(?:a|b|rc)[0-9]+)?", tag)
        obj = self.api(f"repos/{repo}/git/ref/tags/{tag}")["object"]
        for _ in range(8):
            match(COMMIT, obj["sha"])
            if obj["type"] == "commit":
                return obj["sha"]
            require(obj["type"] == "tag", "tag does not resolve to commit")
            obj = self.api(f"repos/{repo}/git/tags/{obj['sha']}")["object"]
        raise Invalid("tag nesting exceeds limit")

    def file(self, repo, commit, path):
        match(COMMIT, commit)
        obj = self.api(f"repos/{repo}/contents/{path}?ref={commit}")
        require(obj["encoding"] == "base64", "unexpected git content encoding")
        raw = base64.b64decode(obj["content"])
        require(git_blob(raw) == obj["sha"], "git blob integrity differs")
        return raw


def git_blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def release(client, component, version):
    v = pair.parse(pair.hpo_version(version, to_semver=True) if component == "hpo" else version)
    tag = "v" + (v.pep440 if component == "hpo" else str(v))
    repo = PRODUCERS[component]
    obj = client.api(f"repos/{repo}/releases/tags/{tag}")
    if obj["draft"]:
        raise Pending("producer release is still draft")
    require(obj["tag_name"] == tag and type(obj["prerelease"]) is bool and obj["prerelease"] == v.prerelease,
            "producer release channel differs")
    commit = client.tag_commit(repo, tag)
    return obj, commit


def asset(client, obj, name, repo):
    found = [item for item in obj["assets"] if item["name"] == name]
    if not found:
        raise Pending("required producer asset unavailable")
    require(len(found) == 1 and found[0].get("state") == "uploaded", "ambiguous or incomplete asset")
    url = f"https://github.com/{repo}/releases/download/{obj['tag_name']}/{name}"
    require(found[0]["browser_download_url"] == url, "asset source identity differs")
    raw = client.download(url, {"github.com"})
    require(len(raw) > 0, "empty producer artifact")
    digest = found[0].get("digest")
    if digest:
        require(digest == "sha256:" + sha256(raw), "release asset digest differs")
    return raw


def tar_files(raw):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        total = 0
        for item in archive:
            require(not item.name.startswith("/") and ".." not in item.name.split("/"), "unsafe archive path")
            require(not item.issym() and not item.islnk(), "archive links refused")
            if item.isfile():
                total += item.size
                require(total <= 256 * 1024 * 1024, "expanded archive exceeds size limit")
                require(item.name not in result, "duplicate archive member")
                result[item.name] = archive.extractfile(item).read()
    return result


def package_sources(files, package):
    result = {}
    marker = package + "/"
    for path, raw in files.items():
        at = path.find(marker)
        if at >= 0 and (at == 0 or path[at - 1] == "/") and path.endswith(".py"):
            name = path[at:]
            require(name not in result, "duplicate package source")
            result[name] = raw
    require(bool(result), "empty package source inventory")
    return result


def package_metadata(files, name, version, *, wheel=False):
    if wheel:
        metadata = [raw for path, raw in files.items() if path.endswith(".dist-info/METADATA")]
    else:
        metadata = [raw for path, raw in files.items() if path.count("/") == 1 and path.endswith("/PKG-INFO")]
    require(len(metadata) == 1, "ambiguous package metadata")
    parsed = BytesParser().parsebytes(metadata[0])
    require(isinstance(parsed["Name"], str) and parsed["Name"].replace("_", "-").lower() == name
            and parsed["Version"] == version, "package metadata identity differs")


def source_matches(client, component, commit, sources, native=None):
    repo = PRODUCERS[component]
    tree = client.api(f"repos/{repo}/git/trees/{commit}?recursive=1")
    require(not tree.get("truncated"), "source tree inventory truncated")
    prefix = "python/" if component == "hpo" else ""
    package = "pineforge_hpo/" if component == "hpo" else "pineforge_codegen/"
    expected = {row["path"][len(prefix):]: row["sha"] for row in tree["tree"]
                if row["type"] == "blob" and row["path"].startswith(prefix + package) and row["path"].endswith(".py")}
    require(expected == {name: git_blob(raw) for name, raw in sources.items()}, "package differs from tagged source")
    if native is not None:
        expected_native = {row["path"]: row["sha"] for row in tree["tree"] if row["type"] == "blob"
                           and (row["path"] == "CMakeLists.txt" or row["path"].startswith(("include/", "src/", "cmake/")))}
        require("CMakeLists.txt" in native and any(name.startswith("src/") for name in native), "empty HPO native source inventory")
        require(expected_native == {name: git_blob(raw) for name, raw in native.items()}, "HPO native sdist differs from tagged source")


def producer(client, component, version):
    """Return manifest pin + byte inventory, after checking even the event's own artifacts."""
    v = pair.parse(pair.hpo_version(version, to_semver=True) if component == "hpo" else version)
    obj, commit = release(client, component, str(v))
    repo = PRODUCERS[component]
    pin = {"version": str(v), "commit": commit}
    inventory = {}
    native = None
    if component == "engine":
        pin["tag"] = "v" + str(v)
        for triple in ("linux-x86_64", "linux-aarch64", "macos-universal"):
            name = f"pineforge-v{v}-{triple}.tar.gz"
            raw = asset(client, obj, name, repo)
            checksum = asset(client, obj, name + ".sha256", repo).decode("ascii").split()
            require(len(checksum) == 2 and checksum[0] == sha256(raw) and checksum[1].lstrip("*") == name,
                    "engine tarball checksum differs")
        return pin, inventory
    if component == "codegen":
        meta = decode(client.download(f"https://pypi.org/pypi/pineforge-codegen/{v.pep440}/json", {"pypi.org"}))
        require(meta["info"]["version"] == v.pep440 and meta["info"]["name"] == "pineforge-codegen", "unexpected PyPI identity")
        sdists = [row for row in meta["urls"] if row["packagetype"] == "sdist" and not row["yanked"]]
        if not sdists:
            raise Pending("codegen sdist unavailable")
        require(len(sdists) == 1, "ambiguous codegen sdist")
        sdist = client.download(sdists[0]["url"], {"files.pythonhosted.org"})
        require(sha256(sdist) == sdists[0]["digests"]["sha256"], "codegen sdist checksum differs")
        sdist_files = tar_files(sdist)
        package_metadata(sdist_files, "pineforge-codegen", v.pep440)
        sources = package_sources(sdist_files, "pineforge_codegen")
        pin.update(pypi_version=v.pep440, sdist_sha256=sha256(sdist))
    else:
        pin.update(package_version=v.pep440, tag="v" + v.pep440)
        wheel = asset(client, obj, f"pineforge_hpo-{v.pep440}-py3-none-any.whl", repo)
        sdist = asset(client, obj, f"pineforge_hpo-{v.pep440}.tar.gz", repo)
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            require(len(archive.namelist()) == len(set(archive.namelist())), "duplicate wheel member")
            require(sum(item.file_size for item in archive.infolist()) <= 256 * 1024 * 1024, "expanded wheel exceeds size limit")
            files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
        package_metadata(files, "pineforge-hpo", v.pep440, wheel=True)
        sources = package_sources(files, "pineforge_hpo")
        sdist_files = tar_files(sdist)
        package_metadata(sdist_files, "pineforge-hpo", v.pep440)
        require(package_sources(sdist_files, "pineforge_hpo") == sources, "HPO wheel and sdist differ")
        unpacked = {path.split("/", 1)[1]: raw for path, raw in sdist_files.items() if "/" in path}
        native = {name: raw for name, raw in unpacked.items() if name == "CMakeLists.txt" or name.startswith(("include/", "src/", "cmake/"))}
        for name in ("hpo_failure_codes.json", "hpo_failure_codes_diff.json"):
            raw = asset(client, obj, name, repo)
            require(raw == files["pineforge_hpo/" + name], "HPO catalog differs from wheel")
            inventory["pineforge_hpo/" + name] = sha256(raw)
        inventory.update({name: sha256(raw) for name, raw in files.items()
                          if name.startswith("pineforge_hpo/hpo_failure_codes") and name.endswith(".json")})
        pin.update(wheel_sha256=sha256(wheel), sdist_sha256=sha256(sdist))
    source_matches(client, component, commit, sources, native)
    inventory.update({name: sha256(raw) for name, raw in sources.items()})
    return pin, inventory


def npm(client, pin, inventory):
    v = pin["version"]
    obj = decode(client.download("https://registry.npmjs.org/@pineforge%2fcodegen-pyodide/" + v, {"registry.npmjs.org"}))
    require(obj["name"] == "@pineforge/codegen-pyodide" and obj["version"] == v, "unexpected npm identity")
    integrity = obj["dist"]["integrity"]
    match(r"sha512-[A-Za-z0-9+/]{86}==", integrity)
    raw = client.download(obj["dist"]["tarball"], {"registry.npmjs.org"})
    require("sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode() == integrity, "npm integrity differs")
    files = tar_files(raw)
    meta = decode(files["package/package.json"])
    require(meta["name"] == obj["name"] and meta["version"] == v, "npm tarball metadata differs")
    release_meta = decode(files["package/release.json"])
    require(release_meta["codegen"] == v, "npm codegen release differs")
    browser_archive = files[f"package/pineforge_codegen-{v}.tar.gz"]
    require(sha256(browser_archive) == release_meta["sha256"], "Pyodide archive digest differs")
    sources = package_sources(files, "pineforge_codegen")
    require(package_sources(tar_files(browser_archive), "pineforge_codegen") == sources, "Pyodide archive and unpacked package differ")
    require({name: sha256(data) for name, data in sources.items()} == inventory, "npm and PyPI codegen sources differ")
    if obj.get("gitHead"):
        require(obj["gitHead"] == pin["commit"], "npm git commit differs")
    return {**pin, "npm_version": v, "npm_integrity": integrity}


def command(*args, input=None):
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, "release command failed: " + args[0])
    return result.stdout
