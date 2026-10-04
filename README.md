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

- **`strategy.pine`** — a PineScript v6 strategy (`//@version=6`; any other
  version is rejected with exit code 5).
- **`ohlcv.csv`** — a header row `timestamp,open,high,low,close,volume`, then one
  bar per row; `timestamp` is UNIX milliseconds (UTC).
- **stdout** is the JSON report (`summary`, `trades`, `metrics`, `equity_curve`,
  `fingerprint`, ...); progress lines go to stderr, so the redirect above stays clean.

A minimal strategy to try it with (any hourly or daily OHLCV file works):

```pine
//@version=6
strategy("EMA cross", overlay=true)
fast = ta.ema(close, 9)
slow = ta.ema(close, 21)
if ta.crossover(fast, slow)
    strategy.entry("L", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("L")
```

```
jq '.summary' report.json      # total_trades, wins, losses, net_pnl, max_drawdown, ...
```

`latest` is the newest stable release; pin one with `:X.Y.Z` (see
[Image tags](#image-tags)). The image is multi-arch (`linux/amd64`, `linux/arm64`).

### Knobs, modes and exit codes

The entrypoint (`docker/entrypoint.sh`) is configured by mounts and `-e` variables:

| Input | Effect |
|-------|--------|
| `/in/strategy.pine` | PineScript v6 source, transpiled in the container (preferred). |
| `/in/strategy.cpp` | A pre-transpiled translation unit, used when there is no `.pine`. Provide exactly one of the two. |
| `-e PINEFORGE_TRANSPILE_ONLY=1` | Transpile `/in/strategy.pine` and print the C++ on stdout; no compile, no backtest, no OHLCV needed. |
| `-e PINEFORGE_INPUTS='{"Fast Length": "8"}'` | `input.*()` name → value overrides. |
| `-e PINEFORGE_OVERRIDES='{"default_qty_value": "5"}'` | `strategy()` header overrides (`initial_capital`, `commission_value`, `pyramiding`, ...). |
| `-e PINEFORGE_INPUT_TF`, `PINEFORGE_SCRIPT_TF` | Chart and strategy timeframe (`1`, `5`, `15`, `60`, `D`, ...). Unset input timeframe = detected from the bar timestamps; the script timeframe must be coarser than or equal to it. |
| `-e PINEFORGE_BAR_MAGNIFIER`, `PINEFORGE_MAGNIFIER_SAMPLES`, `PINEFORGE_MAGNIFIER_DIST` | Bar magnifier switch (`true`/`false`, default `false`), sub-bar sample count (≥ 2, default 4) and distribution (`uniform`, `cosine`, `triangle`, `endpoints` (default), `front_loaded`, `back_loaded`). |
| `-e PINEFORGE_TRADE_START_MS`, `PINEFORGE_CHART_TZ`, `PINEFORGE_MAGNIFIER_VOLUME_WEIGHTED`, `PINEFORGE_SYMINFO`, `PINEFORGE_BENCH` (+ `_WARMUP`, `_REPEATS`) | Optional run knobs; the comments in `docker/entrypoint.sh` say what each does. |
| `-e PINEFORGE_SYMBOL_FEEDS=/in/symbols/symbols.json` | Other symbols' bars for `request.security` on another symbol, from 1.1.0; see [Other symbols' bars](#other-symbols-bars-pineforge_symbol_feeds). |

| Exit code | Meaning |
|-----------|---------|
| `0` | Success (JSON report on stdout, or C++ in transpile-only mode). |
| `2` | A required input mount is missing. |
| `3` | The generated C++ failed to compile. |
| `4` | The backtest failed; a structured `{"engine":"pineforge","error":"..."}` is printed on stdout (for example when `PINEFORGE_SCRIPT_TF` is finer than `PINEFORGE_INPUT_TF`). |
| `5` | The transpile failed (unsupported construct, syntax error or a script that is not PineScript v6). |

To see what a given image contains, read its labels (or the `PINEFORGE_*`
variables listed under [Why this repo exists](#why-this-repo-exists)):

```
docker inspect --format '{{ index .Config.Labels "io.pineforge.engine.version" }} {{ index .Config.Labels "io.pineforge.codegen.version" }}' \
  ghcr.io/pineforge-4pass/pineforge-release:latest      # e.g. "1.0.0 1.0.0"
```

### Other symbols' bars (`PINEFORGE_SYMBOL_FEEDS`)

A script that calls `request.security` on another symbol (`"BINANCE:ETHUSDT"`,
or an `input.symbol`) reads that symbol's own bars, never the chart's. Without
them, or when the index lacks the requested symbol string or timeframe, the run
stops where the request's value is read: exit `4`, with
`{"engine":"pineforge","error":"request.security(...) at line N: no data is pinned for this request, and its value was read"}`
on stdout. From 1.1.0 the image installs those bars from the JSON index that
`PINEFORGE_SYMBOL_FEEDS` names (the harness's `--symbol-feeds`); the published
images up to 1.0.1 ignore the variable.

```json
{"symbols": {
  "BINANCE:ETHUSDT": {
    "syminfo": {"tickerid": "BINANCE:ETHUSDT", "type": "crypto", "currency": "USDT",
                "mintick": 0.01, "session": "24x7", "timezone": "UTC"},
    "feeds": {"240": "ethusdt-240.csv", "1D": "ethusdt-1D.csv"}}}}
```

```bash
docker run --rm \
  -v $(pwd)/strategy.pine:/in/strategy.pine:ro \
  -v $(pwd)/btcusdt-240.csv:/in/ohlcv.csv:ro \
  -v $(pwd)/symbols:/in/symbols:ro \
  -e PINEFORGE_SYMBOL_FEEDS=/in/symbols/symbols.json \
  ghcr.io/pineforge-4pass/pineforge-release:1.1.0 > report.json
```

- A key is the exact string the script passes at run time, exchange prefix and
  suffix included: `BINANCE:ETHUSDT`, `ETHUSDT` and `BINANCE:ETHUSDT.P` are three
  symbols. For an `input.symbol` it is the input's value (its default, or the
  `PINEFORGE_INPUTS` override).
- `feeds` holds one CSV per timeframe the script requests, in the format of
  `/in/ohlcv.csv`, with paths relative to the index. Timeframes use the engine's
  spelling: whole minutes as a bare integer (`"240"`, not `"4h"`), else
  `<n>D|W|M|S`, a bare `D`/`W`/`M`/`S` meaning `1D`/`1W`/`1M`/`1S`. Nothing is
  aggregated: a `240` feed does not serve a `D` request, and a `1` feed serves
  only a `1` request.
- `syminfo` is the symbol's catalog object: its `type`, `timezone`, `session`,
  `currency` and `mintick` are what `syminfo.*` reads inside the request, where
  `syminfo.tickerid` is the key itself.
- The chart must be its own input (`PINEFORGE_SCRIPT_TF` unset or equal to the
  input timeframe). At most 256 symbols and 256 feeds.
- An index or feed the harness cannot install fails the run before it starts:
  exit `4`, one `{"engine":"pineforge","error":"--symbol-feeds: ..."}` line.
- What was installed is recorded as `applied_runtime.symbol_feeds`, so the
  fingerprint digest differs from a run without it. Unset, the report is what it
  was without the variable.

The engine's [`docker/README.md`](https://github.com/pineforge-4pass/pineforge-engine/blob/main/docker/README.md)
has the full contract: bar close times (`time_close` for a session-bound
symbol), warm-up history, the merge rule and its refusals.

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
env vars so consumers can read exactly what is inside (`PINEFORGE_RELEASE_VERSION`
is the release tag, with its `v`: `v1.0.0`).

## Automated release flow

```
pineforge-codegen-oss release ─(codegen-release)─┐
                                                 ├─► handle-upstream.yml
pineforge-engine release ───────(engine-release)─┘    bump pin → set VERSION
                                                      → tag (App) → publish.yml
                                                          build+push image
                                                          → dispatch (pineforge-release)
                                                            → pineforge-backtest-mcp
                                                            → pineforge-mcp-public (private)
                                                            → pineforge-app (private)
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
it after changing either workflow:

```
python3 -m unittest discover -s tests -p 'test_release_*.py'   # the release rules
python3 tools/release-dry-run/dry_run.py                       # needs bash >= 4.4, jq, node, PyYAML: see its README
```

## Pairing and prereleases

> **Status.** The 1.0 line started on 2026-09-30: prerelease `v1.0.0-rc.1`,
> then `v1.0.0` (engine 1.0.0 + codegen 1.0.0). The 0.x line ended at
> `v0.1.25`. Read the Releases page for the newest pair.

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

The files in this repository (Dockerfile, entrypoint, scripts, workflows, tests)
are Apache-2.0: see [LICENSE](LICENSE). The image bundles software under its own
terms, so that licence does not cover the whole image:

- **`pineforge-engine`** (`libpineforge.a` and headers): Apache-2.0, with Eigen
  under MPL-2.0 — see the engine's
  [`LEGAL.md`](https://github.com/pineforge-4pass/pineforge-engine/blob/main/LEGAL.md).
- **`pineforge-codegen`** (the transpiler): source-available under the PolyForm
  Noncommercial License 1.0.0 with a Personal Trading exception. Companies,
  funds, embedding in a product and hosted or public-facing services need a
  commercial licence — see its
  [`LICENSE`](https://github.com/pineforge-4pass/pineforge-codegen-oss/blob/main/LICENSE)
  and [`LEGAL.md`](https://github.com/pineforge-4pass/pineforge-codegen-oss/blob/main/LEGAL.md).
- **Debian base image, g++, Eigen and Python**: their upstream licences.

The image's `org.opencontainers.image.licenses` label names the licences of the
PineForge components:
`Apache-2.0 AND LicenseRef-PolyForm-Noncommercial-1.0.0-Personal-Trading`,
Apache-2.0 for the engine and this repository's files, and a `LicenseRef` for
the transpiler's LICENSE, which is the PolyForm Noncommercial License 1.0.0 as
modified by its Personal Trading and Commercial Use sections (so not the SPDX
`PolyForm-Noncommercial-1.0.0` text). The base image's packages, Eigen
included, keep the upstream licences listed above. Images up to 1.0.0 carry
`Apache-2.0` in that label.

## Harness / engine ABI

`docker/run_json.py` is the engine's harness, vendored from the pinned
`pineforge-engine` tag (`scripts/sync-harness.sh <engine-version>`; `handle-upstream.yml`
fetches the same file for the engine version it releases).
The Dockerfile refuses to build when the harness `EXPECTED_PF_ABI` differs from
`PF_ABI_VERSION` in the bundled engine headers.
