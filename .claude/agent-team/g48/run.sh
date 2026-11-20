#!/bin/bash
# Run gate script $1 for the tree in the cwd on pit2-p03-g48 with ATOM_GATE_LOCAL=1, in the gate image: the caller's
# uid, /share_nfs at its own path, a CPU quota, no GPU, and a network and /dev/shm of its own.
set -uo pipefail
HOST=pit2-p03-g48
DIR=$(cd "$(dirname "$0")" && pwd)
TREE=$(pwd)
RUFF=/home/yajizhan@amd.com/.local/bin/ruff
# Rust toolchain, crates and cargo targets, on g48's disk-backed /tmp: logind empties our /dev/shm after our last session.
RDIR=/tmp/yajizhan-gate-rust
for d in "$TREE" "$(git rev-parse --path-format=absolute --git-common-dir)"; do
    [[ $d == /share_nfs/* ]] || { echo "gate: $d is not under /share_nfs, which $HOST mounts; move the tree or set ATOM_GATE_LOCAL=1"; exit 2; }
done
IMAGE=atom-gate:$(sha256sum <"$DIR/Dockerfile" | cut -c1-12)
NAME=yajizhan-gate-$(basename "$TREE")-$(date +%s%N)
# /dev/shm allows exec, as on the dev host: tests run stub commands and torch loads .so files from pytest's scratch.
ARGS=(--rm --init --name "$NAME" --cpus 16 --tmpfs /dev/shm:rw,exec,size=32g,mode=1777 --ulimit nofile=1048576
    --user "$(id -u):$(id -g)"
    -e ATOM_GATE_LOCAL=1 -e HOME=/dev/shm/home -e "USER=$(id -un)" -e "LOGNAME=$(id -un)" -e LANG=C.UTF-8
    -v /share_nfs:/share_nfs -v "$RUFF:$RUFF:ro" -v "$RDIR:/dev/shm/yajizhan-rust" -w "$TREE")
[ -z "${PYTEST_ADDOPTS:-}" ] || ARGS+=(-e "PYTEST_ADDOPTS=$PYTEST_ADDOPTS")
# The umask is the dev host's; a failed toolchain install is an environment failure, exit 2, as in the gate.
CMD='umask 002 && mkdir -p "$HOME" && { bash "$1" || exit 2; } && exec bash "$2"'
SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "$HOST")
trap '"${SSH[@]}" docker stop "$NAME" >/dev/null 2>&1; kill ${pid:-} 2>/dev/null' INT TERM HUP
# /tmp is shared, so a directory or link someone else made under that name is refused.
"${SSH[@]}" "mkdir -p -m 700 $RDIR && [ ! -L $RDIR ] && [ -O $RDIR ] || { echo 'gate: $RDIR on $HOST is not ours'; exit 2; }
    docker image inspect $IMAGE >/dev/null 2>&1 || docker build -q -t $IMAGE - <$(printf %q "$DIR/Dockerfile") || exit 2
    $(printf '%q ' docker run "${ARGS[@]}" "$IMAGE" bash -c "$CMD" gate "$DIR/rust.sh" "$1")" &
pid=$!
wait $pid
rc=$?
case $rc in
    0 | 1 | 2) ;;
    255) echo "gate: ssh to $HOST failed; ATOM_GATE_LOCAL=1 runs the gate on this host" ;;
    *) echo "gate: the run on $HOST ended with exit $rc" ;;
esac
exit $rc
