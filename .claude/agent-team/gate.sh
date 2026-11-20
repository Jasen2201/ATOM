#!/bin/bash
# agent-team gate_task for jasen/atom-grpc: lint + CPU tests, no GPU.
# Run from a tree root; exit 0 is a pass.
set -uo pipefail

PY=/share_nfs/yajizhan/venv-cpu-test/bin/python
BLACK=/share_nfs/yajizhan/venv-black26/bin/black
RUFF=/home/yajizhan@amd.com/.local/bin/ruff
RUST_ENV=/dev/shm/yajizhan-rust/env.sh

TREE=$(pwd)
NAME=$(basename "$TREE")
SCR=/dev/shm/yajizhan-gate/$NAME
mkdir -p "$SCR"

FORK=$(git remote -v | awk '$2 ~ /Jasen2201\/ATOM(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
[ -n "$FORK" ] || { echo "gate: no remote for Jasen2201/ATOM"; exit 2; }
BASE=$(git merge-base HEAD "$FORK/jasen/atom-grpc") || { echo "gate: no merge base with $FORK/jasen/atom-grpc"; exit 2; }
UPSTREAM=$(git remote -v | awk 'tolower($2) ~ /[:\/]rocm\/atom(\.git)?$/ && $3 == "(fetch)" {print $1; exit}')
[ -n "$UPSTREAM" ] || { echo "gate: no remote for ROCm/ATOM"; exit 2; }
UBASE=$(git merge-base HEAD "$UPSTREAM/main") || { echo "gate: no merge base with $UPSTREAM/main"; exit 2; }

changed() {  # files changed since commit $1 (committed or not) plus untracked ones, filtered by the pathspecs after it
    local since=$1
    shift
    { git diff --name-only --diff-filter=ACMR "$since" -- "$@"; git ls-files --others --exclude-standard -- "$@"; } | sort -u
}

fail=0
# Lint and test only Python files that also differ from upstream main: an upstream merge's own files are upstream's to check.
mapfile -t PYFILES < <(comm -12 <(changed "$BASE" '*.py') <(changed "$UBASE" '*.py'))
if ((${#PYFILES[@]})); then
    echo "== ruff (${#PYFILES[@]} changed files)"
    "$RUFF" check "${PYFILES[@]}" || fail=1
    echo "== black 26"
    "$BLACK" --check "${PYFILES[@]}" || fail=1
fi

# tests/entrypoints always, plus every changed or new test file elsewhere under tests/.
mapfile -t TESTS < <(printf '%s\n' "${PYFILES[@]}" | grep -E '^tests/(.*/)?test_[^/]*\.py$' | grep -v -e '^tests/entrypoints/' -e '^tests/atom_grpc/')
[ -d tests/atom_grpc ] && TESTS+=(tests/atom_grpc)  # this line's own tests run on every task
echo "== pytest tests/entrypoints ${TESTS[*]}"
TMPDIR=$SCR PYTHONPATH=. "$PY" -m pytest -q -p no:cacheprovider --basetemp="$SCR/bt" \
    tests/entrypoints "${TESTS[@]}" || fail=1

# mesh: only when the change, an upstream merge included, touches atom/mesh or a .proto file.
if [ -n "$(changed "$BASE" 'atom/mesh/*' '*.proto')" ]; then
    [ -f "$RUST_ENV" ] || { echo "gate: $RUST_ENV missing (see the overlay body)"; exit 2; }
    echo "== mesh: cargo fmt --check; cargo check --all-targets (default features); cargo test --features atom-engine --lib, then the integration tests one test at a time"
    (
        . "$RUST_ENV"
        export CARGO_TARGET_DIR=/dev/shm/yajizhan-rust/target-$NAME
        cd atom/mesh
        ITESTS=()
        for f in tests/*.rs; do
            n=$(basename "$f" .rs)
            case "$n" in
                ext_proc_tests | ext_proc_envoy) ;;  # need the ext-proc feature
                *) ITESTS+=(--test "$n") ;;
            esac
        done
        # Check at the default features, which .github/scripts/atomesh/build_mesh.sh ships; test with atom-engine, as CI does.
        # Integration tests run one test at a time until #7: routing_tests' PD tests race for ports in parallel.
        cargo fmt --check && cargo check --all-targets -q -j 16 \
            && cargo test --features atom-engine --lib -q -j 16 \
            && cargo test --features atom-engine "${ITESTS[@]}" -q -j 16 -- --test-threads=1
    ) || fail=1
fi

echo "gate: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
