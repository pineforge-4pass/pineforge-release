# Release dry run

`dry_run.py` replays this repo's real `handle-upstream.yml` and `publish.yml`
on your machine and checks what each run would tag, build, release and send
downstream. It covers the 0.x line and the 1.0 line: `v1.0.0-rc.1`, then
`v1.0.0`, in either arrival order, plus the refusals and recoveries.

Nothing leaves the machine and no token is needed:

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

## Run

Requirements: bash 4.4 or later first on `PATH` (GitHub's runners have bash 5;
macOS's `/bin/bash` 3.2 is refused), git 2.32 or later, jq, node 20 or later,
and Python 3.9 or later with PyYAML.

```bash
python3 -m pip install pyyaml        # or use a virtualenv
python3 tools/release-dry-run/dry_run.py
```

The first run clones `docker/metadata-action` at the major version
`publish.yml` uses into `~/.cache/pineforge-release-dry-run/`. Pass
`--metadata-action DIR` to use a checkout you already have.

Each check prints `PASS` or `FAIL`. The exit status is 0 only when every check
passes. Transcripts and state go to a new temporary directory (`--out DIR` to
choose a new or empty one). `-k WORLD` runs one world and `-v` prints every run
as it happens.

## Worlds

| world | what it proves |
|---|---|
| `pair` | Engine rc.1 first waits; codegen rc.1 completes the pair (tag `v1.0.0-rc.1`, pins in the tag message). The rc image gets only its fixed tags (no `latest`, no `1.0`), a GitHub prerelease with `--latest=false`, and `prerelease=true` to all three consumers. A duplicate event is a no-op. 1.0.0 then pairs the same way and takes `1.0`, `latest` and GitHub Latest, even with a draft `v9.9.9` present. Refused: a late rc, a 0.x event after 1.0, a contradicting prerelease flag, a mismatched pair (named), and a probe answered 503. |
| `reverse` | Codegen first, engine second, for rc.1 and then 1.0.0: the same pairs. |
| `lost-partner` | The partner's dispatch never arrives. Re-running the waiting run (same event) completes the pair. |
| `retag` | The release commit reached `main` but its tag push failed. The next event tags that commit. |
| `legacy` | 0.x as before: a patch bump, `latest` and GitHub Latest. A failing `gh release list` fails the step instead of creating the release. A hand-made tag with a mismatched 1.0 pair fails before any build. |

## Checking the checks

To see a check catch a defect, break a workflow in a scratch clone and point
the dry run at it:

```bash
git clone -q . /tmp/mutant && sed -i.bak 's/ --exclude-drafts//' /tmp/mutant/.github/workflows/publish.yml
python3 tools/release-dry-run/dry_run.py --checkout /tmp/mutant   # FAIL: "... GitHub Latest although a draft v9.9.9 exists"
```

`wfrun.py` is the generic runner underneath. It runs one job of any workflow
file (`python3 tools/release-dry-run/wfrun.py --help`).
