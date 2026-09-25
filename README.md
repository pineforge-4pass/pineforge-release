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
separate products. This repo is the single place they are composed. On the 0.x
line they have **independent version lineages**: it pins each one independently
and owns its **own semver**. From 1.0.0 on they **share one version**: the only
supported pair is engine `vX.Y.Z` with codegen `X.Y.Z`, prerelease included, and
the release version is that pair version (see [Pairing](#pairing-and-prereleases)).
Downstream consumers — the MCP servers, the app and end users — depend on this
combined image, never on the bare engine.

No upstream version is pinned in source. CI resolves them at release time and
records the pair in the **release tag message** (`engine=` / `codegen=`, read
back by `publish.yml`) and the image labels below; `docker/Dockerfile` takes them
as build-args with no defaults.

| Version | Where | Meaning |
|---------|-------|---------|
| `ENGINE_VERSION` | resolved by CI; recorded in the release tag message + `io.pineforge.engine.version` label | `pineforge-engine` release whose static-lib tarball (libpineforge.a + headers) is fetched |
| `CODEGEN_VERSION` | resolved by CI; recorded in the release tag message + `io.pineforge.codegen.version` label | `pineforge-codegen` PyPI version baked in |
| `VERSION` | repo root | 0.x: this image's own semver; from 1.0.0 on: the pair version |

The combined image also carries `io.pineforge.engine.version` /
`io.pineforge.codegen.version` labels and the
`PINEFORGE_ENGINE_VERSION` / `PINEFORGE_CODEGEN_VERSION` / `PINEFORGE_RELEASE_VERSION`
env vars so consumers can read exactly what is inside.

## Automated release flow

```
pineforge-codegen-oss release ─(codegen-release)─┐
                                                 ├─► handle-upstream.yml
pineforge-engine release ───────(engine-release)─┘    bump pin → set VERSION
                                                      → tag (App) → publish.yml
                                                          build+push image
                                                          → dispatch (pineforge-release)
                                                            → pineforge-backtest-mcp
                                                            → pineforge-mcp-public
                                                            → pineforge-app
```

- `handle-upstream.yml` — receives `repository_dispatch` from engine / codegen-oss
  (`{version, prerelease, run_id}`), applies the pairing rule, sets `VERSION`,
  commits + tags (idempotent, with half-failed-release recovery and a downgrade
  guard).
- `publish.yml` — on the pushed tag: checks the pair, waits for the upstream
  artifacts to be available, builds + pushes the multi-arch image to GHCR, cuts a
  GitHub Release, then dispatches `pineforge-release`
  (`{release_version, prerelease, run_id}`) to both MCP repos and the app.

The rules live in `scripts/release_pair.py` (unit-tested in `tests/`).
`tools/release-dry-run/` replays both workflows offline through the 0.x and 1.0
lines and checks every tag, image tag, release and dispatch they would make; run
it after changing either workflow.

## Pairing and prereleases

- **0.x** — an upstream event moves its own component's pin; the other stays at
  the last release, and `VERSION` gets a patch bump. The 0.x line is stable-only.
- **From 1.0.0 on** — engine and codegen must be the same `X.Y.Z`, prerelease
  included, and `VERSION` becomes that pair version. Upstreams dispatch after
  their artifacts are published, so in either order the first event of a pair
  waits (its run logs the pending pin and builds nothing) and the partner's
  event finds the first one's artifacts (engine tarballs, codegen on PyPI) and
  completes the pair. It fails naming both versions when the partner has
  released a different new version (the pair can never form), when a tag records
  a mismatched pair, and when a 0.x event arrives after a 1.0 pair has landed.
  A probe that cannot reach GitHub or PyPI fails the run instead of waiting; re-run
  it. If a pair's tag push failed after its release commit reached `main`, the next
  event for that pair tags that commit. If both upstreams released but the hub only
  shows the first event's wait (the partner's dispatch was lost), re-run that
  waiting run: it probes again and completes the pair.
- **Prereleases** (`X.Y.Z-alpha.N`, `-beta.N`, `-rc.N`; PyPI spells the codegen
  `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`) are published as a GitHub prerelease that is
  never Latest, with `prerelease: true` downstream.
- `client_payload.force: true` lets a 0.x event move its component backwards. On
  the 1.0 line a release older than the landed pair is refused even forced: the
  release version is the pair's, so roll back by re-pointing image tags.

Credentials are a single org GitHub App (`PINEFORGE_APP_ID` /
`PINEFORGE_APP_PRIVATE_KEY`); no personal access tokens.

## Image tags

`X.Y.Z` · `X.Y` · `latest` · `engine<E>-codegen<C>` · `sha-<short>`

`latest` and `X.Y` move only for the newest stable release. A prerelease (for
example `1.0.0-rc.1`) gets `X.Y.Z-rc.N` · `engine<E>-codegen<C>` · `sha-<short>`
only.

## License

Apache-2.0.

## Harness / engine ABI

`docker/run_json.py` is the engine's harness, vendored from the pinned
`pineforge-engine` tag (`scripts/sync-harness.sh <engine-version>`; the
upstream-release workflow runs the same sync when it bumps the engine pin).
The Dockerfile refuses to build when the harness `EXPECTED_PF_ABI` differs from
`PF_ABI_VERSION` in the bundled engine headers.
