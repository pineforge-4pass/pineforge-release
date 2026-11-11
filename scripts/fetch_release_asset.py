#!/usr/bin/env python3
"""Acquire one exact GitHub release asset with real curl, API first.

The Dockerfile declares ASSET_FETCH_RETRIES=3 and ASSET_FETCH_RETRY_DELAY=2
and passes both explicitly: N is 0..5 additional attempts, D is 1..10 seconds.
Each curl attempt has a 10-second connection bound and a 120-second total
transfer bound (including at most five redirects). Each curl process also has
a hard wall budget B = (N + 1) * 120 + N * D + 1 seconds, so Retry-After cannot
extend acquisition indefinitely. At most three curl processes run per asset:
release metadata, API asset, browser fallback. Thus the wall budget is at most
3*B: 1461 seconds at the declared defaults, 2313 at the largest allowed inputs,
plus local parsing/file operations. Without redirects, at most 3*(N+1) HTTP
requests are attempted; with redirects the conservative bound is 18*(N+1).

The optional read token is read only from a secret file. Curl receives headers
through stdin configuration, never argv or an environment variable. Curl's
normal redirect handling strips Authorization when the origin changes; never
use --location-trusted. Metadata itself is not redirected, and a metadata asset
URL must equal the expected API origin/repository/asset-id path before auth is
sent there. Explicit loopback roots support real-transport replay; other custom
origins are refused. No download is verified here: successful bytes still go
through the caller's existing sha256sum boundary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit


class AcquisitionError(Exception):
    """A safe public reason, without response bodies, URLs or credentials."""


def bounded_integer(value: str, minimum: int, maximum: int, name: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]?", value):
        raise AcquisitionError(f"invalid {name}")
    number = int(value)
    if not minimum <= number <= maximum:
        raise AcquisitionError(f"invalid {name}")
    return number


def approved_root(value: str, public: str) -> str:
    if value == public:
        return value
    parsed = urlsplit(value)
    # A caller must explicitly select a numeric loopback root for local replay.
    # In particular, an arbitrary HTTPS API origin never receives the token.
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", parsed.path)):
        raise AcquisitionError("unapproved endpoint root")
    return value


def read_token(path: Path) -> str | None:
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
    except FileNotFoundError:
        return None
    if len(raw) > 4096:
        raise AcquisitionError("invalid read token file")
    token = raw.decode("ascii").rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise AcquisitionError("invalid read token file")
    return token


def curl_download(url: str, output: Path, *, token: str | None,
                  binary: bool, redirects: bool, retries: int, delay: int) -> bool:
    """One real curl retry budget; suppress raw diagnostics that may hold URLs."""
    budget = (retries + 1) * 120 + retries * delay + 1
    headers = [
        "User-Agent: pineforge-release-asset-fetch",
        "Accept: application/octet-stream" if binary
        else "Accept: application/vnd.github+json",
    ]
    if token is not None:
        headers.extend(("Authorization: Bearer " + token,
                        "X-GitHub-Api-Version: 2022-11-28"))
    # Config is stdin-only; token validation excludes control characters.
    config = "".join('header = "' + header.replace("\\", "\\\\").replace('"', '\\"')
                     + '"\n' for header in headers)
    protocols = "=http,https" if urlsplit(url).scheme == "http" else "=https"
    command = [
        "curl", "--disable", "--config", "-", "--fail", "--silent", "--show-error",
        "--retry", str(retries), "--retry-all-errors", "--retry-delay", str(delay),
        "--retry-max-time", str(budget - 1),
        "--connect-timeout", "10", "--max-time", "120",
        "--proto", protocols, "--proto-redir", protocols,
        "--output", str(output), "--write-out", "%{http_code}",
    ]
    if redirects:
        command.extend(("--location", "--max-redirs", "5"))
    if not binary:
        command.extend(("--max-filesize", str(4 * 1024 * 1024)))
    command.extend(("--url", url))
    try:
        done = subprocess.run(
            command, input=config, text=True, capture_output=True, timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0 and done.stdout == "200"


def exact_asset(metadata: bytes, *, repo: str, tag: str, name: str,
                api_root: str, browser_url: str) -> str:
    """Malformed/ambiguous successful metadata is a refusal, never a fallback."""
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AcquisitionError("ambiguous release metadata")
            result[key] = value
        return result

    def invalid_constant(value):
        raise AcquisitionError("malformed release metadata")

    try:
        release = json.loads(metadata, object_pairs_hook=unique_fields,
                             parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AcquisitionError("malformed release metadata") from None
    if (not isinstance(release, dict) or release.get("tag_name") != tag
            or release.get("draft") is not False
            or type(release.get("id")) is not int or release["id"] <= 0
            or not isinstance(release.get("assets"), list)):
        raise AcquisitionError("release identity mismatch")
    if any(not isinstance(asset, dict) for asset in release["assets"]):
        raise AcquisitionError("malformed asset metadata")
    matches = [asset for asset in release["assets"] if asset.get("name") == name]
    if len(matches) != 1:
        raise AcquisitionError("missing or ambiguous release asset")
    asset = matches[0]
    if (type(asset.get("id")) is not int or asset["id"] <= 0
            or asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int or asset["size"] < 0):
        raise AcquisitionError("malformed asset identity")
    expected = f"{api_root}/repos/{repo}/releases/assets/{asset['id']}"
    if asset.get("url") != expected or asset.get("browser_download_url") != browser_url:
        raise AcquisitionError("unapproved asset endpoint")
    return expected


def acquire(args) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.repo):
        raise AcquisitionError("invalid repository")
    for value in (args.tag, args.name):
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value)
                or value in {".", ".."}):
            raise AcquisitionError("invalid tag or asset name")
    retries = bounded_integer(args.retries, 0, 5, "ASSET_FETCH_RETRIES")
    delay = bounded_integer(args.retry_delay, 1, 10, "ASSET_FETCH_RETRY_DELAY")
    api_root = approved_root(args.api_root, "https://api.github.com")
    browser_root = approved_root(args.browser_root, "https://github.com")
    token = read_token(args.token_file)
    release_url = f"{api_root}/repos/{args.repo}/releases/tags/{quote(args.tag, safe='')}"
    browser_url = (f"{browser_root}/{args.repo}/releases/download/"
                   f"{quote(args.tag, safe='')}/{quote(args.name, safe='')}")
    with tempfile.TemporaryDirectory(prefix=".asset-fetch-", dir=args.output.parent) as temporary:
        work = Path(temporary)
        metadata = work / "release.json"
        options = {"retries": retries, "delay": delay}
        if curl_download(release_url, metadata, token=token, binary=False,
                         redirects=False, **options):
            api_url = exact_asset(metadata.read_bytes(), repo=args.repo, tag=args.tag,
                                  name=args.name, api_root=api_root, browser_url=browser_url)
            payload = work / "api-asset"
            if curl_download(api_url, payload, token=token, binary=True,
                             redirects=True, **options):
                payload.replace(args.output)
                return
        payload = work / "browser-asset"
        # Public browser fallback never receives Authorization, even on GitHub.
        if curl_download(browser_url, payload, token=None, binary=True,
                         redirects=True, **options):
            payload.replace(args.output)
            return
        raise AcquisitionError("API and browser acquisition exhausted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retries", required=True)
    parser.add_argument("--retry-delay", required=True)
    parser.add_argument("--api-root", default="https://api.github.com")
    parser.add_argument("--browser-root", default="https://github.com")
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/github_token"))
    args = parser.parse_args()
    try:
        acquire(args)
    except AcquisitionError as error:
        print(f"ASSET_FETCH_FAILED: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError):
        print("ASSET_FETCH_FAILED: local acquisition input or tool failure", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
