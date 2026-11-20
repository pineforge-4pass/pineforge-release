# Application protected-main reporter profile

This page describes the optional `application_reporter` section of the `application` receipt policy
(`scripts/release_receipts.py`). It replaces one rule, for one role, at one state: an application
`verified` receipt normally needs its reporting run at `pr.merge`. With the profile, the reporting run
is one reviewed commit on protected `main` (a history separate from the development merge), and signed
App records bind it to the release. **Nothing here changes any other role, or the application role
when the profile is absent**: they keep `reporting_run.head_sha == pr.merge`. The AWS proof found
byte-identical verdicts for exactly two recorded license facts-only receipts. No other role was
included in that recorded comparison.

**Step 1 does not activate the profile.** The draft verifies signatures for the expected binder kind,
but the provider run and the signed records do not authenticate the required dispatch inputs for that
run and attempt. Even if the other checks succeed, the application profile refuses the receipt with
`application reporter dispatch inputs are not authenticated for this run`.

## Policy shape

The application policy keeps its existing fields and gains one optional field. All fields are required.

```text
"application_reporter": {
  "commit": "<40-hex reviewed commit on protected main; also the pinned reusable-workflow commit>",
  "workflow_id": <GitHub workflow ID of the top-level reporting workflow, a positive integer>,
  "reusable": "<file name of the reviewed reusable release-report workflow, a .yml name that differs from `workflow`>",
  "actor": "<login of the reporting run's actor AND triggering actor; first change: github-actions[bot]>",
  "coordinator_public_key": "<the coordinator's Ed25519 public key: one 60-character SubjectPublicKeyInfo PEM line>",
  "coordinator_spki_sha256": "<lowercase SHA-256 of that key's SubjectPublicKeyInfo DER>"
}
```

The key is used only when the SHA-256 of its DER equals the pin. A key is never taken from the provider
run, a signed record, a repository variable or a snapshot. The later actor migration to `pineforge-app[bot]`
is a change of the `actor` value only; the event check stays, so a generation `repository_dispatch` run is
still refused.

## Field model: what the pinned producer writes

The records are exactly what the pinned App coordinator (blob `0c96484d`) and its signed state (blob
`260912eb`) write. The signed-record publisher adds `schema`
(`pineforge/dev-observation/v1`), `owner` (the coordinator's run ID) and `owner_sha` (that run's head) to
every record and stores it as a GitHub deployment (environment `dev-release-evidence`, task
`dev-release-observation`, `ref` and `sha` the owner's head).

| `receipt-run` row member | Producer value | Bound by this validator to |
|---|---|---|
| `snapshot_id` | ID of the snapshot deployment | the snapshot deployment read by that ID |
| `request` | request ID | the snapshot's `request` |
| `repository` | target repository | the target repository |
| `workflow`, `dispatch_ref` | reporting workflow and dispatch ref | the run's `path`, `head_branch` (`main`) |
| `workflow_run_id` | run ID from the dispatch response | the run ID: how the row is found |
| `workflow_sha` | coordinator's `GITHUB_SHA` | the run's `head_sha`, which is the policy `commit` |
| `ref` | receipt tag `dev-release/<owner>/receipt-<sequence>-<uuid>` | the snapshot's `ref` |
| `source` | served source | the snapshot's `source` (and through it `pr.merge`) |
| `pair` | published pair | the snapshot's `pair`, by canonical equality only |
| `sequence` | allocated receipt sequence | the snapshot's `sequence` and the receipt's `sequence` |
| `attempt` | literal `1` | the run's `run_attempt` (also `1`) |
| `owner_attempt` | coordinator attempt | the snapshot's, and the coordinator run's `run_attempt` |
| `created_at` | coordinator's signing time in whole seconds | its form only |

The snapshot carries `ref`, `source`, `request`, `payload` (the hub event), `sequence`,
`observation` (its `pr` has `number` and `merge`), `pair`, `owner_attempt` and `created_at`, and may carry
`before`, `after`, `previousGood`, `deployedGood`, `landed`, `postMerge`, `deploy_ref` and `landed_ref`;
nothing else is allowed. The receipt's `attempt` is the snapshot's `owner` (the coordinator's run, as the
receipt contract says) and its `sequence` the snapshot's.

Corrections to the first draft, reconciled against those blobs: `dry_run` and `receipt_only` are **not**
members of any record; `ref` is the receipt tag, **not** `refs/heads/main`; `pair` is **not** the release
version; `created_at` is the coordinator's signing time, **not** the run's creation time; and the row has
no `dispatch_inputs` or `event`. The member names and the order of the checks are in the tests' tables.

## What `verified` requires with the profile (in order; each refusal has its own exact reason)

1. **Policy pin:** exact fields, each well formed; the key's DER hashes to the pin.
2. **Provider run** (no extra API read): event `workflow_dispatch`; path `.github/workflows/<policy.workflow>`
   and workflow ID equal to the pin; `run_attempt == 1`; `head_branch == main`; `head_sha == commit`;
   `actor` and `triggering_actor` both the pinned `actor`; repository and head repository are the target; the
   run was created strictly after the PR merged; exactly one `referenced_workflows` entry with exactly `path`,
   `sha`, `ref`, naming `<repository>/.github/workflows/<reusable>@<commit>`, `sha == commit`,
   `ref == refs/heads/main`.
3. **Protected main:** the `main` branch is protected and `commit` is on its history (`ahead` or `identical`).
   There is no ancestry requirement between the development and `main` histories.
4. **Receipt-run row,** found by `workflow_run_id` among the store's deployments, never by a hash. A record
   naming the run counts only if it is signed by the pinned key as a `receipt-run`; unsigned, malformed,
   wrongly signed and other-kind records (including `f6-key-correspondence-proof`) are ignored when a good row
   exists and otherwise each refuses for its own reason. Authentic duplicates must be the same canonical record.
5. **Row members:** exactly the table above, each of its form; then against the provider run.
6. **Snapshot,** read by the row's `snapshot_id` in the same environment and task, signed as a `snapshot`;
   against this release (version, manifest SHA-256, PR number, `pr.merge`, and its `source` is `pr.merge`:
   the served source never becomes the reporter commit); then the row against it, and the receipt against it.
7. **One snapshot, one run:** any other authentic row for this snapshot is refused.
8. **Coordinator run:** for both records `deployment.sha == owner_sha == the owner run's head_sha`; the owner
   run is read by ID in the same repository; its `run_attempt` equals `owner_attempt`.
9. **The checks that already followed the reporter check** (deployed source and image, deploy and post-merge runs).
10. **Dispatch inputs** (last, always refused until the gap below is closed).

With the profile selected there is no fallback to the legacy rule: missing or invalid evidence is a refusal.
The public receipt is unchanged: it carries no reporter commit, deployment ID or key.

## Step 1 verifier and remaining evidence gap

The verifier in [release-signed-record.md](release-signed-record.md) uses the DER key and digest from
hub policy, canonicalizes the App record, verifies the Ed25519 signature, and checks the specific binder
kind the caller requires. The App profile has no fallback to the legacy rule. The binding tests exercise
the verifier with labelled test keys; the future real-key proof is only a verifier test and cannot bind a run.
Step 1 leaves the collector's shared token permissions unchanged. Do not enable the application profile
until an application-scoped token can read deployments and its installation grant has been verified.

**Dispatch inputs.** `dry_run == "false"` and `receipt_only == "true"` are inputs of the coordinator's
workflow dispatch (coordinator 716-720, sent with `return_run_details`). No signed record keeps them (row 724-728,
snapshot 708-712, readback 731-737); the run API does not return them (the retained manual-dispatch run record
has no `inputs`); `requireReportedSnapshot` (660-674) checks the run, one job and four steps only; and the receipt
contract has no field for them. A value inferred from the workflow name, the event or a job's success would be
an invented claim, so the validator refuses instead.

Minimum additional producer evidence, any one of which would do, bound to this exact `workflow_run_id` and
attempt `1`: (a) the coordinator's signed `receipt-run` row (or a new signed record) carrying the exact `inputs`
object it dispatched (`release_version`, `manifest_sha256`, `dry_run`, `receipt_only`, `promotion_snapshot`,
`receipt_workflow_sha`), so the hub can require `dry_run == "false"`, `receipt_only == "true"`,
`promotion_snapshot == snapshot_id` and the rest against the snapshot; or (b) an authenticated record written by
the pinned reporter itself at run time (a signed or attested artifact of its own `github.event.inputs`); or (c) a
provider run record that carries inputs. The choice, and whether a signed claim is acceptable, is CORE's and the
App's.

## Not confirmed by the pinned inputs

* `pair`'s form (`publishedPair`, an unpinned module) is only compared. The canonical source is separately pinned to blob `6520023f1b6f4ef5af6bb8ffab064a52cd755616` for the vector oracle.
* The consumer receipt builder (unpinned): that the receipt's `attempt` is the snapshot's `owner` and its
  `sequence` the snapshot's rests on the receipt contract in `docs/release-chain.md` and the pinned counters.
* The pinned coordinator refuses candidate promotion until the reporter is enabled, so there is no
  live signed `receipt-run` row to capture from this blob.
* That the minted token can read `branches/main` with its `protected` flag and the deployments listing.
