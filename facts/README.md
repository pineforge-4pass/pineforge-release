# PineForge facts tokens

`facts.json` is the public, machine-written quantity store shared by the engine,
codegen, corpus, website and hosted MCP. `facts.schema.json` defines its contract.
Only `lab facts export` writes this file; do not hand-edit quantities.

The exporter reads the parity registry's active baseline and digest-verified
snapshot/population. Each scoreboard records its baseline id, snapshot digest,
engine/codegen commits and UTC promotion date. The evidence digest references
private receipts without publishing private infrastructure or probe identities.
`releases[version].scoreboard` retains the immutable baseline a released pair was
graded on. Tags preserve the file in their tagged tree; advancing main does not
rewrite a release's numbers or create a product release.

Registry trade-row totals and distinct source/slug counts are private evidence
only, referenced by `privateEvidenceSha256`; they are not public trade or script
inventories. Website-only metadata, MCP quotas, endpoints and gallery data stay
in the website facts module.

## Inventory source and refill rule

`inventory` is historical, independent of the active scoreboard. `sourceRelease`,
`sourceCommit` and `populationSha256` bind it to an immutable release baseline
and its public engine README. `lab facts export` always reads that pinned source,
even after an active population rebind; consumers validate against the source
release, never against main. Do not describe these inventory totals as the new
population's inventory. Rebind regression tests cover export, README rendering
and the website facts module.

To refill inventory, select one release mapping's `inventoryCommit` in the
campaign exporter configuration, pinned to a public README with the authored
script and closed-trade inventory. Export checks its probe/script sums against
that release's measured population and fails if the evidence is missing or
inconsistent. Remove the previous mapping's inventory pin when selecting a new
source. Never infer authored scripts or deduplicated trades from registry slugs
or trade rows. A main promotion or population rebind alone does not refill it.

Hard-lane membership and its corpus/closed probe counts derive from registry
`hard` surfaces. The lane table places them first without a hardcoded market.

Maintainers export to a checkout, open a facts update PR here, then refresh pinned
copies in the website and hosted MCP. Their builds use committed copies so an
offline build is deterministic and does not silently change when main advances;
an online drift check compares those copies with this public repo's main file.

READMEs use readable hidden markers. Run `lab facts render --repo PATH --facts
facts/facts.json`, then `lab facts check` with the same file (or a raw URL pinned
to a commit). The default raw URL is this repository's main file. Render only
rewrites marked values; check is read-only. Publish in order: campaign tooling,
this hub, website, READMEs, hosted MCP. Never replace release metrics with main's.
Use `code` or `short-code` formats to put backticks inside marker bounds. Table
format separators are escaped by the renderer so GitHub keeps every column.

The hub's `publish` workflow triggers only on `v*` tags; `handle-upstream` only
on upstream release dispatches. A facts-only branch or main change triggers
neither. The ordinary test workflow may still run.

## Public repository descriptions

`repo-descriptions.json` is the versioned description policy, separate from the
registry-derived facts. It contains only confirmed-public repository identities,
approved product wording, templates, and license labels with exact public
`LICENSE` commit URLs. It makes no new licensing decision. Held repositories
retain their published text, are checked for drift, and never receive a command.
Nonpublic repositories are excluded from public policy, output, and artifacts.

HPO is now a managed public role following its v0.11.0 release. Its original
code uses PineForge Source License 1.2 from v0.11.0; versions through v0.10.0
remain Apache-2.0. Personal trading and noncommercial use are free under the
license's definitions; commercial use needs a license, shared with codegen.
The HPO policy has its own immutable [LICENSE](https://github.com/pineforge-4pass/pineforge-hpo/blob/dab7b6775588da0f112eb363a70561c212dce80b/LICENSE) pin, verified
against the released [README](https://github.com/pineforge-4pass/pineforge-hpo/blob/dab7b6775588da0f112eb363a70561c212dce80b/README.md#license). This changes HPO's current
description policy only; other products' terms and historical releases retain
their existing pins. The conservative description adds no parity or performance
claim.

Quantities, dates, release identifiers, and product commits use
`{{facts:token.name|format}}` from the explicitly supplied `facts.json`; formats
are `int`, `grouped` (thousands separators), `decimal`, and `text` (strings only).
For example, `releases.1.0.1.scoreboard.graded` selects that immutable release,
not current main. `{{license:id}}` inserts a centrally approved label and records
its pinned source. `{{word:id}}` inserts approved static product wording, such as
language/ABI standard names; it is not a quantity store.

The engine preserves the approved published stopgap exactly. Its parenthesized
open-corpus strategy and closed-test script counts are **historical inventory**
from `inventory`, not the current graded-probe denominator. The policy records
that scope explicitly. Corpus copy names the historical source release separately
from current graded probes. Advancing the scoreboard never refills inventory or
changes a released scoreboard.

The approved engine all-graded claim fails closed unless `scoreboard.belowStrong`
is zero and `scoreboard.excellent + scoreboard.strong == scoreboard.graded`.
An unsupported snapshot is refused before output or live API calls, so it cannot
produce partial command proposals. GitHub owner/repository identities compare
case-insensitively for source verification, duplicates, live reads, and holds;
pinned commits and the `LICENSE` source path still match exactly.

## Stage 1 validation and descriptions reconciliation

Stage 1 validates and reconciles descriptions. Export and promotion keep using
the existing reviewed process. The independent Stage 2 dispatch signal is
described below; consumer publication, hosted cards, README switches and the
readable-text branch have separate rollout requirements. The card renderer here
emits bytes for validation; it deploys nothing.

Install the pinned validator in the approved test environment:

```sh
python3 -m pip install -r facts/requirements-validation.txt
python3 scripts/facts_validate.py --facts facts/facts.json
python3 scripts/repo_descriptions.py render --facts facts/facts.json
python3 scripts/facts_cards.py --facts facts/facts.json --variant full --format svg
python3 scripts/facts_cards.py --facts facts/facts.json --variant corpus --format json
```

The complete Draft 2020-12 schema runs with format checking, followed by semantic
checks on every current and released scoreboard: safe integer counts, tier/lane/
scope sums, percentages, unique lane identities, hard-lane evidence, source
identities and matching provenance. Historical inventory binds to its source
release and its sums, independently of current main. Remote schema references
are disabled. Descriptions additionally enforce the all-graded claim, public
role identities, license source URLs, length, and fixed managed scope. Invalid inputs
produce no stdout and no consumer calls. All source and policy bytes validate
before prepare emits files or minting becomes eligible.

`facts-validate.yml` runs on PRs and main pushes touching `facts/**` or affected
scripts, tests or workflows. Its token has only `contents: read`; PR code never
receives App credentials. It renders full/corpus SVG in light/dark themes and
endpoint JSON, compares deterministic bytes, and runs subprocess acceptance that
parses the actual SVG and JSON and checks their facts. `python-test.yml` also
runs the existing description renderer acceptance regressions.

Exact prose acceptance uses `tests/fixtures/repo-descriptions-facts.json`, a
frozen snapshot that canonical promotions must not refresh. Current-facts
checks derive their quantities and provenance from the supplied document;
consistent synthetic advances change counts, date and engine together while
preserving historical inventory and released scoreboards. Schema/semantic
rejection and the unsupported below-strong-domain refusal remain required.

`facts-descriptions.yml` replaces the retired duplicate drift workflow. A main
push touching its path filters wakes it even when the commit includes other
files. Manual dispatch defaults to `dry_run=true`; a schedule reconciles every
half hour. The event commit is a wake-up, not the source pin: prepare resolves
current main, fetches facts and policy by that immutable commit, verifies Git
blob integrity, validates everything, and records SHA256. Reconcile validates
that snapshot again and confirms the immutable bytes before comparisons.

Only the four managed public roles (engine, codegen, corpus, HPO) can be token targets
or writes, in that order. Static rows are audit-only. Every
read confirms the exact public repository with case-insensitive owner/repository
matching. Equal descriptions are no-ops. Writes use only
`PATCH https://api.github.com/repos/{owner}/{repo}` with the exact JSON object
`{"description": renderedText}`. There are no name, visibility or other keys.
Every write is followed by a separate GET and exact readback comparison. Main
is re-read immediately before each write. Source movement yields a superseded
receipt and defers to a later serialized run. This avoids writing an already
superseded snapshot; GitHub offers no atomic transaction between the source ref
and repository settings, so the next scheduled run remains the recovery path
for a main change concurrent with a request.

The consumer has its own concurrency group, with `cancel-in-progress: false`.
Cancelled runs do not emit alerts. Failed reads, network errors, rate limits,
identity/visibility mismatches and refused PATCHes are errors. A failed token
mint is explicitly `mint-failure-unclassified` and fails the run while reporting
read-only drift: the action provides no reliable structured permission diagnosis.
It never claims proven permission absence, and a PATCH 403 is never converted
into a permission-unavailable success. Missing setup reports without displaying
secret values. Read-only comparisons use `github.token`; there is no fallback
to the release App.

## TOP setup and migration checklist

TOP alone provisions settings/secrets and enables application after independent
review and reviewed dry-run proof. Provisioning has been reported for the new
descriptions App/environment; the executor does not inspect secrets or mint
live tokens to verify it. Live environment/App proof, including the authorized
four-repository descriptions scope, remains pending. TOP reports that the old
org App is Contents-only and that `release-automation` exists with target secrets.
TOP reports that the new App key is provisioned in `release-automation` and that
this release repository's private-key fallback has been removed. This is a TOP
setup receipt, not an executor secret inspection. The first protected release's
no-fallback proof remains separate; engine/codegen environment moves follow
their own rollout. Never inspect or hash secret values to establish origin.

1. Protect main. Configure environment `descriptions` with selected deployment
   **branch `main` only**, no tag patterns or other branches. Use the new
   descriptions App, only the four selected public managed repositories above, with
   Administration: write and the required Metadata: read. Do not grant Contents
   write or install it on held/static rows. Keep the exact environment secrets
   `DESCRIPTIONS_APP_ID` and `DESCRIPTIONS_APP_PRIVATE_KEY`.
2. Keep environment variable `FACTS_DESCRIPTIONS_APPLY_ENABLED` set to literal
   `false` in `descriptions`. Read the organization, repository and environment
   values and verify the protected workflow's effective value is `false`; an
   absent environment value can expose an inherited `true`. PRs have no App
   access. Dispatch with `dry_run=true` and review the
   immutable source receipt and every delta. This comparison does not mint a
   token. A live App mint/apply proof needs TOP's separate authorization.
3. Configure `release-automation` with selected deployment **branch `main` and
   tag pattern `v*` only**. Confirm the new `PINEFORGE_APP_ID` and
   `PINEFORGE_APP_PRIVATE_KEY` are provisioned there, together with
   `RELEASE_HOSTED_MCP_REPOSITORY` and `RELEASE_APPLICATION_REPOSITORY` as
   environment secrets containing the existing consumer repository short names.
   Confirm each neutral matrix label maps to its existing consumer. The offline
   public consumer stays explicit. Missing target setup fails before minting,
   rather than falling back to all repositories. Never put nonpublic identities
   in public YAML, artifacts or logs.
4. Verify the next protected release without the repository-level App-key
   fallback, which TOP reports removed. A no-op mint does not prove the bump push or consumer mapping;
   those need their applicable release proof. Tokens request Contents: write on
   only the job's repository, never Administration. Other repositories' key moves
   are separate work; scoped inputs alone do not remove inherited credentials.
5. After reviewed dry-run and separately authorized live proof, TOP may set
   `FACTS_DESCRIPTIONS_APPLY_ENABLED=true` in `descriptions`. Writes additionally
   require protected `refs/heads/main`, an authorized push/manual/schedule event,
   and effective `dry_run=false`. With rollout enabled, push/schedule reconcile
   automatically; manual dispatch retains its safe default. To stop subsequent
   application, set the environment variable to literal `false`, then read back
   organization, repository and environment values and verify the protected
   workflow's effective value is `false`. Do not delete the environment value:
   that can expose an inherited `true`. Keep the retired drift job retired.

These are TOP operator commands, not steps executed by the implementation lane.
Use the approved GitHub wrapper where one is required:

```sh
GH_HOST=github.com gh workflow run facts-descriptions.yml --repo pineforge-4pass/pineforge-release --ref main -f dry_run=true
# After independent review and authorized live proof only:
GH_HOST=github.com gh variable set FACTS_DESCRIPTIONS_APPLY_ENABLED --repo pineforge-4pass/pineforge-release --env descriptions --body true
GH_HOST=github.com gh workflow run facts-descriptions.yml --repo pineforge-4pass/pineforge-release --ref main -f dry_run=false
# Disable future application, then verify all scopes and the effective value:
GH_HOST=github.com gh variable set FACTS_DESCRIPTIONS_APPLY_ENABLED --repo pineforge-4pass/pineforge-release --env descriptions --body false
GH_HOST=github.com gh variable list --org pineforge-4pass
GH_HOST=github.com gh variable list --repo pineforge-4pass/pineforge-release
GH_HOST=github.com gh variable list --repo pineforge-4pass/pineforge-release --env descriptions
```

## Receipts, alerts and remaining proof

Each run uploads `descriptions-audit-{run_id}-{run_attempt}/receipt.json`, with
schema `pineforge/facts-descriptions-receipt/v1`, consumer, source commit,
facts/policy SHA256, dry-run flag, diagnostic, status, alert, exit code and only
public policy rows. Rows include expected/actual text, disposition, status,
source tokens and pinned license sources. `permission_unavailable_proven` stays
false until a future reviewed implementation can classify mint failures reliably.

Exit 0 means an exact comparison/application or a superseded hold (inspect status);
exit 1 means drift or missing setup; exit 2 means validation, mint or API failure.
Report-only drift fails visibly. Missing receipts, failed artifact uploads and
source-resolution failures must never be interpreted as healthy telemetry.
Cancelled superseded runs are ignored by failure monitoring. A new run resolves
current main again, so duplicate events are no-ops and old events can reconcile
the newest facts.

Stage-1 heartbeat: poll the latest bounded main runs, exclude cancellations,
alert on completed failures (including report-only drift), and flag absent/stale
telemetry. For example, fetch a bounded page without logs or private data:

```sh
GH_HOST=github.com gh api --hostname github.com 'repos/pineforge-4pass/pineforge-release/actions/workflows/facts-descriptions.yml/runs?branch=main&per_page=20'
```

The executor handoff includes the exact bounded-time heartbeat command and its
failure/staleness handling for TOP. Full source-versus-served digest monitoring
with a **30-minute facts-change freshness threshold is pending stage 2**. The
stage-1 run monitor is not served-state proof and unrelated main commits must
not reset that future freshness clock.

Offline acceptance commands (run on the approved Spot test host):

```sh
python3 -m unittest discover -s tests -p 'test_facts_*.py' -v
python3 -m unittest discover -s tests -p 'test_repo_descriptions.py' -v
python3 -m unittest discover -s tests -p 'test_release_*.py' -v
python3 tests/test_fingerprint_canonical.py
```

New acceptance uses real CLI subprocesses against a loopback fake GitHub HTTP
service; existing read-only renderer tests retain their fake third-party `gh`
executable. Neither substitutes for live protected-environment/App proof.
The compatibility renderer still supports review-only `--format commands` and
read-only `check --live --gh PATH`; it never executes its proposed commands.
Use the guarded workflow for automated application. Do not promote static or
held rows by manually applying compatibility output.

## Stage 2 hub dispatch signal

`facts-fanout.yml` wakes on a main push touching `facts/facts.json`, including a
mixed commit, on manual dispatch, and at minutes 7, 22, 37 and 52 of each hour.
Its job has a ten-minute timeout and its own `facts-web-dispatch` concurrency
group with `cancel-in-progress: false`. It has no dependency on the descriptions
workflow. Missed, failed or obsolete notifications are repaired by a later wake-up
that resolves authoritative current main again; GitHub schedules can be delayed.

The helper reuses the existing immutable GitHub source reader and complete
schema/semantic validator. Preparation checks the local canonical input, verifies
the public hub identity, resolves current main, fetches facts and schema by that
immutable commit, verifies Git blob integrity and computes the facts SHA256.
The remote schema must equal the reviewed local schema. A source move before
preparation completes produces a `superseded` hold without permitting a mint.
The selected commit, digest and requested boolean are bound in `payload.json`.
Before sending, the helper validates those files again, compares fresh immutable
bytes and checks main immediately before the POST. Source movement holds that
snapshot for the next serialized run; a run never silently changes its source.
There is no atomic transaction between a main read and a dispatch, so the receiver
must also reconcile authoritative source and the next wake-up remains the repair
path for a simultaneous source change.

The single API write is a repository dispatch with event type `facts-update`.
Its `client_payload` has **exactly** these fields:

```json
{"commit":"<40 lowercase hex characters>","sha256":"<64 lowercase hex characters>","dry_run":true}
```

The boolean is a JSON boolean, never a string or number. Unknown, missing and
duplicate fields are refused. The target comes only from protected setup, never
from that payload. Repeating the same source repeats this idempotent signal;
the hub never writes facts or consumer files. A successful dispatch means GitHub
accepted the signal with HTTP 204. It does not establish receiver completion,
deployment or served freshness.

The job declares environment `release-automation` and uses only its existing
`PINEFORGE_APP_ID` / `PINEFORGE_APP_PRIVATE_KEY` plus the new environment secret
`FACTS_WEB_REPOSITORY`. Set the latter to one short repository name within the
fixed owner `pineforge-4pass`: 1–100 ASCII letters, digits, underscores, hyphens
or dots, starting with a letter, digit, underscore or hyphen. Empty targets,
controls, whitespace, separators, URLs, owner prefixes and encoded paths cannot
be token targets. Missing target/key setup while disabled produces a visible
`setup-required` receipt; invalid nonempty targets fail. When enabled, any
missing setup fails before source calls or minting.

The new environment variable `FACTS_WEB_DISPATCH_ENABLED` is **off by default**:
absent, empty or `false` means no mint and no POST. Only literal `true` enables
the signal, and minting also requires a successful validated preparation and a
protected main push/manual/schedule context. The token action requests only the
explicit secret-supplied repository and `permission-contents: write`. It never
uses descriptions credentials or requests Administration. The helper checks
credential presence only and does not inspect or hash their values.

Manual `dry_run` defaults to `true` and an explicitly requested `false` survives
workflow expression evaluation. With the dispatch gate off, either value only
plans. Once TOP separately enables dispatch, either requested boolean is sent:
`true` asks the receiver to validate without publishing. Enabled push/schedule
runs send `false`; disabled automatic runs plan with `true`. The receiver must
have its own protected publication gate. The hub's enable flag grants no receiver
write authority.

Public receipts and artifact names use the neutral label `web` and contain only
public canonical commit/digest provenance, requested mode, setup secret names,
status, category, numeric HTTP status, alert and exit code. The private target,
token, URLs and response bodies are never copied into helper output. The target
is passed as a secret directly to the scoped token action and is never a job
output. Redirects are refused; errors retain a category and HTTP status when
available. Generic authentication, network, rate-limit or mint failures fail
visibly, with `permission_unavailable_proven=false`; a 403 is not evidence of
missing permission. Mint failure remains unclassified because the token action
does not expose a reliable structured cause.

Each workflow run uploads `fanout-audit-{run_id}-{run_attempt}` containing
`prepare.json` and, when minting was eligible, `dispatch.json`. Artifact upload
requires a receipt. Cancellation does not alert. A setup/plan or superseded hold
exits 0; validation, mint, transport or receipt failures exit 2. A setup-required
receipt has `alert=true` even while rollout is deliberately off. All receipts
keep `installed=false`. Missing receipts and failed uploads are missing telemetry,
never a successful consumer health check.

TOP's read-only workflow heartbeat uses the approved `ghq` wrapper on `PATH` and
requires an explicit path to the reviewed checkout. It bounds the GitHub query
to 45 seconds and 2 MiB of stdout, and the status parser to 10 seconds and 32
KiB of stdout. Each budget starts before its command is created and covers
process creation, process-group setup, `exec`, input, output and exit.

The single-threaded parent forks an owned group-leader worker before any command
creation. Both sides establish the worker's process group; the worker reports
ready and waits for the parent's verified GO before creating the nested command.
Only that worker calls `Popen`/`wait`, so command creation, `exec` and setup stalls
cannot block the parent's deadline loop. A bounded 64-byte control channel carries
the real command exit separately. The worker stays alive after reporting it.
Stdout and stdin use nonblocking pipes; stderr is drained without retention.

Every completion and exception enters cleanup, even after command or worker
failure and when descendants have closed their pipes. Before reaping the worker,
the parent sends TERM to its verified owned group, allows three seconds, then
sends KILL. The unreaped worker (live or zombie) reserves its PID/PGID through the
last destructive signal. Before ownership is established, only the unreaped
child PID is signalled and no command is released. Reaping has a two-second bound;
an additional two-second signal-0 observation checks group absence. No destructive
signal is sent after reaping. Failure to establish cleanup emits a neutral
`*-cleanup-failed` receipt with exit 2, never command output or a success receipt.
Cleanup can add up to seven seconds to each command's 45/10-second budget.
Handled SIGINT/SIGTERM only record cancellation; they never raise through fork,
ownership or finalizer bookkeeping. Normal control flow observes cancellation
and returns the existing neutral unavailable failure after bounded teardown.
The parent keeps those handlers throughout cleanup, including repeated signals;
it does not temporarily ignore cancellation or turn it into a success receipt.

Platform contract: Python 3.9+ on POSIX macOS or Linux with `fork`, process groups,
nonblocking pipes and a pipe-capable selector. The supervisor uses only the
Python standard library, with no `waitid`, `WNOWAIT` or Linux-only process API.
The real status parser separately requires the unchanged pins in
`facts/requirements-validation.txt`. Install them in the **same interpreter**
used below, for example `python3 -m pip install -r
/path/to/reviewed/pineforge-release/facts/requirements-validation.txt`; the
recipe launches the parser with `sys.executable`. Run as the standalone,
single-threaded interpreter shown below with default child reaping (no external
SIGCHLD handler or auto-reaper), permission to signal its children, and ordinary
runnable kernel syscalls. Kernel suspension/uninterruptible I/O and termination
of the watchdog itself cannot be bounded by a userspace watchdog. Commands must
not change their group/session: escaped descendants are outside this ownership
contract. A delayed orphan reaper or PGID reuse during the final observation may
cause a conservative cleanup failure. Runtime acceptance on Linux does not
constitute macOS runtime proof. Failures are neutral and rollout remains off.

```sh
python3 - /path/to/reviewed/pineforge-release <<'PY'
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

RUNS = (
    "repos/pineforge-4pass/pineforge-release/actions/workflows/"
    "facts-fanout.yml/runs?branch=main&per_page=20"
)
MAX_QUERY_BYTES = 2 * 1024 * 1024
MAX_STATUS_BYTES = 32 * 1024
QUERY_SECONDS = 45
STATUS_SECONDS = 10
TERM_GRACE_SECONDS = 3
REAP_SECONDS = 2
EMPTY_PROBE_SECONDS = 2
cancelled = False


def close_fd(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def teardown(pid, owned):
    # Do not poll/wait/reap before the LAST group signal. Even a failed worker
    # stays our unreaped child, reserving its PID (and therefore this PGID).
    ok = True
    try:
        if owned:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(TERM_GRACE_SECONDS)
    except ProcessLookupError:
        pass
    except OSError:
        ok = False
    try:
        if owned:
            os.killpg(pid, signal.SIGKILL)
        else:
            # No GO was sent: only this child can exist, never a command.
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        ok = False
    end = time.monotonic() + REAP_SECONDS
    while True:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                break
        except ChildProcessError:
            # Unexpected external reaping invalidates the ownership contract.
            return False
        except OSError:
            return False
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)
    if not owned:
        return ok
    end = time.monotonic() + EMPTY_PROBE_SECONDS
    while True:
        try:
            # Observation only. Never send TERM/KILL after releasing the PID.
            os.killpg(pid, 0)
        except ProcessLookupError:
            return ok
        except OSError:
            return False
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def worker(command, fds):
    try:
        for name in ("ctl_r", "go_w", "in_w", "out_r", "err_r"):
            close_fd(fds[name])
        os.setpgid(0, 0)
        # A caught handler resets to default on exec; unlike SIG_IGN it does
        # not make the nested command ignore TERM. Keep the anchor for cleanup.
        signal.signal(signal.SIGTERM, lambda *_: None)
        signal.signal(signal.SIGINT, lambda *_: None)
        os.set_blocking(fds["ctl_w"], False)
        os.write(fds["ctl_w"], b"r\n")
        if os.read(fds["go_r"], 1) != b"g":
            os._exit(126)
        process = subprocess.Popen(command, stdin=fds["in_r"],
                                   stdout=fds["out_w"], stderr=fds["err_w"],
                                   close_fds=True)
        for name in ("in_r", "out_w", "err_w"):
            close_fd(fds[name])
        code = process.wait()  # Only the worker can block on command creation/exit.
        os.write(fds["ctl_w"], ("x%d\n" % code).encode("ascii"))
        # Stay alive even after the command exits; parent owns all teardown.
        os.read(fds["go_r"], 1)
    except BaseException:
        try:
            os.write(fds["ctl_w"], b"e\n")
        except OSError:
            pass
    finally:
        os._exit(127)


def run_bounded(command, seconds, limit, data=None):
    """Return (real command exit, bounded stdout, neutral failure reason)."""
    if cancelled:
        return None, b"", "setup"
    deadline = time.monotonic() + seconds
    fds = {}
    pid = None
    owned = False
    selector = None
    output = bytearray()
    control = bytearray()
    reason = None
    code = None
    state = "handshake"
    try:
        for name in ("ctl", "go", "out", "err", "in"):
            fds[name + "_r"], fds[name + "_w"] = os.pipe()
        # The inherited handler only records cancellation, so it cannot throw
        # the child into the parent's bookkeeping before worker() takes over.
        pid = os.fork()
        if pid == 0:
            worker(command, fds)
        # Ownership is recorded immediately, before selector/pipe setup.
        # Both sides set the same group; the child cannot launch before GO.
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        owned = os.getpgid(pid) == pid
        if not owned:
            raise RuntimeError("group setup")
        for name in ("ctl_w", "go_r", "in_r", "out_w", "err_w"):
            close_fd(fds.pop(name))
        for fd in fds.values():
            os.set_blocking(fd, False)
        selector = selectors.DefaultSelector()
        for name, kind in (("ctl_r", "ctl"), ("out_r", "out"), ("err_r", "err")):
            selector.register(fds[name], selectors.EVENT_READ, kind)
        payload = memoryview(data) if data is not None else memoryview(b"")
        offset = 0
        if payload:
            selector.register(fds["in_w"], selectors.EVENT_WRITE, "in")
        else:
            close_fd(fds.pop("in_w"))
        while True:
            if cancelled:
                reason = "setup"  # Existing neutral unavailable receipt.
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            # The worker's bounded record carries command exit independently
            # of worker lifetime. Still drain all output and deliver all input.
            if code is not None and all(k.data == "ctl" for k in selector.get_map().values()):
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                fd, kind = key.fd, key.data
                if kind == "in":
                    try:
                        offset += os.write(fd, payload[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        reason = "stdin-closed"
                        break
                    if offset == len(payload):
                        selector.unregister(fd)
                        close_fd(fds.pop("in_w"))
                    continue
                size = min(65536, limit - len(output) + 1) if kind == "out" else 65536
                if kind == "ctl":
                    size = 65 - len(control)
                try:
                    chunk = os.read(fd, size)
                except BlockingIOError:
                    continue
                if kind == "ctl":
                    if not chunk:
                        reason = "worker-failed"
                        break
                    control.extend(chunk)
                    if len(control) > 64:
                        reason = "worker-failed"
                        break
                    while b"\n" in control:
                        record, _, tail = control.partition(b"\n")
                        control[:] = tail
                        if state == "handshake" and record == b"r":
                            if os.getpgid(pid) != pid:
                                raise RuntimeError("group changed")
                            os.write(fds["go_w"], b"g")  # One byte, nonblocking.
                            state = "running"
                        elif state == "running" and record.startswith(b"x"):
                            code = int(record[1:])
                            state = "finished"
                        else:
                            reason = "worker-failed"
                            break
                elif not chunk:
                    selector.unregister(fd)
                elif kind == "out":
                    if len(output) + len(chunk) > limit:
                        reason = "stdout-limit"
                        break
                    output.extend(chunk)
                if reason is not None:
                    break
            if reason is not None:
                break
    except BaseException:
        reason = "setup"
    finally:
        # The same recording-only handlers remain installed during teardown.
        # No handler transition can throw past this bounded finalizer.
        try:
            if pid is not None and pid > 0:
                if not teardown(pid, owned):
                    reason = "cleanup-failed"
        except BaseException:
            reason = "cleanup-failed"
        finally:
            if selector is not None:
                try:
                    selector.close()
                except Exception:
                    reason = "cleanup-failed"
            for fd in fds.values():
                close_fd(fd)
    if cancelled and reason is None:
        reason = "setup"
    return code, bytes(output), reason


def failure(diagnostic, query_exit=None, status_exit=None):
    receipt = {
        "alert": True,
        "category": "telemetry",
        "consumer": "web",
        "diagnostic": diagnostic,
        "exit_code": 2,
        "installed": False,
        "served_freshness": "unverified",
        "status": "telemetry-alert",
        "telemetry_missing_or_stale": True,
    }
    if query_exit is not None:
        receipt["query_exit_code"] = query_exit
    if status_exit is not None:
        receipt["status_exit_code"] = status_exit
    print(json.dumps(receipt, sort_keys=True))
    return 2


def main():
    if len(sys.argv) != 2:
        return failure("reviewed-checkout-required")
    if os.name != "posix" or not hasattr(os, "fork"):
        return failure("process-supervision-unavailable")
    ghq = shutil.which("ghq")
    if ghq is None:
        return failure("github-client-unavailable")
    try:
        checkout = Path(sys.argv[1]).expanduser().resolve(strict=True)
        helper = checkout / "scripts/facts_fanout.py"
        if not helper.is_file():
            return failure("reviewed-checkout-status-command-missing")
    except (OSError, ValueError):
        return failure("reviewed-checkout-required")
    query_exit, raw, reason = run_bounded(
        [os.path.abspath(ghq), "api", "--method", "GET", "--hostname",
         "github.com", RUNS],
        QUERY_SECONDS,
        MAX_QUERY_BYTES,
    )
    if reason == "cleanup-failed":
        return failure("github-query-cleanup-failed")
    if reason == "timeout":
        return failure("github-query-timeout")
    if reason == "stdout-limit":
        return failure("github-response-too-large")
    if reason in ("setup", "worker-failed"):
        return failure("github-query-unavailable")
    if reason is not None or query_exit != 0:
        return failure("github-query-failed", query_exit=query_exit)

    status_exit, status_output, reason = run_bounded(
        [sys.executable, str(helper), "status"],
        STATUS_SECONDS,
        MAX_STATUS_BYTES,
        data=raw,
    )
    if reason == "cleanup-failed":
        return failure("status-parser-cleanup-failed")
    if reason == "timeout":
        return failure("status-parser-timeout")
    if reason == "stdout-limit":
        return failure("status-receipt-too-large")
    if reason in ("setup", "worker-failed"):
        return failure("status-parser-unavailable")
    if reason is not None or status_exit not in (0, 1):
        return failure("status-parser-failed", status_exit=status_exit)
    try:
        receipt = json.loads(status_output)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return failure("status-receipt-invalid", status_exit=status_exit)
    if (not isinstance(receipt, dict)
            or receipt.get("consumer") != "web"
            or receipt.get("served_freshness") != "unverified"
            or receipt.get("status") not in {"workflow-observed", "telemetry-alert"}):
        return failure("status-receipt-invalid", status_exit=status_exit)
    if cancelled:
        return failure("status-parser-unavailable")
    print(json.dumps(receipt, sort_keys=True))
    return status_exit


def interrupted(*_):
    global cancelled
    cancelled = True


signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
sys.exit(main())
PY
```

`status` reads at most 2 MiB and 20 run records, excludes cancellations, and
returns a neutral JSON receipt. It alerts (exit 1) for absent/completed telemetry
older than 30 minutes, a failed completed run in that window, or a noncompleted
run older than 15 minutes. Malformed input exits 2. Exit 0 is `workflow-observed`,
with `served_freshness=unverified`, not installation proof. This heartbeat checks
workflow runs; TOP must also read each neutral artifact, including setup alerts.
It cannot replace the receiver's source-versus-served digest check. That required
30-minute freshness clock starts at the facts-byte change, not an unrelated main
commit, workflow creation time or a retry.

## TOP Stage 2 rollout checklist (leave dispatch off)

1. Land the reviewed receiver first. Confirm it accepts `facts-update`, validates
   exactly the three payload fields and authoritative current source, and gates
   writes independently. Its protected code, scoped web-only App and tested
   facts-only commit confinement belong to the separate consumer change.
2. Obtain fresh independent and security reviews and the complete CI rollup on
   the final hub and receiver trees. Spot subprocess/HTTP acceptance does not
   replace live App, environment or receiver proof. No RTM or enablement follows
   from the source change alone.
3. In `release-automation`, provision `FACTS_WEB_REPOSITORY` and verify the
   existing Contents-only App has the intended selected repository. Retain the
   environment's protected main / `v*` restrictions. Keep
   `FACTS_WEB_DISPATCH_ENABLED` set to the literal `false` at the protected
   environment while rollout is off. Read the organization, repository and
   environment values and verify the protected workflow's effective value is
   `false`; an absent environment value can expose an inherited `true`. Inspect
   the disabled manual plan, immutable source, setup receipt and artifact before
   enabling anything. Do not restore the removed repository-level key fallback.
4. Follow TOP's separately authorized receiver rehearsal on the fixed throwaway
   branch, preserving its source/receipt proof and exact Spot build. Branch-only
   proof is not deployment proof. Enable hub dispatch only after the receiver
   and its independent rollout controls are ready. For enabled manual
   `dry_run=true` receipt delivery, keep the receiver publication gate disabled.
   Enabled automatic main pushes and schedules send `dry_run=false`, so a manual
   dry-run does not make it safe to enable receiver publication.
5. Pages Git integration owns production build/deployment from main. Neither
   this hub nor the receiver rollout needs Cloudflare credentials, a Cloudflare
   API call or a Pages upload command. The receiver must observe the Pages
   GitHub check bound to its exact committed SHA, then independently verify
   served provenance and canonical bytes/digest. Surface deployment races and
   missing telemetry instead of attributing success to the wrong commit.
6. TOP's first authorized real main run must close source-versus-served proof
   within 30 minutes of the facts-byte change. Confirm welcome-template runtime
   override/readback in the consumer rollout. Keep release no-key-fallback proof
   separate. To stop future hub minting/dispatch, set
   `FACTS_WEB_DISPATCH_ENABLED` to the literal `false` in protected
   `release-automation`, then read back organization, repository and environment
   configuration and verify the effective value is `false`. Do not remove the
   environment value: an inherited `true` could become effective. Retain receipts
   and scheduled plan visibility. HPO wording waits for a genuine next
   descriptions-policy change.

Affected offline acceptance (approved Spot host only):

```sh
python3 -m unittest discover -s tests -p 'test_facts_*.py' -v
python3 -m unittest discover -s tests -p 'test_repo_descriptions.py' -v
python3 scripts/facts_validate.py --facts facts/facts.json
python3 scripts/repo_descriptions.py render --facts facts/facts.json
```

Fanout acceptance reuses the existing loopback GitHub fixture and runs the real
parser, validator, workflow shell and dispatch helper. Only external GitHub
responses and token-action outcomes are simulated. Tests capture exact POST
bytes, exercise disabled/missing setup, strict payloads and targets, source
movement, repeated signals, failures/redaction, independent workflow wiring and
missing/stale telemetry. The implementation lane performs no live mint, dispatch,
consumer publication or deployment.
