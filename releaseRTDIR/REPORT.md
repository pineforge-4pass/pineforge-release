# Release lane report

The initial workflow/job inventory and detailed fix notes are in [releaseRT.md](releaseRT.md).

## Fix round 1

Updated all 13 previously migrated `runs-on` expressions across eight workflow files to the exact public fragment from pf-ci-spot `1fe46216757d3f2733272f61320981f9fb215e42`, read from `origin/main:workflows/public.yml`. The only workflow changes are the 13 additions of:

```yaml
      github.actor != 'dependabot[bot]' && github.triggering_actor != 'dependabot[bot]' &&
```

Dependabot as either actor now selects `ubuntu-latest`. Existing job-level labels, all six hosted Dependabot jobs, steps, timeouts, and matrices remain unchanged. All existing files were preserved.

Read-only text comparison found 13 exact fragment matches and 13 exact `PF_CI_LABELS` values; `git diff --check` passed. No builds, tests, workflow executions, repository scripts, dependency installs, or commit hooks were run. Nothing was pushed.

One fix commit: `ci: track pf-ci-spot fragment 1fe46216 (dependabot stays hosted)`. Completion marker: [DONE2](DONE2).
