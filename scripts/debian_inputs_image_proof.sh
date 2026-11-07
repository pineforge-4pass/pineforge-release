#!/usr/bin/env bash
# Controller only: run against the separately checked out frozen product.
# Keep all diagnostic images, containers and the owned builder. No publication.
set -euo pipefail
[[ $# -eq 2 ]]
control="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
product="$(realpath -- "$1")"
out="$(realpath -- "$2")"
[[ -d "$out" && "$product" != "$control" && "$out" != "$product" ]]
[[ "$out" != "$product/"* && ! -e "$out/helper.started" ]]
stage=

begin_stage() {
    [[ -z "$stage" && "$1" =~ ^[a-z][a-z0-9-]*$ ]]
    stage="$1"
    date -u '+%Y-%m-%dT%H:%M:%S.%NZ' > "$out/$stage.started"
}
end_stage() {
    date -u '+%Y-%m-%dT%H:%M:%S.%NZ' > "$out/$stage.ended"
    printf '0\n' > "$out/$stage.exit"
    stage=
}
finish() {
    local rc=$?
    trap - EXIT
    if [[ -n "$stage" ]]; then
        date -u '+%Y-%m-%dT%H:%M:%S.%NZ' > "$out/$stage.ended"
        printf '%s\n' "$rc" > "$out/$stage.exit"
    fi
    date -u '+%Y-%m-%dT%H:%M:%S.%NZ' > "$out/helper.ended"
    printf '%s\n' "$rc" > "$out/helper.exit"
    exit "$rc"
}
trap finish EXIT
date -u '+%Y-%m-%dT%H:%M:%S.%NZ' > "$out/helper.started"

begin_stage identity
test "${GITHUB_EVENT_NAME:?}" = push
test "${GITHUB_REPOSITORY:?}" = pineforge-4pass/pineforge-release
test "${GITHUB_REF:?}" = refs/heads/diag/debian-inputs-proof
test "${RUNNER_ENVIRONMENT:?}" = github-hosted
test "${RUNNER_OS:?}" = Linux
test "${RUNNER_ARCH:?}" = X64
test "${PF_CONTROLLER_CHECKOUT:?}" = success
test "${PF_PRODUCT_CHECKOUT:?}" = success
[[ "${GITHUB_SHA:?}" =~ ^[0-9a-f]{40}$ ]]
[[ "${GITHUB_RUN_ID:?}" =~ ^[1-9][0-9]{0,19}$ ]]
[[ "${GITHUB_RUN_ATTEMPT:?}" =~ ^[1-9][0-9]{0,8}$ ]]
git -C "$control" rev-parse HEAD > "$out/controller.sha"
git -C "$control" rev-parse 'HEAD^{tree}' > "$out/controller.tree"
test "$(cat "$out/controller.sha")" = "$GITHUB_SHA"
test -z "$(git -C "$control" status --porcelain --untracked-files=all)"
git -C "$product" rev-parse HEAD > "$out/product.sha"
git -C "$product" rev-parse 'HEAD^{tree}' > "$out/product.tree"
test "$(cat "$out/product.sha")" = 54d13b3809c4f321dfe94a58e46bee8c16f2fdfa
test "$(cat "$out/product.tree")" = fdf6a620757b5725afc140cf843a236dd6b9ed16
git -C "$product" status --porcelain --untracked-files=all > "$out/product-before.log"
test ! -s "$out/product-before.log"
printf 'repository\t%s\nref\t%s\nevent\t%s\ncontroller\t%s\nrun_id\t%s\nrun_attempt\t%s\nrunner_environment\t%s\nrunner_os\t%s\nrunner_arch\t%s\n' \
    "$GITHUB_REPOSITORY" "$GITHUB_REF" "$GITHUB_EVENT_NAME" "$GITHUB_SHA" \
    "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$RUNNER_ENVIRONMENT" "$RUNNER_OS" "$RUNNER_ARCH" > "$out/run.tsv"
cd -- "$control"
sha256sum .github/workflows/debian-inputs-proof.yml scripts/debian_inputs_image_proof.sh \
    scripts/debian_inputs_image_results.py > "$out/controller-files.sha256"
end_stage

begin_stage host
test "$(uname -m)" = x86_64
grep -Fxq 'ID=ubuntu' /etc/os-release
grep -Fxq 'VERSION_ID="24.04"' /etc/os-release
test -z "${DOCKER_HOST-}"
test -z "${DOCKER_CONTEXT-}"
test "$(docker context inspect --format '{{.Endpoints.docker.Host}}')" = unix:///var/run/docker.sock
test "$(docker info --format '{{.Architecture}}')" = x86_64
test "$(docker info --format '{{.OSType}}')" = linux
cpus="${PF_BUILD_CPUS:-2}"
memory_gib="${PF_BUILD_MEMORY_GIB:-6}"
[[ "$cpus" =~ ^[2-8]$ && "$memory_gib" =~ ^[1-9][0-9]?$ ]]
host_cpus="$(nproc)"
daemon_cpus="$(docker info --format '{{.NCPU}}')"
host_memory_kib="$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\) kB$/\1/p' /proc/meminfo)"
available_kib="$(sed -n 's/^MemAvailable:[[:space:]]*\([0-9]*\) kB$/\1/p' /proc/meminfo)"
daemon_memory="$(docker info --format '{{.MemTotal}}')"
allowed="$(sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status)"
[[ "$host_cpus" =~ ^[1-9][0-9]*$ && "$daemon_cpus" =~ ^[1-9][0-9]*$ ]]
[[ "$host_memory_kib" =~ ^[1-9][0-9]*$ && "$available_kib" =~ ^[1-9][0-9]*$ && "$daemon_memory" =~ ^[1-9][0-9]*$ ]]
[[ "$allowed" =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]]
memory_bytes=$((memory_gib * 1073741824))
(( cpus <= host_cpus && cpus <= daemon_cpus ))
(( memory_bytes <= host_memory_kib * 1024 && memory_bytes <= available_kib * 1024 && memory_bytes <= daemon_memory ))
IFS=, read -r -a ranges <<< "$allowed"
selected=()
for range in "${ranges[@]}"; do
    first="${range%-*}"
    last="${range#*-}"
    (( first <= last && last < 1048576 ))
    for ((cpu=first; cpu<=last && ${#selected[@]}<cpus; cpu++)); do
        selected+=("$cpu")
    done
done
[[ "${#selected[@]}" -eq "$cpus" ]]
cpuset="$(IFS=,; printf '%s' "${selected[*]}")"
quota=$((cpus * 100000))
printf 'cpus\t%s\nmemory_gib\t%s\ncpuset\t%s\nhost_cpus\t%s\ndaemon_cpus\t%s\nhost_memory_bytes\t%s\navailable_memory_bytes\t%s\ndaemon_memory_bytes\t%s\nhost_allowed_cpus\t%s\n' \
    "$cpus" "$memory_gib" "$cpuset" "$host_cpus" "$daemon_cpus" "$((host_memory_kib * 1024))" \
    "$((available_kib * 1024))" "$daemon_memory" "$allowed" > "$out/resource-request.tsv"
uname -m > "$out/host.platform"
docker info --format '{{.OSType}}/{{.Architecture}}' > "$out/daemon.platform"
git --version > "$out/versions.log"
python3 --version >> "$out/versions.log"
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}' >> "$out/versions.log"
docker buildx version >> "$out/versions.log"
end_stage

begin_stage inputs
cd -- "$product"
python3 -I scripts/release_sources.py debian-args docker/debian-inputs.args > "$out/debian.args"
python3 -I scripts/release_sources.py toolchain-args docker/hpo-toolchain.args > "$out/hpo.args"
mapfile -t debian_args < "$out/debian.args"
mapfile -t hpo_args < "$out/hpo.args"
[[ "${#debian_args[@]}" -eq 3 && "${#hpo_args[@]}" -eq 6 ]]
build_args=()
for argument in "${debian_args[@]}" "${hpo_args[@]}"; do
    build_args+=(--build-arg "$argument")
done
# Released 1.10.0 triple and old known guard carry. No policy-label repair.
test "$(cat docker/GUARD-POLICY-SETS.pin)" = 02235ead051bdd9e6a53d74fc62048f58611c11469199b9febb31cf104f433c3
build_args+=(--build-arg ENGINE_VERSION=1.10.0 --build-arg CODEGEN_VERSION=1.10.0
    --build-arg HPO_VERSION=1.10.0
    --build-arg GUARD_POLICY_SETS_SHA256=02235ead051bdd9e6a53d74fc62048f58611c11469199b9febb31cf104f433c3
    --build-arg RELEASE_VERSION=1.10.0
    --build-arg RELEASE_GIT_SHA=54d13b3809c4f321dfe94a58e46bee8c16f2fdfa
    --build-arg HPO_WHEEL_SHA256=59abc767e8f7ac55ac3304283eca95c5afc68aaa27f041457593fc4a235f6417
    --build-arg HPO_SDIST_SHA256=5139a4b2cd47f0e4ec14cac6f58100dbe0274322dd659a2807c1c23c2c698737)
printf '%s\n' "${build_args[@]}" > "$out/build-arguments.log"
sha256sum docker/Dockerfile docker/debian-inputs.args docker/hpo-toolchain.args \
    docker/GUARD-POLICY-SETS.pin docker/HARNESS-SHA256SUMS docker/LICENSE-SHA256SUMS \
    docker/SOURCE-SETS.tsv scripts/release_sources.py tests/fixtures/release-smoke/study.json \
    tests/fixtures/release-smoke/strategy.pine tests/fixtures/release-smoke/ohlcv.csv > "$out/product-inputs.sha256"
cp tests/fixtures/release-smoke/study.json "$out/study.json"
harness_files=()
while read -r digest name; do
    [[ "$digest" =~ ^[0-9a-f]{64}$ && "$name" =~ ^[a-z_]+\.(py|sh)$ ]]
    harness_files+=("$name")
done < docker/HARNESS-SHA256SUMS
[[ "${#harness_files[@]}" -eq 16 ]]
licence_files=()
while read -r digest name; do
    [[ "$digest" =~ ^[0-9a-f]{64}$ && "$name" =~ ^[A-Za-z0-9_+./-]+$ ]]
    [[ "$name" != /* && "$name" != *..* ]]
    licence_files+=("$name")
done < docker/LICENSE-SHA256SUMS
[[ "${#licence_files[@]}" -gt 0 ]]
test -s /etc/ssl/certs/ca-certificates.crt
end_stage

begin_stage builder
builder="debian-inputs-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
builder_container="buildx_buildkit_${builder}0"
printf '[worker.oci]\n  max-parallelism = 1\n  gc = false\n' > "$out/buildkit.toml"
# No --use, global builder selection, imported cache or shared cache.
docker buildx create --name "$builder" --driver docker-container \
    --driver-opt "cpuset-cpus=$cpuset" --driver-opt cpu-period=100000 \
    --driver-opt "cpu-quota=$quota" --driver-opt "memory=${memory_gib}g" \
    --buildkitd-config "$out/buildkit.toml" > "$out/builder-create.log" 2>&1
docker buildx inspect --bootstrap "$builder" > "$out/builder-bootstrap.log" 2>&1
docker inspect --format '{{.HostConfig.CpusetCpus}} {{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}} {{.HostConfig.Memory}}' \
    "$builder_container" > "$out/builder-resources.txt"
docker exec "$builder_container" cat /sys/fs/cgroup/cpu.max > "$out/builder-cpu.txt"
docker exec "$builder_container" cat /sys/fs/cgroup/cpuset.cpus.effective > "$out/builder-cpuset.txt"
docker exec "$builder_container" cat /sys/fs/cgroup/memory.max > "$out/builder-memory.txt"
python3 -I "$control/scripts/debian_inputs_image_results.py" resources "$out"
end_stage

begin_stage missing-ca
# A network/setup failure earns no negative credit. Require BuildKit's exact
# required-secret refusal, then require both real builds to succeed with CA.
if docker buildx build --builder "$builder" --platform linux/amd64 --target codegen \
    --file docker/Dockerfile --progress=plain --no-cache "${build_args[@]}" . > "$out/missing-ca.log" 2>&1; then
    ca_rc=0
else
    ca_rc=$?
fi
printf '%s\n' "$ca_rc" > "$out/missing-ca.rc"
test "$ca_rc" -ne 0
grep -Fq 'secret debian_ca: not found' "$out/missing-ca.log"
end_stage

begin_stage hpo-build
docker buildx build --builder "$builder" --platform linux/amd64 --target hpo \
    --file docker/Dockerfile --progress=plain --pull --no-cache \
    --secret id=debian_ca,src=/etc/ssl/certs/ca-certificates.crt "${build_args[@]}" \
    --load --tag "debian-inputs-hpo:${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
    --iidfile "$out/hpo.iid" . > "$out/hpo-build.log" 2>&1
end_stage

begin_stage default-build
# Only stages in this new owned builder from this run may be reused.
# This is not a claim that Docker's base layers or downloads were cold.
docker buildx build --builder "$builder" --platform linux/amd64 --target default \
    --file docker/Dockerfile --progress=plain \
    --secret id=debian_ca,src=/etc/ssl/certs/ca-certificates.crt "${build_args[@]}" \
    --load --tag "debian-inputs-default:${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
    --iidfile "$out/default.iid" . > "$out/default-build.log" 2>&1
end_stage

runtime_args=(--platform linux/amd64 --network none --cpus "$cpus"
    --cpuset-cpus "$cpuset" --memory "${memory_gib}g")
for target in default hpo; do
    begin_stage "$target-image"
    image="$(cat "$out/$target.iid")"
    [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]]
    docker image inspect --format '{{.Id}}' "$image" > "$out/$target.id"
    docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image" > "$out/$target.platform"
    test "$(cat "$out/$target.platform")" = linux/amd64
    docker image inspect --format '{{json .Config.Labels}}' "$image" > "$out/$target.labels.json"
    docker run "${runtime_args[@]}" --name "$builder-$target-inventory" --entrypoint dpkg-query "$image" \
        -W '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' > "$out/$target-amd64.tsv" 2> "$out/$target-inventory.stderr"
    python3 -I scripts/release_sources.py pairs "$out/$target-amd64.tsv" > "$out/$target-source-pairs.txt"
    docker run "${runtime_args[@]}" --name "$builder-$target-harness" --workdir /opt/pineforge/bin \
        --entrypoint sha256sum "$image" -c --strict HARNESS-SHA256SUMS > "$out/$target-harness.log" 2>&1
    docker run "${runtime_args[@]}" --name "$builder-$target-harness-hashes" --workdir /opt/pineforge/bin \
        --entrypoint sha256sum "$image" "${harness_files[@]}" > "$out/$target-harness.sha256"
    docker run "${runtime_args[@]}" --name "$builder-$target-ca" --entrypoint test "$image" \
        ! -e /run/secrets/debian_ca > "$out/$target-ca.log" 2>&1
    docker run "${runtime_args[@]}" --name "$builder-$target-guard" --entrypoint cat "$image" \
        /opt/pineforge/guard-policy.sha256 > "$out/$target.guard"
    docker run "${runtime_args[@]}" --name "$builder-$target-smoke" \
        --mount "type=bind,src=$product/tests/fixtures/release-smoke,dst=/in,readonly" \
        "$image" > "$out/$target-backtest.json" 2> "$out/$target-backtest.stderr"
    end_stage
done

begin_stage hpo-licences
image="$(cat "$out/hpo.iid")"
docker run "${runtime_args[@]}" --name "$builder-hpo-licences" --workdir /opt/pineforge/licenses \
    --entrypoint sha256sum "$image" -c --strict LICENSE-SHA256SUMS > "$out/hpo-licences.log" 2>&1
docker run "${runtime_args[@]}" --name "$builder-hpo-licence-hashes" --workdir /opt/pineforge/licenses \
    --entrypoint sha256sum "$image" "${licence_files[@]}" > "$out/hpo-licences.sha256"
end_stage

begin_stage hpo-study
docker run "${runtime_args[@]}" --name "$builder-hpo-study" \
    --mount "type=bind,src=$product/tests/fixtures/release-smoke,dst=/in,readonly" \
    "$image" hpo run /in/study.json --engine-root /opt/pineforge \
    > "$out/hpo-study.json" 2> "$out/hpo-study.stderr"
end_stage

begin_stage frozen-after
git -C "$product" rev-parse HEAD > "$out/product-after.sha"
git -C "$product" rev-parse 'HEAD^{tree}' > "$out/product-after.tree"
test "$(cat "$out/product-after.sha")" = "$(cat "$out/product.sha")"
test "$(cat "$out/product-after.tree")" = "$(cat "$out/product.tree")"
git -C "$product" status --porcelain --untracked-files=all > "$out/product-after.log"
test ! -s "$out/product-after.log"
test -z "$(git -C "$control" status --porcelain --untracked-files=all)"
end_stage
