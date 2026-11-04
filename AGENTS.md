# AGENTS.md — pineforge-release

This file is the harness-neutral twin of CLAUDE.md (same rules, same body). Keep the two in step.

## What this repo is
- The release hub: it composes the `pineforge-engine` static library and the `pineforge-codegen` transpiler (plus `pineforge-hpo` for the HPO image) into one image, `ghcr.io/pineforge-4pass/pineforge-release`: a PineScript v6 `.pine` file in, a deterministic backtest report out (README.md).
- Engine, codegen and hpo are the producers: their release events reach `handle-upstream.yml`, which sets `VERSION` and pushes the tag. No upstream version is pinned in source.
- From 1.0.0 engine `vX.Y.Z` and codegen `X.Y.Z` are one pair, and the release version is the pair version.
- A user with write access dispatches `publish.yml` on `main` with that tag; `release-finalize.yml` then publishes the manifest and image tags and notifies the consumers (docs/release-chain.md).
- It also holds the public facts store (`facts/facts.json`) and the Python rules, validators and tests for all of it.

## Build / run
```sh
python3 -m pip install -r facts/requirements-validation.txt   # pinned test dependencies incl. PyYAML; CI uses Python 3.12
bash scripts/sync-harness.sh --files                          # the engine harness files a sync writes (offline)
bash scripts/sync-harness.sh <engine-version>                 # vendor them into docker/ from the engine tag (network; handle-upstream.yml runs it)
python3 scripts/facts_validate.py --facts facts/facts.json
python3 scripts/repo_descriptions.py render --facts facts/facts.json
python3 scripts/consumer_receipt_example.py                   # print a synthetic consumer receipt (docs/release-chain.md)
python3 tools/release-dry-run/dry_run.py --ready              # offline replay of the release graph on fixtures (needs bash, git, jq, PyYAML)
docker run --rm -v "$PWD/strategy.pine:/in/strategy.pine:ro" -v "$PWD/ohlcv.csv:/in/ohlcv.csv:ro" ghcr.io/pineforge-4pass/pineforge-release > report.json
```
`docker/Dockerfile` requires build args `ENGINE_VERSION`, `CODEGEN_VERSION` and `GUARD_POLICY_SETS_SHA256` (value from `docker/GUARD-POLICY-SETS.pin`); `--target hpo` adds `HPO_VERSION` and the six `HPO_*` args in `docker/hpo-toolchain.args`. `publish.yml` builds it and also passes `RELEASE_VERSION`, `RELEASE_GIT_SHA` and, for hpo, `HPO_WHEEL_SHA256` and `HPO_SDIST_SHA256`.

## Layout
- `docker/` — `Dockerfile`, the sixteen vendored engine harness files with `HARNESS-SHA256SUMS`, `GUARD-POLICY-SETS.pin`, `SOURCE-SETS.tsv` (Debian source-set registry), `hpo-toolchain.args`, the HPO dispatcher, license sums.
- `docs/release-chain.md` — the release-chain contract: candidate and readiness, the `pineforge-release` ready event, consumer receipts, rollback.
- `facts/` — the public facts store (`facts.json`, `facts.schema.json`), `requirements-validation.txt`, `receipt-exclusions.json`, `repo-descriptions.json`; its README.md says how it is validated.
- `scripts/` — the logic the workflows run: `release_pair.py` (version order, pairing), `release_pipeline.py` (candidate, finalize, dispatch, reconcile), `release_receipts.py`, `release_sources.py`, `release_smoke.py`, `facts_*.py`, `sync-harness.sh`.
- `tests/` — `unittest` modules `test_*.py` and `fixtures/`, except `test_fingerprint_canonical.py`, a standalone script (`python3 tests/test_fingerprint_canonical.py`); `facts_other_markets.py` is a fixture helper, not a test.
- `tools/release-dry-run/` — offline replay of the release graph (`dry_run.py`, `wfrun.py`, shims); its README.md has the options.
- `.github/workflows/` — PR gates (`python-test.yml`, `facts-validate.yml`), release automation (`handle-upstream`, `publish`, `release-*`), `facts-*` automation; `.github/dependabot.yml` bumps actions weekly.

## Testing
### a. What CI runs, per trigger
A check's name is the job `name:` when set, else the job key. **REQUIRED** checks: `authz` and `release/ready` (docs/release-chain.md).
**Pull request** (no path filter; `python-test.yml` also runs on `merge_group`):
- `python-test.yml` `authz` — **REQUIRED**; lets only our own events and same-repo PRs from write-access authors or allowlisted bots run; refuses forks and `pull_request_target`.
- `changes` — detects a documentation-only PR; skipped on a draft PR.
- `fingerprint-canonical` — `python3 tests/test_fingerprint_canonical.py` (needs `node`): fingerprint canonicalization of `docker/run_json.py`.
- `release-rules` — `python3 -m unittest discover -s tests -p 'test_release_*.py' -v`, after fetching two published HPO wheels (sha256-checked): release rules, workflows, receipts, Dockerfile, sources.
- `description-renderer` — `python3 -m unittest discover -s tests -p 'test_repo_descriptions.py' -v`.
- `release-ready-replay` — `python3 tools/release-dry-run/dry_run.py --ready`: candidate, finalizer and receipt graph replayed on fixtures, no network.
- `release/ready` (job key `release-ready`) — **REQUIRED**; always runs; needs `authz` and `changes` green and the four suites above all `success` (all `skipped` only for a documentation-only PR). A draft PR leaves it red until ready for review.
- `facts-validate.yml` `authz` (same name) and `validate` — `scripts/facts_validate.py`, `repo_descriptions.py render`, each SVG render repeated and compared byte for byte, the endpoint JSON rendered once per variant (not compared), then `python3 -m unittest discover -s tests -p 'test_facts_*.py' -v`; steps skip on a documentation-only PR.
- Documentation-only PR: every changed file is `.md`/`.rst` (or a `docs/` text, image or pdf) outside `.github/workflows/`, `scripts/`, `tools/`, and not `README.md`, `docs/release-chain.md` or `LICENSE` (those are test inputs). Such a PR skips the four suites and `release/ready` accepts that; a draft PR also skips them but leaves `release/ready` red.

**Push to `main`** (`python-test.yml` also lists `master`): `python-test.yml` runs every job above with no documentation-only skip; `facts-validate.yml` only for `facts/**`, `scripts/repo_descriptions.py`, `scripts/facts_*.py`, `tests/test_repo_descriptions.py`, `tests/test_facts_*.py`, `tests/test_release_workflows.py`, `.github/workflows/**`; the automation rows below with a push trigger run on their own paths.
**Tag push or GitHub release event**: nothing runs. "Pushing a tag alone does not start publication."
**Schedule, `repository_dispatch`, `workflow_call` and manual dispatch** (automation: acts on real tags, images and consumers; not tests, never dispatch one to test):

| Trigger | Workflow: jobs | Does |
|---|---|---|
| `repository_dispatch` `engine-release`, `codegen-release`, `hpo-release` | `handle-upstream.yml`: `bump` | validates the event and the producer's artifacts, applies the pairing rule, bumps `VERSION`, syncs the harness, pushes the tag |
| dispatch on `main`, input `tag` | `publish.yml`: `tag-guard`, `prepare`, `publish`, `notify-consumers`, `failure-report` | guards the tag; reuses a stored verified candidate, else builds both architectures, checks the Debian source mirror, runs `release_smoke.py` on both child digests and stores a draft candidate; calls `release-finalize.yml` |
| `workflow_call`, dispatch (`tag`), push to `main` on `facts/facts.json`, cron `3 * * * *` | `release-finalize.yml`: `finalize`, `release-attempt-cleanup`, `notify-consumers`, `report-compute-configuration-error` | binds packages and measured facts; publishes manifest and tags; sends `pineforge-release` to consumers |
| `repository_dispatch` `pineforge-release-status`, dispatch, cron `1,31 * * * *` | `release-receipts.yml`: `plan`, `verify` | validates consumer receipts |
| dispatch, cron `11 * * * *` | `release-reconcile.yml`: `producers` | replays missed stable producer events |
| push to `main` on `facts/facts.json`, dispatch (`dry_run`), cron `7 3 * * *` | `facts-fanout.yml`: `web`, `license` | validates facts, signals consumers (dry run unless enabled) |
| push to `main` on `facts/**`, `scripts/repo_descriptions.py`, `scripts/facts_*.py`, `tests/test_repo_descriptions.py`, `tests/test_facts_*.py`, `.github/workflows/facts-descriptions.yml`; dispatch (`dry_run`); cron `17 3 * * *` | `facts-descriptions.yml`: `descriptions` | reconciles public repository descriptions (dry run unless enabled) |
| dispatch only | `release-rollback.yml` `restore`; `release-redeliver.yml` `redeliver` | restore aliases of a rejected release; repair one consumer's delivery |

### b. Local-only checks (not run by CI)
- `PINEFORGE_SINK_TEST_IMAGE=<image> python3 -m unittest discover -s tests -p 'test_release_sink_image.py' -v` — sink converter in an already built image (needs docker; `PINEFORGE_SINK_TEST_PLATFORM` picks `linux/amd64` or `linux/arm64`). CI runs only its fixture-integrity class; the image class skips. Run it when you change `docker/pineforge_sink_to_json.py`, `docker/pf_sink.py` or `tests/fixtures/sink-vectors/`.
- `PINEFORGE_POLICY_TEST_IMAGE=<image> python3 -m unittest discover -s tests -p 'test_release_guard_policy.py' -v` — layer 2 (image file, label and pin agree). CI skips it: "a skipped layer 2 is NOT a pass of the PR". Run it when you change `docker/GUARD-POLICY-SETS.pin`, the Dockerfile's guard-policy step or its label.
- `python3 -m unittest discover -s tools/release-dry-run -p 'test_wfrun.py' -v` — regressions of the replay runner (requirements: `tools/release-dry-run/README.md`, "Run"); no workflow runs it. Run it when you change `tools/release-dry-run/`.
- `python3 tools/release-dry-run/wfrun.py --help` — runs one job of any workflow file locally against shims (dummy secrets, nothing sent to a real service; `uses:` publish steps only logged): use it to try a workflow change instead of dispatching the workflow.
- `python3 tools/release-dry-run/dry_run.py --historical --checkout <pre-binding-checkout> --metadata-action <prepared>` — replay of old workflow snapshots, audit only; never part of a normal change.

A local skip is not a pass: `HpoResolutionTest` (`test_release_hpo_image.py`) skips when neither `PINEFORGE_HPO_REJECTED_WHEEL` nor `PINEFORGE_HPO_ACCEPTED_WHEEL` is set (it fails when only one is); CI sets both, so the CI run is the one that counts.

### c. During development
Run only the tests that touch your change, one module at a time, e.g. `python3 -m unittest discover -s tests -p 'test_release_pair.py' -v` (`-k <name>` selects a class or method). The whole suite runs once, on the final candidate: the PR's CI is that run. Before pushing: `python3 -m py_compile <changed .py>` and `git diff --check`.
- `scripts/release_pair.py` → `test_release_pair.py`; `.github/workflows/*.yml`, `scripts/sync-harness.sh` → `test_release_workflows.py` (`uses:` pins: `test_release_action_pins.py`).
- `scripts/release_pipeline.py`, `release_contract.py` → `test_release_binding.py`, `test_release_binding_workflows.py`; redelivery → `test_release_redeliver.py`; `release_fanout_check.py` → `test_release_fanout_check.py`; `release_smoke.py` → `test_release_runtime_smoke.py`.
- `scripts/release_receipts.py` → `test_release_receipts.py`, `test_release_coordinator_receipts.py`, `test_release_hosted_postmerge_receipts.py`, `test_release_receipt_legacy_previous.py`, `test_release_receipt_collect_sweep.py`.
- `scripts/release_sources.py`, `docker/SOURCE-SETS.tsv` → `test_release_sources.py`; `docker/Dockerfile`, `docker/hpo-toolchain.args`, HPO dispatcher → `test_release_hpo_image.py`, `test_release_guard_policy.py`; `docker/run_json.py` → `python3 tests/test_fingerprint_canonical.py`.
- `facts/`, `scripts/facts_*.py` → `test_facts_*.py` and the facts commands above; `scripts/repo_descriptions.py` → `test_repo_descriptions.py`; README.md and docs text → the tests that pin it: `test_release_docs_runtime.py`, `test_release_workflows.py`, `test_release_sources.py`, `test_release_hpo_image.py`, `test_release_guard_policy.py`; the JSON examples in `docs/release-chain.md` are also parsed by `test_release_binding.py`, `test_release_coordinator_receipts.py` and `test_release_hosted_postmerge_receipts.py`.

### d. One target per test
Every test has one specific target. No duplicates: tests with the same target are merged, in a separate reviewed change. Add a test only for a target no existing test covers. Never delete or skip a test whose target is unique.

### e. No extra verification layers
Add nothing beyond the gates above: no home-made kits, receipt packs, second reads or reruns (the re-runs that README.md and `publish.yml` name as recovery steps are not verification). `handle-upstream.yml`, `publish.yml`, `release-*.yml` and the `facts-*` automation run only through their normal trigger or dispatch, never re-dispatched to test.

### f. Pine parity is not tested here
What a Pine script computes is a parity probe, not a CI test; probes live in the parity campaign (public corpus for crypto data, the private set otherwise); this repo's tests are real unit tests and workflow checks with one target each.

## Conventions and safety rules (from this repo's own files)
- Release order (README.md): producer release → `handle-upstream.yml` (lockstep `VERSION` + tag push with the GitHub App) → dispatch of `publish.yml` on `main` with the tag → `release-finalize.yml` → consumers → `release-receipts.yml`. `handle-upstream.yml` writes `VERSION` and the `chore(release): vX.Y.Z — …` commit, then pushes the tag.
- Tags before publish: "After the tag exists on main and `handle-upstream` is green, a user with write access runs publication or replay: `gh workflow run publish.yml --repo pineforge-4pass/pineforge-release --ref main -f tag=<vX.Y.Z or vX.Y.Z-rc.N>`." "Pushing a tag alone does not start publication."
- Never publish from a branch: "Publication admits runs at `refs/heads/main` only." "A missing, malformed, non-commit or side-branch tag fails before tagged checkout or publication."
- Append-only: recovery "never force-pushes or rewrites the tag"; "A release's source assets are never deleted while its image remains on ghcr."; `scripts/release_rollback.py` restores recorded aliases and deletes nothing.
- Secrets: "Credentials are a single org GitHub App (`PINEFORGE_APP_ID` / `PINEFORGE_APP_PRIVATE_KEY`); no personal access tokens." "Private targets are configured in the protected `release-automation` environment, not published in the workflow or this diagram". "Private targets never enter matrix outputs, log messages or public receipts."
- "The guard policy value is read from `docker/GUARD-POLICY-SETS.pin`, computed privately and never re-derived here". "Every harness file in the image must be byte for byte the engine's file at the ref that scripts/sync-harness.sh synced from" (`docker/Dockerfile`): never hand-edit the sixteen vendored files or `docker/HARNESS-SHA256SUMS`.
- `facts/facts.json` is machine-written: "do not hand-edit quantities". "PR code never receives App credentials"; `authz` refuses `pull_request_target`.
- Every `uses:` in `.github` is a lowercase 40-hex commit SHA, a local path without a ref, or a sha256-pinned Docker image (`test_release_action_pins.py`); major bumps of release-chain actions are "reviewed and merged by hand", never auto-merged (`.github/dependabot.yml`). Add no `timeout-minutes` to a job (`python-test.yml` says a checker refuses new ones).
- `README.md`, `docs/release-chain.md` and `LICENSE` are inputs of the release tests, and several tests pin their exact text (the README pair rule is pinned by SHA-256): change that text only together with its test.
