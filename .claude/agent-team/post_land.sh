#!/bin/bash
# agent-team gate_post_land for jasen/atom-grpc: CI's CPU test scope and every mesh test target (ext-proc ones checked only, as CI does).
# Red is judged against the tip before the run's first landing; only new failures count.
set -uo pipefail
[ "${ATOM_GATE_LOCAL:-}" = 1 ] || exec bash "$(dirname "$0")/g48/run.sh" "$0"  # on pit2-p03-g48 unless ATOM_GATE_LOCAL=1

PY=/share_nfs/yajizhan/venv-cpu-test/bin/python
RUST_ENV=/dev/shm/yajizhan-rust/env.sh
NAME=$(basename "$(pwd)")
SCR=/dev/shm/yajizhan-gate/$NAME-post
mkdir -p "$SCR"

fail=0
echo "== pytest tests/ (CI's unit-test scope)"
# test_atomesh_mooncake_store.py's cleanup kills its process group, which ends the whole run outside CI.
TMPDIR=$SCR PYTHONPATH=. "$PY" -m pytest -q -p no:cacheprovider --basetemp="$SCR/bt" --continue-on-collection-errors -rfE \
    tests/ --ignore=tests/plugin --ignore=tests/entrypoints/test_openai_server.py \
    --ignore=tests/test_atomesh_mooncake_store.py || fail=1

echo "== mesh: test targets with atom-engine, CI's ext-proc check, then the mocker harness"
(
    . "$RUST_ENV"
    export CARGO_TARGET_DIR=/dev/shm/yajizhan-rust/target-$NAME-post
    cd atom/mesh || exit 1
    rc=0
    # One test at a time until #7: routing_tests' PD tests race for ports in parallel.
    cargo test --features atom-engine --lib --tests --no-fail-fast -q -j 16 -- --test-threads=1 || rc=1
    cargo check --features ext-proc --tests -q -j 16 || rc=1  # --tests above skips the ext-proc-only targets
    cargo test --manifest-path mocker/Cargo.toml --release -q -j 16 test_atomesh_harness || rc=1
    exit $rc
) || fail=1

echo "gate_post_land: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
