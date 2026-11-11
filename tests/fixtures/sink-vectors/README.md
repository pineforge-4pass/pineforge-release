These frozen files come from `pineforge-engine`'s
`tests/data/sink_vectors_v1/`, read at the same revision as the runtime harness
sync. The upstream bytes, including the trailing newlines, are preserved.

- `valid.pfsink` and `valid.expected.to-json.json` are the committed all-record
  vector and report oracle. The image test compares stdout directly with the
  saved oracle bytes.
- `native.pfsink`, `native.expected.parse.json` and `native.provenance.json`
  preserve a real C++ writer capture made by `docker/capture_native_sink_vector.py`.
  The capture includes five equity rows and one trade. Its original header
  says engine `1.7.0`; it remains unchanged as an input compatibility vector.
- `MANIFEST.json` is the complete upstream manifest, unchanged even though only
  the five files above are needed here. Its `engine_sha` records the original
  vector generation revision, which predates the harness sync. The fixture
  check verifies the copied files against its byte counts and SHA-256 digests.
- `stdout.json` is an empty metadata object supplied by this repository, matching
  the upstream converter-vector test's input.

Upstream provides no `native.expected.to-json.json`. The native image test
therefore checks converted equity and trade rows against the frozen parse
expectation, using the public trade fields from the valid report oracle.
The valid image test supplies the full byte-for-byte report comparison,
including float spelling, escaping, field order and the final newline.

Run only on a build host with an already built image:

```bash
PINEFORGE_SINK_TEST_IMAGE=<image> \
  python3 -m unittest discover -s tests -p 'test_release_sink_image.py' -v
```

Set `PINEFORGE_SINK_TEST_PLATFORM=linux/amd64` or `linux/arm64` to select a
platform explicitly. Without `PINEFORGE_SINK_TEST_IMAGE`, discovery checks
fixture integrity and skips image execution. The image test mounts only these
data files read-only, disables network access, and runs the installed Python
module. It never imports a host copy of the converter or regenerates an oracle.
