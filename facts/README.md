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

Maintainers export to a checkout, open a facts-only PR here, then refresh pinned
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

At **every promotion**, without delayed batching:

1. Immediately export canonical facts using `lab facts export`, following the
   existing exporter/pinned-inventory process. A pipeline never creates a model.
2. Render repository, README, website, and hosted-consumer outputs from that same
   explicit snapshot; refresh pinned consumer copies and run `lab facts render`
   and `lab facts check` for marked documentation.
3. Render and inspect exact descriptions, source tokens, license sources, hashes,
   and the proposed command text:

   ```sh
   python3 scripts/repo_descriptions.py render --facts facts/facts.json > descriptions.json
   python3 scripts/repo_descriptions.py render --facts facts/facts.json --format commands > apply-commands.txt
   ```

4. TOP alone reviews and applies applicable commands. The renderer does not
   execute its output; there is no apply mode. Held rows emit no command.
5. Prove exact live agreement immediately after application:

   ```sh
   python3 scripts/repo_descriptions.py check --facts facts/facts.json --live
   ```

Use `--policy FILE` for a reviewed alternate policy. `check --gh PATH` accepts an
operator-supplied executable without embedding workstation paths in source.
Checks issue one read-only GitHub API GET per public row with a sixty-second
timeout, no shell, and exact public identity verification. JSON output includes
expected/actual deltas; null is distinct from empty text. Exit codes are zero for
all matches, one for drift (including held text), and two for input, API, auth, or
timeout errors. Failed reads never count as matches. Descriptions must be
nonempty, at most 350 Unicode characters, and free of line separators/control
characters; malformed JSON, duplicate keys, unknown fields/tokens, invalid
numbers, and unresolved templates fail closed. Facts are validated against the
unchanged public schema with the supported standard-library validator.

The read-only `repository-description-drift` workflow runs on relevant pushes to
main and manual dispatch, not pull requests. It fails on drift/errors and uploads
exact proposals, review-only command text, and live deltas even on failure. It
has no write permission or automatic application. Offline CLI acceptance uses a
fake third-party executable, not live GitHub; run it in the approved test
environment with `python3 -m unittest discover -s tests -p 'test_repo_descriptions.py' -v`.
