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
and versions older than hub VERSION. It checks whether the desired stable
version already has a hub Git tag before downloading any artifacts. Only a
version without that tag has its artifacts verified and the **same producer
events** sent back to the existing handler. A hub tag hands completion to
publish/finalize, even while the release is a draft awaiting prerequisites.
Older missed versions are superseded by the newest desired stable version;
they are never replayed as downgrades after it. Duplicate notifications reuse
the handler's existing decision. A stored candidate makes an unchanged-version
decision a no-op even while the fixed image tag is absent. Nothing calls a
producer's version-bump job.

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

The checked candidate is attached to a draft GitHub release. Candidate lookup
uses the paginated releases list matched by `tag_name`, with a token that has
Contents write/push access to see drafts. GitHub's `releases/tags/{tag}` endpoint
exposes published releases only. Prepare, replay and finalization reuse the same
draft and never create a second candidate for its version. The finalizer runs
as a reusable job after publication, on facts-file changes, every 15 minutes,
and on manual replay. It never rebuilds a stored verified candidate. It requires:

- Engine's published Linux and macOS archives and matching checksums.
- The matching codegen PyPI sdist and its tagged Python source.
- The matching npm Pyodide tarball, registry SHA-512 integrity, versioned browser
  archive digest, and source agreement across npm/PyPI/tagged source.
- When HPO is included: wheel, sdist and both failure catalogs, with matching
  versions, hashes, package/native sources and tagged commits.
- For stable releases, a schema/semantically valid `facts.releases[release_version]` with nonempty
  measured evidence and engine/codegen commits equal to the candidate's sources.
  The active scoreboard is never substituted for the stable release mapping.

**Prerelease rule:** prereleases skip the per-release measured facts requirement.
They bind the SHA-256 and commit of schema/semantically validated `main` facts;
they do not wait for `facts.releases["X.Y.Z-rc.N"]`. The ready event still carries
`facts.release_key = release_version`; for a prerelease that key may be absent.
When absent, consumers must use the pinned snapshot's exact bytes; newer facts
retention is allowed only when a measured target entry exists and is identical.
All package, source, digest and architecture/runtime gates still apply. Stable
releases still require their measured, source-matching immutable release entry.

A real missing artifact (404), missing sdist/npm or absent release facts stays
pending and creates/updates a neutral incident. All other failures are errors.
`lab facts export` in the workflow repository must create the measured mapping
through its existing evidence/PR process; this hub never fabricates evidence or
receives private registry credentials.

Only then does finalization attach `release-manifest.json`, assign the fixed
version image tag, move eligible aliases, publish the GitHub release, and fan
out. Alias decisions and delivery reservations share job-level `release-finalize`
concurrency, including reusable calls. Delivery jobs serialize per role/tag. Each
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
validator and transport. A facts-only change sends `facts-update` to enabled
facts consumers and wakes stored candidates awaiting release evidence; it does
not create a product release.

## Consumer completion adapter

Product dispatch is enabled only for roles in repository variable
`RELEASE_READY_CONSUMERS` (comma-separated, no spaces). Enable `offline`,
`hosted`, `application`, `web` or `data` only after the default-branch receiver
creates PRs and gates merge/deployment on its full CI/E2E. Keep `license` out of
product dispatch: its facts receiver is independently selected by
`FACTS_LICENSE_DISPATCH_ENABLED=true`. Web facts polling is similarly selected
by `FACTS_WEB_DISPATCH_ENABLED=true`. Each selected role needs a receipt policy.
Disabled product roles are omitted from aggregate consumers, completion and
staleness calculations; an empty product set completes with no deliveries.
Prerelease application/web/data/license roles, if configured, are explicitly
`skipped_by_policy`; offline/hosted must enforce their dev/next policy.

Delivery acceptance is recorded once as `delivery-<consumer>.json`, independently
of consumer progress. Its presence stops further dispatch even if the consumer
has not yet reported completion. A missing receipt becomes pending/overdue; it
never triggers another build or an endless product event retry. Before each
send the finalizer reserves an append-only `delivery-attempt-<consumer>-N.json`.
It permits **three total attempts**: the first immediately, a second at least
15 minutes after the first, and a third at least 60 minutes after the second.
After a further 15-minute acknowledgement grace period without delivery
acceptance, it appends `delivery-failed-<consumer>.json`, changes that role to
failed, updates the release incident once, and stops. Failed/cancelled jobs also
consume their reserved attempt. Each delivery job rechecks acceptance and retry
exhaustion before sending, so a late queued job also stops. Later valid consumer
evidence can still advance progress; scheduled runs do not clear the retry
history. Investigate and use an explicit reviewed
recovery for an exhausted delivery.

`release-receipts.yml` polls selected consumers every 15 minutes and accepts the
callback below to repair dropped notifications. It mints a target-only read
token, verifies the artifact and full GitHub CI/E2E/deploy evidence, then appends
a neutral receipt. Acceptance of a dispatch or callback never means verified.

## Consumer receipt contract v1

This is the single consumer-to-hub contract for all six lanes. Implement the
artifact and callback in the consumer's trusted default-branch coordinator.
No Python knowledge is required. The hub validates both complete examples below
and the [example builder](../scripts/consumer_receipt_example.py) in CI.

**Transport and policy.** The artifact naming convention is
`release-receipt-<consumer>`, for example `release-receipt-offline`,
`release-receipt-hosted`, `release-receipt-application`, `release-receipt-web`,
`release-receipt-data`, or `release-receipt-license`. The hub owner must set
`RELEASE_RECEIPT_POLICY[consumer].artifact` to that **exact literal name**; the
adapter matches the configured name exactly, with no glob, version or run-ID
suffix expansion. A legacy owner-selected name is also accepted if configured
exactly; its allowed characters are `[A-Za-z0-9_.-]+`. Use one non-expired,
immutable snapshot artifact with that name per reporting workflow run. Publish
subsequent snapshots from subsequent trusted reporting runs. Artifact names are
already scoped to their Actions run. An Actions rerun must not leave two live
artifacts with the configured name.

The ZIP must contain exactly one root file named **`release-receipt.json`**,
with no directories or other files. Its UTF-8 JSON object must be at most 2 MiB,
with no duplicate keys, extra fields or non-finite numbers. Keep the artifact
available until hub collection; send the callback only after upload succeeds.
The hub checks GitHub's archive SHA-256 when supplied. Use sorted keys, compact
separators and a final LF for reproducible canonical evidence hashes. The
examples are pretty-printed for reading; JSON whitespace is not identity.

The owner policy also names `workflow` (trusted reporting workflow filename),
`base` (`main` or `dev`), and `ci`, `e2e`, `deploy`. Each check policy has exactly
`workflow` (a `.yml` filename), `expected` (the complete nonempty, unique list of
actual job names, including matrix expansions), and `approved_skips` (a subset
of `expected`, never the whole list). Workflow filenames match
`[A-Za-z0-9_.-]+\.yml`; do not put paths or private repository identities in the
callback. Reporting runs must use that configured workflow and an event of
`push`, `workflow_dispatch`, `workflow_run`, or `repository_dispatch`.

**Types and product receipt fields.** An integer below is a JSON integer from
1 through 9007199254740991, never a boolean or numeric string. A commit is
exactly 40 lowercase hexadecimal characters. A SHA-256 is exactly 64 lowercase
hexadecimal characters; a digest is `sha256:` followed by a SHA-256. Every field
in this table is required. Evidence fields can be `null` until their stage
requires them. Objects have exactly the fields defined here.

| Field | Type and allowed value |
|---|---|
| `schema` | String, exactly `pineforge/consumer-receipt/v1`. |
| `event_id` | Digest string, exactly the verified hub manifest's event ID. |
| `consumer` | String: `offline`, `hosted`, `application`, `web`, `data`, or `license`; must match the hub's scoped target/policy. License implements the facts-only variant below. |
| `release_version` | Bare canonical semver string `X.Y.Z` or `X.Y.Z-{alpha,beta,rc}.N`, exactly the hub event's version; no leading `v` or leading-zero integers. |
| `attempt` | Integer identifying an automation attempt, increasing per consumer/target; use the coordinator's GitHub run ID, not `GITHUB_RUN_ATTEMPT` alone. |
| `sequence` | Integer, strictly increasing for new snapshots within that attempt. It may restart at 1 for a larger attempt. |
| `state` | One of the ten strings in the state table below. |
| `timestamp` | Real UTC timestamp string `YYYY-MM-DDTHH:MM:SSZ`, at most five minutes in the future; for verified, at/after the final deploy check completion. |
| `pr` | PR object below, or `null` before PR evidence exists. |
| `ci`, `e2e`, `deploy` | Each a check-evidence object below, or `null` before that evidence exists. |
| `preview`, `deployed`, `previous` | Each a served-identity object below, or `null` before that evidence exists. `previous` records the rollback identity; only `data.previous` has the temporary pre-manifest bootstrap form below. |

The PR object has exactly these fields:

| Field | Type and allowed value |
|---|---|
| `number` | Integer, a PR in the configured consumer repository; forks are refused. |
| `base` | String `main` or `dev`, equal to policy. |
| `base_sha` | Commit, the base tested with this PR head. |
| `head` | Commit, the actual PR head. |
| `merge` | `null` before merge, then the actual merge/squash commit. |

A check-evidence object has exactly these fields, for each of `ci`, `e2e`,
`deploy`:

| Field | Type and allowed value |
|---|---|
| `run_id` | Integer, GitHub Actions run in the configured consumer repository. |
| `run_attempt` | Integer, the particular GitHub attempt whose jobs are checked. |
| `sha` | Commit; `pr.head` for CI/E2E, `pr.merge` for deploy. Hosted `release-coordinator.yml` E2E alone uses `pr.base_sha`, subject to the binding checks below. |
| `expected` | Array of unique job-name strings, exactly the corresponding policy list, in the same order. |
| `approved_skips` | Array of job-name strings, exactly the corresponding policy list, in the same order. |
| `results` | Object with exactly one key per expected job; values `success`, or `skipped` only for owner-approved skips. Must equal actual GitHub jobs. |

The referenced run must have completed successfully. Every expected job must
exist exactly once and finish successfully or have its explicit approved skip;
at least one must succeed. CI/E2E must finish before the PR's actual merge time.
The merge commit's first parent must be `base_sha`, proving the tested base
landed. An unmerged PR must still have the tested base. Rebase/merge-queue source
graphs need an approved adapter before enablement.

**Hosted coordinator binding.** Only consumer `hosted`, with CI workflow
`release-pr.yml` and E2E workflow `release-coordinator.yml`, can use the base-code
adapter in [`check_coordinator`](../scripts/release_receipts.py). Its E2E
inventory is exactly `["candidate", "release/ready"]` in that order, with no
approved skips. CI still runs at `pr.head`; deploy still runs at `pr.merge`.
All other E2E workflows retain the exact PR-head check in `check_run`.

The coordinator run must be completed successfully at the recorded E2E run ID
and attempt, with `head_sha == pr.base_sha`. Its event must be `workflow_run`;
both its repository and head repository must equal the scoped consumer
repository. The repository's configured default branch, PR base and run's
`head_branch` must all be `main`. This checks the recorded tested base rather
than requiring today's moving main SHA after the PR has merged. The existing
unmerged-base, merge-first-parent and CI/E2E-before-merge gates still apply.

From that coordinator run, the hub requires exactly one nonexpired artifact
named `coordinator-binding-<run_id>-<run_attempt>`. Its ZIP SHA-256 must be
present in GitHub metadata and match the downloaded bytes **before parsing**.
The ZIP and expanded JSON each have a 2 MiB limit; the ZIP contains only one
root `binding.json`. That JSON has exactly `schema`, `pr`, `head`, `base`,
`source`, `coordinator`, and `artifact`, with schema
`pineforge/mcp-coordinator-binding/v1` and these bindings:

- `pr`, `head`, `base` equal the receipt PR number, PR head and tested base.
- `source` equals `{"run_id": ci.run_id, "attempt": ci.run_attempt}`.
- `coordinator` equals the checked E2E run ID, attempt and tested-base `sha`.
- `artifact` has exactly `id`, `name`, `digest`, `manifest_sha256`,
  `worker_sha256`, and `container_sha256`. The ID is a positive integer, the
  name is `candidate-<ci.run_id>-<ci.run_attempt>`, the digest is a SHA-256
  digest, and the other hashes are SHA-256 strings. Exactly one nonexpired
  artifact of that name must remain in the source CI run, with the same ID
  and digest; missing, duplicate or replaced candidates fail closed.

Binding PR/run/artifact IDs and attempts must be positive JSON integers;
booleans, numeric strings and floats are refused.

Only `candidate` and `release/ready` are the required hosted E2E jobs.
`pending`, `policy` and `merge` are orchestration jobs, not additions to that
inventory. Hosted CI requires `validate`, `checks`, `container`, `candidate`;
hosted deploy requires `deploy`. Neither inventory permits skips. The public
receipt and callback schemas remain v1; the binding stays consumer-side.

An ordinary served-identity object has exactly these five fields:

| Field | Type and allowed value |
|---|---|
| `source` | Commit; `pr.head` for preview, `pr.merge` for deployed, previous deployed source for previous. |
| `image_digest` | Digest string; preview and deployed equal the product manifest's index digest. Facts-only may use `null` when no product image is consumed. |
| `facts_commit` | Commit, actually consumed canonical hub facts source. |
| `facts_sha256` | SHA-256 of the exact facts bytes consumed. |
| `deployment_id_sha256` | SHA-256 of the consumer's raw deployment/distribution identity; no raw private deployment ID or URL. |

The ordinary form never has a `provenance` key, including for a real previous
release. The sole alternative is the temporary data `previous` form below.

For product receipts, the hub fetches facts from its fixed repository, verifies
bytes and schema, checks equal/newer Git ancestry, and requires an unchanged
immutable release entry. The prerelease pinned-snapshot rule above applies when
that release entry is absent. The consumer's deploy inventory must include real
served/distribution identity verification and previous/rollback identity
recording. A successful deploy command alone is insufficient. For npm/PyPI,
served state means verified distribution/channel identity. The verified
reporting run itself must be at `pr.merge`.

**Pre-manifest data `previous` (temporary).** Pineforge-data 0.3.1 is the first
data release under this chain. Its previous release, 0.3.0, has neither a
`release_lock.json` nor a `release_manifest.json`. It used the pinned hub 1.0.0
image, while hub facts start at 1.0.1. No true `facts_commit` or `facts_sha256`
ever existed for that previous release. Those keys are **ABSENT**: absence is
chosen over a null or a made-up sentinel value. Either key present with a value,
including `null`, is refused.

Only a product receipt (`schema: pineforge/consumer-receipt/v1`) with
`consumer: data` may use this object, and only in `previous`. It is refused in
`preview`, `deployed`, every other consumer, and every facts-only receipt. The
object has exactly the seven keys shown here; extra or missing keys are refused:

```json
{
  "provenance": "pre-manifest-bootstrap",
  "package": "pineforge-data",
  "version": "0.3.0",
  "source": "fb5cec39b6c6d70af103df0df182d47bb5812191",
  "image_digest": "sha256:b271873d0e91a9eeeb4595daaded9bdfbdbcd3925d3ec1300a833395e2004c86",
  "deployment_id_sha256": "12ffb9b3d3cd270604b06f558ae73a9eb0ca1e2f82a84d45728232091d3e3307",
  "distributions": {
    "pineforge_data-0.3.0-py3-none-any.whl": "609b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc",
    "pineforge_data-0.3.0.tar.gz": "f29a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc"
  }
}
```

Every value is pinned: `source` is the v0.3.0 tag commit, `image_digest` is the
hub 1.0.0 default, and `distributions` contains exactly those public wheel/sdist
names and their recorded PyPI/GitHub-release SHA-256 values. Swapped names or
hashes fail. `deployment_id_sha256` is recomputed as SHA-256 of canonical JSON
over exactly `{package, version, distributions}`, matching the data adapter's
`served()` identity: sorted keys, compact separators, UTF-8, and a final LF.

This form is valid once, only for pineforge-data 0.3.0 as the previous release
when publishing 0.3.1. Any other package/version and every real previous must
use the ordinary five-key form; a real previous with any `provenance` key is
refused. Data 0.3.1 as a later release's previous is a real identity.
**Removal: remove with the data exemption once 0.3.1 is verified.** Delete this
temporary form from the contract at that point.

**State machine.** Ordered progress is
`pending → pr_open → testing → ready → merged → deploying → verified`.
Intermediate snapshots may be omitted: the hub checks all prerequisites of the
reported state even if earlier snapshots were never collected.

| State | Required evidence and meaning |
|---|---|
| `pending` | Target accepted by coordinator; evidence fields may all be null. |
| `pr_open` | Current same-repository PR with tested base/head recorded. |
| `testing` | Same PR evidence; tests are in progress, not yet sufficient for ready. |
| `ready` | PR + successful complete CI/E2E inventories + preview source/image/facts identity. |
| `merged` | Ready evidence plus actual merge commit, tested first parent and CI/E2E completion before merge. |
| `deploying` | Same evidence as merged; serving has not yet been verified. |
| `verified` | All prior gates + successful complete deploy inventory + deployed and previous identities; report source is the merge commit. |
| `failed` | Terminal failure in this attempt; evidence may be null, details stay in the consumer report. |
| `superseded` | Terminal obsolete target in this attempt; evidence may be null. |
| `skipped_by_policy` | Terminal policy skip, accepted only for product prereleases, never stable or facts-only. |

For one consumer/target, compare `(attempt, sequence)` lexicographically. Older
snapshots are ignored. Equal identities are no-ops only for the same entire
canonical receipt and reporting run; reusing them for different evidence is an
error. Within one attempt ordered progress cannot go backward. Failed,
superseded and policy-skipped states require a **larger attempt** to restart;
verified can only remain verified, even across attempts. A later target has its
own state machine. A product target is its event ID; a facts target is the pair
`(facts.commit, facts.sha256)`. Neither delivery attempts nor callback run IDs
advance consumer progress on their own.

**Facts-only variant.** Web and license use exactly the common product fields
above with these changes: set `schema` to
`pineforge/facts-consumer-receipt/v1`; **omit `event_id` and `release_version`
entirely**; add the required `facts` object with exactly `commit` (commit string)
and `sha256` (SHA-256 string) from the validated `facts-update` request. No
product event, manifest, version, image or ready release is needed. Keep the
`image_digest` key in identities, using `null` if irrelevant, or an observed
installed image digest if available. Preview/deployed facts commit and hash must
both equal the target. Previous records the prior served identity using the
ordinary five-key form; the data bootstrap never applies to facts-only receipts.
The same PR, full CI/E2E-before-merge, deploy and reporting-source gates apply.

The hub fetches `facts/facts.json` at that exact commit, checks its SHA-256 and
schema/semantics, and requires the commit to be on canonical main history.
Facts receipts are appended separately under the hub's never-published draft
`facts-receipts`, keyed by role + facts commit + hash + attempt + sequence. The
serialized receipt planner creates this audit bucket before parallel collectors;
it is not a product candidate and creates no product signal. Facts flags select
receipt polling independently of `RELEASE_READY_CONSUMERS`, so license need not
receive `pineforge-release`. Audit readers need authenticated draft access.

**Complete product receipt example.** All identities and job names here are
synthetic. The matching example policy uses reporting `release.yml`, base
`main`, and `ci.yml` / `e2e.yml` / `deploy.yml`, each with its two listed jobs and
no approved skips. The PR merges at `2020-01-01T00:05:00Z`; checks finish at
`2020-01-01T00:04:00Z`; reporting run 90 is a push at the merge commit.

```json
{
  "schema": "pineforge/consumer-receipt/v1",
  "consumer": "offline",
  "attempt": 90,
  "sequence": 7,
  "state": "verified",
  "timestamp": "2020-01-01T00:07:00Z",
  "pr": {
    "number": 9,
    "base": "main",
    "base_sha": "3333333333333333333333333333333333333333",
    "head": "1111111111111111111111111111111111111111",
    "merge": "2222222222222222222222222222222222222222"
  },
  "event_id": "sha256:12a79d3776c447b1caf9513c96db3407b1d3cd2d71a510081e6763e994c2288e",
  "release_version": "1.6.0",
  "ci": {
    "run_id": 1,
    "run_attempt": 1,
    "sha": "1111111111111111111111111111111111111111",
    "expected": [
      "ci/one",
      "ci/two"
    ],
    "approved_skips": [],
    "results": {
      "ci/one": "success",
      "ci/two": "success"
    }
  },
  "e2e": {
    "run_id": 2,
    "run_attempt": 1,
    "sha": "1111111111111111111111111111111111111111",
    "expected": [
      "e2e/one",
      "e2e/two"
    ],
    "approved_skips": [],
    "results": {
      "e2e/one": "success",
      "e2e/two": "success"
    }
  },
  "deploy": {
    "run_id": 3,
    "run_attempt": 1,
    "sha": "2222222222222222222222222222222222222222",
    "expected": [
      "deploy/one",
      "deploy/two"
    ],
    "approved_skips": [],
    "results": {
      "deploy/one": "success",
      "deploy/two": "success"
    }
  },
  "preview": {
    "source": "1111111111111111111111111111111111111111",
    "image_digest": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "7777777777777777777777777777777777777777777777777777777777777777",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "deployed": {
    "source": "2222222222222222222222222222222222222222",
    "image_digest": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "7777777777777777777777777777777777777777777777777777777777777777",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "previous": {
    "source": "4444444444444444444444444444444444444444",
    "image_digest": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "7777777777777777777777777777777777777777777777777777777777777777",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  }
}
```

**Complete facts-only receipt example.** The policy is the same structure with
artifact `release-receipt-license`. The facts target below is synthetic; a real
consumer must copy and verify the requested canonical commit/hash.

```json
{
  "schema": "pineforge/facts-consumer-receipt/v1",
  "consumer": "license",
  "attempt": 90,
  "sequence": 7,
  "state": "verified",
  "timestamp": "2020-01-01T00:07:00Z",
  "pr": {
    "number": 9,
    "base": "main",
    "base_sha": "3333333333333333333333333333333333333333",
    "head": "1111111111111111111111111111111111111111",
    "merge": "2222222222222222222222222222222222222222"
  },
  "ci": {
    "run_id": 1,
    "run_attempt": 1,
    "sha": "1111111111111111111111111111111111111111",
    "expected": [
      "ci/one",
      "ci/two"
    ],
    "approved_skips": [],
    "results": {
      "ci/one": "success",
      "ci/two": "success"
    }
  },
  "e2e": {
    "run_id": 2,
    "run_attempt": 1,
    "sha": "1111111111111111111111111111111111111111",
    "expected": [
      "e2e/one",
      "e2e/two"
    ],
    "approved_skips": [],
    "results": {
      "e2e/one": "success",
      "e2e/two": "success"
    }
  },
  "deploy": {
    "run_id": 3,
    "run_attempt": 1,
    "sha": "2222222222222222222222222222222222222222",
    "expected": [
      "deploy/one",
      "deploy/two"
    ],
    "approved_skips": [],
    "results": {
      "deploy/one": "success",
      "deploy/two": "success"
    }
  },
  "preview": {
    "source": "1111111111111111111111111111111111111111",
    "image_digest": null,
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "deployed": {
    "source": "2222222222222222222222222222222222222222",
    "image_digest": null,
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "previous": {
    "source": "4444444444444444444444444444444444444444",
    "image_digest": null,
    "facts_commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "facts_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "deployment_id_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "facts": {
    "commit": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
  }
}
```

**Callback.** After artifact upload, use a hub-only App installation token with
Contents write to POST `repos/pineforge-4pass/pineforge-release/dispatches`.
The repository_dispatch event type is exactly **`pineforge-release-status`**.
`client_payload` has exactly four keys, with no receipt, state, facts, repository
name, URL or extra identity fields:

| Key | Type and allowed value |
|---|---|
| `schema` | String, exactly `pineforge/consumer-signal/v1`. |
| `consumer` | One of the six role strings; must be selected by the product or facts flags. |
| `release_version` | Product version string as above; JSON `null` for a facts-only callback. The key is always present. |
| `run_id` | Integer, the trusted **reporting workflow run containing the uploaded receipt artifact**, not the CI/E2E/deploy run or a hub run. |

Complete product callback request:

```json
{
  "event_type": "pineforge-release-status",
  "client_payload": {
    "schema": "pineforge/consumer-signal/v1",
    "consumer": "offline",
    "release_version": "1.6.0",
    "run_id": 90
  }
}
```

Complete facts-only callback request (the facts commit/hash live in the artifact):

```json
{
  "event_type": "pineforge-release-status",
  "client_payload": {
    "schema": "pineforge/consumer-signal/v1",
    "consumer": "license",
    "release_version": null,
    "run_id": 90
  }
}
```

The callback is only a wakeup. Its version is a routing hint, never evidence;
the validated artifact/manifest or facts target is authoritative. Polling can
collect the same artifact after a lost callback. Never dispatch the full
receipt as `client_payload` or use issue comments/issues as receipt transport.

Run `python3 scripts/consumer_receipt_example.py` to print the synthetic product
example, or `python3 scripts/consumer_receipt_example.py --facts-only` for the
license example. Real consumer builders must supply actual evidence, target
identities and their owner-approved inventories. The hub test validates these
examples against its own validator, including the documented JSON.
Both complete examples and the synthetic builder use the ordinary five-key
form; the data bootstrap above is a separate, literal regression fixture.
Use `--consumer hosted` for a synthetic hosted receipt with CI at the PR head
and coordinator E2E at the tested base. Its example policy is the exact hosted
entry below; a real coordinator must also publish its verified binding artifact.

Neutral hub assets retain numeric IDs, commit/image/facts identities and hashes
of private evidence/inventories. Private repository names, job labels, deployment
URLs and raw deployment IDs stay in the consumer artifact. Aggregate assets are
appended only when state changes; one release incident tracks progress and is
assigned/mentioned to the configured owner. Only verified served-state evidence
produces consumer success. Facts freshness uses the latest facts-file change,
not unrelated main commits; current served facts evidence becomes overdue after
30 minutes. Product readiness and consumer-stage ages have separate thresholds,
with a longer app default. Monitor these assets/issues, not green dispatch runs.

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
| `RELEASE_READY_CONSUMERS` (new variable) | Hub repository | Explicit comma-separated upgraded product receiver roles. Empty sends nothing; disabled roles do not block aggregate completion or become overdue. Facts flags independently select web/license receipt polling and freshness. |
| `RELEASE_REPORT_OWNER` (new variable) | Hub repository | GitHub login to assign/mention on release and failure issues. |
| `RELEASE_READY_MAX_AGE_MINUTES` (optional variable; 60) | Hub repository | Missing readiness prerequisite alert threshold. |
| `RELEASE_CONSUMER_MAX_AGE_MINUTES` (optional variable; 180) | Hub repository | Non-app missing/stalled completion threshold. |
| `RELEASE_APPLICATION_MAX_AGE_MINUTES` (optional variable; 1440) | Hub repository | App stage freshness, separate from per-test time limits. |
| `FACTS_LICENSE_DISPATCH_ENABLED` (new variable) | Hub repository | Set true after the license facts receiver and receipt policy are ready; also selects license receipt polling without product dispatch. Existing repository `FACTS_WEB_DISPATCH_ENABLED` independently selects web facts dispatch/polling. |
| Actions permissions, Issues, GHCR visibility | Hub repository | Permit workflow contents/packages/issues writes; enable Issues and public pulls of the existing GHCR package. |
| `release-automation` environment (existing) | Hub repository | Permit protected main and release `v*` tags and the intended automatic jobs. Required human reviewers would pause full automation. |
| Release asset setting | Hub repository | These releases keep append-only audit/receipt assets after publication; GitHub's repository-wide immutable-release setting must permit these appends. The scripts enforce manifest/fixed-tag byte immutability and never overwrite/delete them. |
| Receivers and callback credentials | Each enabled consumer | Receiver on default branch; own CI + E2E aggregate required; scoped hub Contents-write callback token; artifact upload/read access. |

Policy example: the `offline` entry remains a synthetic structure, not a
production inventory. The `hosted` entry is the exact policy from the reviewed
hosted credential-isolation contract. Merge that hosted entry into the existing
`RELEASE_RECEIPT_POLICY` only after installing this hub adapter, preserving every
other consumer entry. The supervisor applies the live policy; this change does
not alter GitHub settings.

```json
{
  "offline": {
    "workflow": "release.yml", "artifact": "release-receipt-offline", "base": "main",
    "ci": {"workflow": "ci.yml", "expected": ["check", "unit"], "approved_skips": []},
    "e2e": {"workflow": "e2e.yml", "expected": ["stdio-parity", "syminfo", "negative"], "approved_skips": []},
    "deploy": {"workflow": "publish.yml", "expected": ["publish", "served-identity"], "approved_skips": []}
  },
  "hosted": {
    "workflow": "release-report.yml",
    "artifact": "release-receipt-hosted",
    "base": "main",
    "ci": {
      "workflow": "release-pr.yml",
      "expected": ["validate", "checks", "container", "candidate"],
      "approved_skips": []
    },
    "e2e": {
      "workflow": "release-coordinator.yml",
      "expected": ["candidate", "release/ready"],
      "approved_skips": []
    },
    "deploy": {
      "workflow": "release.yml",
      "expected": ["deploy"],
      "approved_skips": []
    }
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
repair rather than deleting an alias or inventing a good target. **A hub rollback
sends no consumer signal.** Consumers may keep the rejected release while hub
aliases revert, so mixed versions are possible. Each consumer lane owns its
explicit rollback and served-state verification; coordinate those rollbacks
separately.

CI runs the existing release/source/harness tests plus the ready replay and
negative controls. See [the current replay entry point](../tools/release-dry-run/README.md).
`tests/test_release_coordinator_receipts.py` adds local API/ZIP fixtures for
truthful hosted receipts and red cases for source trust, binding and candidate
replacement, archive digests, PR/head/base/attempt mismatches and inventory
changes, plus regressions for the original exact-SHA path. The existing
`test_release_*.py` discovery includes it in CI. These tests are written for
CI/cloud execution and are not run on the Mac; local validation is limited to
Python compilation and `git diff --check` (plus YAML parsing if a workflow
changes).
No test suite, Docker runtime/build, live dispatch, publication, token mint,
credential read or deployment is needed on the Mac to implement this lane.
