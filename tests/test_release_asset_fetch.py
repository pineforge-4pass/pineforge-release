"""CR-918 acquisition regressions, replaying the real engine Dockerfile RUN.

Extraction is deliberately mechanical: find the RUN containing the engine's
``sha256sum -c "${base}.tar.gz.sha256"``, keep all its physical continuation
lines, remove only ``RUN ``, the explicit BuildKit secret mount, and Docker's
backslash-newline continuations, and
execute the ENTIRE instruction, including its real tar/mkdir/cp consumers.
There is no replacement download program, verifier, curl shim, or helper import.
The copied helper is executed as a real subprocess from its repository path.

The only shell substitutions are URL/destination roots:

* https://github.com -> the loopback server's /browser root;
* https://api.github.com -> its /api root, if present in the actual RUN;
* cd /tmp and the quoted /tmp/ sources -> CR918_FIXTURE_TMP;
* /opt/pineforge -> CR918_FIXTURE_INSTALL.
* /tmp/fetch_release_asset.py -> the actual COPY source in this repository;
* /run/secrets/github_token -> a file containing only synthetic read-token bytes.

At the Phase A baseline the instruction contains two real browser curl calls,
then the real sha256sum, then extraction/copy. The API fixture is available but
the harness never calls it on the product's behalf. The proposed public retry
inputs are ASSET_FETCH_RETRIES (N additional attempts) and
ASSET_FETCH_RETRY_DELAY (D seconds); the baseline ignores them. The candidate's
secret file contains only a synthetic fixture read token. This maps the actual
helper and secret paths without changing the helper's behavior.

The server accepts only enumerated release/asset routes, validates API auth and
octet-stream Accept, and serves nonempty fixture bytes. Expected hashes come
from independently supplied archive bytes, before any corrupted transfer is
configured. Bash xtrace observes actual commands; it does not replace them.
Diagnostics contain raw exits and safe HTTP/command observations, never token
values, account paths, or captured shell output. Setup problems raise errors,
not the named missing-behavior assertions, and nothing is marked expectedFailure.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parent.parent
ENGINE_CHECK = 'sha256sum -c "${base}.tar.gz.sha256"'
ACQUISITION_REASON = "ASSET_FETCH_FAILED"
HELPER_COPY = "COPY scripts/fetch_release_asset.py /tmp/fetch_release_asset.py"
SECRET_MOUNT = "--mount=type=secret,id=github_token "


@dataclass(frozen=True)
class _Payload:
    version: str
    arch: str
    base: str
    archive: bytes
    checksum: bytes
    members: dict[str, bytes]
    token: str


def _payload(version: str, arch: str, seed: bytes) -> _Payload:
    """Produce independent, nonempty archive inputs, not a product-derived hash."""
    triple = {"amd64": "linux-x86_64", "arm64": "linux-aarch64"}[arch]
    base = f"pineforge-v{version}-{triple}"
    members = {
        "lib/libpineforge.a": b"fixture static library\0" + seed,
        "include/pineforge.h": b"/* loopback fixture */\n" + seed.hex().encode() + b"\n",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for relative, data in members.items():
            entry = tarfile.TarInfo(f"{base}/{relative}")
            entry.size = len(data)
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(data))
    supplied = output.getvalue()
    expected = hashlib.sha256(supplied).hexdigest()
    return _Payload(
        version, arch, base, supplied,
        f"{expected}  {base}.tar.gz\n".encode(),
        members, "fixture-read-" + hashlib.sha256(seed).hexdigest(),
    )


@dataclass(frozen=True)
class _Request:
    route: str
    attempt: int
    status: int
    octet_stream: bool
    authenticated: bool
    body_bytes: int
    at: float

    def safe_record(self, start: float) -> dict:
        return {
            "route": self.route,
            "attempt": self.attempt,
            "status": self.status,
            "octet_stream": self.octet_stream,
            "authenticated": self.authenticated,
            "body_bytes": self.body_bytes,
            "elapsed_seconds": round(self.at - start, 3),
        }


class _LocalRelease:
    """Real HTTP with exact routes, independent per-route response schedules."""

    def __init__(self, payload: _Payload, *, schedules=None, transferred=None):
        self.payload = payload
        self.transferred = payload.archive if transferred is None else transferred
        self.schedules = {
            "api-metadata": (200,),
            "api-archive": (200,),
            "api-checksum": (200,),
            "browser-archive": (200,),
            "browser-checksum": (200,),
        }
        for route, statuses in (schedules or {}).items():
            if route not in self.schedules or not statuses:
                raise RuntimeError("fixture setup: invalid response schedule")
            self.schedules[route] = tuple(statuses)
        self._requests = []
        self._attempts = {}
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner._serve(self)

            def log_message(self, format, *args):
                # Default HTTP logs would expose fixture paths and account names.
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        repo = "pineforge-4pass/pineforge-engine"
        browser_root = f"/browser/{repo}/releases/download/v{payload.version}"
        api_root = f"/api/repos/{repo}/releases"
        archive_name = payload.base + ".tar.gz"
        assets = []
        self.routes = {}
        # IDs vary with the supplied fixture identity, not with observed output.
        first_id = int(hashlib.sha256(payload.base.encode()).hexdigest()[:7], 16)
        for offset, (kind, name, body) in enumerate((
            ("archive", archive_name, self.transferred),
            ("checksum", archive_name + ".sha256", payload.checksum),
        )):
            api_path = f"{api_root}/assets/{first_id + offset}"
            browser_path = f"{browser_root}/{name}"
            self.routes[api_path] = ("api-" + kind, body, "application/octet-stream")
            self.routes[browser_path] = ("browser-" + kind, body, "application/octet-stream")
            assets.append({
                "id": first_id + offset,
                "name": name,
                "state": "uploaded",
                "size": len(body),
                "url": self.origin + api_path,
                "browser_download_url": self.origin + browser_path,
            })
        metadata = json.dumps({
            "id": first_id,
            "tag_name": "v" + payload.version,
            "draft": False,
            "prerelease": False,
            "assets": assets,
        }).encode()
        self.routes[f"{api_root}/tags/v{payload.version}"] = (
            "api-metadata", metadata, "application/json",
        )
        self.browser_archive_url = self.origin + browser_root + "/" + archive_name
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("fixture teardown: loopback server did not stop")

    def requests(self, after: int = 0) -> tuple[_Request, ...]:
        with self._lock:
            return tuple(self._requests[after:])

    def _serve(self, request):
        route, supplied, content_type = self.routes.get(
            request.path, ("unknown", b"unknown fixture route\n", "text/plain"),
        )
        authenticated = request.headers.get("Authorization") == "Bearer " + self.payload.token
        octet_stream = request.headers.get("Accept") == "application/octet-stream"
        with self._lock:
            attempt = self._attempts.get(route, 0) + 1
            self._attempts[route] = attempt
            if route == "unknown":
                status = 404
            elif route.startswith("api-") and not authenticated:
                status = 401
            elif route in ("api-archive", "api-checksum") and not octet_stream:
                status = 406
            else:
                sequence = self.schedules[route]
                status = sequence[min(attempt - 1, len(sequence) - 1)]
            body = supplied if status == 200 else f"fixture HTTP {status}\n".encode()
            self._requests.append(_Request(
                route, attempt, status, octet_stream, authenticated,
                len(body), time.monotonic(),
            ))
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Connection", "close")
        request.end_headers()
        request.wfile.write(body)


def _engine_instruction(dockerfile: str) -> str:
    """Read actual physical RUN lines, never reconstruct acquisition/checksum."""
    lines = iter(dockerfile.splitlines(keepends=True))
    matches = []
    for line in lines:
        if not line.startswith("RUN "):
            continue
        physical = [line]
        while physical[-1].rstrip("\r\n").endswith("\\"):
            continued = next(lines, None)
            if continued is None:
                raise RuntimeError("fixture setup: unterminated Dockerfile RUN")
            physical.append(continued)
        block = "".join(physical)
        if ENGINE_CHECK in block:
            matches.append(block)
    if len(matches) != 1:
        raise RuntimeError("fixture setup: expected one actual engine checksum RUN")
    script = matches[0][len("RUN "):].replace("\\\n", "").rstrip("\n")
    if not script.startswith(SECRET_MOUNT):
        raise RuntimeError("fixture setup: expected the actual optional secret mount")
    script = script[len(SECRET_MOUNT):]
    if "\n" in script:
        raise RuntimeError("fixture setup: engine RUN is not one logical instruction")
    return script


@dataclass(frozen=True)
class _Observation:
    completed: subprocess.CompletedProcess
    requests: tuple[_Request, ...]

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(re.findall(
            r"(?m)^\++ (?:/\S*/)?(sha256sum|tar|mkdir|cp)(?:\s|$)",
            self.completed.stderr,
        ))

    @property
    def reasons(self) -> tuple[str, ...]:
        # Typed stderr contract: a line starts ASSET_FETCH_FAILED: (or that
        # reason alone). A raw curl exit or untyped exception is insufficient.
        return tuple(re.findall(
            r"(?m)^(ASSET_FETCH_FAILED)(?::|$)", self.completed.stderr,
        ))

    def proof(self) -> dict:
        start = self.requests[0].at if self.requests else 0.0
        return {
            "raw_exit": self.completed.returncode,
            "http": [request.safe_record(start) for request in self.requests],
            "executed_steps": self.commands,
            "typed_reasons": self.reasons,
        }


class ReleaseAssetFetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
        engine = dockerfile.split("FROM debian:trixie-slim AS engine\n", 1)[1].split(
            "\nFROM ", 1,
        )[0]
        if engine.splitlines().count(HELPER_COPY) != 1:
            raise RuntimeError("fixture setup: expected the explicit engine helper COPY")
        cls.helper = REPO / "scripts" / "fetch_release_asset.py"
        if not cls.helper.is_file():
            raise RuntimeError("fixture setup: actual copied helper is missing")
        cls.script = _engine_instruction(dockerfile)
        if (cls.script.count("python3 -I /tmp/fetch_release_asset.py ") != 2
                or cls.script.count("--token-file /run/secrets/github_token") != 2):
            raise RuntimeError("fixture setup: expected both real helper/secret invocations")
        # Use system binaries, not any test or lane PATH shims.
        binaries = {name: shutil.which(name, path=os.defpath)
                    for name in ("bash", "python3", "curl", "sha256sum", "tar", "gzip", "mkdir", "cp")}
        missing = [name for name, path in binaries.items() if path is None]
        if missing:
            raise RuntimeError("fixture setup: missing real tools: " + ", ".join(missing))
        cls.bash = binaries["bash"]
        cls.curl = binaries["curl"]

    def _environment(self, root: Path, payload: _Payload, retries: int, delay: int):
        # An explicit empty curl config and a whitelist environment prevent
        # inherited credentials, proxies, startup scripts, or curl configuration.
        (root / ".curlrc").write_text("", encoding="utf-8")
        (root / "github_token").write_text(payload.token, encoding="ascii")
        return {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "CURL_HOME": str(root),
            "NO_PROXY": "127.0.0.1",
            "ENGINE_VERSION": payload.version,
            "TARGETARCH": payload.arch,
            "ASSET_FETCH_RETRIES": str(retries),
            "ASSET_FETCH_RETRY_DELAY": str(delay),
            "CR918_FIXTURE_HELPER": str(self.helper),
            "CR918_FIXTURE_TOKEN_FILE": str(root / "github_token"),
            "CR918_FIXTURE_TMP": str(root / "download"),
            "CR918_FIXTURE_INSTALL": str(root / "installed"),
        }

    def _run(self, label: str, root: Path, release: _LocalRelease, *,
             retries: int, delay: int, after: int = 0) -> _Observation:
        work = root / "download"
        work.mkdir()
        script = self.script.replace(
            "https://github.com", release.origin + "/browser",
        ).replace("https://api.github.com", release.origin + "/api")
        script = script.replace("/tmp/fetch_release_asset.py", '"${CR918_FIXTURE_HELPER}"')
        script = script.replace("/run/secrets/github_token", '"${CR918_FIXTURE_TOKEN_FILE}"')
        script = script.replace("cd /tmp;", 'cd "${CR918_FIXTURE_TMP}";')
        script = script.replace('"/tmp/', '"${CR918_FIXTURE_TMP}/')
        script = script.replace("/opt/pineforge", '"${CR918_FIXTURE_INSTALL}"')
        if "/tmp" in script or "/opt/pineforge" in script:
            raise RuntimeError("fixture setup: unredirected engine destination")
        urls = re.findall(r'https?://[^\s"\']+', script)
        if any(urlsplit(url).netloc != urlsplit(release.origin).netloc for url in urls):
            raise RuntimeError("fixture setup: unredirected external URL")
        if ENGINE_CHECK not in script:
            raise RuntimeError("fixture setup: actual sha256sum command was changed")
        completed = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-x", "-c", script],
            cwd=root,
            env=self._environment(root, release.payload, retries, delay),
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotIn(release.payload.token, completed.stdout)
        self.assertNotIn(release.payload.token, completed.stderr)
        observed = _Observation(completed, release.requests(after))
        print("CR918 " + json.dumps({"case": label, **observed.proof()}, sort_keys=True),
              flush=True)
        return observed

    def _assert_known_routes(self, observed: _Observation):
        self.assertNotIn("unknown", [request.route for request in observed.requests],
                         "product requested an unconfigured fixture route")
        for request in observed.requests:
            if request.route.startswith("browser-"):
                self.assertFalse(request.authenticated,
                                 "the read token reached the public browser fallback")

    def _assert_bytes_and_consumed(self, root: Path, payload: _Payload,
                                   observed: _Observation):
        self.assertEqual(observed.completed.returncode, 0, observed.proof())
        self.assertEqual((root / "download" / (payload.base + ".tar.gz")).read_bytes(),
                         payload.archive)
        self.assertEqual((root / "download" / (payload.base + ".tar.gz.sha256")).read_bytes(),
                         payload.checksum)
        self.assertTrue(payload.base + ".tar.gz: OK" in observed.completed.stdout,
                        "actual sha256sum did not report the supplied archive as OK")
        self.assertEqual(observed.commands.count("sha256sum"), 1)
        self.assertIn("tar", observed.commands, "actual extraction was not executed")
        self.assertLess(observed.commands.index("sha256sum"), observed.commands.index("tar"))
        for relative, supplied in payload.members.items():
            self.assertEqual((root / "installed" / relative).read_bytes(), supplied)
        self._assert_known_routes(observed)

    def test_healthy_fixture_downloads_verifies_and_consumes_supplied_bytes(self):
        for version, arch, seed in (
            ("1.8.7", "amd64", b"healthy archive alpha\0"),
            ("2.3.4", "arm64", bytes(range(96))),
        ):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                payload = _payload(version, arch, seed)
                with _LocalRelease(payload) as release:
                    observed = self._run("healthy", Path(tmp), release, retries=0, delay=1)
                self._assert_bytes_and_consumed(Path(tmp), payload, observed)
                transfers = [request for request in observed.requests
                             if request.route != "api-metadata"]
                self.assertEqual([request.status for request in transfers], [200, 200])
                self.assertEqual([request.body_bytes for request in transfers],
                                 [len(payload.archive), len(payload.checksum)])

    def test_a_real_curl_recovers_after_configured_transient_retries(self):
        cases = (
            ("1.9.2", "amd64", b"two gateway timeouts\0" * 9,
             2, 1, (504, 504, 200), (200,)),
            ("2.5.1", "arm64", bytes(range(255, -1, -1)),
             3, 2, (502, 504, 200), (504, 200)),
        )
        for version, arch, seed, retries, delay, archive_statuses, checksum_statuses in cases:
            with self.subTest(N=retries, D=delay), tempfile.TemporaryDirectory() as tmp:
                payload = _payload(version, arch, seed)
                schedules = {
                    transport + "-" + asset: statuses
                    for transport in ("api", "browser")
                    for asset, statuses in (("archive", archive_statuses),
                                            ("checksum", checksum_statuses))
                }
                with _LocalRelease(payload, schedules=schedules) as release:
                    observed = self._run("transient-recovery", Path(tmp), release,
                                         retries=retries, delay=delay)
                self._assert_known_routes(observed)
                self.assertEqual(
                    observed.completed.returncode, 0,
                    "missing_transient_retry_recovery: actual acquisition must recover; "
                    + json.dumps(observed.proof()),
                )
                self._assert_bytes_and_consumed(Path(tmp), payload, observed)
                for asset, expected in (("archive", archive_statuses),
                                        ("checksum", checksum_statuses)):
                    transfers = [request for request in observed.requests
                                 if request.route.endswith("-" + asset)]
                    self.assertEqual([request.status for request in transfers], list(expected))
                    self.assertEqual(len({request.route for request in transfers}), 1,
                                     "transient recovery must occur within one transport")
                    self.assertEqual([request.attempt for request in transfers],
                                     list(range(1, len(expected) + 1)))
                    self.assertLessEqual(len(transfers), retries + 1)
                    for previous, current in zip(transfers, transfers[1:]):
                        self.assertGreaterEqual(
                            current.at - previous.at, delay * 0.8,
                            "configured real curl retry delay was ignored",
                        )

    def test_b_api_is_primary_when_independently_proven_browser_route_rejects(self):
        for version, arch, seed, browser_status in (
            ("1.8.9", "arm64", b"API primary bytes\0" * 17, 403),
            ("2.6.3", "amd64", bytes(range(113)), 502),
        ):
            with self.subTest(browser_status=browser_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = _payload(version, arch, seed)
                schedules = {
                    "browser-archive": (browser_status,),
                    "browser-checksum": (browser_status,),
                }
                with _LocalRelease(payload, schedules=schedules) as release:
                    # This is an independent real-curl configuration probe, not
                    # a substitute for either product transfer.
                    probe = subprocess.run(
                        [self.curl, "-fsSL", release.browser_archive_url,
                         "-o", str(root / "browser-probe")],
                        cwd=root, env=self._environment(root, payload, 0, 1),
                        stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=10,
                    )
                    probe_observed = _Observation(probe, release.requests())
                    print("CR918 " + json.dumps(
                        {"case": "independent-browser-rejection", **probe_observed.proof()},
                        sort_keys=True,
                    ), flush=True)
                    self.assertEqual(probe.returncode, 22, probe_observed.proof())
                    self.assertEqual(
                        [(request.route, request.status) for request in probe_observed.requests],
                        [("browser-archive", browser_status)],
                    )
                    mark = len(release.requests())
                    observed = self._run("api-primary", root, release,
                                         retries=0, delay=1, after=mark)
                self._assert_known_routes(observed)
                self.assertEqual(
                    observed.completed.returncode, 0,
                    "missing_api_primary_octet_stream: valid API metadata/assets must "
                    "succeed despite the proven browser rejection; "
                    + json.dumps(observed.proof()),
                )
                self._assert_bytes_and_consumed(root, payload, observed)
                self.assertEqual(observed.requests[0].route, "api-metadata")
                metadata = [request for request in observed.requests
                            if request.route == "api-metadata"]
                self.assertTrue(metadata, "actual acquisition never requested API metadata")
                transfers = [request for request in observed.requests
                             if request.route != "api-metadata"]
                self.assertEqual([request.route for request in transfers],
                                 ["api-archive", "api-checksum"])
                self.assertTrue(all(request.status == 200 and request.authenticated
                                    for request in metadata + transfers))
                self.assertTrue(all(request.octet_stream for request in transfers),
                                "real API asset requests require octet-stream Accept")

    def test_c_successful_corrupt_transfer_is_refused_by_actual_sha256sum(self):
        for version, arch, seed, position in (
            ("1.9.4", "amd64", b"independent checksum original\0" * 11, 13),
            ("2.7.5", "arm64", bytes(range(192)), -7),
        ):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = _payload(version, arch, seed)
                index = position % len(payload.archive)
                corrupt = (payload.archive[:index]
                           + bytes([payload.archive[index] ^ 0x5A])
                           + payload.archive[index + 1:])
                self.assertNotEqual(corrupt, payload.archive)
                with _LocalRelease(payload, transferred=corrupt) as release:
                    observed = self._run("corrupt-transfer", root, release,
                                         retries=0, delay=1)
                self._assert_known_routes(observed)
                transfers = [request for request in observed.requests
                             if request.route != "api-metadata"]
                self.assertEqual([request.status for request in transfers], [200, 200],
                                 "checksum control requires genuinely successful transport")
                self.assertEqual([request.body_bytes for request in transfers],
                                 [len(corrupt), len(payload.checksum)])
                self.assertEqual((root / "download" / (payload.base + ".tar.gz")).read_bytes(),
                                 corrupt)
                self.assertEqual(
                    (root / "download" / (payload.base + ".tar.gz.sha256")).read_bytes(),
                    payload.checksum,
                )
                self.assertNotEqual(observed.completed.returncode, 0, observed.proof())
                self.assertEqual(observed.commands, ("sha256sum",))
                self.assertTrue(payload.base + ".tar.gz: FAILED" in observed.completed.stdout,
                                "actual sha256sum did not refuse the corrupted archive")
                self.assertFalse(payload.base + ".tar.gz: OK" in observed.completed.stdout)
                self.assertFalse((root / "download" / payload.base).exists(),
                                 "corrupt bytes reached extraction")
                self.assertFalse((root / "installed").exists(),
                                 "corrupt bytes reached the installed payload")
                self.assertEqual(observed.reasons, (),
                                 "checksum rejection must not become acquisition failure")

    def test_d_exhausted_transports_have_typed_refusal_before_checksum_or_consume(self):
        for version, arch, seed, retries, delay, api_statuses, browser_statuses in (
            ("1.9.6", "amd64", b"finite failure budget one\0", 0, 1, (504,), (503,)),
            ("2.8.7", "arm64", bytes(range(79)), 1, 2, (502, 504), (503, 502)),
        ):
            with self.subTest(N=retries, D=delay), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = _payload(version, arch, seed)
                schedules = {
                    "api-archive": api_statuses,
                    "browser-archive": browser_statuses,
                }
                with _LocalRelease(payload, schedules=schedules) as release:
                    observed = self._run("transport-exhaustion", root, release,
                                         retries=retries, delay=delay)
                self._assert_known_routes(observed)
                self.assertNotEqual(observed.completed.returncode, 0, observed.proof())
                self.assertTrue(observed.requests, "no real transport was attempted")
                self.assertFalse(any(request.status == 200 and request.route.endswith("-archive")
                                     for request in observed.requests))
                # Keep this assertion separately named: the baseline's raw curl
                # refusal is real, but does not satisfy the typed contract.
                with self.subTest(contract="typed_acquisition_refusal"):
                    self.assertIn(
                        ACQUISITION_REASON, observed.reasons,
                        "missing_typed_acquisition_refusal: exhausted real transfers "
                        "must emit ASSET_FETCH_FAILED; " + json.dumps(observed.proof()),
                    )
                with self.subTest(contract="no_checksum_or_consume_after_failure"):
                    self.assertEqual(observed.commands, ())
                    self.assertFalse(any(request.route.endswith("-checksum")
                                         for request in observed.requests))
                    self.assertFalse(
                        (root / "download" / (payload.base + ".tar.gz.sha256")).exists(),
                    )
                    self.assertFalse((root / "download" / payload.base).exists())
                    self.assertFalse((root / "installed").exists())
                with self.subTest(contract="bounded_api_then_browser_attempts"):
                    transfers = [request for request in observed.requests
                                 if request.route != "api-metadata"]
                    self.assertEqual(
                        [request.route for request in transfers],
                        ["api-archive"] * (retries + 1)
                        + ["browser-archive"] * (retries + 1),
                        "both primary and fallback must exhaust the supplied finite budget",
                    )
                    self.assertEqual(
                        [request.status for request in transfers],
                        list(api_statuses) + list(browser_statuses),
                    )
                    for route in ("api-archive", "browser-archive"):
                        attempts = [request.attempt for request in transfers
                                    if request.route == route]
                        self.assertEqual(attempts, list(range(1, retries + 2)))


if __name__ == "__main__":
    unittest.main()
