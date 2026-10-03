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
