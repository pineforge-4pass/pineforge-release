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

This slice validates and reconciles descriptions only. Export and promotion keep
using the existing reviewed process. Website/MCP consumers, hosted cards, README
switches, dispatch fanout, and the readable-text branch are later stages. The
card renderer here emits bytes for validation; it deploys nothing.

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
role identities, license source URLs, length, and HPO's hold. Invalid inputs
produce no stdout and no consumer calls. All source and policy bytes validate
before prepare emits files or minting becomes eligible.

`facts-validate.yml` runs on PRs and main pushes touching `facts/**` or affected
scripts, tests or workflows. Its token has only `contents: read`; PR code never
receives App credentials. It renders full/corpus SVG in light/dark themes and
endpoint JSON, compares deterministic bytes, and runs subprocess acceptance that
parses the actual SVG and JSON and checks their facts. `python-test.yml` also
runs the existing description renderer acceptance regressions.

`facts-descriptions.yml` replaces the retired duplicate drift workflow. A main
push touching its path filters wakes it even when the commit includes other
files. Manual dispatch defaults to `dry_run=true`; a schedule reconciles every
half hour. The event commit is a wake-up, not the source pin: prepare resolves
current main, fetches facts and policy by that immutable commit, verifies Git
blob integrity, validates everything, and records SHA256. Reconcile validates
that snapshot again and confirms the immutable bytes before comparisons.

Only the three managed public roles (engine, codegen, corpus) can be token targets
or writes. Engine runs first; HPO is held and static rows are audit-only. Every
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
live tokens to verify it. Live environment/App proof remains pending.

1. Protect main. Configure environment `descriptions` with selected deployment
   **branch `main` only**, no tag patterns or other branches. Use the new
   descriptions App, selected public managed repositories only, with
   Administration: write and the required Metadata: read. Do not grant Contents
   write or install it on held/static rows. Keep the exact environment secrets
   `DESCRIPTIONS_APP_ID` and `DESCRIPTIONS_APP_PRIVATE_KEY`.
2. Leave environment variable `FACTS_DESCRIPTIONS_APPLY_ENABLED` absent (or not
   `true`). PRs have no App access. Dispatch with `dry_run=true` and review the
   immutable source receipt and every delta. This comparison does not mint a
   token. A live App mint/apply proof needs TOP's separate authorization.
3. Configure `release-automation` with selected deployment **branch `main` and
   tag pattern `v*` only**. Copy the existing `PINEFORGE_APP_ID` and
   `PINEFORGE_APP_PRIVATE_KEY` there. Provision
   `RELEASE_HOSTED_MCP_REPOSITORY` and `RELEASE_APPLICATION_REPOSITORY` as
   environment secrets containing the existing consumer repository short names.
   Confirm each neutral matrix label maps to its existing consumer. The offline
   public consumer stays explicit. Missing target setup fails before minting,
   rather than falling back to all repositories. Never put nonpublic identities
   in public YAML, artifacts or logs.
4. Independently verify the protected release workflows and their existing tag,
   paired-release and three-consumer behavior before removing repository-level
   old-App keys. Tokens request Contents: write on only the job's repository;
   no release job requests Administration. TOP/owner must remove Administration
   from the old App itself; scoped workflow inputs do not change the installation.
5. After reviewed dry-run and separately authorized live proof, TOP may set
   `FACTS_DESCRIPTIONS_APPLY_ENABLED=true` in `descriptions`. Writes additionally
   require protected `refs/heads/main`, an authorized push/manual/schedule event,
   and effective `dry_run=false`. With rollout enabled, push/schedule reconcile
   automatically; manual dispatch retains its safe default. Removing the variable
   disables all subsequent application without restoring the retired drift job.

These are TOP operator commands, not steps executed by the implementation lane.
Use the approved GitHub wrapper where one is required:

```sh
GH_HOST=github.com gh workflow run facts-descriptions.yml --repo pineforge-4pass/pineforge-release --ref main -f dry_run=true
# After independent review and authorized live proof only:
GH_HOST=github.com gh variable set FACTS_DESCRIPTIONS_APPLY_ENABLED --repo pineforge-4pass/pineforge-release --env descriptions --body true
GH_HOST=github.com gh workflow run facts-descriptions.yml --repo pineforge-4pass/pineforge-release --ref main -f dry_run=false
# Disable future application:
GH_HOST=github.com gh variable delete FACTS_DESCRIPTIONS_APPLY_ENABLED --repo pineforge-4pass/pineforge-release --env descriptions
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
