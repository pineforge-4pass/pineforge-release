# Main-commit proof before publication

Release publication requires the hub's own complete CI and facts validation
for the exact release commit on `main`. A green pull request, merge queue,
producer workflow, other commit, or similarly named check cannot authorize it.
The existing tag ancestry, producer artifacts, runtime smoke, measured facts,
immutable manifest, and consumer policies continue to apply.

`scripts/release_main_proof.py` reads GitHub's public hub repository, workflow,
run, and job identities. It resolves each workflow by its file path and numeric
ID, selects its newest `push` run for `main` at the requested 40-character SHA,
and reads that run's current attempt. Both workflows and every job below must
be `completed` with conclusion `success`:

| Workflow | Complete expected job inventory |
| --- | --- |
| `python-test.yml` | `authz`, `changes`, `fingerprint-canonical`, `release-rules`, `description-renderer`, `release-ready-replay`, `release/ready` |
| `facts-validate.yml` | `authz`, `validate` |

The reader checks the repository and head repository identities, workflow
path, event, branch, SHA, main ancestry, run ID, attempt, and each job's source
identity. It reads paginated results and refuses incomplete or ambiguous
inventories. Missing, red, running, cancelled, skipped, neutral, or malformed
proof refuses publication. An older successful run cannot hide a newer failed
or unfinished run. There is no success override or supplied proof-file input.
The unit fixtures model endpoint responses; production always reads GitHub.
Jobs are fetched through the selected attempt's endpoint; when GitHub includes
the optional per-job attempt field, it must match that endpoint's attempt.

## Publication boundaries

| Route | Required proof |
| --- | --- |
| Fresh upstream release | Push the final VERSION and harness commit to `main`, prove that exact SHA, then create and push its annotated tag. |
| Retag recovery | Prove the existing final release commit before creating its missing tag. |
| Candidate reuse and image build | Re-resolve the existing tag, compare it with the SHA admitted by `tag-guard`, and prove that SHA before reuse or image publication. |
| Every finalizer trigger | Guard each selected candidate's tag and `hub_commit`; prove the facts commit actually read before writing its manifest or publishing aliases and readiness. |
| Replaying an existing manifest | Prove its release commit and its immutable bound facts commit before replaying publication. |
| Normal consumer delivery and redelivery | Prove the published manifest's release and facts commits before minting a consumer token. |
| Facts web and license signals | Prove the commit in the prepared facts payload, with its facts digest bound to that snapshot, before minting or sending. |
| Producer reconciliation | Prove the checked-out main SHA before replaying producer events; the handler separately proves the release commit it creates. |

`finalize` in the new helper calls the existing `release_pipeline.finalize_all`
with guarded storage and source reads. This covers reusable, manual, scheduled,
and facts-push finalization, including an empty-tag candidate sweep. It avoids a
preflight over one candidate list followed by publication from a newly selected
list. Facts may arrive in a later main commit than the release commit, so each
of those exact SHAs requires its own full CI and facts proof. Historical rows
read only to compare versions do not acquire publication authority.

The handler waits up to 1,800 seconds for missing or unfinished runs, without
dispatching or rerunning CI. Failed proof stops immediately. If proof never
becomes complete, the main commit remains available and no release tag is
created. Other routes refuse unavailable proof at their normal wake-up; their
existing recovery triggers remain responsible for subsequent attempts. Old
releases without the required proof receive no exemption.

The workflows grant their hub token `actions: read` for proof reads; consumer
tokens retain their existing scopes. Proof snapshots are cached only within
one operation, never persisted as a reusable authorization. GitHub reads and
publication are separate API operations; existing immutable tags and assets
remain part of the release contract.

## Focused controls

Run these in an authorized test environment, one module at a time:

```sh
python3 -m py_compile scripts/release_main_proof.py tests/test_release_main_proof.py tests/test_release_workflows.py tests/test_release_binding_workflows.py
python3 -m unittest discover -s tests -p 'test_release_main_proof.py' -v
python3 -m unittest discover -s tests -p 'test_release_workflows.py' -v
python3 -m unittest discover -s tests -p 'test_release_binding_workflows.py' -v
python3 -m unittest discover -s tests -p 'test_facts_fanout.py' -v
python3 -m unittest discover -s tests -p 'test_release_redeliver.py' -v
python3 tools/release-dry-run/dry_run.py --ready
```

The new controls cover valid identities and full-green acceptance, each absent
workflow and job, failed or unfinished proof, incorrect SHA/event/repository,
skipped proof, stale attempts, pagination, finalizer publication refusal, and
the owned workflow boundaries. Existing workflow text pins and shell replay
bridges pin the guarded finalizer entrypoint and proof-reader calls. The shell
replays serialize GitHub responses and execute the real reader for tag,
candidate, image, and redelivery boundaries. Their fixture clock expires an
unfinished handler wait without replacing its decision. Removed, softened,
or late guards have negative controls alongside the full-green controls.

Source changes and offline controls do not establish live release readiness.
The normal pull-request CI, review and merge route still applies, followed by
the required CI on the final main release commit and the normal publication
and consumer checks.
