# Rebuilds the repository_dispatch body of the hub's release-pair send from the PUBLIC hub release assets,
# field by field as scripts/release_contract.py::pair_payload() and
# scripts/release_pipeline.py dispatch (event_type + client_payload) do. Inputs: --slurpfile m release-manifest.json,
# --slurpfile c release-candidate.json, --arg msha sha256(manifest bytes), --arg csha sha256(candidate bytes).
($m[0]) as $manifest
| ($c[0]) as $candidate
| ("https://github.com/pineforge-4pass/pineforge-release/releases/download/v" + $manifest.release_version + "/") as $base
| {
    event_type: "release-pair",
    client_payload: {
      pair_id: $manifest.release_version,
      engine_tag: $manifest.versions.engine.tag,
      codegen_tag: ("v" + $manifest.versions.codegen.version),
      hpo_tag: (if $manifest.versions.hpo then $manifest.versions.hpo.tag else null end),
      receipt_url: ($base + "release-manifest.json"),
      receipt_sha256: $msha,
      source_sets: [$candidate.sources],
      build: {
        prerelease: $manifest.prerelease,
        hub_commit: $manifest.hub_commit,
        run_id: $manifest.run_id,
        image: $manifest.image,
        candidate_url: ($base + "release-candidate.json"),
        candidate_sha256: $csha
      }
    }
  }
