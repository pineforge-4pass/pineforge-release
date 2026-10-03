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

TV trade rows and distinct source/slug identifiers are labeled literally. They
are not the historical README's deduplicated closed trades or authored-script
inventory, which the pinned population cannot establish. Website-only metadata,
MCP quotas, endpoints and gallery data stay in the website facts module.

Maintainers export to a checkout, open a facts-only PR here, then refresh pinned
copies in the website and hosted MCP. Their builds use committed copies so an
offline build is deterministic and does not silently change when main advances;
an online drift check compares those copies with this public repo's main file.

READMEs use readable hidden markers. Run `lab facts render --repo PATH --facts
facts/facts.json`, then `lab facts check` with the same file (or a raw URL pinned
to a commit). The default raw URL is this repository's main file. Render only
rewrites marked values; check is read-only. Publish in order: campaign tooling,
this hub, website, READMEs, hosted MCP. Never replace release metrics with main's.

The hub's `publish` workflow triggers only on `v*` tags; `handle-upstream` only
on upstream release dispatches. A facts-only branch or main change triggers
neither. The ordinary test workflow may still run.
