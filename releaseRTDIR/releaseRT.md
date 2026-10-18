# CR-171 release workflow runner routing

Completed on branch `core/cr171-ci-spot`, starting from `ee31da1342f594cb565c1a86fe604cbef8514757`.

Sources read before editing:

- Pool specification: `~/.local/state/pineforge-super-top/ci-spot-labels-1018.txt`.
- Public fragment: `git -C ~/code/pf-ci-spot show origin/main:workflows/public.yml`, with `origin/main` at `908569dc04e33c7cdf5733dd65507ddebe71b0e3`.
- Live-pool authorization: bulletin #613, L6 10-18 22:34, as supplied in the brief.
- Repository trigger and job definitions, including `.github/dependabot.yml`.

All 10 workflow files and all 20 jobs were reviewed. There are 19 jobs with their own runner and one reusable-workflow call. Thirteen runner jobs in eight files now use the public fragment and its job-level `PF_CI_LABELS`. Six Dependabot PR jobs retain their original hosted configuration. Of the 13 fragment jobs, 12 can use ci-spot under their existing triggers; `handle-upstream.bump` remains hosted through the fragment's event fallback.

The table's **public fragment** means this exact expression and environment value, with only the indentation required to nest it under a job:

```yaml
runs-on: >-
  ${{ github.event_name != 'pull_request_target' &&
      contains(fromJSON('["push","pull_request","workflow_dispatch","schedule","release"]'), github.event_name) &&
      (github.event_name != 'pull_request' ||
       (github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.head.repo.fork == false)) &&
      fromJSON('["self-hosted","pf-ci","pf-ci-x64"]') || 'ubuntu-latest' }}
env:
  PF_CI_LABELS: '["self-hosted","pf-ci","pf-ci-x64"]'
```

| Workflow | Job | Before -> after `runs-on` | Effective routing and hosted exceptions |
| --- | --- | --- | --- |
| `facts-descriptions.yml` | `descriptions` | `ubuntu-latest` -> public fragment | ci-spot for its existing main push, manual, and schedule events. |
| `facts-fanout.yml` | `web` | `ubuntu-latest` -> public fragment | ci-spot for its existing main push, manual, and schedule events. |
| `facts-fanout.yml` | `license` | `ubuntu-latest` -> public fragment | ci-spot for its existing main push, manual, and schedule events. |
| `facts-validate.yml` | `validate` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: weekly Dependabot GitHub Actions PRs modify `.github/workflows/**`, which matches this workflow's PR path filter. The entire job stays unchanged under the Dependabot exception. |
| `handle-upstream.yml` | `bump` | `ubuntu-latest` -> public fragment | Hosted: its only trigger is `repository_dispatch`, which is absent from the fragment's trusted-event list. The fragment and labels are installed, but its existing events resolve to `ubuntu-latest`. |
| `publish.yml` | `prepare` | `ubuntu-latest` -> public fragment | ci-spot for existing tag pushes and manual replay, subject to the unchanged tag condition. |
| `publish.yml` | `publish` | `ubuntu-latest` -> public fragment | ci-spot for existing tag pushes and manual replay, subject to the unchanged candidate condition. Building amd64 and arm64 images does not change this job's x64 runner architecture. |
| `publish.yml` | `notify-consumers` | No `runs-on` -> no `runs-on` (unchanged) | Reusable call to `./.github/workflows/release-finalize.yml`; that workflow's runner jobs are listed below. |
| `publish.yml` | `failure-report` | `ubuntu-latest` -> public fragment | ci-spot for existing tag pushes and manual replay when the unchanged failure condition applies. |
| `python-test.yml` | `fingerprint-canonical` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: runs on Dependabot GitHub Actions PRs. Its push and merge-group behavior is also preserved. |
| `python-test.yml` | `release-rules` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: runs on Dependabot GitHub Actions PRs. Its push and merge-group behavior is also preserved. |
| `python-test.yml` | `description-renderer` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: runs on Dependabot GitHub Actions PRs. Its push and merge-group behavior is also preserved. |
| `python-test.yml` | `release-ready-replay` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: runs on Dependabot GitHub Actions PRs. Its push and merge-group behavior is also preserved. |
| `python-test.yml` | `release-ready` | `ubuntu-latest` -> `ubuntu-latest` (unchanged) | Hosted: this required aggregate gate also runs on Dependabot GitHub Actions PRs. Its existing `always()` condition, dependencies, push, and merge-group behavior are preserved. |
| `release-finalize.yml` | `finalize` | `ubuntu-latest` -> public fragment | ci-spot for its existing manual, main facts push, and schedule events. When called by `publish.yml`, the caller's push/manual event context controls the expression. |
| `release-finalize.yml` | `notify-consumers` | `ubuntu-latest` -> public fragment | Same event routing as `finalize`; the existing consumer/tag matrix is unchanged and all its runner legs are x64 Linux. |
| `release-receipts.yml` | `plan` | `ubuntu-latest` -> public fragment | ci-spot for existing manual and schedule events; hosted for `repository_dispatch` through the fragment fallback. |
| `release-receipts.yml` | `verify` | `ubuntu-latest` -> public fragment | Same event routing as `plan`; the existing consumer matrix is unchanged and all its runner legs are x64 Linux. |
| `release-reconcile.yml` | `producers` | `ubuntu-latest` -> public fragment | ci-spot for its existing manual and schedule events. |
| `release-rollback.yml` | `restore` | `ubuntu-latest` -> public fragment | ci-spot for its existing manual event. |

The repository has no macOS runner jobs, ARM runner jobs, or jobs triggered only by `pull_request_target`. No OS matrix needed modification. The public fragment retains `ubuntu-latest` for fork PRs, `pull_request_target`, `workflow_run`, `repository_dispatch`, `merge_group`, and other events outside its trusted-event list.

Dependabot is configured for grouped weekly `github-actions` updates. Both workflows it can run on those PRs, `python-test.yml` and `facts-validate.yml`, remain byte-for-byte unchanged. No `PF_CI_LABELS` was added to their six hosted jobs. The pool's actor allowlist remains `luisleo526`, `pineforge-app[bot]`, and `github-actions[bot]` for chain triggers only; no actor policy was changed.

Five existing job `env` blocks received the new key, and eight jobs received a new `env` block. Each migrated job has exactly one `PF_CI_LABELS` key with the fragment's string value. The workflow-level `REGISTRY` and `IMAGE` variables in `publish.yml` are preserved. Steps, timeouts, trigger definitions, permissions, concurrency, conditions, and matrices are unchanged. Every existing file was kept; no files were deleted.

One local commit was created for each changed workflow file group:

| Commit | Workflow file group |
| --- | --- |
| `055fa71` | `.github/workflows/facts-descriptions.yml` |
| `5f37833` | `.github/workflows/facts-fanout.yml` |
| `ebf7870` | `.github/workflows/handle-upstream.yml` |
| `88850cb` | `.github/workflows/publish.yml` |
| `78e61f0` | `.github/workflows/release-finalize.yml` |
| `0547521` | `.github/workflows/release-receipts.yml` |
| `e25c91b` | `.github/workflows/release-reconcile.yml` |
| `ce18c4b` | `.github/workflows/release-rollback.yml` |

The report and `DONE` marker form a separate documentation commit.

Verification was limited to reading files, comparing text, and inspecting Git diffs and history. The 13 expressions were compared against the source fragment after removing the four spaces required by job nesting; all matched. All 13 `PF_CI_LABELS` values matched, no duplicate job `env` blocks were found, and six original `ubuntu-latest` jobs remained. Git comparison confirmed that both Dependabot workflows and `.github/dependabot.yml` are unchanged. `git diff --check ee31da1342f594cb565c1a86fe604cbef8514757 HEAD` passed for the workflow commits. Each workflow commit contains exactly one workflow file.

No builds, tests, workflow executions, repository scripts, dependency installs, or commit hooks were run. Nothing was pushed.
