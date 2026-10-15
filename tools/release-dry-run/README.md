# Release dry run

## Current candidate/readiness replay (2026-10-16)

The default entry point now runs the candidate → finalizer → completion graph:

```bash
# Run in CI or on the approved cloud host, never on the Mac.
python3 tools/release-dry-run/dry_run.py --ready -v
python3 tools/release-dry-run/dry_run.py --ready -k ArrivalReplay
```

It executes the current handler's real validation/probe/decision shell for all
six stable and prerelease component arrival orders, and the production package,
manifest, finalizer and receipt validators against nonempty external-service
fixtures. Unknown fixture routes fail. It covers missing npm/sdist/facts,
corruption, auth errors, duplicate/lost delivery, older completion, rollback,
missing/failed/skipped E2E jobs and a stale facts-file clock. Mutation controls
must detect removal of the runtime gate, early latest tags and bypassed finalizer
dependencies. The CI aggregate `release/ready` requires this replay as well as
the existing release/source/harness and fingerprint jobs.

No token is minted, no registry is written and no real HTTP is allowed. This
replay does not claim a real image passed E2E: `publish.yml` separately runs
`release_smoke.py` on both pushed child digests in CI before storing a verified
candidate. It also does not claim live App permission, branch protection or
consumer deployment proof.

The pre-binding runner and worlds below are preserved for historical audit.
They describe the old version-only sender, not current acceptance criteria.
Use `--historical --checkout <pre-binding-checkout> --metadata-action <prepared>`
to replay those workflow snapshots. Current replay needs Python, PyYAML, the
pinned facts validator dependencies, bash, git, jq and the existing local shims;
it does not need a metadata-action checkout. `-k` selects current test-name text.

See [release-chain.md](../../docs/release-chain.md) for setup and receipt protocol.

## Historical workflow replay

`dry_run.py` replays this repo's real `handle-upstream.yml` and `publish.yml`
on a Linux test host and checks what each run would tag, build, release and send
downstream. It covers the 0.x line, the 1.0 pair line, and the three-component
`triple` world: all six engine/codegen/hpo arrival orders at `1.6.0-rc.1` and `1.6.0`, plus
the cutoff, refusals, mismatches and recoveries. The cutoff is defined only by
`HPO_PAIRING_FROM` in `scripts/release_pair.py`, compared by semver precedence.
The first rc and every later version require hpo; earlier alpha/beta versions
keep two-component pairing. Hpo events accept canonical semver or PEP 440,
while its release tags and wheel assets use PEP 440 (`v1.6.0rc1` and
`pineforge_hpo-1.6.0rc1-py3-none-any.whl`). The annotated hub tag retains
`hpo=1.6.0-rc.1`; the image's `HPO_VERSION` is `1.6.0rc1`. Fixtures reuse
`hpo_version()` and the existing PEP 440 ordering helper in `release_pair.py`.
The triple replay also checks a pending rc wheel and a newer rc mismatch using
the real release-list probe, including prereleases, numeric rc ordering and draft exclusion.

Workflow execution is dummy-only: no production credential or real App token
is read, minted or needed. It sends nothing to a real service. Dependencies
and a checkout of the workflow's metadata-action version must already be
prepared on the signed Spot host; the replay never downloads either. Workflow
third-party interactions are replayed locally:

- git pushes go to a local bare origin. It is seeded from a checkout of this
  repo (its working tree, uncommitted edits included) on a synthetic history
  whose newest tag is `v0.1.25` (engine 0.13.1 + codegen 0.10.4).
- `curl`, `gh`, `docker` and `sleep` are shims (`shims/`). They answer from the
  world's state directory: which engine tarballs, PyPI versions and hpo GitHub
  Release wheels exist, which images the registry has, and this repo's GitHub
  releases. Their asset metadata also supplies an optional SHA256 digest. Every call is logged
  to `state/actions.jsonl` as what WOULD have happened. A call the shims do not
  model fails instead of pretending to work. Only a correctly spelled artifact
  inventory route can answer 404 for a version not published yet; unknown URLs,
  methods or options fail with 000, including a misspelled hpo asset request.
- `uses:` steps are stubbed (checkout = the prepared work tree, App token =
  a dummy, build-push = logged). `docker/metadata-action` runs for real under
  `node`, so the image tags checked are the action's own.
- Only a short allowlist of your environment reaches a step. git in a step can
  only use local paths and ignores your git config. Job scratch directories,
  temporary files and action caches stay inside the world's state directory.
  Python bytecode caching is disabled so replay does not write to the source
  checkout or installed packages. All synced engine harness files are explicit
  fixtures, read from the existing `scripts/sync-harness.sh` manifest.
  New release/tag fixtures write objects and refs directly in the temporary
  bare repository, including release commits whose tag has not landed yet.
- The real `consumer` matrix and its `repository_secret` includes are checked
  and passed unchanged: `offline`, `hosted`, and `application`. The public
  offline repository remains `pineforge-backtest-mcp`. Dummy configuration is
  always `RELEASE_HOSTED_MCP_REPOSITORY=release-fixture-hosted` and
  `RELEASE_APPLICATION_REPOSITORY=release-fixture-application`; caller environment
  values never supply these names or credentials. Private deployment target
  identities are not fixture data. Each token action is recorded as a dummy
  scope, and `gh api --hostname github.com` is answered by the local shim.
- Independent consumer execution requires the real workflow's explicit
  `strategy.fail-fast: false`. Removing it or setting it to `true` fails before
  any consumer token action or dispatch is replayed.
- The dispatch shim constructs the nested JSON body from bracketed object
  fields. `-F`/`--field` converts booleans, signed 64-bit integers and `null`;
  `-f`/`--raw-field` preserves strings. The transcript records both flag kinds
  and the resulting body. Unmodeled arrays, file inputs and repository
  placeholders, as well as duplicate/conflicting paths, fail visibly.
- Unknown expression syntax, actions, API hosts and unmodeled shim calls fail
  visibly. Failed or skipped consumer jobs cannot make a publish run green.

## Run

Requirements: bash 4.4 or later first on `PATH` (GitHub's runners have bash 5;
macOS's `/bin/bash` 3.2 is refused), git 2.32 or later, jq, node 20 or later,
and Python 3.9 or later with PyYAML.

For this campaign, every replay and regression check runs on an AWS Spot EC2
host, never on the Mac or an on-demand instance. Use a lane-owned output and
cache directory; keep build/test transcripts internal. The commands below are
run on that host, with no production credentials copied to it:

```bash
python3 tools/release-dry-run/dry_run.py --metadata-action /prepared/docker-metadata-action
python3 tools/release-dry-run/dry_run.py -k triple --metadata-action /prepared/docker-metadata-action
python3 -m unittest discover -s tools/release-dry-run -p 'test_wfrun.py' -v
python3 -m unittest discover -s tests -p 'test_release_*.py' -v
```

`--metadata-action DIR` is required: supply the prepared `docker/metadata-action`
checkout at the version `publish.yml` uses. It must contain `dist/index.js` and
its dependencies. The replay reads that checkout and uses only its local
GitHub API stand-in; it never auto-clones or writes a home-directory cache.

Each check prints `PASS` or `FAIL`. The exit status is 0 only when every check
passes. Transcripts and state go to a new temporary directory (`--out DIR` to
choose a new or empty one). `-k WORLD` runs one world and `-v` prints every run
as it happens.

Record the source commit/tree, host/runtime versions and the action checkout's
exact `git rev-parse HEAD` with the exits and transcripts. This is release
workflow replay evidence, not proof of live App authorization, registry pushes,
GitHub deployment or Pages publication.

## Worlds

| world | what it proves |
|---|---|
| `pair` | Engine rc.1 first waits; codegen rc.1 completes the pair (tag `v1.0.0-rc.1`, pins in the tag message). The rc image gets only its fixed tags (no `latest`, no `1.0`), a GitHub prerelease with `--latest=false`, and `prerelease=true` to the offline and hosted consumers only: the application leg stays green, mints no App token, sends no dispatch and logs that it receives a prerelease by hand-off. A duplicate event is a no-op. 1.0.0 then pairs the same way, takes `1.0`, `latest` and GitHub Latest, even with a draft `v9.9.9` present, and goes to all three consumers. Refused: a late rc, a 0.x event after 1.0, a contradicting prerelease flag, a mismatched pair (named), and a probe answered 503. |
| `reverse` | Codegen first, engine second, for rc.1 and then 1.0.0: the same pairs, real metadata-action tag assertions and the scoped fanout payloads (offline and hosted for rc.1, all three for 1.0.0), including `run_id`. |
| `lost-partner` | The partner's dispatch never arrives. Re-running the waiting run (same event) completes the pair. |
| `retag` | The release commit reached `main` but its tag push failed. The next event tags that commit. |
| `legacy` | 0.x as before: a patch bump, `latest` and GitHub Latest. A failing `gh release list` fails the step instead of creating the release. A hand-made tag with a mismatched 1.0 pair fails before any build. |
| `triple` | Twelve independent histories from 1.5.9 cover every engine/codegen/hpo arrival order for 1.6.0-rc.1 and 1.6.0. The first two events wait and name every missing partner, using PEP 440 for hpo, without moving main, tagging, building or dispatching; the third records three adjacent semver pins. Publish checks the triple, selects the explicit `hpo` target and supplies the PEP 440 `HPO_VERSION` and available `HPO_WHEEL_SHA256`. The rc image keeps fixed tags only, a GitHub prerelease with Latest disabled and prerelease fanout; stable keeps the existing moving tags and fanout. Landed v1.5.0, v1.5.9 and earlier alpha/beta tags without `hpo=` still publish with the old build arguments and no target. Hpo events below the cutoff and malformed payloads are refused. Hpo 1.6.1 with landed engine/codegen 1.6.0 waits for both; different new partner versions fail naming all odd versions. Uncertain HTTP statuses and an unmodeled hpo URL fail; missing, invalid, PEP 440 or mismatched hpo tag pins fail before any build/release/dispatch for both rc and stable. Duplicate triples are no-ops, and half-done triples tag the existing release commit. |

The pair world also runs the real consumer job, for the rc.1 prerelease and for
1.0.0, with missing, empty, owner/path and multiline target configuration for
each private role. For a stable release only that consumer must fail before
token creation or dispatch; the other two still finish. For a prerelease the
hosted role behaves the same, while the application role is withheld: its leg
stays green with no token or dispatch whatever its configuration, because a
prerelease reaches the application by hand-off instead. Every successful
fanout checks exactly its expected destinations (offline and hosted for a
prerelease, all three for a stable release), token scopes, hostname, event type
and the complete nested `client_payload` object: `release_version` is a string,
`prerelease` is a JSON boolean and `run_id` is a JSON integer. All three
consumer legs always run green, and the withheld application leg must log the
hand-off line once.

## Checking the checks

The full run includes negative controls, for both the prerelease and the stable
fanout: successful but wrong target, `release_version`, `prerelease` (the
opposite of the real flag), or `run_id` dispatches must fail the fanout oracle.
Skipped-job and skipped-dispatch controls modify only synthetic checkouts of
the real workflow, not the source checkout, and must also be detected; the
skipped-dispatch control turns the dispatch step's own existing condition into
`if: false` (never a second `if:` key), and an edit that finds no target or
several is a harness error. A prerelease control also breaks the decision step
in a synthetic checkout: only its first condition changes and every guard
stays, so the application leg dispatches and mints its token on an all-green
run. The unmodified oracle must reject that run, and the control checks that
the run really reached the application dispatch. All existing worlds and
failure/recovery assertions remain alongside `triple`. Controls also remove/change
`fail-fast: false` and switch each typed `prerelease`/`run_id` field from `-F`
to `-f`: successful dispatches carrying the same text with the wrong JSON type
must fail the oracle.

To see a check catch a defect, break a workflow in a scratch clone and point
the dry run at it:

```bash
git clone -q . /tmp/mutant && sed -i.bak 's/ --exclude-drafts//' /tmp/mutant/.github/workflows/publish.yml
python3 tools/release-dry-run/dry_run.py --checkout /tmp/mutant --metadata-action /prepared/docker-metadata-action   # FAIL: "... GitHub Latest although a draft v9.9.9 exists"
```

`wfrun.py` is the generic runner underneath. It runs one job of any workflow
file (`python3 tools/release-dry-run/wfrun.py --help`).
