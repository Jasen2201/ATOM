#!/bin/bash
# `bash tests/atom_grpc/timing/run.sh <case>` on pit2-p03-g48: one timing case in one container of the ATOM image,
# this tree mounted read-only, no GPU device, on a core set of its own. Exit 0 passes, 1 fails, 2 refuses to run.
set -euo pipefail
CASE=${1:-}
case $CASE in
    a2t) CPUS=${ATOM_TIMING_CPUS:-112-115} ;;  # a front end, a rank, atomesh and the client
    a7t) CPUS=${ATOM_TIMING_CPUS:-112-118} ;;  # a front end, up to four ranks, atomesh and the client
    a8) CPUS=${ATOM_TIMING_CPUS:-37-127} ;;  # 57 for the instances, 16 for atomesh, 16 for aiperf, 2 for the driver
    *) echo "usage: run.sh a2t|a7t|a8" >&2; exit 2 ;;
esac
SHM=16g
[ "$CASE" != a8 ] || SHM=64g  # its hub cache copy, prepared dataset and engine records
TREE=$(cd "$(dirname "$0")/../../.." && pwd)
NAME=$(basename "$TREE")
WHO=$(id -un | cut -d@ -f1)
IMAGE=${ATOM_TIMING_IMAGE:-rocm/atom-dev:latest}
CACHE=${ATOM_TIMING_CACHE:-/share_nfs/$WHO/atom-grpc-timing}
COMPASS_SHA=600fde979768a9039c8fcece2e28c18152b33585  # jgong5/ATOM: Compass's evaluator and laws
COMPASS=$CACHE/compass-$COMPASS_SHA
ID=$(docker image inspect -f '{{.Id}}' "$IMAGE") || { echo "run.sh: no image $IMAGE here" >&2; exit 2; }

# Compass's evaluator and its laws, checked out once into the cache.
if [ "$CASE" != a7t ] && [ ! -d "$COMPASS" ]; then
    mkdir -p "$CACHE"
    tmp=$(mktemp -d "$CACHE/.compass.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    git -C "$tmp" init -q
    git -C "$tmp" remote add origin https://github.com/jgong5/ATOM.git
    git -C "$tmp" sparse-checkout set --cone atom/compass atom/plugin scripts/compass/laws
    git -C "$tmp" fetch -q --depth 1 --filter=blob:none origin "$COMPASS_SHA"
    git -C "$tmp" -c advice.detachedHead=false checkout -q FETCH_HEAD
    mv -T "$tmp" "$COMPASS" 2>/dev/null || [ -d "$COMPASS" ]  # or a concurrent run's fetch landed first
fi

# atomesh as the gate builds it on this host: the gate's image and Rust toolchain, and this tree's cargo target.
# For a7t, also its library's test binary, which holds the split benchmark: the build's last line is its path.
GATE=$TREE/.claude/agent-team/g48
BUILD=atom-gate:$(sha256sum <"$GATE/Dockerfile" | cut -c1-12)
VOL=yajizhan-gate-rust
RUST=/dev/shm/yajizhan-rust
docker image inspect "$BUILD" >/dev/null 2>&1 || docker build -q -t "$BUILD" - <"$GATE/Dockerfile" >/dev/null
docker run --rm -v "$VOL:/r" "$BUILD" chown "$(id -u):$(id -g)" /r
built=$(docker run --rm --init --name "$WHO-timing-build-$NAME-$(date +%s%N)" --cpus 16 \
    --tmpfs /dev/shm:rw,exec,size=32g,mode=1777 --user "$(id -u):$(id -g)" -e HOME=/dev/shm/home \
    -v /share_nfs:/share_nfs -v "$VOL:$RUST" -w "$TREE/atom/mesh" "$BUILD" bash -c 'set -o pipefail && mkdir -p "$HOME" &&
        bash "$1" && . "$2/env.sh" && export CARGO_TARGET_DIR=$3 && cargo build --profile ci --bin atomesh -q -j 16 &&
        if [ "$4" = a7t ]; then cargo test --profile ci --lib --no-run -q -j 16 --message-format=json |
            jq -r "select(.executable).executable"; fi' \
    build "$GATE/rust.sh" "$RUST" "$RUST/target-$NAME" "$CASE" 2>&1) || { echo "$built" >&2; exit 2; }
case $CASE in
    a2t | a8) ARGS=(--compass "$COMPASS") ;;
    a7t) ARGS=(--split "${built##*$'\n'}") ;;
esac

COMMIT=$(git -C "$TREE" rev-parse HEAD)
[ -z "$(git -C "$TREE" --no-optional-locks status --porcelain)" ] || COMMIT+=" with uncommitted changes"
# The host's kernel threads in uninterruptible sleep, the median of 11 counts over 2 s: each counts in its load
# average and runs nothing, and the container cannot see them. The case's refusal takes them off the load average.
sleeping() { ps -eo ppid=,stat= | awk '$1 == 2 && $2 ~ /^D/' | wc -l; }
KTHREADS=$(for _ in {1..11}; do sleeping; sleep 0.2; done | sort -n | sed -n 6p)
# a8's gRPC routers also take the flags in ATOM_A8_GRPC_ROUTER_ARGS, as shell words.
exec docker run --rm --init --name "$WHO-timing-$CASE-$NAME-$(date +%s%N)" --hostname "$(hostname -s)" \
    --cpuset-cpus "$CPUS" --tmpfs "/dev/shm:rw,exec,size=$SHM,mode=1777" --ulimit nofile=1048576 \
    --user "$(id -u):$(id -g)" -e HOME=/dev/shm/home -e "USER=$(id -un)" -e "LOGNAME=$(id -un)" -e TMPDIR=/dev/shm \
    -e PYTHONPATH="$TREE" -e ATOMESH_BIN="$RUST/target-$NAME/ci/atomesh" -e ATOM_TIMING_D_KTHREADS="$KTHREADS" \
    -e ATOM_TIMING_IMAGE="$IMAGE $ID" -e ATOM_TIMING_COMMIT="$COMMIT" \
    -e ATOM_A8_GRPC_ROUTER_ARGS="${ATOM_A8_GRPC_ROUTER_ARGS:-}" \
    -v /share_nfs:/share_nfs:ro -v "$VOL:$RUST:ro" -w "$TREE" "$IMAGE" \
    bash -c 'mkdir -p "$HOME" && exec python3 -m "tests.atom_grpc.timing.$1" "${@:2}"' timing "$CASE" "${ARGS[@]}"
