# Automatic release hub

This implements briefs 1 (hub reconciliation) and 2 of the 2026-10-16 release
chain design. Engine/codegen/HPO remain the producers. `release_pair.py` remains
the authority for semver order, lockstep and the HPO `1.6.0-rc.1` cutoff. The
hub does not create producer versions or consumer commits, merge consumer PRs,
or deploy consumers.

## Release runner isolation

Owner decision 10-21 (msg 1562390657274748981) sets these release and facts-automation
jobs to the literal `runs-on: ubuntu-latest`, a standing R6/rule-B exception
for pineforge-release:

| Workflow | Jobs |
|---|---|
| `facts-descriptions.yml` | `descriptions` |
| `facts-fanout.yml` | `web`, `license` |
| `handle-upstream.yml` | `bump` |
| `publish.yml` | `tag-guard`, `prepare`, `publish`, `failure-report` |
| `release-finalize.yml` | `finalize`, `notify-consumers` |
| `release-receipts.yml` | `plan`, `verify` |
| `release-reconcile.yml` | `producers` |
| `release-rollback.yml` | `restore` |

The former release class has its own runner group restricted to selected workflows at protected
refs, its own no-role service account, and one job per runner, with nothing shared
with PR classes. Secret-bearing jobs and jobs that build artifacts later published
by a release job formerly belonged in this class. `prepare`, receipt `plan`, and
`failure-report` also hold the automatic `GITHUB_TOKEN` through `github.token`,
with Contents or Issues write access. The `publish.yml` reusable
`notify-consumers` caller obtains its runners from `release-finalize.yml`.

`pull_request` and `pull_request_target` workflows must never use this class.
The PR jobs in `python-test.yml` and `facts-validate.yml` also use
`runs-on: ubuntu-latest` per the owner decision.
The hub runs these jobs on GitHub-hosted runners without waiting for
the release pool. These hosted jobs do not require a release runner group allowlist.

Publication admits runs at `refs/heads/main` only. Publication
uses `workflow_dispatch` on `main` with an explicit existing tag input;
`tag-guard` fails any other workflow ref and is a required dependency of every
later job. With Contents read access and no secrets, it checks out main with
full history and no persisted credentials. It validates the complete
`vX.Y.Z` / `vX.Y.Z-rc.N` pattern before using the input in git, fetches tags and
main, verifies that the tag exists and peels to a commit, and requires that
commit to be an ancestor of `refs/remotes/origin/main` (named in full: a tag
called `origin/main` cannot stand in for it). Both tagged checkouts use the guard's
verified commit SHA, with no persisted credentials, so moving a tag between
jobs cannot change the tree built. A failed or skipped guard prevents
finalization; failure reporting first refuses to check out or run repository
code unless the guard succeeded.
The reusable `release-finalize.yml` call inherits the caller's main ref.
Tag pushes, release events and `workflow_run` do not start publication.

## Receive, verify and replay

`handle-upstream.yml` still accepts `engine-release`, `codegen-release` and
`hpo-release` with `{version, prerelease?, run_id?}`. Version is a string,
prerelease is a JSON boolean, and a supplied run ID is a positive JSON integer.
HPO accepts semver or PEP 440; HPO release tags/assets use PEP 440. Each event's
own published artifacts are verified before the existing partner/pair decision.
Incomplete lockstep waits. Invalid flags, source mismatches, authentication,
rate limits and transport failures fail visibly.

`release-reconcile.yml` wakes hourly or manually. It paginates the
three fixed producer repositories, excludes drafts/prereleases/non-product tags
and versions older than hub VERSION. It checks whether the desired stable
version already has a hub Git tag before downloading any artifacts. Only a
version without that tag has its artifacts verified and the **same producer
events** sent back to the existing handler. The handler pushes the hub tag;
after the tag exists on main and `handle-upstream` is green, a user with write
access dispatches `publish.yml` on `main` with that tag using the command below.
Finalization then continues even while the release is a draft awaiting prerequisites.
Older missed versions are superseded by the newest desired stable version;
they are never replayed as downgrades after it. Duplicate notifications reuse
the handler's existing decision. A stored candidate makes an unchanged-version
decision a no-op even while the fixed image tag is absent. Nothing calls a
producer's version-bump job.

Recovery of an existing hub tag emits an operator notice and never force-pushes
or rewrites the tag. Publication and replay use the same operator command,
run by a user with write access after the tag exists on main and
`handle-upstream` is green:
`gh workflow run publish.yml --repo pineforge-4pass/pineforge-release --ref main -f tag=<vX.Y.Z or vX.Y.Z-rc.N>`.
Use the actual existing tag as `tag`. Pushing a tag alone publishes nothing.
Both a new tag push and retag recovery leave publication to the operator;
an unchanged version with an image or verified candidate keeps its existing no-op.
Tags predating this workflow need a reviewed migration; the hub does not invent
new manifests for historical images without candidate verification.

## Candidate and readiness

`publish.yml` validates the release tag before preparation can reuse
`release-candidate.json` if already stored. Otherwise
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
as a reusable job after publication, on facts-file changes, hourly,
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

The `offline`, `hosted`, `web`, `data` and `license` event type remains
**`pineforge-release`**, with unchanged payload bytes. Its ten payload fields are
exactly section 3.2 of the design: `schema`, `event_id`, `release_version`,
`prerelease`, `hub_commit`, `versions`, `image`, `facts`, `manifest`, `run_id`.
The `application` and `compute` roles use **`release-pair`**, described below.
`EVENT_TYPES` in `scripts/release_contract.py` is the single role-to-event table.

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

`image.repository` must equal EXACTLY one of two values, the image path before
and after the GitHub organization rename:
`ghcr.io/pineforge-4pass/pineforge-release` or `ghcr.io/tunaxis/pineforge-release`.
The validator in `scripts/release_contract.py` compares the whole string; any
other organization, repository name, case, trailing slash, prefix or empty value is
rejected. A consumer builds its pin from the validated payload value, never from a
constant of its own.

Consumers must fetch the asset from the fixed hub repository's matching GitHub
release, hash the original bytes, compare the complete payload, and verify the
referenced artifacts. No arbitrary repository/asset/path is accepted from the
sender. A newer verified canonical facts snapshot may be retained if its target
release entry agrees exactly. Record the actually consumed commit and digest.

### Pair event for application and compute

`pair_payload(manifest_raw, candidate_raw)` in `scripts/release_contract.py`
builds the same `release-pair` payload for both targets. It validates the
immutable manifest and the candidate's existing smoke/source gates, then
requires equal release version, image, hub commit, build run and producer pins.
The manifest's codegen npm fields are the finalizer's additions; all original
candidate codegen fields must still agree. Missing or mismatched input fails
before a dispatch. URLs come from the fixed hub repository and fixed asset
names, never from target configuration.

The payload has exactly these eight top-level keys:

| Key | Value / source |
|---|---|
| `pair_id` | Manifest `release_version`, bare canonical semver including any prerelease suffix. |
| `engine_tag` | Manifest `versions.engine.tag`. |
| `codegen_tag` | `v` plus manifest `versions.codegen.version`; the codegen binding has no tag field. |
| `hpo_tag` | Manifest `versions.hpo.tag`, using its PEP 440 spelling, or JSON null before the HPO cutoff. |
| `receipt_url` | Fixed hub's `releases/download/v<pair_id>/release-manifest.json` HTTPS URL. |
| `receipt_sha256` | Lowercase SHA-256 of the untouched published manifest bytes, equal to the ready contract's `manifest.sha256`. |
| `source_sets` | A one-element array containing the checked candidate's `sources`: exactly `release_tag`, `list_asset`, `list_sha256`, `amd64_sha256`, `arm64_sha256`. Source tags must match the producer's `release_sources.RELEASE_TAG` pattern `v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?` against the whole string; list assets have the public `set-NN.SOURCES.list` form. |
| `build` | Exactly the six members in the following table. |

| `build` key | Value / source |
|---|---|
| `prerelease` | Manifest boolean `prerelease`. |
| `hub_commit` | Manifest `hub_commit`. |
| `run_id` | Manifest integer `run_id`, the original candidate build run. |
| `image` | Manifest image object: exactly `repository`, `digest`, `amd64_digest`, `arm64_digest`, with the fixed public hub image paths and bare OCI digests validated above. |
| `candidate_url` | Fixed hub's `releases/download/v<pair_id>/release-candidate.json` HTTPS URL. |
| `candidate_sha256` | Lowercase SHA-256 of the untouched published candidate bytes. |

The application remains a normal receipt role. Its event type changes to
`release-pair` so it receives the shared pair/build inputs; this does not change
its CI/E2E/deploy receipt requirements. Derive
`event_id = "sha256:" + receipt_sha256`; facts and image identity come from the
manifest fetched at `receipt_url`, after checking its untouched bytes against
`receipt_sha256`. It remains `skipped_by_policy` for prereleases because its
receiver refuses those dispatches.

Builders remain deployers: the application builds its Cloud Run image and
hosted builds its container. The compute receiver only validates each pair and
records evidence; it builds nothing and dispatches nothing onward. It reports
no consumer receipt to this hub. Private target repository names, deployment
registry paths, project/account identities and VM names stay in trusted deployer
configuration. Public deployment evidence carries bare OCI digests and hashes
of private evidence, never those private identifiers.

Both target repository names live in the hub's `release-automation` environment
secrets: `RELEASE_APPLICATION_REPOSITORY` and `RELEASE_COMPUTE_REPOSITORY`.
The sender reuses the existing App token mint, scoped to one target with
Contents write. The existing App must be installed on the private compute
repository with the App's full permission set, to be confirmed in the App's
settings before the owner decides **O-1**. App permissions apply to every
repository in the installation: the existing App already holds Actions write
for the hub, and installing it on compute grants that and every other App
permission there. The mint uses `owner: github.repository_owner`, so compute
joins the same owner's installation; each mint's requested permissions limit
that token rather than the App installation.
The supervisor supplies the secret values; source, matrices, public audit
assets and incidents contain only neutral roles.

For an agreed manual replay, select a non-rejected finalized pair and fetch its
original manifest and candidate. Reconstruct these eight inputs using the same
mapping and byte hashes, then run the configured compute receiver
`release-pair.yml` on its default branch. Supply `source_sets` and `build` as
JSON strings; blank `hpo_tag` decodes to null. Repeating the same
`(pair_id, receipt_sha256)` is idempotent. Failed sends retain the existing
bounded delivery history and report a neutral incident to
`RELEASE_REPORT_OWNER`. No older-pair backfill is needed for the initial rollout.

The hub's `release-redeliver.yml` uses the same `release-pair` mapping for
application and compute, fetching the original manifest and candidate bytes.
Compute repair also requires its role selection and a valid, nonempty
`RELEASE_COMPUTE_FIRST_PAIR` no greater than the pair's version. A newer release
does not supersede an eligible compute repair; application keeps its existing
prerelease skip and supersession rules.

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
by `FACTS_WEB_DISPATCH_ENABLED=true`. Each selected receipt role needs a receipt
policy; `compute` is delivery-only and needs none.
Disabled product roles are omitted from aggregate consumers, completion and
staleness calculations; an empty product set completes with no deliveries.
Prerelease application/web/data/license roles, if configured, are explicitly
`skipped_by_policy`; offline/hosted must enforce their dev/next policy.

| Role | Product event type | Completion | Prerelease policy |
|---|---|---|---|
| `offline` | `pineforge-release` | Verified consumer receipt | Consumer dev/next gates |
| `hosted` | `pineforge-release` | Verified consumer receipt | Consumer dev/next gates |
| `application` | `release-pair` | Verified consumer receipt | `skipped_by_policy` |
| `web` | `pineforge-release` | Verified consumer receipt | `skipped_by_policy` |
| `data` | `pineforge-release` | Verified consumer receipt | `skipped_by_policy` |
| `license` | `pineforge-release` if explicitly selected; normally facts-only | Verified consumer receipt | `skipped_by_policy` for product releases |
| `compute` | `release-pair` | `delivered` after transport acceptance | Receives every eligible pair, including rc |

Compute is planned only when **both** `compute` is in `RELEASE_READY_CONSUMERS`
and repository variable `RELEASE_COMPUTE_FIRST_PAIR` is nonempty and no greater
than the pair's version, using the hub's semver parser. Empty means off, even if
the role is listed. Below-floor pairs are omitted from compute's deliveries,
aggregate and staleness inventory. Leave the variable empty until the compute
receiver is merged, then the supervisor sets the agreed current application/
hosted pair. There is no automatic backfill below that floor.

`DELIVERY_ONLY = ("compute",)` explicitly separates delivery roles from the six
`RECEIPT_ROLES`. Before `delivery-compute.json`, compute is `pending`; after that
asset it is `delivered`, which counts as complete and can close a release
incident. It never becomes `superseded` or `skipped_by_policy`, and a newer ready
release does not refuse an eligible compute send or retry. `delivered` proves
transport acceptance; it makes no deployment or served-state claim.

Delivery acceptance is recorded once as `delivery-<consumer>.json`, independently
of consumer progress. Its presence stops further dispatch even if the consumer
has not yet reported completion. For receipt roles, a missing receipt becomes
pending/overdue; it never triggers another build or an endless product event
retry. Before each send the finalizer reserves an append-only
`delivery-attempt-<consumer>-N.json`.
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

Use `release-redeliver.yml` for an explicitly reviewed repair of one published
release to one consumer after the three automatic attempts are spent or the
consumer was not ready, including accepted delivery without verified completion.
It runs only from main.
Only operators permitted to dispatch the workflow may trigger it, and the job
uses the `release-automation` environment: today that environment permits main
and release tags and has no required reviewers, so the dispatch right is the
only approval unless the owner gives the repair a dedicated reviewed environment.
The consumer must be in `RELEASE_READY_CONSUMERS`. It resends the immutable manifest's identical
payload and appends `delivery-redelivered-<consumer>-<n>.json` only after dispatch
acceptance, recording the event, independent contiguous redelivery number, run,
timestamp and `GITHUB_ACTOR` login. It never touches `delivery-<consumer>.json`,
`delivery-failed-<consumer>.json`, `delivery-attempt-*`, receipts or other release
assets. If dispatch succeeds but the audit write fails, the run fails; when no
audit asset was written, the next run sends again with the same number, so a
duplicate delivery is possible. The receipt contract is unchanged: redelivery
keeps the same `event_id`, and the consumer's idempotence rules decide how to
handle it.

`release-receipts.yml` polls selected consumers every 30 minutes and accepts the
callback below to repair dropped notifications. It mints a target-only read
token, verifies the artifact and full GitHub CI/E2E/deploy evidence, then appends
a neutral receipt. Its planner excludes `DELIVERY_ONLY` roles, so compute has
no policy lookup, read token or workflow poll; compute callbacks and consumer
receipts are refused. Acceptance of a dispatch or callback never means verified.

The planner and collector both receive `RELEASE_READY_CONSUMERS`. A configured
product consumer requires an event-bound receipt even before delivery acceptance,
after automatic retries are exhausted, or after manual redelivery; a missing
`delivery-<consumer>.json` does not exempt it. A sweep reports skipped invalid
artifacts before failing for missing required receipts. Supersession follows
dispatch and redelivery: a stable release yields to a newer stable release,
while a prerelease yields to any newer published, non-rejected manifest-backed
release. Drafts and releases without a manifest cannot supersede a target.
Receipt ZIP entries are read with a bounded decompressed-byte limit of 2 MiB;
malformed entry streams are invalid evidence.

## Consumer receipt contract v1

This is the single consumer-to-hub contract for the six receipt lanes;
delivery-only compute is excluded. Implement the artifact and callback in the
consumer's trusted default-branch coordinator.
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

Only the `application` policy may additionally contain `post_merge`. It has the
same exact `workflow`, `expected`, `approved_skips` fields and inventory rules,
with `approved_skips` required to be `[]`. Its workflow must be a `.yml`
filename. Policies for all other roles must omit it.

**Types and product receipt fields.** An integer below is a JSON integer from
1 through 9007199254740991, never a boolean or numeric string. A commit is
exactly 40 lowercase hexadecimal characters. A SHA-256 is exactly 64 lowercase
hexadecimal characters; a digest is `sha256:` followed by a SHA-256. Every field
in this table is required except the conditional `post_merge` field. Evidence
fields can be `null` until their stage requires them. Objects have exactly the
fields defined here.

| Field | Type and allowed value |
|---|---|
| `schema` | String, exactly `pineforge/consumer-receipt/v1`. |
| `event_id` | Digest string, exactly the verified hub manifest's event ID. For application `release-pair`, `event_id = "sha256:" + receipt_sha256`; facts and image identity come from the manifest fetched at `receipt_url`, with its untouched bytes checked against `receipt_sha256`. |
| `consumer` | String: `offline`, `hosted`, `application`, `web`, `data`, or `license`; must match the hub's scoped target/policy. License implements the facts-only variant below. |
| `release_version` | Bare canonical semver string `X.Y.Z` or `X.Y.Z-{alpha,beta,rc}.N`, exactly the hub event's version; no leading `v` or leading-zero integers. |
| `attempt` | Integer identifying an automation attempt, increasing per consumer/target; use the coordinator's GitHub run ID, not `GITHUB_RUN_ATTEMPT` alone. |
| `sequence` | Integer, strictly increasing for new snapshots within that attempt. It may restart at 1 for a larger attempt. |
| `state` | One of the ten strings in the state table below. |
| `timestamp` | Real UTC timestamp string `YYYY-MM-DDTHH:MM:SSZ`, at most five minutes in the future; for verified, at/after the final deploy check completion and, when configured, the final post-merge check completion; for hosted post-merge E2E, at or after the last E2E job's completion. |
| `pr` | PR object below, or `null` before PR evidence exists. |
| `ci`, `e2e`, `deploy` | Each a check-evidence object below, or `null` before that evidence exists. |
| `post_merge` | Application only: required exactly when the policy contains `post_merge`, otherwise forbidden. Same check-evidence object below; may be `null` before `verified`. |
| `preview`, `deployed`, `previous` | Each a served-identity object below, or `null` before that evidence exists. `previous` records the rollback identity; only `data.previous` has the temporary pre-manifest bootstrap form below and only `offline.previous` may carry the legacy predecessor form. |

The PR object has exactly these fields:

| Field | Type and allowed value |
|---|---|
| `number` | Integer, a PR in the configured consumer repository; forks are refused. |
| `base` | String `main` or `dev`, equal to policy. |
| `base_sha` | Commit, the base tested with this PR head. |
| `head` | Commit, the actual PR head. |
| `merge` | `null` before merge, then the actual merge/squash commit. |

A check-evidence object has exactly these fields, for each of `ci`, `e2e`,
`deploy`, and the conditional application `post_merge`:

| Field | Type and allowed value |
|---|---|
| `run_id` | Integer, GitHub Actions run in the configured consumer repository. |
| `run_attempt` | Integer, the particular GitHub attempt whose jobs are checked. |
| `sha` | Commit; `pr.head` for CI/E2E, `pr.merge` for deploy and application post-merge. Hosted `release-postmerge.yml` E2E uses `pr.merge`; legacy hosted `release-coordinator.yml` E2E uses `pr.base_sha`, subject to the binding checks below. |
| `expected` | Array of unique job-name strings, exactly the corresponding policy list, in the same order. |
| `approved_skips` | Array of job-name strings, exactly the corresponding policy list, in the same order. |
| `results` | Object with exactly one key per expected job; values `success`, or `skipped` only for owner-approved skips. Must equal actual GitHub jobs. |

The referenced run must have completed successfully. Every expected job must
exist exactly once and finish successfully or have its explicit approved skip;
at least one must succeed. CI/E2E must finish before the PR's actual merge time,
except hosted post-merge E2E as described below.
The merge commit's first parent must be `base_sha`, proving the tested base
landed. An unmerged PR must still have the tested base. Rebase/merge-queue source
graphs need an approved adapter before enablement.

For a configured application `post_merge` gate, only `verified` validates its
value. The hub checks the recorded run and attempt at exactly `pr.merge`, with
path `.github/workflows/<post_merge.workflow>`, completed status, successful run
conclusion, and every expected job completed successfully; no skips are allowed.
The run read from GitHub must have the receipt's run id and attempt.
GitHub results must equal the receipt. The run must have event `push` and
`head_branch == policy.base` (`dev` in the application example), and the receipt
timestamp must be at or after the last expected job completion. Earlier states
still require the member but do not validate its value, just as deployment
evidence is deferred. The neutral verified receipt adds `post_merge` containing
only `run_id`, `run_attempt`, and `inventory_sha256`, the SHA-256 of the canonical
results object, in the same form as `deploy`.

**Hosted post-merge E2E.** Hosted now uses CI `release-pr.yml`, E2E
`release-postmerge.yml` and deploy `release.yml`. E2E requires exactly
`["authz", "build", "verified"]` with no approved skips. Its run and
`preview.source` bind to `pr.merge`; the run must use `workflow_dispatch` on
`main`, also the repository default branch and PR base. Its jobs must finish
at or after the PR's actual `merged_at` and at or before the receipt timestamp;
CI still finishes before merge. The run must retain exactly one non-expired
`postmerge-evidence-<run_id>-<run_attempt>` artifact; the hub checks its name and
existence, while its contents remain the hosted repository's private contract.
E2E must be non-null exactly in `verified`. The verifier takes the old coordinator
path below only while the live `hosted` policy entry is the old coordinator policy.
There is one live policy per role and no per-event or per-date policy, so after
the `hosted` key is replaced by the post-merge policy, a retained coordinator-shape
`verified` receipt is refused with "required check inventory differs from owner policy".
Retained receipts whose evidence is null (`pending`, `failed`, `superseded`,
`skipped_by_policy`) are unaffected.

**Hosted coordinator binding.** Only consumer `hosted`, with CI workflow
`release-pr.yml` and E2E workflow `release-coordinator.yml`, can use the base-code
adapter in [`check_coordinator`](../scripts/release_receipts.py). Its E2E
inventory is exactly `["candidate", "release/ready"]` in that order, with no
approved skips. CI still runs at `pr.head`; deploy still runs at `pr.merge`.
Other E2E workflows, apart from hosted post-merge above, retain the exact
PR-head check in `check_run`.

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

For the legacy coordinator policy, only `candidate` and `release/ready` are
the required hosted E2E jobs.
`pending`, `policy` and `merge` are orchestration jobs, not additions to that
inventory. The legacy coordinator policy's hosted CI requires `validate`, `checks`,
`container`, `candidate`; its deploy requires `deploy`. Neither inventory permits
skips. The post-merge policy's inventories (CI `authz, validate, checks, container`;
E2E `authz, build, verified`; deploy `decide, deploy`) are in the policy example
below. The public
receipt and callback schemas remain v1; the binding stays consumer-side.

An ordinary served-identity object has exactly these five fields:

| Field | Type and allowed value |
|---|---|
| `source` | Commit; `pr.head` for preview (hosted post-merge uses `pr.merge`), `pr.merge` for deployed, previous deployed source for previous. |
| `image_digest` | Digest string; preview and deployed equal the product manifest's index digest. Facts-only may use `null` when no product image is consumed. |
| `facts_commit` | Commit, actually consumed canonical hub facts source. |
| `facts_sha256` | SHA-256 of the exact facts bytes consumed. |
| `deployment_id_sha256` | SHA-256 of the consumer's raw deployment/distribution identity; no raw private deployment ID or URL. |

The ordinary form never has a `provenance` key, including for a real previous
release. The only alternatives are the temporary data `previous` form and the offline legacy `previous` form below.

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

This form is meant for one use, only for pineforge-data 0.3.0 as the previous
release when publishing 0.3.1. The hub validates the form and its scope (data
consumer, `previous` slot, exact values); a receipt carries no data package
version, so the hub does NOT itself refuse the form in a later data receipt.
The one-time limit is enforced by the data producer (the exemption in
pineforge-data `scripts/release_chain/publishing.py`, constant block
`BOOTSTRAP_PACKAGE`, only for the 0.3.1 first-adopter lock) and by removal.
Any other package/version and every real previous must use the ordinary
five-key form; a real previous with any `provenance` key is refused. Data 0.3.1
as a later release's previous is a real identity.
**Removal (both repos, in step):** delete this temporary form from the contract
and the data exemption only after (1) data 0.3.1 is verified AND (2) every
stored `release-receipt-data` artifact whose receipt carries the bootstrap
`previous` has expired (data uploads it with 90-day retention): the scheduled
collector re-validates every retained receipt artifact that the hub has not
already stored, so dropping the form earlier makes each such artifact fail
validation; in a sweep it is reported and skipped (see the collector skip rules
below), and a callback naming that run still fails closed.

**Legacy offline `previous`.** Only a product receipt with `consumer: offline` may
record a predecessor that has no `release-lock.json`, and only in `previous`; the
object is refused in `preview`, `deployed`, every other consumer and every
facts-only receipt. It has exactly five keys: `legacy` (the boolean `true`),
`npm_version` (bare canonical semver, no leading `v`), `npm_integrity` (`sha512-`
plus 86 base64 characters and `==`), `git_head` (a 40-hex commit) and `image_digest`
(`sha256:` plus 64 hex, never `null`). The hub checks shape only; it does not pin
the values to a particular release.

**State machine.** Ordered progress is
`pending → pr_open → testing → ready → merged → deploying → verified`.
Intermediate snapshots may be omitted: the hub checks all prerequisites of the
reported state even if earlier snapshots were never collected.

| State | Required evidence and meaning |
|---|---|
| `pending` | Target accepted by coordinator; evidence fields may all be null. |
| `pr_open` | Current same-repository PR with tested base/head recorded. |
| `testing` | Same PR evidence; tests are in progress, not yet sufficient for ready. |
| `ready` | PR + successful complete CI/E2E inventories + preview source/image/facts identity. Under the hosted post-merge policy, a `ready` receipt is not used: its E2E and preview bind to the merge commit, so the hub refuses a pre-merge `ready` receipt; `e2e` is non-null exactly in `verified`. |
| `merged` | Ready evidence plus actual merge commit, tested first parent and CI/E2E completion before merge (CI only for hosted post-merge). |
| `deploying` | Same evidence as merged; serving has not yet been verified. |
| `verified` | All prior gates + successful complete deploy inventory + deployed and previous identities; report source is the merge commit. Hosted post-merge requires its E2E here. Application also requires its post-merge gate when configured. |
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
Facts-only receipts and policies never contain `post_merge`.

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
form; the data bootstrap above is a separate, literal documented example.
Use `--consumer hosted` for a synthetic hosted receipt with CI at the PR head
and post-merge E2E and preview at the merge commit. Its example policy is the
exact hosted entry below; the E2E run must retain its post-merge evidence artifact.
Use `--consumer application` for a synthetic application receipt with base `dev`
and `post_merge` evidence at the merge commit. Its policy matches the application
example below; replace all `example-*` values with APP's real workflows and
inventories before use. Other consumer examples omit `post_merge`.

Neutral hub assets retain numeric IDs, commit/image/facts identities and hashes
of private evidence/inventories. Private repository names, job labels, deployment
URLs and raw deployment IDs stay in the consumer artifact. Aggregate assets are
appended only when state changes; one release incident tracks progress and is
assigned/mentioned to the configured owner. Only verified served-state evidence
produces receipt-role success; compute completes at delivery acceptance as
described above. Facts freshness uses the latest facts-file change,
not unrelated main commits; current served facts evidence becomes overdue after
30 minutes. Product readiness and consumer-stage ages have separate thresholds,
with a longer app default. Monitor these assets/issues, not green dispatch runs.

## Supervisor configuration

Do not enable a receipt role until its consumer lane has removed moving runtime
defaults and installed PR/CI/E2E/deploy gates; the same holds for every role.
Compute instead waits for its merged pair-validation receiver and explicit
first-pair gate. Consumer App identities must not bypass
their required branch checks, and no consumer may use an admin merge. Configure
`authz` and `release/ready` as REQUIRED hub branch/ruleset status checks.
`release/ready` requires successful authz and change detection, then the complete
suite inventory; only a verified documentation-only PR may skip suites.

The hub's `python-test.yml` and `facts-validate.yml` PR checks use the literal
`runs-on: ubuntu-latest` per owner decision 10-21 (msg 1562390657274748981),
a standing R6/rule-B exception for pineforge-release, without a `PF_CI_LABELS`
environment variable. Both workflows run on every pull request without path or
branch filters. Their first job is the canonical `authz` from
`ci-labels.md` with the owner-approved runner exception, `name: authz`,
`runs-on: ubuntu-latest`, `permissions: {}`, no
condition, checkout or secrets, and the canonical no-timeout comment.
Its eight payload fields are `EVENT`, `REPO`, `HEAD_REPO`, `HEAD_IS_FORK`,
`ASSOCIATION`, `PR_AUTHOR`, `WR_HEAD_REPO` and `WR_ACTOR`; the other two
environment values are `GH_TOKEN` and
`ALLOWED_BOTS: "dependabot[bot] pineforge-app[bot]"`.
The job grants no token scopes and uses `${{ github.token }}` only for the
`pull_request` and `workflow_run` metadata permission lookups.
For `pull_request`, authz requires this repository's non-fork head and accepts
an `OWNER`, `MEMBER` or `COLLABORATOR` author association, the exact author login
`dependabot[bot]` or `pineforge-app[bot]`, or author permission `admin`, `maintain`
or `write` read from GitHub.
An author outside the bot allowlist reported as `CONTRIBUTOR`, including a
member with private organization membership, passes only with `admin`,
`maintain` or `write` permission read from GitHub; a failed lookup refuses the run.
For `workflow_run`, authz requires this repository's head and either an
allowlisted bot or triggering-actor permission `admin`, `maintain` or
`write`; a failed permission lookup refuses the run.
The allowed ordinary events are `push`, `workflow_dispatch`, `schedule`,
`merge_group`, `release`, `workflow_call` and `repository_dispatch`;
`pull_request_target` and unknown events fail red.
Allowed runs are recorded in the job summary; refusals emit a GitHub error
annotation, record the reason in the summary and exit with status 1.

Every other job needs `authz`, retaining the existing documentation-only
conditions. `release/ready` uses `if: ${{ always() }}` and starts with
`Refuse an unauthorised run before anything else` before evaluating the job inventory.
That first step has `if: ${{ needs.authz.result != 'success' }}` and a two-line
`run: |` block, exactly as in the canonical aggregate: an `echo` of
`::error::authz did not pass; nothing of this run's code is checked out or run`,
then `exit 1` on the next line.
An unsuccessful authz result fails the job before any other step; successful
authz skips that refusal. The existing inline
inventory script then requires successful change detection and the expected
result for every suite. The inventory step also has
`if: needs.authz.result == 'success'`.
Only the existing documentation-only PR rule accepts skipped suites; authz
must always succeed. Ordinary jobs with a status override such as `!cancelled()`
must also require `needs.authz.result == 'success'` in their condition; this
enforces dependency success rather than testing a PR's repository.
The workflow pins require the same first refusal step for every job with a
direct authz dependency whose condition uses `always()`, `!cancelled()`,
`failure()` or `success() || failure()`, before any checkout, action or
repository code (SECURITY #819).
Every subsequent step in these jobs, including third-party actions, requires
`if: needs.authz.result == 'success'`; an existing condition follows `&&`
inside parentheses so every branch remains guarded (#836/#837).
The required aggregate uses `always()` so cancellation cannot skip the check:
GitHub counts a skipped required check as success, allowing a cancelled run
to appear green without running its tests (#838/#839).

The repository setting is required because a fork can edit or delete
`authz` in its proposed workflow.
For private repositories, Settings > Actions > General > "Fork pull request
workflows" must have "Run workflows from fork pull requests" OFF, and the
setting must be read back.
For public repositories, require approval for all external contributors
(`all_external_contributors`); the self-hosted runner group must restrict
access to the org's private repositories with "Allow public repositories" OFF.
The dispatcher's identity gate decides who gets a runner; authz supplies
the second lock and the readable red verdict.
An outside contributor's run held for approval leaves required authz
pending and blocks merging.
If approved, the dispatcher refuses the runner and cancels the run;
cancelled or failed authz blocks merging.
A member's own fork fails the canonical authz check.
The existing `merge_group` trigger remains
configured and passes the canonical authz script. macOS stays hosted
(no hardware); this repository has no macOS jobs. Credential/release jobs
use `ubuntu-latest` per owner decision 10-21, including publication workflows.

| Name / setting | Scope | Purpose |
|---|---|---|
| `PINEFORGE_APP_ID`, `PINEFORGE_APP_PRIVATE_KEY` (existing) | Hub `release-automation` environment | Short-lived, one-target installation tokens; no PAT fallback. |
| App installation permissions | The App's full permission set on every installed repository, including the private compute repository | Confirm the full set in the App's settings before the owner decides O-1. The App already holds Actions write for the hub; installing it on compute grants that and every other App permission there. The mint uses `owner: github.repository_owner` and joins the same owner's installation. Hub tag pushes and producer-event reconciliation; consumer dispatch and completion evidence reads. Publication and replay use the write-access operator's login. Each mint asks only for the permissions needed by that job: hub Contents write; receipt consumers Contents read/write, Actions read and Pull requests read; delivery-only compute uses only the existing Contents-write dispatch mint. Consumer PR writers in other repos separately need Pull requests write without branch bypass. |
| `RELEASE_HOSTED_MCP_REPOSITORY`, `RELEASE_APPLICATION_REPOSITORY`, `FACTS_WEB_REPOSITORY` (existing) | Hub `release-automation` environment secrets | Private short repository names for neutral hosted/application/web targets. Public offline/data/license names are fixed in workflows. |
| `RELEASE_COMPUTE_REPOSITORY` (new secret) | Hub `release-automation` environment secrets | Private short name of the compute target repository; the application target likewise lives in `RELEASE_APPLICATION_REPOSITORY`. Both reuse the existing one-target App mint. No values belong in source or public audit assets. |
| `RELEASE_RECEIPT_POLICY` (new secret) | Hub repository Actions secret (read by the receipt verify job, which runs in the `release-automation` environment; do not also create it in the environment, it would shadow this one) | JSON map of each enabled receipt role to its trusted receipt workflow/artifact/base and full CI/E2E/deploy job inventories, plus optional application-only `post_merge`. Compute needs no entry. Keep private workflow/job details out of public source. |
| `RELEASE_READY_CONSUMERS` (new variable) | Hub repository | Explicit comma-separated upgraded product receiver roles. Empty sends nothing; disabled roles do not block aggregate completion or become overdue. Facts flags independently select web/license receipt polling and freshness. |
| `RELEASE_COMPUTE_FIRST_PAIR` (new variable; empty/off) | Hub repository | Bare canonical semver lower bound for compute, including prereleases. Compute must also be in `RELEASE_READY_CONSUMERS`. When selected, validate the value once before processing releases. A malformed value disables compute for that run and sets the fixed `compute_configuration_error` output before any release work. Other roles are processed as with compute off; the configuration error alone leaves the script's exit status at 0. The workflow reports it after consumer notification as described below. Diagnostics name the variable and say its value was not accepted, without exposing the value. The configuration error creates or reopens no per-release incident. An unselected compute role ignores the value. Keep empty until the receiver merges; no older-pair backfill for the initial rollout. |
| `RELEASE_REPORT_OWNER` (new variable) | Hub repository | GitHub login to assign/mention on release and failure issues. |
| `RELEASE_READY_MAX_AGE_MINUTES` (optional variable; 60) | Hub repository | Missing readiness prerequisite alert threshold. |
| `RELEASE_CONSUMER_MAX_AGE_MINUTES` (optional variable; 180) | Hub repository | Non-app missing/stalled completion threshold. |
| `RELEASE_APPLICATION_MAX_AGE_MINUTES` (optional variable; 1440) | Hub repository | App stage freshness, separate from per-test time limits. |
| `FACTS_LICENSE_DISPATCH_ENABLED` (new variable) | Hub repository | Set true after the license facts receiver and receipt policy are ready; also selects license receipt polling without product dispatch. Existing repository `FACTS_WEB_DISPATCH_ENABLED` independently selects web facts dispatch/polling. |
| Actions permissions, Issues, GHCR visibility | Hub repository | Permit workflow contents/packages/issues writes; enable Issues and public pulls of the existing GHCR package. |
| `release-automation` environment (existing) | Hub repository | Permit protected main and release `v*` tags and the intended automatic jobs. Required human reviewers would pause full automation. |
| Release asset setting | Hub repository | These releases keep append-only audit/receipt assets after publication; GitHub's repository-wide immutable-release setting must permit these appends. The scripts enforce manifest/fixed-tag byte immutability and never overwrite/delete them. |
| Receivers and callback credentials | Each enabled receipt consumer | Receiver on default branch; own CI + E2E aggregate required; scoped hub Contents-write callback token; artifact upload/read access. Delivery-only compute instead requires its pair-validation receiver, with no consumer callback or receipt artifact polling. |

The `finalize` job exports `compute_configuration_error` from the `ready` step
alongside its existing `deliveries` output. For a malformed compute floor alone,
the script exits 0, so `set -euo pipefail` reaches the unchanged delivery-output
export and `notify-consumers` sends the other roles' planned deliveries. That
job's condition remains `needs.finalize.outputs.deliveries != '[]'`.
The final `report-compute-configuration-error` job has
`needs: [finalize, notify-consumers]` and
`if: ${{ always() && needs.finalize.outputs.compute_configuration_error != '' }}`.
After those jobs finish or skip, it prints exactly one fixed
`::error::RELEASE_COMPUTE_FIRST_PAIR value was not accepted; compute is off for this run`
line and exits 1, making the run fail visibly. It uses `permissions: {}` and a
single shell step, with no checkout, environment secrets or token. An empty
configuration output skips this job. A separate row or facts failure retains
its existing failure path; the configuration output is already written before
either can raise, so the final job still reports the configuration problem.
Outside Actions, the same `compute_configuration_error` key and fixed text are
printed as a `key=value` line on stdout when `GITHUB_OUTPUT` is unavailable.

Policy example: the `offline` entry remains a synthetic structure, not a
production inventory. The `hosted` entry is the exact policy from hosted
`receipt.mjs` at `0a1c82c8`, following the 2026-10-21 post-merge change.
Merge that hosted entry into the existing
`RELEASE_RECEIPT_POLICY` only after installing this hub adapter, preserving every
other consumer entry. The supervisor applies the live policy; this change does
not alter GitHub settings.

The `application` entry uses the supplied base `dev`, literal artifact name,
eight CI job names (including exact matrix expansions), and post-merge gate.
Its reporting `workflow`, CI workflow filename, and entire `e2e` and `deploy`
sections below are **example values**, marked `example-*`; APP must provide
their real values. When CI expands to 13 jobs, APP must supply all 13 exact job
names. Do not use the example E2E/deploy inventories as production gates.

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
      "expected": ["authz", "validate", "checks", "container"],
      "approved_skips": []
    },
    "e2e": {
      "workflow": "release-postmerge.yml",
      "expected": ["authz", "build", "verified"],
      "approved_skips": []
    },
    "deploy": {
      "workflow": "release.yml",
      "expected": ["decide", "deploy"],
      "approved_skips": []
    }
  },
  "application": {
    "workflow": "example-application-report.yml",
    "artifact": "release-receipt-application",
    "base": "dev",
    "ci": {
      "workflow": "example-application-ci.yml",
      "expected": [
        "check",
        "image-build-contexts",
        "studio-responsive (1)",
        "studio-responsive (2)",
        "studio-responsive (3)",
        "studio-paths",
        "studio-perf-build",
        "studio-perf"
      ],
      "approved_skips": []
    },
    "e2e": {
      "workflow": "example-application-e2e.yml",
      "expected": ["example-candidate-e2e"],
      "approved_skips": []
    },
    "deploy": {
      "workflow": "example-application-deploy.yml",
      "expected": ["example-served-identity", "example-rollback-identity"],
      "approved_skips": []
    },
    "post_merge": {
      "workflow": "ci-post-merge.yml",
      "expected": ["test", "int"],
      "approved_skips": []
    }
  }
}
```

Supervisor must populate the **actual complete expected job names**, including
matrix expansions, from each reviewed consumer lane. A tiny example inventory
must never be used to omit existing required checks.

Rollout order for the application `post_merge` section: a receipt must carry
`post_merge` exactly when the policy has it, and `collect` re-checks every
retained application reporting artifact that the hub has not already stored.
Put the policy with `post_merge` in place together with the application
emitter change. While an artifact of the old shape is retained and not yet
stored, each sweep reports and skips it (collector skip rules below) until it
expires. Changing any inventory has the same effect.

**Hosted rollout order.**
`collect` re-validates every retained `release-receipt-hosted` artifact in ascending
run-id order, except an artifact the hub already stored (collector skip rules below).
After the `hosted` policy swap, a retained coordinator-shape `verified` artifact that
the hub has NOT already stored is refused by validation ("required check inventory
differs from owner policy"); in a sweep (the scheduled sweep and a manual `--run-id 0`
sweep) it is reported with a `::warning::` line and one incident issue per role
(`Release chain: receipt artifacts skipped (hosted)`) and skipped, and the sweep
continues with the remaining artifacts. One that the hub already stored with the
same `(attempt, sequence)`, canonical-evidence digest and reporting run ID is
not validated again, including when a newer receipt is also stored. A collection
triggered by a callback names one run id, checks only that run and still fails
closed, so a new post-merge receipt verifies through its callback either way.
Removing the retained coordinator-shape artifacts from the hosted repository before
the swap is no longer required; the stored public form is recorded on the hub
release as `consumer-hosted-<run>-<sequence>.json`.

**Collector skip rules.** In a sweep, a bad or old-shape artifact is reported
with a `::warning::` line and one stable incident issue per role, then skipped.
This includes encrypted ZIP entries, unsupported compression and corrupt
compressed data, JSON `null` and other non-object receipt documents. The sweep
omits validation of a product artifact only when the hub retains its own
`(attempt, sequence)`, canonical-evidence digest **and reporting run ID** for
the same role and release. A greater stored key alone does not establish that
an earlier artifact was validated. An unmatched older artifact is validated
and reported; even otherwise-valid older evidence is reported as having no
matching retained evidence, without rewriting the newer receipt. Equal evidence
from a different reporting run is validated and refused as receipt identity
reuse, including when a newer receipt is also stored. Facts-only artifacts are
still validated. A callback names one run and still fails closed. Non-404 HTTP
errors (including authentication, rate limits and server failures), transport
errors from the real client and `OSError` stay fatal. `Pending` (including a
404, unknown release or unavailable artifact) also aborts the sweep before any
later receipt can advance against the missing earlier state. On retry the
earlier receipt is processed first, so terminal-state and state-regression
checks still apply to the later receipt. The incident issue lists at most 50 runs
and a count of any additional skipped runs, reasons are reduced to a fixed safe
character set, and the issue is not closed automatically when the artifacts
expire.

After processing all candidates and reporting skips, the sweep checks the
required product targets for its role. A published, manifest-backed release
requires a stored receipt when the role is in `RELEASE_READY_CONSUMERS` or the
release has an accepted `delivery-<role>.json` audit. The audit preserves this
requirement when only the workflow planner received the product configuration.
Draft and rejected releases, releases superseded by a newer stable release,
and the aggregate's prerelease policy skips are exempt. A missing required
receipt fails the collector, including when no artifact was discovered or
every candidate was skipped. A valid receipt of an intermediate state
satisfies collection; aggregate completion still requires its terminal
completion states. Facts-only polling creates no product delivery requirement
and retains its separate canonical-history and freshness checks.

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
