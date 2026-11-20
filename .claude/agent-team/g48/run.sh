#!/bin/bash
# Run gate script $1 for the tree in the cwd on pit2-p03-g48 with ATOM_GATE_LOCAL=1, in the gate image: the caller's
# uid, /share_nfs at its own path, a CPU quota, no GPU, and a network and /dev/shm of its own.
set -uo pipefail
HOST=pit2-p03-g48
DIR=$(cd "$(dirname "$0")" && pwd)
TREE=$(pwd)
RUFF=/home/yajizhan@amd.com/.local/bin/ruff
# Rust toolchain, crates and cargo targets: a docker volume on g48's disk, out of reach of logind, which empties our
# /dev/shm, and of Spur's epilog, which deletes our old files in /tmp. docker creates it on first use.
VOL=yajizhan-gate-rust
# The image build's lock is this directory itself, since the epilog deletes only files.
LOCK=/tmp/yajizhan-gate-lock
for d in "$TREE" "$(git rev-parse --path-format=absolute --git-common-dir)"; do
    [[ $d == /share_nfs/* ]] || { echo "gate: $d is not under /share_nfs, which $HOST mounts; move the tree or set ATOM_GATE_LOCAL=1"; exit 2; }
done
IMAGE=atom-gate:$(sha256sum <"$DIR/Dockerfile" | cut -c1-12)
NAME=yajizhan-gate-$(basename "$TREE")-$(date +%s%N)
# /dev/shm allows exec, as on the dev host: tests run stub commands and torch loads .so files from pytest's scratch.
ARGS=(--rm --init --name "$NAME" --cpus 16 --tmpfs /dev/shm:rw,exec,size=32g,mode=1777 --ulimit nofile=1048576
    --user "$(id -u):$(id -g)"
    -e ATOM_GATE_LOCAL=1 -e HOME=/dev/shm/home -e "USER=$(id -un)" -e "LOGNAME=$(id -un)" -e LANG=C.UTF-8
    -v /share_nfs:/share_nfs -v "$RUFF:$RUFF:ro" -v "$VOL:/dev/shm/yajizhan-rust" -w "$TREE")
[ -z "${PYTEST_ADDOPTS:-}" ] || ARGS+=(-e "PYTEST_ADDOPTS=$PYTEST_ADDOPTS")
# The umask is the dev host's, and a failed setup exits 2, as in the gate. Opening each file refreshes g48's NFS attribute
# cache, which can show python, ruff and cargo a file the dev host changed seconds ago with its old mtime.
CMD='umask 002 && mkdir -p "$HOME" && bash "$1" || exit 2; '
CMD+='git ls-files -z --cached --others --exclude-standard | xargs -0r -P 32 -n 64 head -qc0 -- 2>/dev/null; exec bash "$2"'
SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "$HOST")
# On a signal, stop the container until ssh ($!) exits: the container may not exist yet, and the remote run outlives ssh.
stop_run() {
    while kill -0 "${!:-}" 2>/dev/null; do "${SSH[@]}" docker stop "$NAME" >/dev/null 2>&1 || sleep 1; done
    echo "gate: stopped by a signal"
    exit "$1"
}
trap 'stop_run 130' INT
trap 'stop_run 143' TERM
trap 'stop_run 129' HUP
BUILD="docker image inspect $IMAGE >/dev/null 2>&1 || docker build -q -t $IMAGE - <$(printf %q "$DIR/Dockerfile")"
# /tmp is shared, so a lock someone else made is refused; runs build the image one at a time, then give the volume's root
# to the caller. ssh ignores the signals, a process group's included, so only the end of the remote run ends it.
(trap '' INT TERM HUP && exec "${SSH[@]}" "mkdir -p -m 700 $LOCK && [ ! -L $LOCK ] && [ -O $LOCK ] || { echo 'gate: $LOCK on $HOST is not ours'; exit 2; }
    flock $LOCK bash -c $(printf %q "$BUILD") || exit 2
    docker run --rm -v $VOL:/r $IMAGE chown $(id -u):$(id -g) /r || exit 2
    $(printf '%q ' docker run "${ARGS[@]}" "$IMAGE" bash -c "$CMD" gate "$DIR/rust.sh" "$1")") &
wait $!
rc=$?
case $rc in
    0 | 1 | 2) ;;
    255) echo "gate: ssh to $HOST failed; ATOM_GATE_LOCAL=1 runs the gate on this host" ;;
    *) echo "gate: the run on $HOST ended with exit $rc" ;;
esac
exit $rc
