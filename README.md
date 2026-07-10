# pineforge-release

The **full PineScript v6 → deterministic backtest** product image: the
[`pineforge-engine`](https://github.com/pineforge-4pass/pineforge-engine) pure
runtime **plus** the bundled [`pineforge-codegen`](https://pypi.org/project/pineforge-codegen/)
transpiler. Run a `.pine` file in, get trades out — no hosted API, source never
leaves the box.

```
docker pull ghcr.io/pineforge-4pass/pineforge-release:latest
docker run --rm \
  -v "$PWD/strategy.pine:/in/strategy.pine:ro" \
  -v "$PWD/ohlcv.csv:/in/ohlcv.csv:ro" \
  ghcr.io/pineforge-4pass/pineforge-release > report.json
```

## Why this repo exists

The engine (the C++ runtime) and the transpiler (`pineforge-codegen`) are
separate products with **independent version lineages**. This repo is the single
place they are composed: it pins each one independently and owns its **own
semver**. Downstream consumers — the MCP servers and end users — depend on this
combined image, never on the bare engine.

No upstream version is pinned in source. CI resolves them at release time and
records the pair in the **release tag message** (`engine=` / `codegen=`, read
back by `publish.yml`) and the image labels below; `docker/Dockerfile` takes them
as build-args with no defaults.

| Version | Where | Meaning |
|---------|-------|---------|
| `ENGINE_VERSION` | resolved by CI; recorded in the release tag message + `io.pineforge.engine.version` label | `pineforge-engine` release whose static-lib tarball (libpineforge.a + headers) is fetched |
| `CODEGEN_VERSION` | resolved by CI; recorded in the release tag message + `io.pineforge.codegen.version` label | `pineforge-codegen` PyPI version baked in |
| `VERSION` | repo root | this image's own semver |

The combined image also carries `io.pineforge.engine.version` /
`io.pineforge.codegen.version` labels and the
`PINEFORGE_ENGINE_VERSION` / `PINEFORGE_CODEGEN_VERSION` / `PINEFORGE_RELEASE_VERSION`
env vars so consumers can read exactly what is inside.

## Automated release flow

```
pineforge-codegen-oss release ─(codegen-release)─┐
                                                 ├─► handle-upstream.yml
pineforge-engine release ───────(engine-release)─┘    bump pin → patch-bump VERSION
                                                      → tag (App) → publish.yml
                                                          build+push image
                                                          → dispatch (pineforge-release)
                                                            → pineforge-backtest-mcp
                                                            → pineforge-mcp-public
```

- `handle-upstream.yml` — receives `repository_dispatch` from engine / codegen-oss,
  bumps the matching pin, patch-bumps `VERSION`, commits + tags (idempotent, with
  half-failed-release recovery and a downgrade guard).
- `publish.yml` — on the pushed tag: waits for the upstream artifacts to be
  available, builds + pushes the multi-arch image to GHCR, cuts a GitHub Release,
  then dispatches `pineforge-release` to both MCP repos.

Credentials are a single org GitHub App (`PINEFORGE_APP_ID` /
`PINEFORGE_APP_PRIVATE_KEY`); no personal access tokens.

## Image tags

`X.Y.Z` · `X.Y` · `latest` · `engine<E>-codegen<C>` · `<short-sha>`

## License

Apache-2.0.
