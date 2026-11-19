#!/usr/bin/env bash
# Run the ATOM native, non-GPU unit test suite and emit a JUnit report.
#
# Scope: pure-Python unit tests that run on a plain runner (CPU torch, no GPU,
# no aiter/MoRIIO native libs). Two paths are excluded here because they cannot
# run in this environment:
#   - tests/plugin/                            : sglang/vllm/rtpllm plugin
#       tests (next-stage work). They also install module-level sys.modules
#       stubs at import time that would leak into and break native tests.
#   - tests/entrypoints/test_openai_server.py  : integration test that spawns a
#       real ATOM server and blocks on wait_for_ready; needs a GPU + model.
#
# Other non-unit tests (P/D disaggregation) self-skip via importorskip guards
# inside the test modules, so they show up as visible SKIPs rather than errors.
#
# Env:
#   UNIT_TEST_REPORT  JUnit XML output path (default: unit-report.xml)
set -euo pipefail

REPORT="${UNIT_TEST_REPORT:-unit-report.xml}"
# Spike #10, on its branch only: spike/ci_capacity runs in place of the tests, and
# under GitHub Actions a run the kernel's OOM killer ends runs again with swap added.
PR6=d7d96c7466329f79b71f10103473787e67eb4437 # refs/pull/6/head: stand-ins, stub runner

git cat-file -e "$PR6^{commit}" 2>/dev/null || git fetch -q origin refs/pull/6/head
pr6=$(mktemp -d)
git archive "$PR6" spike/cpu_engine | tar -x -C "$pr6"
export SPIKE_CPU_ENGINE="$pr6/spike/cpu_engine"

drive() {
  free -m
  swapon --show
  PYTHONPATH=spike/ci_capacity python -m pytest tests/ \
    --ignore=tests/plugin \
    --ignore=tests/entrypoints/test_openai_server.py \
    -p capacity -s -q \
    --junitxml="${REPORT}"
}
oom_kills() { sudo dmesg | grep -c 'Killed process' || true; }

if [ -z "${GITHUB_ACTIONS:-}" ]; then
  drive
  exit
fi
df -h / /mnt || true
before=$(oom_kills)
status=0
drive || status=$?
if [ "$status" -eq 0 ] || [ "$(oom_kills)" -eq "$before" ]; then
  exit "$status"
fi
sudo dmesg | grep -E 'Out of memory|Killed process' | tail -n 20
pkill -KILL -f 'stub_server|ATOM::' || true
swap=$(mountpoint -q /mnt && echo /mnt/spike.swap || echo /spike.swap)
sudo fallocate -l 16G "$swap"
sudo chmod 600 "$swap"
sudo mkswap "$swap"
sudo swapon "$swap"
drive
