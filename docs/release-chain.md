# Automatic release hub

This implements briefs 1 (hub reconciliation) and 2 of the 2026-10-16 release
chain design. Engine/codegen/HPO remain the producers. `release_pair.py` remains
the authority for semver order, lockstep and the HPO `1.6.0-rc.1` cutoff. The
hub does not create producer versions or consumer commits, merge consumer PRs,
or deploy consumers.

## Receive, verify and replay

`handle-upstream.yml` still accepts `engine-release`, `codegen-release` and
`hpo-release` with `{version, prerelease?, run_id?}`. Version is a string,
prerelease is a JSON boolean, and a supplied run ID is a positive JSON integer.
HPO accepts semver or PEP 440; HPO release tags/assets use PEP 440. Each event's
own published artifacts are verified before the existing partner/pair decision.
Incomplete lockstep waits. Invalid flags, source mismatches, authentication,
rate limits and transport failures fail visibly.

`release-reconcile.yml` wakes every 15 minutes or manually. It paginates the
three fixed producer repositories, excludes drafts/prereleases/non-product tags
and versions older than hub VERSION, verifies the desired stable version's
artifacts, then sends the **same producer events** back to the existing handler.
Older missed versions are superseded by the newest desired stable version;
they are never replayed as downgrades after it. Duplicate notifications reuse
the handler's existing decision. Nothing calls a producer's version-bump job.

Recovery of an existing hub tag explicitly dispatches `publish.yml` **on that
tag**. It never force-pushes or rewrites it. Manual replay is also available
through Actions: select `publish`, select the existing tag as its ref, run.
Tags predating this workflow need a reviewed migration; the hub does not invent
new manifests for historical images without candidate verification.

## Candidate and readiness

`publish.yml` first reuses `release-candidate.json` if already stored. Otherwise
it preserves the existing harness, ABI, license and Debian source-mirror gates,
builds only a unique `candidate-<commit>-<run>-<attempt>` image tag, and verifies
the pushed child digests on amd64 and arm64. Each child must pass an actual
Pine transpile/compile/backtest with a nonempty trade result. HPO must also pass
a nine-trial grid study with full parameter coverage. Runtime execution has no
network. Installed codegen/HPO bytes and HPO catalogs must match checked package
bytes. HPO wheel and native sdist digests are supplied to the Docker builds.
The sdist's CMake/native and Python files must agree with the HPO source tag.

The checked candidate is attached to a draft GitHub release. The finalizer runs
as a reusable job after publication, on facts-file changes, every 15 minutes,
and on manual replay. It never rebuilds a stored verified candidate. It requires:

- Engine's published Linux and macOS archives and matching checksums.
- The matching codegen PyPI sdist and its tagged Python source.
- The matching npm Pyodide tarball, registry SHA-512 integrity, versioned browser
  archive digest, and source agreement across npm/PyPI/tagged source.
- When HPO is included: wheel, sdist and both failure catalogs, with matching
  versions, hashes, package/native sources and tagged commits.
- A schema/semantically valid `facts.releases[release_version]` with nonempty
  measured evidence and engine/codegen commits equal to the candidate's sources.
  The active scoreboard is never substituted for the release mapping.

A real missing artifact (404), missing sdist/npm or absent release facts stays
pending and creates/updates a neutral incident. All other failures are errors.
`lab facts export` in the workflow repository must create the measured mapping
through its existing evidence/PR process; this hub never fabricates evidence or
receives private registry credentials.

Only then does finalization attach `release-manifest.json`, assign the fixed
version image tag, move eligible aliases, publish the GitHub release, and fan
out. Alias decisions and fan-out share `release-finalize` concurrency. Each
decision re-reads ready releases by semver. Late completion/replay of an older
release cannot move aliases backward. Prereleases receive no stable aliases.
The previous alias digests are preserved before the first promotion.

## Exact ready event contract

The event type remains **`pineforge-release`**. Its ten payload fields are exactly
section 3.2 of the design: `schema`, `event_id`, `release_version`, `prerelease`,
`hub_commit`, `versions`, `image`, `facts`, `manifest`, `run_id`.

Fixtures for consumer implementations:

- [Manifest bytes](../tests/fixtures/release-manifest.json)
- [Ready client_payload](../tests/fixtures/release-ready.json)
- [Strict validator](../scripts/release_contract.py)

Fixture identities are synthetic, never live release evidence. The manifest is
UTF-8 JSON with sorted keys, compact separators and one final LF. It contains
the eight fields above other than `event_id` and `manifest`. Both omitted fields
contain its SHA-256; excluding them avoids a circular hash. `run_id` is the
original candidate build run, a JSON integer. Replays fetch the existing bytes;
attempt IDs occur only in receipts. HPO is explicitly null before the cutoff.

Consumers must fetch the asset from the fixed hub repository's matching GitHub
release, hash the original bytes, compare the complete payload, and verify the
referenced artifacts. No arbitrary repository/asset/path is accepted from the
sender. A newer verified canonical facts snapshot may be retained if its target
release entry agrees exactly. Record the actually consumed commit and digest.

`facts-update` remains exactly `{commit, sha256, dry_run}`. The existing guarded
web sender is retained and an independently gated license job uses the same
validator and transport. A facts-only change does not create a product release;
it only wakes already stored candidates awaiting release evidence.

## Consumer completion adapter

Dispatch is enabled only for roles in repository variable
`RELEASE_READY_CONSUMERS` (comma-separated, no spaces): `offline`, `hosted`,
`application`, `web`, `data`, `license`. Leave roles absent until their default
branch receivers create PRs and gate merge/deployment on their own full CI/E2E.
This prevents the old direct-deploy receivers from being activated by a new
payload. Absent stable consumers remain **pending**, never installed or verified.
The existing prerelease hand-off policy is retained: application/web/data/license
are explicitly skipped; offline/hosted receivers must enforce dev/next policy.

After uploading its evidence artifact, the consumer can wake the hub using a
**hub-only App installation token**, `Contents: write`, with:

```json
{
  "event_type": "pineforge-release-status",
  "client_payload": {
    "schema": "pineforge/consumer-signal/v1",
    "consumer": "offline",
    "release_version": "1.6.0",
    "run_id": 123456
  }
}
```

This is a completion wakeup, not a replacement product event. Repository names,
URLs or claims of success are not accepted in it. `release-receipts.yml` also
polls the configured consumer workflow histories every 15 minutes, repairing
dropped callbacks. Each job mints one target-only **read** token. It downloads a
single configured Actions artifact, containing exactly `release-receipt.json`.
All stages use the following full private receipt shape:

```json
{
  "schema": "pineforge/consumer-receipt/v1",
  "event_id": "sha256:<manifest hash>",
  "consumer": "offline",
  "release_version": "1.6.0",
  "attempt": 123456,
  "sequence": 7,
  "state": "verified",
  "timestamp": "2026-10-16T12:00:00Z",
  "pr": {"number": 42, "base": "main", "base_sha": "<40 hex>", "head": "<40 hex>", "merge": "<40 hex>"},
  "ci": {"run_id": 100, "run_attempt": 1, "sha": "<PR head>", "expected": ["check"], "approved_skips": [], "results": {"check": "success"}},
  "e2e": {"run_id": 101, "run_attempt": 1, "sha": "<PR head>", "expected": ["candidate-e2e"], "approved_skips": [], "results": {"candidate-e2e": "success"}},
  "deploy": {"run_id": 102, "run_attempt": 1, "sha": "<merge commit>", "expected": ["deploy", "served-identity"], "approved_skips": [], "results": {"deploy": "success", "served-identity": "success"}},
  "preview": {"source": "<PR head>", "image_digest": "sha256:<index>", "facts_commit": "<40 hex>", "facts_sha256": "<64 hex>", "deployment_id_sha256": "<64 hex>"},
  "deployed": {"source": "<merge commit>", "image_digest": "sha256:<index>", "facts_commit": "<40 hex>", "facts_sha256": "<64 hex>", "deployment_id_sha256": "<64 hex>"},
  "previous": {"source": "<previous source>", "image_digest": "sha256:<previous image>", "facts_commit": "<40 hex>", "facts_sha256": "<64 hex>", "deployment_id_sha256": "<64 hex>"}
}
```

The allowed states are pending, pr_open, testing, ready, merged, deploying,
verified, failed, superseded, skipped_by_policy. All fields are present; evidence
not available at an early stage can be null. Use a globally increasing attempt
identity per event (for example the coordinator's GitHub run ID), with increasing
sequence within an attempt. Do not use `GITHUB_RUN_ATTEMPT` alone: it restarts at
1 for another run. Reused identities must have identical bytes. Late/duplicate
receipts cannot regress a verified state.

The owner-configured expected inventories and workflow paths are checked against
GitHub Actions, not trusted from the receipt. Missing, failed, cancelled or
unexpectedly skipped jobs fail. CI/E2E must match the PR head and finish before
merge; deployed source must be the actual PR merge. Preview and served image
digests must match the manifest, facts must come from the hub and be equal or
newer by Git ancestry with the same release entry. Each consumer's deployment
inventory **must include its real served/distribution identity verification and
rollback identity recording**, not just its deploy command. Hub checks cannot
replace that consumer-owned runtime proof. For npm/PyPI consumers, “deployed”
means verified distribution/channel identity, not installation on external hosts.

The current receipt adapter uses the strict up-to-date branch path from section
3.6: record the tested base SHA and use merge or squash, so the landed commit's
first parent can be checked against that base. Rebase or merge-queue adapters
with another source graph must add corresponding candidate evidence before
being enabled; the hub refuses mismatched merge inputs.

For a facts-only web/license PR, use `pineforge/facts-consumer-receipt/v1` with
the same fields plus `facts: {commit, sha256}` for the requested canonical target.
Keep the installed product's manifest event/version/image identity. It is stored
separately from product progress and never initiates a product release. The same
wakeup/poller and CI/E2E checks apply.

Public receipt assets contain neutral roles, numeric IDs, commit/image/facts
identities, hashes of the private evidence and expected inventory. Private
repository names, job labels, deployment URLs and raw deployment IDs stay in the
consumer's report. Aggregate status has every expected role; delivery acceptance
is a separate first-delivery asset and an artifact for each delivery attempt.
Aggregate assets are appended only when state changes. One issue per release is reused through
pending/failure/recovery/completion and assigned/mentioned to the configured
owner. Only verified served-state evidence can produce a success state.

Facts freshness uses the latest **facts-file change** (the commits API filtered
by `facts/facts.json`), never an unrelated main commit. Missing current served
facts evidence becomes overdue after 30 minutes. Release readiness and consumer
stage age have separate configurable thresholds; app stages have a longer
default. The dashboard lane should monitor the neutral assets/issues rather than
count green dispatcher runs as deployments.

## Supervisor configuration

Do not enable a role until its consumer lane has removed moving runtime defaults
and installed PR/CI/E2E/deploy gates. Consumer App identities must not bypass
their required branch checks, and no consumer may use an admin merge. Configure
`release/ready` as a required hub branch/ruleset status; its exact inventory
rejects missing/skipped/cancelled jobs and covers merge-group events too.

| Name / setting | Scope | Purpose |
|---|---|---|
| `PINEFORGE_APP_ID`, `PINEFORGE_APP_PRIVATE_KEY` (existing) | Hub `release-automation` environment | Short-lived, one-target installation tokens; no PAT fallback. |
| App installation permissions | Hub: Contents write + Actions write; enabled consumers: Contents read/write, Actions read, Pull requests read | Hub tag/dispatch and explicit publish replay; consumer dispatch and completion evidence reads. Each mint asks only for the permissions needed by that job. Consumer PR writers in other repos separately need Pull requests write without branch bypass. |
| `RELEASE_HOSTED_MCP_REPOSITORY`, `RELEASE_APPLICATION_REPOSITORY`, `FACTS_WEB_REPOSITORY` (existing) | Hub `release-automation` environment secrets | Private short repository names for neutral hosted/application/web targets. Public offline/data/license names are fixed in workflows. |
| `RELEASE_RECEIPT_POLICY` (new secret) | Hub `release-automation` environment | JSON map of each enabled role to its trusted receipt workflow/artifact/base and full CI/E2E/deploy job inventories. Keep private workflow/job details out of public source. |
| `RELEASE_READY_CONSUMERS` (new variable) | Hub repository | Explicit comma-separated upgraded receiver roles; also selects receipt polling/facts freshness monitoring. Empty sends nothing and leaves stable roles pending. |
| `RELEASE_REPORT_OWNER` (new variable) | Hub repository | GitHub login to assign/mention on release and failure issues. |
| `RELEASE_READY_MAX_AGE_MINUTES` (optional variable; 60) | Hub repository | Missing readiness prerequisite alert threshold. |
| `RELEASE_CONSUMER_MAX_AGE_MINUTES` (optional variable; 180) | Hub repository | Non-app missing/stalled completion threshold. |
| `RELEASE_APPLICATION_MAX_AGE_MINUTES` (optional variable; 1440) | Hub repository | App stage freshness, separate from per-test time limits. |
| `FACTS_LICENSE_DISPATCH_ENABLED` (new variable) | Hub repository or `release-automation` environment | Set true after the license facts receiver is on its default branch. Existing `FACTS_WEB_DISPATCH_ENABLED` remains independent. |
| Actions permissions, Issues, GHCR visibility | Hub repository | Permit workflow contents/packages/issues writes; enable Issues and public pulls of the existing GHCR package. |
| `release-automation` environment (existing) | Hub repository | Permit protected main and release `v*` tags and the intended automatic jobs. Required human reviewers would pause full automation. |
| Release asset setting | Hub repository | These releases keep append-only audit/receipt assets after publication; GitHub's repository-wide immutable-release setting must permit these appends. The scripts enforce manifest/fixed-tag byte immutability and never overwrite/delete them. |
| Receivers and callback credentials | Each enabled consumer | Receiver on default branch; own CI + E2E aggregate required; scoped hub Contents-write callback token; artifact upload/read access. |

Example **structure**, not a production policy:

```json
{
  "offline": {
    "workflow": "release.yml", "artifact": "release-receipt", "base": "main",
    "ci": {"workflow": "ci.yml", "expected": ["check", "unit"], "approved_skips": []},
    "e2e": {"workflow": "e2e.yml", "expected": ["stdio-parity", "syminfo", "negative"], "approved_skips": []},
    "deploy": {"workflow": "publish.yml", "expected": ["publish", "served-identity"], "approved_skips": []}
  }
}
```

Supervisor must populate the **actual complete expected job names**, including
matrix expansions, from each reviewed consumer lane. A tiny example inventory
must never be used to omit existing required checks.

## Rollback and validation

`release-rollback.yml` is an explicit protected-environment entry point. It adds
a rejection marker, stops future fan-out, restores only aliases still pointing
to that rejected image from the previously recorded good digests, and verifies
readback. Newer aliases are left alone. Fixed tags, images, source assets and
manifests are preserved. If no previous good alias exists, it reports incomplete
repair rather than deleting an alias or inventing a good target. Consumer
rollback remains the responsibility of that consumer's lane.

CI runs the existing release/source/harness tests plus the ready replay and
negative controls. See [the current replay entry point](../tools/release-dry-run/README.md).
No test suite, Docker runtime/build, live dispatch, publication, token mint,
credential read or deployment is needed on the Mac to implement this lane.
