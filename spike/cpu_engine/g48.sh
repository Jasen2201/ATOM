#!/bin/bash
# drive.py at this checkout's HEAD on pit2-p03-g48: the CPU venv and a git archive
# in tmpfs, in a container on pinned cores (CPUS, default four). Args go to drive.py.
set -euo pipefail
host=pit2-p03-g48
cpus=${CPUS:-96-99}
sha=$(git rev-parse --short HEAD)
dir=/dev/shm/yajizhan-spike5-$sha
ssh "$host" "rm -rf $dir && mkdir -p $dir/src && cp -a /share_nfs/yajizhan/venv-cpu-test $dir/venv"
git -C "$(git rev-parse --show-toplevel)" archive HEAD | ssh "$host" "tar -x -C $dir/src"
ssh "$host" "uptime; docker run --rm --cpuset-cpus $cpus --user \$(id -u):\$(id -g) \
  -e HOME=/tmp -e TMPDIR=/tmp -v $dir:/w -w /w/src atom-gate:local \
  /w/venv/bin/python spike/cpu_engine/drive.py $*; rm -rf $dir"
