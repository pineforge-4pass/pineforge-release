# Release dry run

`dry_run.py` replays this repo's real `handle-upstream.yml` and `publish.yml`
on a Linux test host and checks what each run would tag, build, release and send
downstream. It covers the 0.x line and the 1.0 line: `v1.0.0-rc.1`, then
`v1.0.0`, in either arrival order, plus the refusals and recoveries.

Workflow execution is dummy-only: no production credential or real App token
is read, minted or needed. It makes no GitHub write or Pages call. Dependency
installation and the initial public metadata-action checkout need network
access; workflow third-party interactions are replayed locally:

- git pushes go to a local bare origin. It is seeded from a checkout of this
  repo (its working tree, uncommitted edits included) on a synthetic history
  whose newest tag is `v0.1.25` (engine 0.13.1 + codegen 0.10.4).
- `curl`, `gh`, `docker` and `sleep` are shims (`shims/`). They answer from the
  world's state directory: which engine tarballs and PyPI versions exist, which
  images the registry has, and this repo's GitHub releases. Every call is logged
  to `state/actions.jsonl` as what WOULD have happened. A call the shims do not
  model fails instead of pretending to work.
- `uses:` steps are stubbed (checkout = the prepared work tree, App token =
  a dummy, build-push = logged). `docker/metadata-action` runs for real under
  `node`, so the image tags checked are the action's own.
- Only a short allowlist of your environment reaches a step. git in a step can
  only use local paths and ignores your git config.
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
python3 -m pip install pyyaml        # or use a virtualenv
python3 tools/release-dry-run/dry_run.py
python3 -m unittest discover -s tools/release-dry-run -p 'test_wfrun.py' -v
python3 -m unittest discover -s tests -p 'test_release_*.py' -v
```

The first run clones `docker/metadata-action` at the major version
`publish.yml` uses into `~/.cache/pineforge-release-dry-run/`. Pass
`--metadata-action DIR` to use a checkout you already have.

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
the run really reached the application dispatch. The existing five worlds and
failure/recovery assertions remain. Controls also remove/change
`fail-fast: false` and switch each typed `prerelease`/`run_id` field from `-F`
to `-f`: successful dispatches carrying the same text with the wrong JSON type
must fail the oracle.

To see a check catch a defect, break a workflow in a scratch clone and point
the dry run at it:

```bash
git clone -q . /tmp/mutant && sed -i.bak 's/ --exclude-drafts//' /tmp/mutant/.github/workflows/publish.yml
python3 tools/release-dry-run/dry_run.py --checkout /tmp/mutant   # FAIL: "... GitHub Latest although a draft v9.9.9 exists"
```

`wfrun.py` is the generic runner underneath. It runs one job of any workflow
file (`python3 tools/release-dry-run/wfrun.py --help`).
